import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from demo.evaluation import reports, run
from demo.evaluation.models import METRIC_NAMES, QUALITY_METRICS


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(
        run.Settings,
        "load",
        lambda: SimpleNamespace(
            endpoint="https://example.services.ai.azure.com/api/projects/demo",
            resource_endpoint="https://example.cognitiveservices.azure.com",
            judge_deployment="judge",
            workspace_id="workspace",
        ),
    )
    monkeypatch.setattr(run, "credential", MagicMock())
    monkeypatch.setattr(run, "LogsQueryClient", MagicMock())
    now = datetime(2026, 9, 28, 10, 19, 33, tzinfo=UTC)
    monkeypatch.setattr(run, "utc_now", lambda: now)
    return now


def files(tmp_path):
    folder = next(tmp_path.iterdir())
    technical = folder / "technical"
    summary = json.loads((technical / "04_summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((technical / "05_run-manifest.json").read_text(encoding="utf-8"))
    for filename in ("02_chat_turns.jsonl", "03_judge_results.jsonl", "01_summary.md"):
        assert (folder / filename).exists()
    assert (
        manifest["input_sha256"]
        == hashlib.sha256((folder / "02_chat_turns.jsonl").read_bytes()).hexdigest()
    )
    assert {path.name for path in folder.iterdir()} == {
        "01_summary.md",
        "02_chat_turns.jsonl",
        "03_judge_results.jsonl",
        "technical",
    }
    assert {path.name for path in technical.iterdir()} <= {
        "04_summary.json",
        "05_run-manifest.json",
        "06_raw-log-records.json",
        "07_decision-output.jsonl",
    }
    return folder, summary, manifest


def assert_console_report(capsys, folder, summary):
    captured = capsys.readouterr()
    assert captured.out == (
        f"{reports.render_console(summary)}\n\nDetails: {(folder / '01_summary.md').resolve()}\n"
    )
    assert "\r" not in captured.err
    assert "\x1b" not in captured.err
    return captured.err


def test_empty_success_is_no_data_not_pass(prepared, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run, "fetch", lambda *args: [])
    evaluator = MagicMock()
    monkeypatch.setattr(run, "evaluate", evaluator)
    assert run.main(["--output-dir", str(tmp_path)]) == 0
    folder, summary, manifest = files(tmp_path)
    progress_output = assert_console_report(capsys, folder, summary)
    assert progress_output == "Reading chat logs...\n"
    assert summary["status"] == "no_data"
    assert summary["violation_rate"] is None
    assert summary["coverage"] is None
    assert summary["fetched"] == 0
    assert set(summary["metrics"]) == set(METRIC_NAMES)
    assert all(m["pass_rate"] is None for m in summary["metrics"].values())
    assert all(m["mean_score"] is None for m in summary["metrics"].values())
    assert manifest["end"] == "2026-09-28T10:19:33Z"
    assert manifest["start"] == "2026-09-27T10:19:33Z"
    evaluator.assert_not_called()


@pytest.mark.parametrize("failed_metric", METRIC_NAMES)
def test_findings_do_not_fail_process(
    prepared, monkeypatch, tmp_path, turn, failed_metric, evaluation_result, capsys
):
    monkeypatch.setattr(run, "fetch", lambda *args: [turn])
    evaluation_result["metrics"][failed_metric]["passed"] = False
    if failed_metric in QUALITY_METRICS:
        evaluation_result["metrics"][failed_metric]["score"] = 2
    evaluation_result["apple_scope_compliance"] = failed_metric != "apple_scope_compliance"
    monkeypatch.setattr(run, "evaluate", lambda *args: iter([evaluation_result]))
    assert run.main(["--output-dir", str(tmp_path)]) == 0
    folder, summary, _ = files(tmp_path)
    progress_output = assert_console_report(capsys, folder, summary)
    assert progress_output == "Reading chat logs...\nScoring responses (1)...\n"
    assert summary["status"] == "completed"
    assert summary["violation_rate"] == (1 if failed_metric == "apple_scope_compliance" else 0)
    assert summary["metrics"][failed_metric]["failed"] == 1
    assert set(summary["metrics"]) == set(METRIC_NAMES)
    assert summary["metrics"][failed_metric]["mean_score"] == 2
    assert "unnecessary_refusal_rate" not in summary


def test_technical_failure_keeps_snapshot(prepared, monkeypatch, tmp_path, turn, capsys):
    monkeypatch.setattr(run, "fetch", lambda *args: [turn])
    monkeypatch.setattr(run, "evaluate", MagicMock(side_effect=TimeoutError("Timed out")))
    assert run.main(["--output-dir", str(tmp_path)]) == 1
    folder, summary, _ = files(tmp_path)
    assert_console_report(capsys, folder, summary)
    assert summary["status"] == "technical_error"
    assert summary["not_evaluated"] == 1
    assert summary["violation_rate"] is None
    assert all(m["not_evaluated"] == 1 for m in summary["metrics"].values())
    assert (
        json.loads((folder / "02_chat_turns.jsonl").read_text(encoding="utf-8"))["response"]
        == turn.response
    )


def test_query_failure_is_not_no_data(prepared, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run, "fetch", MagicMock(side_effect=RuntimeError("Access denied")))
    assert run.main(["--output-dir", str(tmp_path)]) == 1
    folder, summary, _ = files(tmp_path)
    assert_console_report(capsys, folder, summary)
    assert summary["status"] == "technical_error"
    assert summary["fetched"] is None


def test_invalid_range_writes_error_report(tmp_path, capsys):
    assert run.main(["--start", "2026-09-28", "--output-dir", str(tmp_path)]) == 1
    folder, summary, _ = files(tmp_path)
    assert_console_report(capsys, folder, summary)
    assert summary["status"] == "technical_error"


def test_partial_metrics_remain_reportable(
    prepared, monkeypatch, tmp_path, turn, evaluation_result, capsys
):
    monkeypatch.setattr(run, "fetch", lambda *args: [turn])

    def partial(*args):
        evaluation_result["metrics"]["apple_scope_compliance"] = {
            "technical_status": "error",
            "passed": None,
            "score": None,
            "reason": None,
            "error": "Throttled",
        }
        evaluation_result["technical_status"] = "technical_error"
        yield evaluation_result
        raise RuntimeError("One metric failed")

    monkeypatch.setattr(run, "evaluate", partial)
    assert run.main(["--output-dir", str(tmp_path)]) == 1
    folder, summary, _ = files(tmp_path)
    assert_console_report(capsys, folder, summary)
    assert summary["evaluated"] == 0
    assert summary["metrics"]["apple_scope_compliance"]["mean_score"] is None
    assert summary["metrics"]["apple_scope_compliance"]["evaluated"] == 0
    assert summary["violation_rate"] is None


def test_language_guard_keeps_original_dataset(prepared, monkeypatch, tmp_path, turn, capsys):
    legacy = turn.model_copy(update={"prompt_version": "chat-v1"})
    monkeypatch.setattr(run, "fetch", lambda *args: [legacy])
    assert run.main(["--output-dir", str(tmp_path)]) == 1
    folder, summary, manifest = files(tmp_path)
    assert_console_report(capsys, folder, summary)
    assert summary["status"] == "technical_error"
    assert "legacy/multilingual" in summary["error"]
    assert json.loads((folder / "02_chat_turns.jsonl").read_text()) == legacy.model_dump(
        mode="json"
    )
    assert manifest["eval_id"] is None
    assert manifest["run_id"] is None


def test_interrupted_run_prints_partial_results_and_keeps_exit_code(
    prepared, monkeypatch, tmp_path, turn, evaluation_result, capsys
):
    monkeypatch.setattr(run, "fetch", lambda *args: [turn])

    def interrupted(*args):
        yield evaluation_result
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "evaluate", interrupted)
    assert run.main(["--output-dir", str(tmp_path)]) == 130
    folder, summary, manifest = files(tmp_path)
    assert_console_report(capsys, folder, summary)
    assert summary["status"] == "technical_error"
    assert summary["evaluated"] == 1
    assert summary["metrics"]["apple_scope_compliance"]["mean_score"] == 4
    assert "Interrupted" in manifest["error"]


@pytest.mark.parametrize("legacy_numbered", [False, True])
def test_three_primary_artifacts_and_existing_runs_are_preserved(
    prepared, monkeypatch, tmp_path, turn, evaluation_result, capsys, legacy_numbered
):
    expected_names = [
        "01_summary.md",
        "02_chat_turns.jsonl",
        "03_judge_results.jsonl",
        "04_summary.json",
        "05_run-manifest.json",
        "06_raw-log-records.json",
        "07_decision-output.jsonl",
    ]
    existing = tmp_path / "previous-run"
    existing.mkdir()
    legacy_names = [
        name.replace("judge_results", "results").replace("chat_turns", "input")
        for name in expected_names
    ]
    if not legacy_numbered:
        legacy_names = [name[3:] for name in legacy_names]
    original_files = {name: f"Existing contents of {name}".encode() for name in legacy_names}
    for name, content in original_files.items():
        (existing / name).write_bytes(content)
    raw_logs = [{"payload": turn.payload()}]
    raw_output = {"test_output": str(turn.turn_id)}

    def fetch(client, workspace, start, end, preserve):
        preserve(raw_logs)
        return [turn]

    def evaluate(endpoint, judge, auth, turns, update, preserve_raw, timeout):
        preserve_raw(raw_output)
        yield evaluation_result

    monkeypatch.setattr(run, "fetch", fetch)
    monkeypatch.setattr(run, "evaluate", evaluate)
    assert run.main(["--output-dir", str(tmp_path)]) == 0
    new_folders = [path for path in tmp_path.iterdir() if path != existing]
    assert len(new_folders) == 1
    folder = new_folders[0]
    assert sorted(path.name for path in folder.iterdir()) == expected_names[:3] + ["technical"]
    technical = folder / "technical"
    assert sorted(path.name for path in technical.iterdir()) == expected_names[3:]
    assert {path.name: path.read_bytes() for path in existing.iterdir()} == original_files
    assert json.loads((folder / "02_chat_turns.jsonl").read_text()) == turn.model_dump(mode="json")
    assert json.loads((folder / "03_judge_results.jsonl").read_text()) == evaluation_result
    assert json.loads((technical / "06_raw-log-records.json").read_text()) == raw_logs
    assert json.loads((technical / "07_decision-output.jsonl").read_text()) == raw_output
    manifest = json.loads((technical / "05_run-manifest.json").read_text())
    assert (
        manifest["input_sha256"]
        == hashlib.sha256((folder / "02_chat_turns.jsonl").read_bytes()).hexdigest()
    )
    markdown = (folder / "01_summary.md").read_text()
    assert "`02_chat_turns.jsonl`" in markdown
    assert "`03_judge_results.jsonl`" in markdown
    summary = json.loads((technical / "04_summary.json").read_text())
    assert_console_report(capsys, folder, summary)


def test_invalid_log_payload_keeps_numbered_evidence(prepared, monkeypatch, tmp_path, capsys):
    raw_logs = [{"payload": "{broken JSON"}]

    def fetch(client, workspace, start, end, preserve):
        preserve(raw_logs)
        raise ValueError("Invalid payload")

    monkeypatch.setattr(run, "fetch", fetch)
    assert run.main(["--output-dir", str(tmp_path)]) == 1
    folder, summary, _ = files(tmp_path)
    assert_console_report(capsys, folder, summary)
    technical = folder / "technical"
    assert json.loads((technical / "06_raw-log-records.json").read_text()) == raw_logs
    assert json.loads((folder / "02_chat_turns.jsonl").read_text()) == {
        "invalid_log_record": raw_logs[0]
    }
    result = json.loads((folder / "03_judge_results.jsonl").read_text())
    assert result["technical_status"] == "not_evaluated"
    assert result["record_index"] == 0
    assert summary["status"] == "technical_error"
    assert summary["fetched"] == 1
    assert summary["evaluated"] == 0
    assert not (technical / "07_decision-output.jsonl").exists()
    markdown = (folder / "01_summary.md").read_text()
    assert (
        f"see {Path('technical') / '04_summary.json'} "
        f"and {Path('technical') / '05_run-manifest.json'}"
    ) in markdown


@pytest.mark.parametrize(
    "relative_path",
    [
        "settings.py",
        "contracts.py",
        "chat/main.py",
        "evaluation/run.py",
        "evaluation/models.py",
        "evaluation/progress.py",
        "prompts/chat-en-v2.txt",
        "chat/static/index.html",
        "chat/static/chat.js",
        "chat/static/style.css",
    ],
)
def test_code_hash_covers_reorganized_source_and_assets(monkeypatch, tmp_path, relative_path):
    monkeypatch.setattr(reports, "ROOT", tmp_path)
    file = tmp_path / "demo" / relative_path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("original", encoding="utf-8")
    original_hash = reports.code_hash()
    assert reports.code_hash() == original_hash
    file.write_text("changed", encoding="utf-8")
    assert reports.code_hash() != original_hash
    file.write_text("original", encoding="utf-8")
    file.rename(file.with_name("renamed" + file.suffix))
    assert reports.code_hash() != original_hash


def test_code_hash_ignores_runtime_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(reports, "ROOT", tmp_path)
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "settings.py").write_text("source", encoding="utf-8")
    original_hash = reports.code_hash()
    (tmp_path / "demo" / "settings.pyc").write_bytes(b"generated")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "01_summary.md").write_text("report", encoding="utf-8")
    assert reports.code_hash() == original_hash
