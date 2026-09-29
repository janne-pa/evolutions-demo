import hashlib
import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from demo.contracts import ChatTurn, json_text, utc_now
from demo.evaluation.models import (
    EVALUATOR_NAME,
    METRIC_LABELS,
    METRIC_NAMES,
    QUALITY_METRICS,
    QUALITY_THRESHOLD,
    EvaluationSummary,
    MetricName,
    MetricSummary,
    RunStatus,
    TurnEvaluation,
    missing_metrics,
)
from demo.settings import ROOT

SUMMARY_MARKDOWN = "01_summary.md"
INPUT_JSONL = "02_chat_turns.jsonl"
RESULTS_JSONL = "03_judge_results.jsonl"
SUMMARY_JSON = "04_summary.json"
RUN_MANIFEST_JSON = "05_run-manifest.json"
RAW_LOG_RECORDS_JSON = "06_raw-log-records.json"
FOUNDRY_OUTPUT_JSONL = "07_foundry-output.jsonl"
TECHNICAL_DIR = "technical"


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def code_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "demo").rglob("*")):
        if path.is_file() and path.suffix in (".py", ".txt", ".html", ".js", ".css"):
            digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def build_summary(
    results: Sequence[TurnEvaluation],
    manifest: Mapping[str, Any],
    status: RunStatus,
    fetched: int | None,
    error: str | None = None,
) -> EvaluationSummary:
    count = sum(result["technical_status"] == "completed" for result in results)
    metric_summaries: dict[MetricName, MetricSummary] = {}
    for name in METRIC_NAMES:
        values = [
            result["metrics"][name]
            for result in results
            if result["metrics"][name]["technical_status"] == "completed"
        ]
        evaluated = len(values)
        passed = sum(value["passed"] is True for value in values)
        mean_score = None
        if evaluated and name in QUALITY_METRICS:
            total = 0.0
            for value in values:
                score = value["score"]
                if score is None or isinstance(score, bool):
                    raise ValueError(f"Completed quality metric {name} needs a numeric score.")
                total += score
            mean_score = total / evaluated
        metric_summaries[name] = {
            "evaluated": evaluated,
            "not_evaluated": fetched - evaluated if fetched is not None else None,
            "passed": passed,
            "failed": evaluated - passed,
            "pass_rate": passed / evaluated if evaluated else None,
            "coverage": evaluated / fetched if fetched else None,
            "mean_score": mean_score,
        }
    scope = metric_summaries[EVALUATOR_NAME]
    refusal = metric_summaries["no_unnecessary_refusal"]
    return {
        "schema_version": "2",
        "status": status,
        "source": "azure_apptraces",
        "start": manifest.get("start"),
        "end": manifest.get("end"),
        "query_started_at": manifest.get("query_started_at"),
        "fetched": fetched,
        "evaluated": count,
        "not_evaluated": fetched - count if fetched is not None else None,
        "metrics": metric_summaries,
        "scope_evaluated": scope["evaluated"],
        "passed": scope["passed"],
        "violations": scope["failed"],
        "violation_rate": scope["failed"] / scope["evaluated"] if scope["evaluated"] else None,
        "unnecessary_refusals": refusal["failed"],
        "unnecessary_refusal_rate": (
            refusal["failed"] / refusal["evaluated"] if refusal["evaluated"] else None
        ),
        "coverage": count / fetched if fetched else None,
        "error": error,
        "local_run_id": manifest["local_run_id"],
        "eval_id": manifest["eval_id"],
        "run_id": manifest["run_id"],
        "limitations": (
            "Coverage describes fetched rows, not all chat events. Azure ingestion is delayed. "
            "No_data is not evidence of quality. "
            "Metrics are independent, not a combined score. "
            "Fluency/coherence support English responses; short replies can be less reliable. "
            "Coherence is a per-turn writing metric, not our topic policy or a full "
            "conversation assessment. LLM judges are imperfect and do not verify factual truth."
        ),
    }


def render_markdown(summary: EvaluationSummary) -> str:
    metric_rows = []
    for name, data in summary["metrics"].items():
        mean = f"{data['mean_score']:.2f}/5" if data["mean_score"] is not None else "N/A"
        rate = f"{data['pass_rate']:.1%}" if data["pass_rate"] is not None else "N/A"
        metric_rows.append(
            f"| {METRIC_LABELS[name]} | {data['evaluated']} | {data['passed']} | "
            f"{data['failed']} | {rate} | {mean} |"
        )
    return (
        "# Apple Chat evaluation\n\n"
        f"**Status:** {summary['status']}\n\n"
        f"UTC interval: [{summary['start']}, {summary['end']})\n\n"
        f"Fetched: **{summary['fetched']}**. "
        f"Turns with all four metrics completed: **{summary['evaluated']}**.\n\n"
        "| Metric | Evaluated | Passed | Failed | Pass rate | Mean score |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(metric_rows)
        + "\n\n"
        + f"Quality scores use a 1-5 scale (pass >= {QUALITY_THRESHOLD}). "
        + "Binary metric scores are N/A.\n"
        + "A failed **No unnecessary refusal** result means an unnecessary refusal was found. "
        + "Its rate uses all successfully judged turns, not just in-scope questions.\n\n"
        + f"Foundry eval: `{summary['eval_id']}`  \n"
        + f"Foundry run: `{summary['run_id']}`\n\n"
        + (
            f"**Technical error:** see {Path(TECHNICAL_DIR) / SUMMARY_JSON} "
            f"and {Path(TECHNICAL_DIR) / RUN_MANIFEST_JSON}.\n\n"
            if summary["error"]
            else ""
        )
        + f"Original texts: `{INPUT_JSONL}`. "
        + f"Per-metric decisions and reasons: `{RESULTS_JSONL}`.\n\n"
        + summary["limitations"]
        + "\n"
    )


