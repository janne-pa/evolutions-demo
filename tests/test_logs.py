from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.monitor.query import LogsQueryStatus

from demo.contracts import sha256, utc_text
from demo.evaluation.logs import MAX_ROWS, fetch, query_text


def log_row(turn):
    return [turn.payload(), sha256(turn.payload()), str(turn.turn_id), turn.timestamp.isoformat()]


def client_for(rows):
    table = SimpleNamespace(
        columns=["payload", "payload_sha256", "turn_id", "turn_timestamp"], rows=rows
    )
    return Mock(
        query_workspace=Mock(
            return_value=SimpleNamespace(status=LogsQueryStatus.SUCCESS, tables=[table])
        )
    )


def test_half_open_interval_and_dedup(turn):
    start = turn.timestamp
    end = start + timedelta(hours=24)
    text = query_text(start, end)
    assert f">= datetime({utc_text(start)})" in text
    assert f"< datetime({utc_text(end)})" in text
    client = client_for([log_row(turn), log_row(turn)])
    preserve = Mock()
    assert fetch(client, "workspace", start, end, preserve) == [turn]
    preserve.assert_called_once()
    # Inclusive start accepted; exclusive end is defensively rejected.
    with pytest.raises(ValueError, match="outside"):
        fetch(client, "workspace", start - timedelta(hours=24), start, Mock())


def test_identical_query_window_on_repeated_calls(turn):
    client = client_for([log_row(turn)])
    start, end = turn.timestamp, turn.timestamp + timedelta(hours=24)
    fetch(client, "ws", start, end, Mock())
    fetch(client, "ws", start, end, Mock())
    assert client.query_workspace.call_args_list[0] == client.query_workspace.call_args_list[1]


def test_partial_is_error():
    client = Mock(
        query_workspace=Mock(
            return_value=SimpleNamespace(
                status=LogsQueryStatus.PARTIAL, partial_error="query truncated", partial_data=[]
            )
        )
    )
    with pytest.raises(RuntimeError, match="Partial"):
        from demo.contracts import utc_now

        now = utc_now()
        fetch(client, "ws", now - timedelta(hours=24), now, Mock())


def test_no_silent_truncation(turn):
    client = client_for([log_row(turn)] * (MAX_ROWS + 1))
    with pytest.raises(RuntimeError, match="exceeded"):
        fetch(client, "ws", turn.timestamp, turn.timestamp + timedelta(hours=1), Mock())


def test_conflicting_duplicates(turn):
    other = turn.model_copy(update={"response": "Different response"})
    client = client_for([log_row(turn), log_row(other)])
    with pytest.raises(ValueError, match="Conflicting"):
        fetch(client, "ws", turn.timestamp, turn.timestamp + timedelta(hours=1), Mock())


def test_hash_detects_truncated_payload(turn):
    row = log_row(turn)
    row[0] = row[0][:-1]
    with pytest.raises(ValueError, match="truncated"):
        fetch(client_for([row]), "ws", turn.timestamp, turn.timestamp + timedelta(hours=1), Mock())
