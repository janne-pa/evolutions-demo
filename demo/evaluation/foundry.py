import time
from collections.abc import Callable, Iterator
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import TestingCriterionAzureAIEvaluator
from azure.core.exceptions import ResourceNotFoundError
from openai.types.eval_create_params import DataSourceConfigCustom
from openai.types.evals.create_eval_jsonl_run_data_source_param import (
    CreateEvalJSONLRunDataSourceParam,
    SourceFileContent,
    SourceFileContentContent,
)

from demo.contracts import ChatTurn, json_text, sha256
from demo.evaluation.models import (
    EVALUATOR_NAME,
    METRIC_LABELS,
    METRIC_NAMES,
    QUALITY_THRESHOLD,
    MetricName,
    MetricResult,
    TurnEvaluation,
    is_metric_name,
    missing_metrics,
)
from demo.settings import PACKAGE_DIR

EVALUATOR_PROMPT_VERSION = "apple-scope-v2"
CUSTOM_PROMPTS: dict[MetricName, str] = {
    EVALUATOR_NAME: EVALUATOR_PROMPT_VERSION,
    "no_unnecessary_refusal": "no-unnecessary-refusal-v1",
}
BUILTIN_VERSIONS: dict[MetricName, str] = {"fluency": "13", "coherence": "13"}
ENGLISH_PROMPT_VERSION = "chat-en-v2"
FIELDS = ["turn_id", "query", "response", "context"]
DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in FIELDS},
    "required": FIELDS,
}


def custom_definition(name: MetricName) -> dict[str, Any]:
    return {
        "type": "prompt",
        "prompt_text": (PACKAGE_DIR / "prompts" / f"{CUSTOM_PROMPTS[name]}.txt").read_text(
            encoding="utf-8"
        ),
        "init_parameters": {
            "type": "object",
            "properties": {
                "deployment_name": {"type": "string"},
                "threshold": {"type": "number"},
            },
            "required": ["deployment_name", "threshold"],
        },
        "data_schema": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in FIELDS if field != "turn_id"},
            "required": ["query", "response", "context"],
        },
        "metrics": {name: {"type": "boolean", "desirable_direction": "increase"}},
    }


def evaluator_version(
    project: AIProjectClient, name: MetricName, definition: dict[str, Any]
) -> str:
    definition_hash = sha256(json_text(definition))
    try:
        versions = list(project.beta.evaluators.list_versions(name=name))
    except ResourceNotFoundError:
        versions = []
    for version in versions:
        if (version.metadata or {}).get("definition_sha256") == definition_hash:
            stored = version.definition.as_dict()
            # The service materializes unset numeric bounds as null for boolean metrics.
            for metric in stored.get("metrics", {}).values():
                for bound in ("min_value", "max_value"):
                    if metric.get(bound) is None:
                        metric.pop(bound, None)
            if stored != definition:
                raise ValueError(f"Evaluator metadata does not match its definition: {name}")
            return version.version
    created = project.beta.evaluators.create_version(
        name=name,
        evaluator_version={
            "name": name,
            "display_name": METRIC_LABELS[name],
            "description": "Independent binary assessment of original apple-chat responses.",
            "categories": ["quality"],
            "metadata": {"definition_sha256": definition_hash},
            "definition": definition,
        },
    )
    return created.version


