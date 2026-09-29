from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from demo.evaluation.foundry import (
    BUILTIN_VERSIONS,
    EVALUATOR_NAME,
    METRIC_NAMES,
    evaluate,
    input_item,
    parse_result,
)


def raw_result(turn, passed=True):
    return {
        "datasource_item": input_item(turn),
        "status": "completed",
        "eval_id": "eval-1",
        "run_id": "run-1",
        "results": [
            {
                "name": EVALUATOR_NAME,
                "metric": EVALUATOR_NAME,
                "status": "completed",
                "passed": passed,
                "score": None,
                "label": "pass" if passed else "fail",
                "reason": "Explanation from a test double.",
                "sample": {"model": "judge-version"},
            },
            *[
                {
                    "name": name,
                    "metric": name,
                    "status": "completed",
                    "passed": True,
                    "label": "pass",
                    "reason": "Independent test explanation.",
                    "score": 4 if name in BUILTIN_VERSIONS else None,
                    "threshold": 3 if name in BUILTIN_VERSIONS else None,
                    "sample": {"model": "judge-version"},
                }
                for name in METRIC_NAMES
                if name != EVALUATOR_NAME
            ],
        ],
    }


@pytest.mark.parametrize("passed", [True, False])
def test_service_boolean_contract(turn, passed):
    result = parse_result(raw_result(turn, passed), {str(turn.turn_id): turn})
    assert result["apple_scope_compliance"] is passed
    assert result["technical_status"] == "completed"


@pytest.mark.parametrize(
    "change",
    [
        {"reason": ""},
        {"reason": None},
        {"passed": None},
        {"passed": "true"},
        {"score": 0.5},
        {"score": 0},
        {"label": "fail"},
        {"status": "error"},
        {"error": {"message": "throttled"}},
    ],
)
def test_invalid_evaluator_result(turn, change):
    raw = raw_result(turn)
    raw["results"][0].update(change)
    result = parse_result(raw, {str(turn.turn_id): turn})
    assert result["technical_status"] == "technical_error"
    assert result["metrics"][EVALUATOR_NAME]["technical_status"] == "error"
    assert result["metrics"]["fluency"]["score"] == 4


def test_original_content_must_match(turn):
    raw = raw_result(turn)
    raw["datasource_item"]["response"] = "Regenerated replacement"
    with pytest.raises(ValueError, match="original"):
        parse_result(raw, {str(turn.turn_id): turn})


def fake_project(turn, status="completed"):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.with_options.return_value = client
    client.evals.create.return_value = Mock(id="eval-1")
    client.evals.runs.create.return_value = Mock(id="run-1", status="queued", report_url=None)
    client.evals.runs.retrieve.return_value = Mock(
        id="run-1", status=status, report_url=None, error="test error"
    )
    raw = raw_result(turn)
    client.evals.runs.output_items.list.return_value = iter([Mock(model_dump=lambda: raw)])
    project = Mock(get_openai_client=Mock(return_value=client))
    project.beta.evaluators.get_version.side_effect = lambda name, version: Mock(
        definition=Mock(
            as_dict=Mock(
                return_value={
                    "metrics": {
                        name.removeprefix("builtin."): {
                            "type": "ordinal",
                            "min_value": 1,
                            "max_value": 5,
                        }
                    }
                }
            )
        )
    )
    return project, client


def test_dataset_not_model_target_and_ids_persisted_immediately(turn):
    project, client = fake_project(turn)
    updates = []
    with patch("demo.evaluation.foundry.evaluator_version", return_value="1"):
        results = list(evaluate(project, "judge", [turn], lambda **v: updates.append(v), Mock()))
    data_source = client.evals.runs.create.call_args.kwargs["data_source"]
    assert data_source["type"] == "jsonl"
    assert data_source["source"]["content"][0]["item"] == input_item(turn)
    assert not {"model", "target", "agent"}.intersection(data_source)
    assert any(update.get("run_id") == "run-1" for update in updates)
    assert results[0]["turn_id"] == str(turn.turn_id)
    criteria = client.evals.create.call_args.kwargs["testing_criteria"]
    assert {criterion["name"] for criterion in criteria} == set(METRIC_NAMES)
    assert all(c["initialization_parameters"]["deployment_name"] == "judge" for c in criteria)
    assert next(c for c in criteria if c["name"] == "fluency")["data_mapping"] == {
        "response": "{{item.response}}"
    }
    assert all(len(update.get("evaluators", {})) <= 4 for update in updates)
    assert len(updates[3]["evaluators"]) == 4
    for criterion in criteria:
        name = criterion["name"]
        if name in BUILTIN_VERSIONS:
            assert criterion["evaluator_name"] == f"builtin.{name}"
            assert criterion["evaluator_version"] == BUILTIN_VERSIONS[name]
            assert criterion["initialization_parameters"]["threshold"] == 3
        else:
            assert criterion["data_mapping"]["context"] == "{{item.context}}"
    project.get_openai_client.assert_called_once_with(max_retries=0, timeout=60)


