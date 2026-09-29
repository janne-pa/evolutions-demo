"""Early live probe: model -> AppTraces ingestion -> custom dataset evaluation."""

import json
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from azure.ai.projects import AIProjectClient
from azure.monitor.query import LogsQueryClient
from opentelemetry import trace

from demo.chat.model import CHAT_PROMPT_VERSION, ChatModel
from demo.chat.telemetry import Telemetry
from demo.contracts import ChatTurn, utc_now
from demo.evaluation.foundry import evaluate
from demo.evaluation.logs import fetch
from demo.settings import Settings, credential


def main() -> None:
    # This live diagnostic creates one synthetic turn; it is not the normal batch eval CLI.
    settings = Settings.load(chat=True)
    # Keep each probe's original payload, ingestion evidence and remote run IDs together.
    output = Path("artifacts") / ("sdk-probe-" + utc_now().strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True, exist_ok=False)
    assert settings.connection_string
    with (
        credential() as auth,
        AIProjectClient(endpoint=settings.endpoint, credential=auth) as project,
        LogsQueryClient(auth) as logs,
    ):
        telemetry = Telemetry(settings.connection_string, auth)
        try:
            # Do not automatically repeat a model call after an ambiguous network failure.
            with project.get_openai_client(max_retries=0, timeout=60) as client:
                with trace.get_tracer("evolutions.probe").start_as_current_span(
                    "sdk_probe"
                ) as span:
                    start = time.monotonic()
                    query = "What is the best way to store apples at home?"
                    answer = ChatModel(client, settings.chat_deployment).answer(query, [])
                    turn = ChatTurn(
                        timestamp=utc_now(),
                        conversation_id=uuid4(),
                        turn_id=uuid4(),
                        trace_id=f"{span.get_span_context().trace_id:032x}",
                        turn_index=1,
                        query=query,
                        response=answer.text,
                        context=[],
                        prompt_version=CHAT_PROMPT_VERSION,
                        model_deployment=settings.chat_deployment,
                        model_version=answer.model,
                        duration_ms=(time.monotonic() - start) * 1000,
                        usage=answer.usage,
                    )
                    # Persist the original answer before export so a later failure retains evidence.
                    (output / "original.json").write_text(turn.payload(), encoding="utf-8")
                    telemetry.emit(turn)
                    print(f"Model and telemetry submitted: {turn.turn_id}", flush=True)
            # A successful telemetry flush is not an ingestion receipt. Poll actual workspace logs.
            # Only the read is retried; neither the model call nor telemetry emission is repeated.
            deadline = time.monotonic() + 600
            while True:
                turns = fetch(
                    logs,
                    settings.workspace_id,
                    turn.timestamp - timedelta(seconds=1),
                    turn.timestamp + timedelta(seconds=1),
                    lambda rows: (output / "ingestion.json").write_text(
                        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
                    ),
                )
                # Nearby turns can share the time window; match the exact ID and full payload.
                match = [item for item in turns if item.turn_id == turn.turn_id]
                if match:
                    if match != [turn]:
                        raise ValueError("Ingested turn differs from original.")
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Probe turn did not arrive in AppTraces within 10 minutes.")
                print("Waiting for actual AppTraces ingestion...", flush=True)
                time.sleep(15)
            print("AppTraces payload and hash verified.", flush=True)
            manifest: dict = {"turn_id": str(turn.turn_id)}

            def update(**values: object) -> None:
                # Save IDs as the eval progresses; the cloud run may outlive a local timeout.
                manifest.update(values)
                (output / "manifest.json").write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8"
                )
                print(values, flush=True)

            # Judge the ingested original response, not a newly generated model-target response.
            results = list(
                evaluate(
                    project,
                    settings.judge_deployment,
                    match,
                    update,
                    lambda raw: (output / "raw-result.json").write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                    ),
                )
            )
            (output / "results.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"Live SDK probe completed: {output}", flush=True)
        finally:
            # Release exporters even if model calls, ingestion checks or evaluation fail.
            telemetry.close()


if __name__ == "__main__":
    main()
