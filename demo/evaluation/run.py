import argparse
import logging
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from azure.core.exceptions import AzureError
from azure.monitor.query import LogsQueryClient

from demo.contracts import ChatTurn, utc_now, utc_text
from demo.evaluation.decision import (
    JUDGE_MODEL,
    JUDGE_MODEL_VERSION,
    JUDGE_PROMPT_VERSION,
    evaluate,
)
from demo.evaluation.logs import fetch, snapshot_rows
from demo.evaluation.models import RunStatus
from demo.evaluation.progress import progress
from demo.evaluation.reports import (
    INPUT_JSONL,
    RAW_LOG_RECORDS_JSON,
    SUMMARY_MARKDOWN,
    Reports,
    render_console,
    write_json,
)
from demo.settings import Settings, credential

LOG = logging.getLogger(__name__)


def parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    utc_text(value)
    return value


def time_window(
    now: datetime, start: str | None, end: str | None, lookback: float | None
) -> tuple[datetime, datetime]:
    utc_text(now)
    if start is not None or end is not None:
        if not start or not end or lookback is not None:
            raise ValueError("--start and --end are required together, without --lookback-hours.")
        beginning, ending = parse_utc(start), parse_utc(end)
    else:
        hours = 24.0 if lookback is None else lookback
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError("--lookback-hours must be a positive finite number.")
        beginning, ending = now - timedelta(hours=hours), now
    if beginning >= ending:
        raise ValueError("--start must precede --end.")
    return beginning, ending


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Score original English Apple Chat responses with Microsoft-Decision-1."
    )
    command.add_argument("--lookback-hours", type=float, default=None)
    command.add_argument("--start")
    command.add_argument("--end")
    command.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    command.add_argument("--timeout-seconds", type=int, default=600)
    return command


def main(argv: list[str] | None = None) -> int:
    started = utc_now()
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    report = Reports(args.output_dir, utc_text(started))
    turns: list[ChatTurn] | None = None
    raw_rows: list[dict[str, Any]] | None = None
    fetched: int | None = None
    failure: str | None = None
    status: RunStatus = "technical_error"
    exit_code = 1

    def preserve(rows: list[dict[str, Any]]) -> None:
        nonlocal raw_rows
        raw_rows = rows
        write_json(report.technical_path / RAW_LOG_RECORDS_JSON, rows)

    try:
        start, end = time_window(started, args.start, args.end, args.lookback_hours)
        if args.timeout_seconds <= 0:
            raise ValueError("--timeout-seconds must be positive.")
        report.update(start=utc_text(start), end=utc_text(end), status="querying")
        settings = Settings.load()
        report.update(
            project_endpoint=settings.endpoint,
            judge_model=JUDGE_MODEL,
            judge_deployment=settings.judge_deployment,
            judge_model_version=JUDGE_MODEL_VERSION,
            judge_prompt_version=JUDGE_PROMPT_VERSION,
            workspace_id=settings.workspace_id,
            query_started_at=utc_text(utc_now()),
        )
        with credential() as auth, LogsQueryClient(auth, retry_total=2) as logs:
            with progress("Reading chat logs"):
                turns = fetch(logs, settings.workspace_id, start, end, preserve)
            fetched = len(turns)
            report.snapshot([turn.model_dump(mode="json") for turn in turns])
            report.update(
                raw_log_rows=len(raw_rows or []),
                duplicate_rows=len(raw_rows or []) - fetched,
                chat_prompt_versions=sorted({turn.prompt_version for turn in turns}),
                chat_deployments=sorted({turn.model_deployment for turn in turns}),
                chat_model_versions=sorted(
                    {turn.model_version for turn in turns if turn.model_version}
                ),
            )
            if not turns:
                status = "no_data"
            else:
                if (report.path / INPUT_JSONL).stat().st_size > 5_000_000:
                    raise ValueError(
                        "Inline dataset exceeds the 5 MB demo limit. Narrow the interval."
                    )
                with progress(f"Scoring responses ({fetched})"):
                    for result in evaluate(
                        settings.resource_endpoint,
                        settings.judge_deployment,
                        auth,
                        turns,
                        report.update,
                        report.append_raw,
                        args.timeout_seconds,
                    ):
                        report.append_result(result)
                status = "completed"
            exit_code = 0
    except (
        AzureError,
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        TypeError,
        OverflowError,
    ) as error:
        failure = f"{type(error).__name__}: {error}"
        LOG.error("Evaluation failed: %s", failure)
    except KeyboardInterrupt:
        failure = "Interrupted during Decision-1 evaluation. Completed per-turn results remain."
        LOG.error(failure)
        exit_code = 130
    finally:
        if not (report.path / INPUT_JSONL).exists():
            report.snapshot(snapshot_rows(raw_rows or []))
        if fetched is None and raw_rows is not None:
            fetched = len(raw_rows)
        summary = report.finish(status, turns, fetched, failure)
        print(render_console(summary))
        print(f"\nDetails: {(report.path / SUMMARY_MARKDOWN).resolve()}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
