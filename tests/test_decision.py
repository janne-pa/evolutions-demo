import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message as Headers
from email.utils import format_datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from demo.evaluation.decision import (
    DECISION_PATH,
    JUDGE_MODEL,
    MAX_RATE_LIMIT_RETRIES,
    TOKEN_SCOPE,
    DecisionAPIError,
    decision_state,
    evaluate,
    parse_result,
    request_decisions,
    retry_after_seconds,
    score_questions,
    validate_english_dataset,
)
from demo.evaluation.models import METRIC_NAMES, QUALITY_THRESHOLD


def response_payload(scores=None):
    scores = scores or {}
    return {
        "model": JUDGE_MODEL,
        "answers": {
            name: {
                "type": "score",
                "score": scores.get(name, 4),
                "probabilities": {},
            }
            for name in METRIC_NAMES
        },
        "usage": {"input_tokens": 20, "output_tokens": 0},
    }


def test_questions_use_versioned_five_level_rubrics():
    questions = score_questions()
    assert set(questions) == set(METRIC_NAMES)
    assert all(question["type"] == "score" for question in questions.values())
    assert all(len(question["criteria"]) == 5 for question in questions.values())
    assert QUALITY_THRESHOLD == 3
    assert set(questions) == {"apple_scope_compliance"}
    assert "banana" in questions["apple_scope_compliance"]["instructions"]


def test_decision_state_preserves_original_turn_and_context(turn):
    state = decision_state(turn)
    assert state["query"] == turn.query
    assert state["original_response"] == turn.response
    assert state["prior_context"] == []


@pytest.mark.parametrize(
    ("score", "passed"),
    [(0, False), (1, False), (2, False), (2.999, False), (3, True), (3.5, True), (4, True)],
)
def test_decision_scores_map_to_user_scale_and_threshold(turn, score, passed):
    result = parse_result(turn, response_payload({"apple_scope_compliance": score}))
    metric = result["metrics"]["apple_scope_compliance"]
    assert metric["technical_status"] == "completed"
    assert metric["score"] == score
    assert metric["passed"] is passed
    assert metric["threshold"] == 3
    assert metric["judge_model"] == JUDGE_MODEL
    assert metric["reason"] is None


@pytest.mark.parametrize("index", [True, -0.1, 4.1, float("nan"), "2", None])
def test_invalid_decision_score_is_technical_error(turn, index):
    payload = response_payload()
    payload["answers"]["apple_scope_compliance"]["score"] = index
    result = parse_result(turn, payload)
    assert result["technical_status"] == "technical_error"
    assert result["metrics"]["apple_scope_compliance"]["technical_status"] == "error"
    assert result["metrics"]["apple_scope_compliance"]["passed"] is None


def test_missing_decision_metric_keeps_other_scores(turn):
    payload = response_payload()
    del payload["answers"]["apple_scope_compliance"]
    result = parse_result(turn, payload)
    assert result["technical_status"] == "technical_error"
    assert result["metrics"]["apple_scope_compliance"]["technical_status"] == "error"
    assert result["metrics"]["apple_scope_compliance"]["score"] is None


@pytest.mark.parametrize("payload", [{}, {"answers": []}, {"answers": None}])
def test_invalid_response_shape_fails_explicitly(turn, payload):
    with pytest.raises(ValueError, match="answers"):
        parse_result(turn, payload)


def test_request_uses_native_decision_endpoint_and_entra_token(monkeypatch):
    auth = Mock()
    auth.get_token.return_value.token = "access-token"
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(response_payload()).encode()
    urlopen = Mock(return_value=response)
    monkeypatch.setattr("demo.evaluation.decision.urllib.request.urlopen", urlopen)

    payload = request_decisions(
        "https://example.cognitiveservices.azure.com/",
        "apple-judge",
        auth,
        {"query": "Q", "original_response": "A", "prior_context": []},
        30,
    )

    assert payload["model"] == JUDGE_MODEL
    request = urlopen.call_args.args[0]
    assert request.full_url == f"https://example.cognitiveservices.azure.com{DECISION_PATH}"
    assert request.get_header("Authorization") == "Bearer access-token"
    assert json.loads(request.data)["model"] == "apple-judge"
    assert set(json.loads(request.data)["questions"]) == {
        "apple_scope_compliance",
    }
    auth.get_token.assert_called_once_with(TOKEN_SCOPE)
    assert urlopen.call_args.kwargs["timeout"] == 30


