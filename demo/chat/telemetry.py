import logging
import threading

from azure.core.credentials import TokenCredential
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from demo.contracts import ChatTurn, sha256


class ExportHealth(logging.Handler):
    """Surface exporter failures without confusing a flush with an ingestion receipt."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.failed = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.failed.set()


class Telemetry:
    def __init__(self, connection_string: str, credential: TokenCredential) -> None:
        self.health = ExportHealth()
        self.exporter_logger = logging.getLogger("azure.monitor.opentelemetry.exporter")
        self.exporter_logger.addHandler(self.health)
        configure_azure_monitor(
            connection_string=connection_string,
            credential=credential,
            logger_name="evolutions.turns",
            sampling_ratio=1.0,
            enable_trace_based_sampling_for_logs=False,
            enable_live_metrics=False,
            disable_offline_storage=True,
            instrumentation_options={"fastapi": {"enabled": False}},
            resource=Resource.create({"service.name": "evolutions-demo"}),
        )
        self.logger = logging.getLogger("evolutions.turns")
        self.logger.setLevel(logging.INFO)

    def emit(self, turn: ChatTurn) -> None:
        if self.health.failed.is_set():
            raise RuntimeError("Telemetry export failed. Fix the issue and restart.")
        payload = turn.payload()
        self.logger.info(
            "chat_turn_completed",
            extra={
                "event_name": turn.event_name,
                "source_app": turn.source_app,
                "schema_version": turn.schema_version,
                "turn_id": str(turn.turn_id),
                "turn_timestamp": turn.timestamp.isoformat(),
                "payload": payload,
                "payload_sha256": sha256(payload),
            },
        )
        provider = get_logger_provider()
        if not isinstance(provider, LoggerProvider) or not provider.force_flush(10_000):
            raise RuntimeError("Telemetry flush did not complete; storage was not confirmed.")
        if self.health.failed.is_set():
            raise RuntimeError("Telemetry submission failed; response is not marked successful.")

    def close(self) -> None:
        provider = get_logger_provider()
        if isinstance(provider, LoggerProvider):
            provider.force_flush(10_000)
            provider.shutdown()
        tracer_provider = trace.get_tracer_provider()
        if isinstance(tracer_provider, TracerProvider):
            tracer_provider.force_flush(10_000)
            tracer_provider.shutdown()
        self.exporter_logger.removeHandler(self.health)
