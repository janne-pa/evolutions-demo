import logging
from unittest.mock import Mock

import pytest
from opentelemetry.sdk._logs import LoggerProvider

from demo.chat.telemetry import Telemetry


def test_turn_logs_never_use_trace_sampling(monkeypatch, turn):
    configure = Mock()
    monkeypatch.setattr("demo.chat.telemetry.configure_azure_monitor", configure)
    provider = Mock(spec=LoggerProvider)
    provider.force_flush.return_value = True
    monkeypatch.setattr("demo.chat.telemetry.get_logger_provider", lambda: provider)
    sink = Telemetry("test-connection-string", Mock())
    try:
        sink.logger = Mock()
        sink.emit(turn)
        assert configure.call_args.kwargs["enable_trace_based_sampling_for_logs"] is False
        assert configure.call_args.kwargs["sampling_ratio"] == 1
        attrs = sink.logger.info.call_args.kwargs["extra"]
        assert attrs["payload"] == turn.payload()
        assert attrs["turn_id"] == str(turn.turn_id)
    finally:
        sink.exporter_logger.removeHandler(sink.health)


def test_exporter_warning_stops_success(monkeypatch, turn):
    monkeypatch.setattr("demo.chat.telemetry.configure_azure_monitor", Mock())
    sink = Telemetry("test-connection-string", Mock())
    try:
        sink.health.emit(
            logging.LogRecord(
                "azure.monitor.opentelemetry.exporter",
                logging.WARNING,
                "",
                0,
                "Upload failed with 403",
                (),
                None,
            )
        )
        with pytest.raises(RuntimeError, match="export failed"):
            sink.emit(turn)
    finally:
        sink.exporter_logger.removeHandler(sink.health)