def testing_criteria(
    project: AIProjectClient, judge: str, update_manifest: Callable[..., None]
) -> list[TestingCriterionAzureAIEvaluator]:
    criteria: list[TestingCriterionAzureAIEvaluator] = []
    versions: dict[str, Any] = {}
    for name in METRIC_NAMES:
        fields: tuple[str, ...]
        if name in CUSTOM_PROMPTS:
            definition = custom_definition(name)
            version = evaluator_version(project, name, definition)
            remote_name: str = name
            fields = ("query", "response", "context")
            threshold = 0.5
        else:
            remote_name = f"builtin.{name}"
            version = BUILTIN_VERSIONS[name]
            registered = project.beta.evaluators.get_version(name=remote_name, version=version)
            definition = registered.definition.as_dict()
            metric = definition.get("metrics", {}).get(name, {})
            if (metric.get("type"), metric.get("min_value"), metric.get("max_value")) != (
                "ordinal",
                1,
                5,
            ):
                raise ValueError(f"Unexpected built-in metric schema: {remote_name}")
            fields = ("response",) if name == "fluency" else ("query", "response")
            threshold = QUALITY_THRESHOLD
        versions[name] = {
            "evaluator_name": remote_name,
            "version": version,
            "prompt_version": CUSTOM_PROMPTS.get(name),
            "definition_sha256": sha256(json_text(definition)),
            "threshold": threshold,
        }
        update_manifest(evaluators=versions.copy())
        criteria.append(
            {
                "type": "azure_ai_evaluator",
                "name": name,
                "evaluator_name": remote_name,
                "evaluator_version": version,
                "initialization_parameters": {"deployment_name": judge, "threshold": threshold},
                "data_mapping": {key: "{{item." + key + "}}" for key in fields},
            }
        )
    return criteria


def validate_english_dataset(turns: list[ChatTurn]) -> None:
    legacy = [str(turn.turn_id) for turn in turns if turn.prompt_version != ENGLISH_PROMPT_VERSION]
    if legacy:
        raise ValueError(
            f"Selected {len(legacy)} turns from a legacy/multilingual prompt. "
            "Fluency/coherence support English responses only. No rows were silently excluded. "
            "Use --start and --end to select a new English chat-en-v2 conversation. "
            f"First incompatible turn_id: {legacy[0]}"
        )


def input_item(turn: ChatTurn) -> dict[str, object]:
    return {
        "turn_id": str(turn.turn_id),
        "query": turn.query,
        "response": turn.response,
        "context": json_text([message.model_dump() for message in turn.context]),
    }


def parse_metric(name: MetricName, result: dict[str, Any]) -> MetricResult:
    if result.get("error") or result.get("status") != "completed":
        raise ValueError(f"Evaluator technical error: {result}")
    if result.get("name") != name or result.get("metric") != name:
        raise ValueError(f"Unexpected evaluator/metric identity: {result}")
    score, passed, reason = result.get("score"), result.get("passed"), result.get("reason")
    if (
        type(passed) is not bool
        or not isinstance(reason, str)
        or not reason.strip()
        or result.get("label") != ("pass" if passed else "fail")
    ):
        raise ValueError(f"Invalid decision or missing explanation: {result}")
    if name in BUILTIN_VERSIONS:
        if (
            type(score) not in (int, float)
            or score not in (1, 2, 3, 4, 5)
            or result.get("threshold") != QUALITY_THRESHOLD
            or passed != (score >= QUALITY_THRESHOLD)
        ):
            raise ValueError(f"Invalid 1-5 score, threshold or decision: {result}")
    elif score is not None and (
        type(score) not in (int, float, bool) or score not in (0, 1) or passed != bool(score)
    ):
        raise ValueError(f"Invalid binary metric: {result}")
    return {
        "technical_status": "completed",
        "passed": passed,
        "score": score,
        "threshold": result.get("threshold"),
        "reason": reason,
        "judge_model": (result.get("sample") or {}).get("model"),
    }


