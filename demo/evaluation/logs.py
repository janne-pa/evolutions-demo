import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from azure.monitor.query import LogsQueryClient, LogsQueryStatus

from demo.contracts import ChatTurn, sha256, utc_text

MAX_ROWS = 1000


def query_text(start: datetime, end: datetime) -> str:
    return f"""
AppTraces
| where Properties.source_app == "evolutions-demo"
| where Properties.event_name == "chat_turn_completed"
| extend turn_time = todatetime(Properties.turn_timestamp)
| where isnull(turn_time) or
    (turn_time >= datetime({utc_text(start)}) and turn_time < datetime({utc_text(end)}))
| project payload=tostring(Properties.payload),
          payload_sha256=tostring(Properties.payload_sha256),
          turn_id=tostring(Properties.turn_id), turn_timestamp=tostring(Properties.turn_timestamp)
| take {MAX_ROWS + 1}
"""


def fetch(
    client: LogsQueryClient,
    workspace_id: str,
    start: datetime,
    end: datetime,
    preserve: Callable[[list[dict[str, Any]]], None],
) -> list[ChatTurn]:
    # Wider transport window covers ingestion delay; event timestamps define membership.
    result = client.query_workspace(
        workspace_id,
        query_text(start, end),
        timespan=(start - timedelta(days=1), end + timedelta(days=1)),
        server_timeout=120,
    )
    if result.status != LogsQueryStatus.SUCCESS:
        partial_tables = result.partial_data or []
        preserve(
            [
                dict(zip(table.columns, row, strict=True))
                for table in partial_tables
                for row in table.rows
            ]
        )
        raise RuntimeError(f"Partial log query: {result.partial_error}")
    if len(result.tables) != 1:
        raise RuntimeError("Log query did not return exactly one table.")
    table = result.tables[0]
    rows = [dict(zip(table.columns, row, strict=True)) for row in table.rows]
    preserve(rows)
    if len(rows) > MAX_ROWS:
        raise RuntimeError(f"Log query exceeded the {MAX_ROWS} row limit. Narrow the interval.")
    unique: dict[str, ChatTurn] = {}
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, str) or sha256(payload) != row["payload_sha256"]:
            raise ValueError("Log content is missing, changed or truncated.")
        turn = ChatTurn.model_validate_json(payload)
        if str(turn.turn_id) != row["turn_id"]:
            raise ValueError("Log turn_id does not match the payload.")
        if datetime.fromisoformat(row["turn_timestamp"]) != turn.timestamp:
            raise ValueError("Log timestamps do not match.")
        if not start <= turn.timestamp < end:
            raise ValueError("Log query returned a turn outside the requested interval.")
        key = str(turn.turn_id)
        if key in unique and unique[key] != turn:
            raise ValueError(f"Conflicting duplicates: {key}")
        unique[key] = turn
    return sorted(unique.values(), key=lambda turn: (turn.timestamp, str(turn.turn_id)))


def snapshot_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve parseable payloads before validating; retain broken records as raw evidence."""
    output = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            output.append({"invalid_log_record": row})
        else:
            output.append(payload if isinstance(payload, dict) else {"invalid_log_record": row})
    return output
