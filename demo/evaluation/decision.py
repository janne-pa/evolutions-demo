import json
import logging
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.message import Message as Headers
from email.utils import parsedate_to_datetime
from typing import Any

from azure.core.credentials import TokenCredential

from demo.contracts import ChatTurn, json_text
from demo.evaluation.models import (
    METRIC_NAMES,
    QUALITY_THRESHOLD,
    SCOPE_METRIC,
    MetricName,
    MetricResult,
    TurnEvaluation,
    missing_metrics,
)
from demo.settings import PACKAGE_DIR

JUDGE_MODEL = "Microsoft-Decision-1"
JUDGE_MODEL_VERSION = "1"
JUDGE_PROMPT_VERSION = "decision-scores-v3"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
DECISION_PATH = "/providers/microsoft/v1/systemone"
REQUEST_TIMEOUT_SECONDS = 60
MAX_SCORE_INDEX = 4
MAX_RATE_LIMIT_RETRIES = 5
LOGGER = logging.getLogger(__name__)

RUBRICS: dict[str, Any] = json.loads(
    (PACKAGE_DIR / "prompts" / f"{JUDGE_PROMPT_VERSION}.json").read_text(encoding="utf-8")
)
if set(RUBRICS) != set(METRIC_NAMES):
    raise ValueError("Decision-1 rubric must define exactly one question for every metric.")
if any(
    not isinstance(rubric, dict)
    or not isinstance(rubric.get("instructions"), str)
    or not rubric["instructions"].strip()
    or not isinstance(rubric.get("criteria"), list)
    or len(rubric["criteria"]) != 5
    or any(not isinstance(level, str) or not level.strip() for level in rubric["criteria"])
    for rubric in RUBRICS.values()
):
    raise ValueError("Every Decision-1 rubric needs instructions and five nonempty score levels.")


