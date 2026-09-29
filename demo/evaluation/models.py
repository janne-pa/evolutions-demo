from typing import Final, Literal, NotRequired, TypedDict, TypeGuard

MetricName = Literal["apple_scope_compliance", "fluency", "coherence", "no_unnecessary_refusal"]
RunStatus = Literal["completed", "no_data", "technical_error"]

EVALUATOR_NAME: Final = "apple_scope_compliance"
METRIC_NAMES: tuple[MetricName, ...] = (
    EVALUATOR_NAME,
    "fluency",
    "coherence",
    "no_unnecessary_refusal",
)
QUALITY_METRICS: tuple[MetricName, ...] = ("fluency", "coherence")
QUALITY_THRESHOLD = 3
METRIC_LABELS: dict[MetricName, str] = {
    EVALUATOR_NAME: "Apple scope compliance",
    "fluency": "Fluency",
    "coherence": "Coherence",
    "no_unnecessary_refusal": "No unnecessary refusal",
}


class MetricResult(TypedDict):
    technical_status: Literal["completed", "error", "not_evaluated"]
    passed: bool | None
    score: int | float | bool | None
    reason: str | None
    threshold: NotRequired[int | float | None]
    judge_model: NotRequired[str | None]
    error: NotRequired[str | None]


class TurnEvaluation(TypedDict):
    schema_version: Literal["2"]
    turn_id: str | None
    conversation_id: str | None
    trace_id: str | None
    technical_status: Literal["completed", "technical_error", "not_evaluated"]
    metrics: dict[MetricName, MetricResult]
    apple_scope_compliance: bool | None
    unnecessary_refusal: bool | None
    reason: str | None
    eval_id: str | None
    run_id: str | None
    error: NotRequired[str | None]
    record_index: NotRequired[int]


class MetricSummary(TypedDict):
    evaluated: int
    not_evaluated: int | None
    passed: int
    failed: int
    pass_rate: float | None
    coverage: float | None
    mean_score: float | None


class EvaluationSummary(TypedDict):
    schema_version: Literal["2"]
    status: RunStatus
    source: Literal["azure_apptraces"]
    start: str | None
    end: str | None
    query_started_at: str | None
    fetched: int | None
    evaluated: int
    not_evaluated: int | None
    metrics: dict[MetricName, MetricSummary]
    scope_evaluated: int
    passed: int
    violations: int
    violation_rate: float | None
    unnecessary_refusals: int
    unnecessary_refusal_rate: float | None
    coverage: float | None
    error: str | None
    local_run_id: str
    eval_id: str | None
    run_id: str | None
    limitations: str


def is_metric_name(value: object) -> TypeGuard[MetricName]:
    return value in METRIC_NAMES


def missing_metrics(error: str | None) -> dict[MetricName, MetricResult]:
    return {
        name: {
            "technical_status": "not_evaluated",
            "passed": None,
            "score": None,
            "reason": None,
            "error": error,
        }
        for name in METRIC_NAMES
    }