def test_missing_rows_fail(turn):
    project, client = fake_project(turn)
    client.evals.runs.output_items.list.return_value = iter([])
    with patch("demo.evaluation.foundry.evaluator_version", return_value="1"):
        with pytest.raises(ValueError, match="missing"):
            list(evaluate(project, "judge", [turn], Mock(), Mock()))


def test_timeout_preserves_run_and_does_not_recreate(turn):
    project, client = fake_project(turn, "in_progress")
    with patch("demo.evaluation.foundry.evaluator_version", return_value="1"):
        with pytest.raises(TimeoutError):
            list(evaluate(project, "judge", [turn], Mock(), Mock(), timeout_seconds=0))
    assert client.evals.runs.create.call_count == 1


def test_second_result_page_is_consumed(turn):
    second = turn.model_copy(update={"turn_id": __import__("uuid").uuid4()})
    project, client = fake_project(turn)
    raw1, raw2 = raw_result(turn), deepcopy(raw_result(second, False))
    client.evals.runs.output_items.list.return_value = iter(
        [Mock(model_dump=lambda: raw1), Mock(model_dump=lambda: raw2)]
    )
    with patch("demo.evaluation.foundry.evaluator_version", return_value="1"):
        results = list(evaluate(project, "judge", [turn, second], Mock(), Mock()))
    assert len(results) == 2
    assert results[1]["apple_scope_compliance"] is False


@pytest.mark.parametrize("score", [None, True, 0, 6, 3.5, float("nan"), "4"])
def test_invalid_quality_scores_are_technical_errors(turn, score):
    raw = raw_result(turn)
    raw["results"][1]["score"] = score
    result = parse_result(raw, {str(turn.turn_id): turn})
    assert result["technical_status"] == "technical_error"
    assert result["metrics"]["fluency"]["technical_status"] == "error"
    assert result["apple_scope_compliance"] is True


def test_metrics_match_by_name_not_position(turn):
    raw = raw_result(turn)
    raw["results"].reverse()
    result = parse_result(raw, {str(turn.turn_id): turn})
    assert result["technical_status"] == "completed"
    assert result["metrics"]["coherence"]["score"] == 4


@pytest.mark.parametrize("score", [1, 2, 3, 4, 5])
def test_quality_threshold_is_inclusive_and_low_scores_are_findings(turn, score):
    raw = raw_result(turn)
    raw["results"][1].update(score=score, passed=score >= 3, label="pass" if score >= 3 else "fail")
    result = parse_result(raw, {str(turn.turn_id): turn})
    assert result["technical_status"] == "completed"
    assert result["metrics"]["fluency"]["passed"] is (score >= 3)


@pytest.mark.parametrize(
    "change", [{"threshold": 4}, {"passed": False, "label": "fail"}, {"metric": "coherence"}]
)
def test_inconsistent_quality_metadata_is_not_a_quality_failure(turn, change):
    raw = raw_result(turn)
    raw["results"][1].update(change)
    result = parse_result(raw, {str(turn.turn_id): turn})
    assert result["metrics"]["fluency"]["technical_status"] == "error"
    assert result["metrics"]["fluency"]["passed"] is None


@pytest.mark.parametrize("duplicate", [False, True])
def test_unexpected_or_duplicate_metric_is_a_structural_error(turn, duplicate):
    raw = raw_result(turn)
    raw["results"][1]["name"] = EVALUATOR_NAME if duplicate else "unexpected"
    with pytest.raises(ValueError, match="Unexpected or duplicate"):
        parse_result(raw, {str(turn.turn_id): turn})


def test_unnecessary_refusal_is_independent_of_scope(turn):
    raw = raw_result(turn)
    raw["results"][-1].update(passed=False, label="fail")
    result = parse_result(raw, {str(turn.turn_id): turn})
    assert result["apple_scope_compliance"] is True
    assert result["unnecessary_refusal"] is True
    assert result["technical_status"] == "completed"


def test_missing_metric_preserves_other_results_and_fails_run(turn):
    project, client = fake_project(turn)
    raw = raw_result(turn)
    raw["results"].pop()
    client.evals.runs.output_items.list.return_value = iter([Mock(model_dump=lambda: raw)])
    with patch("demo.evaluation.foundry.evaluator_version", return_value="1"):
        iterator = evaluate(project, "judge", [turn], Mock(), Mock())
        result = next(iterator)
        assert result["metrics"]["fluency"]["score"] == 4
        assert result["technical_status"] == "technical_error"
        with pytest.raises(RuntimeError, match="metrics failed"):
            next(iterator)


def test_legacy_multilingual_logs_not_silently_evaluated_or_dropped(turn):
    legacy = turn.model_copy(update={"prompt_version": "chat-v1"})
    project = Mock()
    with pytest.raises(ValueError, match="legacy/multilingual"):
        list(evaluate(project, "judge", [legacy, turn], Mock(), Mock()))
    project.get_openai_client.assert_not_called()
