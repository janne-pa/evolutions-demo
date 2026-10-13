import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from demo.evaluation.decision import RUBRICS
from demo.evaluation.models import (
    METRIC_NAMES,
    QUALITY_METRICS,
    is_metric_name,
    missing_metrics,
)
from demo.evaluation.reports import (
    Reports,
    build_summary,
    render_console,
    render_markdown,
    render_turn_table,
)


def test_turn_table_preserves_original_text_and_independent_scores(turn, evaluation_result):
    turn = turn.model_copy(update={"query": "Apples | <script>\n**test**"})
    evaluation_result["metrics"]["apple_scope_compliance"].update(score=2.5, passed=False)
    markdown = render_turn_table([turn], [evaluation_result])
    assert "| 1 |" in markdown
    assert "Apples &#124; &lt;script&gt;<br>&#42;&#42;test&#42;&#42;" in markdown
    assert turn.response in markdown
    assert "Apple scope compliance: 2.50/4" in markdown
    assert "Apple scope compliance: review required (score &lt; 3)." in markdown
    assert str(turn.turn_id) in markdown
    assert "not a model-generated explanation" in markdown


def test_turn_table_includes_every_turn_and_missing_scores(turn, evaluation_result):
    second = turn.model_copy(update={"turn_id": uuid4()})
    markdown = render_turn_table([turn, second], [evaluation_result])
    assert "| 1 |" in markdown and "| 2 |" in markdown
    assert markdown.count("Turn ID:") == 2
    assert "Apple scope compliance: N/A" in markdown
    assert "Apple scope compliance: not evaluated." in markdown


@pytest.fixture
def manifest():
    return {
        "local_run_id": "local-test",
        "eval_id": "eval-test",
        "run_id": "run-test",
        "start": "2026-09-28T08:00:00Z",
        "end": "2026-09-28T09:00:00Z",
        "query_started_at": "2026-09-28T09:00:00Z",
    }


def test_metric_definitions_match_decision_rubrics():
    assert set(QUALITY_METRICS) == set(METRIC_NAMES) == set(RUBRICS)
    assert all(is_metric_name(name) for name in METRIC_NAMES)
    assert not is_metric_name("unknown")
    assert not is_metric_name(None)
    assert not is_metric_name({"name": "fluency"})


def test_summary_has_independent_denominators_and_does_not_mutate_inputs(
    evaluation_result, manifest
):
    first = evaluation_result
    first["metrics"]["apple_scope_compliance"]["passed"] = False
    first["metrics"]["apple_scope_compliance"].update(score=2, passed=False)
    first.update(apple_scope_compliance=False)
    second = deepcopy(evaluation_result)
    second["turn_id"] = "second-turn"
    second["technical_status"] = "technical_error"
    second["metrics"] = missing_metrics("Missing score")
    second.update(apple_scope_compliance=None)
    results = [first, second]
    before = deepcopy((results, manifest))

    summary = build_summary(results, manifest, "technical_error", 3, "Partial results")

    assert (results, manifest) == before
    assert summary["evaluated"] == 1
    assert summary["not_evaluated"] == 2
    assert summary["coverage"] == 1 / 3
    assert summary["scope_evaluated"] == 1
    assert summary["violation_rate"] == 1.0
    assert "unnecessary_refusal_rate" not in summary
    assert summary["metrics"]["apple_scope_compliance"] == {
        "evaluated": 1,
        "not_evaluated": 2,
        "passed": 0,
        "failed": 1,
        "pass_rate": 0.0,
        "coverage": 1 / 3,
        "mean_score": 2.0,
    }
    assert summary["metrics"]["apple_scope_compliance"]["mean_score"] == 2.0
    console = render_console(summary)
    assert "Evaluation incomplete (technical_error)." in console
    assert "Responses fetched: 3; fully evaluated: 1." in console
    assert "Apple-only scope:       0/1 passed - review 1 response; 2 not evaluated" in console


@pytest.mark.parametrize(
    ("status", "fetched", "error"),
    [("no_data", 0, None), ("technical_error", None, "Query failed")],
)
def test_empty_and_failed_queries_are_not_successful_judgments(manifest, status, fetched, error):
    summary = build_summary([], manifest, status, fetched, error)
    assert summary["status"] == status
    assert summary["fetched"] == fetched
    assert summary["not_evaluated"] == fetched
    assert summary["evaluated"] == 0
    assert summary["violation_rate"] is None
    assert "unnecessary_refusal_rate" not in summary
    assert summary["coverage"] is None
    for metric in summary["metrics"].values():
        assert metric["pass_rate"] is None
        assert metric["mean_score"] is None
        assert metric["coverage"] is None
    markdown = render_markdown(summary)
    assert "| Apple scope compliance | 0 | 0 | 0 | N/A | N/A |" in markdown
    assert ("**Technical error:**" in markdown) == bool(error)
    if error:
        assert (
            f"see {Path('technical') / '04_summary.json'} "
            f"and {Path('technical') / '05_run-manifest.json'}"
        ) in markdown
    console = render_console(summary)
    if status == "no_data":
        assert console == (
            "No chat responses found for the selected time window (no_data).\n"
            "No evaluation was performed; Azure ingestion may still be delayed."
        )
    else:
        assert "Evaluation incomplete (technical_error)." in console
        assert "Apple-only scope:       not evaluated" in console
    assert "passed" not in console
    assert "0 detected" not in console


def test_console_summary_is_plain_text_and_preserves_metrics(evaluation_result, manifest):
    summary = build_summary([evaluation_result], manifest, "completed", 1)
    before = deepcopy(summary)
    console = render_console(summary)
    assert console == ("Evaluation completed: 1 response\n\nApple-only scope:       1/1 passed")
    assert "Quality scores" not in console
    assert "Binary mean scores" not in console
    assert "No unnecessary refusal" not in console
    assert "\x1b" not in console
    assert console.isascii()
    assert summary == before