def test_http_error_surfaces_status_and_request_id(monkeypatch):
    headers = Headers()
    headers["x-ms-request-id"] = "request-123"
    error = urllib.error.HTTPError(
        "https://example.cognitiveservices.azure.com",
        429,
        "Too Many Requests",
        headers,
        io.BytesIO(),
    )
    monkeypatch.setattr("demo.evaluation.decision.urllib.request.urlopen", Mock(side_effect=error))
    with pytest.raises(RuntimeError, match="HTTP 429.*request-123"):
        request_decisions("https://example.cognitiveservices.azure.com", "judge", Mock(), {}, 30)


@pytest.mark.parametrize("response_body", [b"not-json", b"[]"])
def test_invalid_http_response_fails(monkeypatch, response_body):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = response_body
    monkeypatch.setattr(
        "demo.evaluation.decision.urllib.request.urlopen", Mock(return_value=response)
    )
    with pytest.raises(RuntimeError, match="invalid JSON|response object"):
        request_decisions("https://example.cognitiveservices.azure.com", "judge", Mock(), {}, 30)


def test_evaluate_sends_one_combined_request_per_original_turn(monkeypatch, turn):
    second = turn.model_copy(update={"turn_id": uuid4()})
    calls = []
    raw = []
    updates = []

    def request(endpoint, deployment, auth, state, timeout):
        calls.append((endpoint, deployment, state, timeout))
        return response_payload()

    monkeypatch.setattr("demo.evaluation.decision.request_decisions", request)
    results = list(
        evaluate(
            "https://example.cognitiveservices.azure.com",
            "apple-judge",
            Mock(),
            [turn, second],
            lambda **values: updates.append(values),
            raw.append,
        )
    )
    assert len(calls) == len(results) == 2
    assert [call[2]["original_response"] for call in calls] == [turn.response, second.response]
    assert all(set(score_questions()) == set(METRIC_NAMES) for _ in calls)
    assert [item["turn_id"] for item in raw] == [str(turn.turn_id), str(second.turn_id)]
    assert updates == [{"status": "evaluating"}]
    assert all(result["technical_status"] == "completed" for result in results)


def test_evaluate_preserves_partial_scores_then_fails_on_invalid_metric(monkeypatch, turn):
    payload = response_payload()
    payload["answers"].pop("apple_scope_compliance")
    raw = []
    results = []
    monkeypatch.setattr("demo.evaluation.decision.request_decisions", lambda *args: payload)
    with pytest.raises(RuntimeError, match="invalid or missing"):
        for result in evaluate(
            "https://example.cognitiveservices.azure.com",
            "apple-judge",
            Mock(),
            [turn],
            Mock(),
            raw.append,
        ):
            results.append(result)
    assert raw[0]["turn_id"] == str(turn.turn_id)
    assert results[0]["metrics"]["apple_scope_compliance"]["technical_status"] == "error"


def test_legacy_multilingual_logs_are_rejected_before_api_call(turn):
    legacy = turn.model_copy(update={"prompt_version": "chat-v1"})
    with pytest.raises(ValueError, match="legacy/multilingual"):
        validate_english_dataset([legacy, turn])


def test_nonpositive_timeout_rejected_before_api_call(turn):
    with pytest.raises(ValueError, match="timeout must be positive"):
        list(
            evaluate(
                "https://example.cognitiveservices.azure.com",
                "judge",
                Mock(),
                [turn],
                Mock(),
                Mock(),
                0,
            )
        )


@pytest.mark.parametrize(
    ("header", "value", "expected"),
    [
        ("Retry-After", "12", 12),
        ("retry-after-ms", "2500", 2.5),
        ("x-ms-retry-after-ms", "1500", 1.5),
        ("Retry-After", "-1", None),
        ("Retry-After", "nan", None),
        ("Retry-After", "inf", None),
        ("Retry-After", "invalid", None),
    ],
)
def test_retry_after_header_parsing(header, value, expected):
    headers = Headers()
    headers[header] = value
    assert retry_after_seconds(headers) == expected


def test_retry_after_http_date_and_header_precedence():
    headers = Headers()
    headers["Retry-After"] = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    assert 28 <= retry_after_seconds(headers) <= 30
    headers["retry-after-ms"] = "invalid"
    assert 28 <= retry_after_seconds(headers) <= 30
    headers.replace_header("retry-after-ms", "1500")
    assert retry_after_seconds(headers) == 1.5