class DecisionAPIError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def retry_after_seconds(headers: Headers) -> float | None:
    for name, divisor in (
        ("retry-after-ms", 1000),
        ("x-ms-retry-after-ms", 1000),
        ("Retry-After", 1),
    ):
        value = headers.get(name)
        if value is None:
            continue
        try:
            delay = float(value) / divisor
        except ValueError:
            if name != "Retry-After":
                continue
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                delay = max(0.0, (date - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return None


def validate_english_dataset(turns: list[ChatTurn]) -> None:
    legacy = [str(turn.turn_id) for turn in turns if turn.prompt_version != "chat-en-v2"]
    if legacy:
        raise ValueError(
            f"Selected {len(legacy)} turns from a legacy/multilingual prompt. "
            "This demo evaluates the English chat-en-v2 policy only. "
            "No rows were silently excluded. "
            "Use --start and --end to select a new English chat-en-v2 conversation. "
            f"First incompatible turn_id: {legacy[0]}"
        )


def decision_state(turn: ChatTurn) -> dict[str, Any]:
    return {
        "query": turn.query,
        "original_response": turn.response,
        "prior_context": [message.model_dump(mode="json") for message in turn.context],
    }


def score_questions() -> dict[str, dict[str, Any]]:
    return {
        name: {
            "type": "score",
            "instructions": rubric["instructions"],
            "criteria": rubric["criteria"],
        }
        for name, rubric in RUBRICS.items()
    }


def request_decisions(
    endpoint: str,
    deployment: str,
    auth: TokenCredential,
    state: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json_text(
        {
            "model": deployment,
            "state": state,
            "questions": score_questions(),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}{DECISION_PATH}",
        data=body,
        headers={
            "Authorization": f"Bearer {auth.get_token(TOKEN_SCOPE).token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=min(REQUEST_TIMEOUT_SECONDS, timeout_seconds)
        ) as response:
            response_body = response.read()
    except urllib.error.HTTPError as error:
        request_id = error.headers.get("x-ms-request-id")
        detail = f"Microsoft-Decision-1 returned HTTP {error.code} ({error.reason})."
        if request_id:
            detail += f" Request ID: {request_id}."
        retry_after = retry_after_seconds(error.headers) if error.code == 429 else None
        error.close()
        raise DecisionAPIError(detail, status_code=error.code, retry_after=retry_after) from error
    except urllib.error.URLError as error:
        raise DecisionAPIError(f"Could not reach Microsoft-Decision-1: {error.reason}.") from error
    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise DecisionAPIError("Microsoft-Decision-1 returned invalid JSON.") from error
    if not isinstance(payload, dict):
        raise DecisionAPIError("Microsoft-Decision-1 returned an invalid response object.")
    return payload


def parse_metric(name: MetricName, answer: Any, model: str) -> MetricResult:
    if not isinstance(answer, dict) or answer.get("type") != "score":
        raise ValueError(f"Decision-1 returned an invalid score answer for {name}.")
    index = answer.get("score")
    if isinstance(index, bool) or not isinstance(index, (int, float)):
        raise ValueError(f"Decision-1 returned an out-of-range score for {name}.")
    numeric_index = float(index)
    if not math.isfinite(numeric_index) or not 0 <= numeric_index <= MAX_SCORE_INDEX:
        raise ValueError(f"Decision-1 returned an out-of-range score for {name}.")
    score = numeric_index
    return {
        "technical_status": "completed",
        "passed": score >= QUALITY_THRESHOLD,
        "score": score,
        "threshold": QUALITY_THRESHOLD,
        "reason": None,
        "judge_model": model,
    }


def parse_result(
    turn: ChatTurn,
    payload: dict[str, Any],
    judge_model: str = JUDGE_MODEL,
) -> TurnEvaluation:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Microsoft-Decision-1 response is missing the answers object.")
    unexpected = set(answers) - set(METRIC_NAMES)
    if unexpected:
        raise ValueError("Microsoft-Decision-1 returned an unexpected score question.")
    metrics = missing_metrics("Missing Decision-1 score.")
    for name in METRIC_NAMES:
        try:
            metrics[name] = parse_metric(name, answers.get(name), judge_model)
        except ValueError as error:
            metrics[name] = {
                "technical_status": "error",
                "passed": None,
                "score": None,
                "threshold": QUALITY_THRESHOLD,
                "reason": None,
                "error": str(error),
            }
    completed = all(metric["technical_status"] == "completed" for metric in metrics.values())
    scope = metrics[SCOPE_METRIC]
    return {
        "schema_version": "6",
        "turn_id": str(turn.turn_id),
        "conversation_id": str(turn.conversation_id),
        "trace_id": turn.trace_id,
        "technical_status": "completed" if completed else "technical_error",
        "metrics": metrics,
        "apple_scope_compliance": scope["passed"],
        "reason": None,
        "eval_id": None,
        "run_id": None,
    }


def evaluate(
    endpoint: str,
    deployment: str,
    auth: TokenCredential,
    turns: list[ChatTurn],
    update_manifest: Callable[..., None],
    preserve_raw: Callable[[dict[str, Any]], None],
    timeout_seconds: int = 600,
) -> Iterator[TurnEvaluation]:
    validate_english_dataset(turns)
    if timeout_seconds <= 0:
        raise ValueError("Evaluation timeout must be positive.")
    update_manifest(status="evaluating")
    deadline = time.monotonic() + timeout_seconds
    metric_errors = False
    for response_index, turn in enumerate(turns, start=1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "Decision-1 evaluation timed out. Completed per-turn results are preserved."
            )
        retries = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Decision-1 evaluation timed out. Completed per-turn results are preserved."
                )
            try:
                payload = request_decisions(
                    endpoint, deployment, auth, decision_state(turn), remaining
                )
                break
            except DecisionAPIError as error:
                preserve_raw({"turn_id": str(turn.turn_id), "error": str(error)})
                if error.status_code != 429 or retries >= MAX_RATE_LIMIT_RETRIES:
                    raise
                delay = max(
                    1.0,
                    error.retry_after
                    if error.retry_after is not None
                    else min(60 * 2**retries, 120),
                )
                if delay >= deadline - time.monotonic():
                    raise DecisionAPIError(
                        f"{error} Rate-limit wait ({delay:g}s) exceeds the evaluation "
                        "time remaining. Increase --timeout-seconds."
                    ) from error
                retries += 1
                LOGGER.warning(
                    "Decision-1 HTTP 429 for response %s/%s (turn %s): "
                    "waiting %.1fs before retry %s/%s.",
                    response_index,
                    len(turns),
                    turn.turn_id,
                    delay,
                    retries,
                    MAX_RATE_LIMIT_RETRIES,
                )
                preserve_raw(
                    {
                        "turn_id": str(turn.turn_id),
                        "response_index": response_index,
                        "response_count": len(turns),
                        "retry": retries,
                        "retry_after_seconds": delay,
                    }
                )
                time.sleep(delay)
        preserve_raw({"turn_id": str(turn.turn_id), "response": payload})
        result = parse_result(turn, payload)
        metric_errors |= result["technical_status"] != "completed"
        yield result
    if metric_errors:
        raise RuntimeError("One or more Decision-1 metric scores were invalid or missing.")