def test_console_calls_out_responses_to_review(evaluation_result, manifest):
    results = [deepcopy(evaluation_result) for _ in range(8)]
    for result in results[:2]:
        result["metrics"]["apple_scope_compliance"].update(passed=False, score=2)
    summary = build_summary(results, manifest, "completed", 8)
    assert render_console(summary) == (
        "Evaluation completed: 8 responses\n\n"
        "Apple-only scope:       6/8 passed - review 2 responses"
    )


def test_console_does_not_report_missing_refusal_judgments_as_zero(evaluation_result, manifest):
    evaluation_result["metrics"] = missing_metrics("Missing")
    evaluation_result["technical_status"] = "technical_error"
    summary = build_summary([evaluation_result], manifest, "technical_error", 1, "Missing")
    console = render_console(summary)
    assert "Apple-only scope:       not evaluated" in console
    assert "0 detected" not in console
    assert "passed" not in console


def test_console_unknown_window_and_count_are_not_zero(manifest):
    manifest.pop("start")
    manifest.pop("end")
    summary = build_summary([], manifest, "technical_error", None, "Invalid time window")
    console = render_console(summary)
    assert "Evaluation incomplete (technical_error)." in console
    assert "Responses fetched: unknown; fully evaluated: 0." in console
    assert "None" not in console


@pytest.mark.parametrize("score", [None, True])
def test_completed_quality_metric_requires_a_numeric_score(evaluation_result, manifest, score):
    evaluation_result["metrics"]["apple_scope_compliance"]["score"] = score
    with pytest.raises(ValueError, match="apple_scope_compliance needs a numeric score"):
        build_summary([evaluation_result], manifest, "completed", 1)


def test_markdown_format_is_preserved(evaluation_result, manifest):
    summary = build_summary([evaluation_result], manifest, "completed", 1)
    before = deepcopy(summary)
    assert render_markdown(summary) == (
        "# Apple Chat evaluation\n\n"
        "**Status:** completed\n\n"
        "UTC interval: [2026-09-28T08:00:00Z, 2026-09-28T09:00:00Z)\n\n"
        "Fetched: **1**. Evaluated responses: **1**.\n\n"
        "| Metric | Evaluated | Passed | Failed | Pass rate | Mean score |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| Apple scope compliance | 1 | 1 | 0 | 100.0% | 4.00/4 |\n\n"
        "Apple policy compliance uses the native 0-4 score; scores below 3 are findings. "
        "One Decision-1 request evaluates each turn. Scores can be fractional "
        "because the API returns probability-weighted rubric scores.\n\n"
        "Original texts: `02_chat_turns.jsonl`. "
        "Per-metric scores and decisions: `03_judge_results.jsonl`.\n\n"
        + summary["limitations"]
        + "\n"
    )
    assert summary == before


def test_missing_metrics_are_independent_and_keep_optional_fields_absent():
    first = missing_metrics("Timed out")
    first["apple_scope_compliance"]["error"] = "Changed"
    second = missing_metrics("Timed out")
    assert first["apple_scope_compliance"]["error"] == "Changed"
    assert second["apple_scope_compliance"] == {
        "technical_status": "not_evaluated",
        "passed": None,
        "score": None,
        "reason": None,
        "threshold": 3,
        "error": "Timed out",
    }


def test_finish_preserves_completed_rows_and_adds_missing_turns(
    tmp_path, turn, evaluation_result, manifest
):
    report = Reports(tmp_path, "2026-09-28T09:00:00Z")
    report.update(**manifest)
    second = turn.model_copy(update={"turn_id": uuid4()})
    turns = [turn, second]
    report.snapshot([item.model_dump(mode="json") for item in turns])
    report.append_result(evaluation_result)
    returned_summary = report.finish("technical_error", turns, 2, "Interrupted")

    rows = [
        json.loads(line)
        for line in (report.path / "03_judge_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows == [
        evaluation_result,
        {
            "turn_id": str(second.turn_id),
            "conversation_id": str(second.conversation_id),
            "trace_id": second.trace_id,
            "technical_status": "not_evaluated",
            "schema_version": "6",
            "metrics": missing_metrics("Interrupted"),
            "apple_scope_compliance": None,
            "reason": None,
            "error": "Interrupted",
            "eval_id": "eval-test",
            "run_id": "run-test",
        },
    ]
    summary = json.loads(
        (report.path / "technical" / "04_summary.json").read_text(encoding="utf-8")
    )
    assert returned_summary == summary
    assert summary == build_summary(rows, report.manifest, "technical_error", 2, "Interrupted")
    assert report.manifest["judge_model_versions"] == ["judge-test"]


def test_invalid_records_keep_original_identifiers_and_record_indexes(tmp_path):
    report = Reports(tmp_path, "2026-09-28T09:00:00Z")
    items = [{"invalid_log_record": {"payload": "broken"}}, {"turn_id": "original-id"}]
    report.snapshot(items)
    report.finish("technical_error", None, 2, "Invalid payload")

    rows = [
        json.loads(line)
        for line in (report.path / "03_judge_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["record_index"] for row in rows] == [0, 1]
    assert [row["turn_id"] for row in rows] == [None, "original-id"]
    assert all(row["technical_status"] == "not_evaluated" for row in rows)
    assert all(row["metrics"] == missing_metrics("Invalid payload") for row in rows)
    assert [
        json.loads(line)
        for line in (report.path / "02_chat_turns.jsonl").read_text(encoding="utf-8").splitlines()
    ] == items