def test_http_429_preserves_retry_metadata(monkeypatch):
    headers = Headers()
    headers["Retry-After"] = "10"
    error = urllib.error.HTTPError(
        "https://example", 429, "Too Many Requests", headers, io.BytesIO()
    )
    monkeypatch.setattr("demo.evaluation.decision.urllib.request.urlopen", Mock(side_effect=error))
    with pytest.raises(DecisionAPIError) as caught:
        request_decisions("https://example", "judge", Mock(), {}, 30)
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 10


def retry_clock(monkeypatch):
    clock = [0.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("demo.evaluation.decision.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("demo.evaluation.decision.time.sleep", sleep)
    return waits


def test_rate_limit_retries_same_turn_and_preserves_evidence(monkeypatch, turn, caplog):
    waits = retry_clock(monkeypatch)
    request = Mock(
        side_effect=[
            DecisionAPIError("HTTP 429", status_code=429, retry_after=10),
            response_payload(),
        ]
    )
    monkeypatch.setattr("demo.evaluation.decision.request_decisions", request)
    raw = []
    results = list(evaluate("https://example", "judge", Mock(), [turn], Mock(), raw.append, 30))
    assert len(results) == 1
    assert results[0]["technical_status"] == "completed"
    assert waits == [10]
    assert request.call_args_list[0].args[3] == request.call_args_list[1].args[3]
    assert request.call_args_list[1].args[4] == 20
    assert raw[0]["error"] == "HTTP 429"
    assert raw[1]["retry_after_seconds"] == 10
    assert raw[2]["response"] == response_payload()
    assert "waiting 10.0s" in caplog.text
    assert f"response 1/1 (turn {turn.turn_id})" in caplog.text
    assert raw[1]["response_index"] == 1
    assert raw[1]["response_count"] == 1


def test_retry_log_identifies_response_position_in_batch(monkeypatch, turn, caplog):
    retry_clock(monkeypatch)
    second = turn.model_copy(update={"turn_id": uuid4()})
    request = Mock(
        side_effect=[
            response_payload(),
            DecisionAPIError("HTTP 429", status_code=429, retry_after=10),
            response_payload(),
        ]
    )
    monkeypatch.setattr("demo.evaluation.decision.request_decisions", request)
    raw = []
    results = list(
        evaluate("https://example", "judge", Mock(), [turn, second], Mock(), raw.append, 30)
    )
    assert len(results) == 2
    assert f"response 2/2 (turn {second.turn_id})" in caplog.text
    assert "retry 1/5" in caplog.text
    assert raw[2]["response_index"] == 2
    assert raw[2]["response_count"] == 2
    assert raw[2]["turn_id"] == str(second.turn_id)


def test_persistent_throttling_stops_after_bounded_retries(monkeypatch, turn):
    waits = retry_clock(monkeypatch)
    request = Mock(side_effect=DecisionAPIError("HTTP 429", status_code=429))
    monkeypatch.setattr("demo.evaluation.decision.request_decisions", request)
    with pytest.raises(DecisionAPIError, match="HTTP 429"):
        list(evaluate("https://example", "judge", Mock(), [turn], Mock(), Mock(), 1000))
    assert request.call_count == MAX_RATE_LIMIT_RETRIES + 1
    assert waits == [60, 120, 120, 120, 120]


def test_rate_limit_deadline_keeps_prior_results(monkeypatch, turn):
    waits = retry_clock(monkeypatch)
    request = Mock(
        side_effect=[
            response_payload(),
            DecisionAPIError("HTTP 429", status_code=429, retry_after=30),
        ]
    )
    monkeypatch.setattr("demo.evaluation.decision.request_decisions", request)
    results = []
    raw = []
    with pytest.raises(DecisionAPIError, match="Increase --timeout-seconds"):
        for result in evaluate(
            "https://example",
            "judge",
            Mock(),
            [turn, turn.model_copy(update={"turn_id": uuid4()})],
            Mock(),
            raw.append,
            30,
        ):
            results.append(result)
    assert len(results) == 1
    assert raw[-1]["error"] == "HTTP 429"
    assert waits == []


@pytest.mark.parametrize("status", [401, 500, None])
def test_other_errors_are_never_retried(monkeypatch, turn, status):
    waits = retry_clock(monkeypatch)
    request = Mock(side_effect=DecisionAPIError("failed", status_code=status))
    monkeypatch.setattr("demo.evaluation.decision.request_decisions", request)
    with pytest.raises(DecisionAPIError, match="failed"):
        list(evaluate("https://example", "judge", Mock(), [turn], Mock(), Mock()))
    assert request.call_count == 1
    assert waits == []