def parse_result(raw: dict[str, Any], expected: dict[str, ChatTurn]) -> TurnEvaluation:
    item = raw.get("datasource_item")
    if not isinstance(item, dict) or not isinstance(item.get("turn_id"), str):
        raise ValueError("Foundry output is missing the original turn_id.")
    turn_id = item["turn_id"]
    if turn_id not in expected or item != input_item(expected[turn_id]):
        raise ValueError("Foundry did not return the expected original dataset.")
    if raw.get("status") != "completed":
        raise ValueError(f"Foundry row is not completed: {raw.get('status')}")
    results = raw.get("results")
    if not isinstance(results, list):
        raise ValueError("Missing evaluator results array.")
    metrics = missing_metrics("Missing evaluator result.")
    seen: set[MetricName] = set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Invalid evaluator result object.")
        name = result.get("name")
        if not is_metric_name(name) or name in seen:
            raise ValueError("Unexpected or duplicate evaluator result.")
        seen.add(name)
        try:
            metrics[name] = parse_metric(name, result)
        except ValueError as error:
            metrics[name] = {
                "technical_status": "error",
                "passed": None,
                "score": None,
                "reason": None,
                "error": str(error),
            }
    completed = all(m["technical_status"] == "completed" for m in metrics.values())
    scope = metrics[EVALUATOR_NAME]
    refusal = metrics["no_unnecessary_refusal"]["passed"]
    turn = expected[turn_id]
    return {
        "turn_id": turn_id,
        "conversation_id": str(turn.conversation_id),
        "trace_id": turn.trace_id,
        "schema_version": "2",
        "technical_status": "completed" if completed else "technical_error",
        "metrics": metrics,
        "apple_scope_compliance": scope["passed"],
        "reason": scope["reason"],
        "unnecessary_refusal": not refusal if refusal is not None else None,
        "eval_id": raw["eval_id"],
        "run_id": raw["run_id"],
    }


def evaluate(
    project: AIProjectClient,
    judge: str,
    turns: list[ChatTurn],
    update_manifest: Callable[..., None],
    preserve_raw: Callable[[dict[str, Any]], None],
    timeout_seconds: int = 600,
) -> Iterator[TurnEvaluation]:
    validate_english_dataset(turns)
    criteria = testing_criteria(project, judge, update_manifest)
    with project.get_openai_client(max_retries=0, timeout=60) as client:
        update_manifest(status="creating_eval")
        definition = client.evals.create(
            name="apple-original-response-evaluation",
            data_source_config=DataSourceConfigCustom(type="custom", item_schema=DATA_SCHEMA),
            testing_criteria=criteria,
        )
        update_manifest(eval_id=definition.id, status="creating_run")
        created_run = client.evals.runs.create(
            eval_id=definition.id,
            name="apple-original-responses",
            data_source=CreateEvalJSONLRunDataSourceParam(
                type="jsonl",
                source=SourceFileContent(
                    type="file_content",
                    content=[SourceFileContentContent(item=input_item(turn)) for turn in turns],
                ),
            ),
        )
        update_manifest(
            run_id=created_run.id, status=created_run.status, report_url=created_run.report_url
        )
        deadline = time.monotonic() + timeout_seconds
        # Only idempotent reads get SDK retries; never blindly repeat run creation.
        reader = client.with_options(max_retries=2, timeout=30)
        run = reader.evals.runs.retrieve(run_id=created_run.id, eval_id=definition.id)
        while run.status not in ("completed", "failed", "canceled", "cancelled"):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Foundry evaluation timed out. The cloud run may still continue."
                )
            time.sleep(5)
            run = reader.evals.runs.retrieve(run_id=run.id, eval_id=definition.id)
            update_manifest(status=run.status, report_url=run.report_url)
        if run.status != "completed":
            raise RuntimeError(f"Foundry run ended as {run.status}: {run.error}")
        expected = {str(turn.turn_id): turn for turn in turns}
        seen: set[str | None] = set()
        metric_errors = False
        for output in reader.evals.runs.output_items.list(run_id=run.id, eval_id=definition.id):
            raw = output.model_dump()
            preserve_raw(raw)
            if raw.get("eval_id") != definition.id or raw.get("run_id") != run.id:
                raise ValueError("Foundry returned a result from the wrong run.")
            parsed = parse_result(raw, expected)
            if parsed["turn_id"] in seen:
                raise ValueError("Foundry returned a duplicate turn.")
            seen.add(parsed["turn_id"])
            metric_errors |= parsed["technical_status"] != "completed"
            yield parsed
        if seen != set(expected):
            raise ValueError("Foundry run is missing per-turn results.")
        if metric_errors:
            raise RuntimeError(
                "One or more metrics failed or are missing. Partial results preserved."
            )