def render_console(summary: EvaluationSummary) -> str:
    if summary["status"] == "no_data":
        return (
            "No chat responses found for the selected time window (no_data).\n"
            "No evaluation was performed; Azure ingestion may still be delayed."
        )
    if summary["status"] == "technical_error":
        fetched = summary["fetched"] if summary["fetched"] is not None else "unknown"
        lines = [
            "Evaluation incomplete (technical_error).",
            f"Responses fetched: {fetched}; fully evaluated: {summary['evaluated']}.",
        ]
    else:
        count = summary["evaluated"]
        noun = "response" if count == 1 else "responses"
        lines = [f"Evaluation completed: {count} {noun}"]
    lines.append("")
    labels: tuple[tuple[MetricName, str], ...] = (
        ("apple_scope_compliance", "Apple-only scope"),
        ("no_unnecessary_refusal", "Unnecessary refusals"),
        ("fluency", "Writing fluency"),
        ("coherence", "Coherence"),
    )
    for name, label in labels:
        data = summary["metrics"][name]
        evaluated, failed, missing = data["evaluated"], data["failed"], data["not_evaluated"]
        if not evaluated:
            detail = "not evaluated"
        elif name == "no_unnecessary_refusal":
            detail = f"{failed} detected"
            if missing:
                detail += f" ({evaluated} evaluated)"
        else:
            detail = f"{data['passed']}/{evaluated} passed"
            if failed:
                noun = "response" if failed == 1 else "responses"
                detail += f" - review {failed} {noun}"
        if evaluated and missing:
            detail += f"; {missing} not evaluated"
        lines.append(f"{label + ':':<24}{detail}")
    return "\n".join(lines)


class Reports:
    def __init__(self, root: Path, started_at: str) -> None:
        self.path = root / (utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
        self.path.mkdir(parents=True, exist_ok=False)
        self.technical_path = self.path / TECHNICAL_DIR
        self.technical_path.mkdir()
        self.manifest: dict[str, Any] = {
            "schema_version": "2",
            "local_run_id": self.path.name,
            "started_at": started_at,
            "source": "azure_apptraces",
            "status": "starting",
            "eval_id": None,
            "run_id": None,
            "report_url": None,
            "input_sha256": None,
            "code_version": "0.2.0",
            "code_sha256": code_hash(),
            "metric_names": list(METRIC_NAMES),
            "evaluators": {},
            "evaluation_language": "en",
            "sdk_versions": {
                name: importlib.metadata.version(name)
                for name in (
                    "azure-ai-projects",
                    "openai",
                    "azure-monitor-query",
                    "azure-monitor-opentelemetry",
                    "azure-identity",
                )
            },
        }
        self.results: list[TurnEvaluation] = []
        self.update()

    def update(self, **values: Any) -> None:
        self.manifest.update(values)
        write_json(self.technical_path / RUN_MANIFEST_JSON, self.manifest)

    def snapshot(self, items: list[dict[str, Any]]) -> None:
        with (self.path / INPUT_JSONL).open("x", encoding="utf-8", newline="\n") as stream:
            for item in items:
                stream.write(json_text(item) + "\n")
        self.update(input_sha256=hashlib.sha256((self.path / INPUT_JSONL).read_bytes()).hexdigest())

    def append_raw(self, value: dict[str, Any]) -> None:
        with (self.technical_path / FOUNDRY_OUTPUT_JSONL).open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(json_text(value) + "\n")

    def append_result(self, value: TurnEvaluation) -> None:
        self.results.append(value)
        with (self.path / RESULTS_JSONL).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json_text(value) + "\n")

    def _not_evaluated(
        self,
        item: Mapping[str, Any],
        error: str | None,
    ) -> TurnEvaluation:
        return {
            "turn_id": item.get("turn_id"),
            "conversation_id": item.get("conversation_id"),
            "trace_id": item.get("trace_id"),
            "technical_status": "not_evaluated",
            "schema_version": "2",
            "metrics": missing_metrics(error),
            "apple_scope_compliance": None,
            "unnecessary_refusal": None,
            "reason": None,
            "error": error,
            "eval_id": self.manifest["eval_id"],
            "run_id": self.manifest["run_id"],
        }

    def _append_missing_results(
        self,
        turns: list[ChatTurn] | None,
        fetched: int | None,
        error: str | None,
    ) -> None:
        seen = {result["turn_id"] for result in self.results}
        for turn in turns or []:
            if str(turn.turn_id) not in seen:
                self.append_result(self._not_evaluated(turn.model_dump(mode="json"), error))
        if turns is None and fetched:
            for index, line in enumerate(
                (self.path / INPUT_JSONL).read_text(encoding="utf-8").splitlines()
            ):
                self.append_result(
                    {
                        "record_index": index,
                        **self._not_evaluated(json.loads(line), error),
                    }
                )

    def finish(
        self,
        status: RunStatus,
        turns: list[ChatTurn] | None,
        fetched: int | None,
        error: str | None = None,
    ) -> EvaluationSummary:
        self._append_missing_results(turns, fetched, error)
        (self.path / RESULTS_JSONL).touch(exist_ok=True)
        summary = build_summary(self.results, self.manifest, status, fetched, error)
        write_json(self.technical_path / SUMMARY_JSON, summary)
        (self.path / SUMMARY_MARKDOWN).write_text(render_markdown(summary), encoding="utf-8")
        self.update(
            last_operation_status=self.manifest["status"],
            status=status,
            error=error,
            finished_at=utc_now().isoformat(),
            judge_model_versions=sorted(
                {
                    model
                    for result in self.results
                    for metric in result["metrics"].values()
                    if (model := metric.get("judge_model"))
                }
            ),
        )
        return summary
