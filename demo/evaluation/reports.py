import hashlib
import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from demo.contracts import ChatTurn, json_text, utc_now
from demo.evaluation.models import (
    METRIC_LABELS,
    METRIC_NAMES,
    QUALITY_METRICS,
    QUALITY_THRESHOLD,
    SCOPE_METRIC,
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
DECISION_OUTPUT_JSONL = "07_decision-output.jsonl"
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
        if path.is_file() and path.suffix in (".py", ".txt", ".json", ".html", ".js", ".css"):
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
    scope = metric_summaries[SCOPE_METRIC]
    return {
        "schema_version": "6",
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
        "coverage": count / fetched if fetched else None,
        "error": error,
        "local_run_id": manifest["local_run_id"],
        "eval_id": manifest["eval_id"],
        "run_id": manifest["run_id"],
        "judge_model": manifest.get("judge_model"),
        "judge_deployment": manifest.get("judge_deployment"),
        "limitations": (
            "Coverage describes fetched rows, not all chat events. Azure ingestion is delayed. "
            "No_data is not evidence of quality. "
            "Only apple-only policy compliance is evaluated, not helpfulness or refusal quality. "
            "The metric uses the native Decision-1 0-4 scale with five rubric levels; "
            "scores can be fractional because "
            "the API returns probability-weighted rubric scores. Scores are not calibrated "
            "against human labels and do not verify factual truth. The rubrics assess the "
            "English apple-only chat policy, including context-dependent replies. "
            "Decision-1 returns scores "
            "without written rationales."
        ),
    }


def render_markdown(summary: EvaluationSummary) -> str:
    metric_rows = []
    for name, data in summary["metrics"].items():
        mean = f"{data['mean_score']:.2f}/4" if data["mean_score"] is not None else "N/A"
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
        f"Evaluated responses: **{summary['evaluated']}**.\n\n"
        "| Metric | Evaluated | Passed | Failed | Pass rate | Mean score |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(metric_rows)
        + "\n\n"
        + "Apple policy compliance uses the native 0-4 score; "
        + f"scores below {QUALITY_THRESHOLD} are findings. "
        + "One Decision-1 request evaluates each turn. Scores can be fractional "
        + "because the API returns probability-weighted rubric scores.\n\n"
        + (
            f"Judge model: `{summary['judge_model']}` "
            f"(deployment `{summary['judge_deployment']}`).\n\n"
            if summary["judge_model"]
            else ""
        )
        + (
            f"**Technical error:** see {Path(TECHNICAL_DIR) / SUMMARY_JSON} "
            f"and {Path(TECHNICAL_DIR) / RUN_MANIFEST_JSON}.\n\n"
            if summary["error"]
            else ""
        )
        + f"Original texts: `{INPUT_JSONL}`. "
        + f"Per-metric scores and decisions: `{RESULTS_JSONL}`.\n\n"
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
    labels: tuple[tuple[MetricName, str], ...] = (("apple_scope_compliance", "Apple-only scope"),)
    for name, label in labels:
        data = summary["metrics"][name]
        evaluated, failed, missing = data["evaluated"], data["failed"], data["not_evaluated"]
        if not evaluated:
            detail = "not evaluated"
        else:
            detail = f"{data['passed']}/{evaluated} passed"
            if failed:
                noun = "response" if failed == 1 else "responses"
                detail += f" - review {failed} {noun}"
        if evaluated and missing:
            detail += f"; {missing} not evaluated"
        lines.append(f"{label + ':':<24}{detail}")
    return "\n".join(lines)


def table_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "&#92;")
        .replace("|", "&#124;")
        .replace("*", "&#42;")
        .replace("_", "&#95;")
        .replace("`", "&#96;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def render_turn_table(turns: Sequence[ChatTurn], results: Sequence[TurnEvaluation]) -> str:
    by_id = {result["turn_id"]: result for result in results}
    rows = []
    for index, turn in enumerate(turns, start=1):
        result = by_id.get(str(turn.turn_id))
        scores = []
        interpretations = []
        for name in METRIC_NAMES:
            metric = result["metrics"][name] if result else None
            if metric and metric["technical_status"] == "completed" and metric["score"] is not None:
                score = metric["score"]
                scores.append(f"{METRIC_LABELS[name]}: {score:.2f}/4")
                outcome = "accepted" if score >= QUALITY_THRESHOLD else "review required"
                interpretations.append(
                    f"{METRIC_LABELS[name]}: {outcome} "
                    f"(score {'>=' if score >= QUALITY_THRESHOLD else '<'} {QUALITY_THRESHOLD})."
                )
            else:
                scores.append(f"{METRIC_LABELS[name]}: N/A")
                detail = metric.get("error") if metric else None
                interpretations.append(
                    f"{METRIC_LABELS[name]}: not evaluated"
                    + (f" ({detail})" if detail else "")
                    + "."
                )
        rows.append(
            f"| {index} | {table_text(chr(10).join(scores))} | {table_text(turn.query)} | "
            f"{table_text(turn.response)} | "
            f"{table_text(chr(10).join(interpretations))} "
            f"<br>Turn ID: {turn.turn_id} |"
        )
    return (
        "\n## Original responses and evaluation scores\n\n"
        "One row per original response with its apple policy grade. "
        f"Scores below {QUALITY_THRESHOLD} require review. Decisions use the full-precision score, "
        "not the rounded display value.\n\n"
        "**Rationale limitation:** Decision-1 does not return written rationales. The final "
        "column is a rule-based interpretation of its scores, not a model-generated explanation "
        "of why the response received those scores.\n\n"
        "| # | Grade (0-4) | Original request | AI response | Evaluation interpretation |\n"
        "| --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n"
    )


class Reports:
    def __init__(self, root: Path, started_at: str) -> None:
        self.path = root / (utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
        self.path.mkdir(parents=True, exist_ok=False)
        self.technical_path = self.path / TECHNICAL_DIR
        self.technical_path.mkdir()
        self.manifest: dict[str, Any] = {
            "schema_version": "6",
            "local_run_id": self.path.name,
            "started_at": started_at,
            "source": "azure_apptraces",
            "status": "starting",
            "eval_id": None,
            "run_id": None,
            "report_url": None,
            "input_sha256": None,
            "code_version": "0.3.0",
            "code_sha256": code_hash(),
            "metric_names": list(METRIC_NAMES),
            "judge_model": None,
            "judge_deployment": None,
            "judge_model_version": None,
            "judge_prompt_version": "decision-scores-v3",
            "score_scale": {"minimum": 0, "maximum": 4, "pass_threshold": QUALITY_THRESHOLD},
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
        with (self.technical_path / DECISION_OUTPUT_JSONL).open(
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
            "schema_version": "6",
            "metrics": missing_metrics(error),
            "apple_scope_compliance": None,
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
        markdown = render_markdown(summary)
        if turns:
            markdown += render_turn_table(turns, self.results)
        (self.path / SUMMARY_MARKDOWN).write_text(markdown, encoding="utf-8")
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
