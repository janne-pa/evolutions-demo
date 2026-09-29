import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Protocol
from uuid import UUID, uuid4

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from demo.chat.model import CHAT_PROMPT_VERSION, Answer, ChatModel
from demo.chat.telemetry import Telemetry
from demo.contracts import (
    MAX_CONTEXT_BYTES,
    MAX_QUERY_BYTES,
    MAX_TURNS,
    ChatTurn,
    Message,
    json_text,
    utc_now,
)
from demo.settings import PACKAGE_DIR, Settings, credential

LOG = logging.getLogger(__name__)
NOTICE = (
    "Synthetic data only. Questions, responses and conversation context are sent to Azure. "
    "Logs appear with a delay; submission does not guarantee ingestion. "
    "Topic restrictions rely on model instructions and are not foolproof."
)


class Model(Protocol):
    def answer(self, query: str, history: list[Message]) -> Answer: ...


class TurnSink(Protocol):
    def emit(self, turn: ChatTurn) -> None: ...


@dataclass
class Runtime:
    model: Model
    telemetry: TurnSink
    deployment: str


@contextmanager
def azure_runtime() -> Iterator[Runtime]:
    settings = Settings.load(chat=True)
    with ExitStack() as stack:
        auth = stack.enter_context(credential())
        if not settings.connection_string:
            raise ValueError("Telemetry connection string is missing.")
        telemetry = Telemetry(settings.connection_string, auth)
        stack.callback(telemetry.close)
        project = stack.enter_context(AIProjectClient(endpoint=settings.endpoint, credential=auth))
        client = stack.enter_context(project.get_openai_client(max_retries=0, timeout=60))
        yield Runtime(
            ChatModel(client, settings.chat_deployment), telemetry, settings.chat_deployment
        )


@dataclass
class Conversation:
    history: list[Message] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    failed: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_BYTES)]


class ChatResponse(BaseModel):
    conversation_id: UUID
    turn_id: UUID
    trace_id: str
    turn_index: int
    response: str
    telemetry_status: str = "submitted_not_ingestion_confirmed"
    notice: str = NOTICE


def create_app(
    runtime_factory: Callable[[], AbstractContextManager[Runtime]] = azure_runtime,
) -> FastAPI:
    conversations: dict[UUID, Conversation] = {}
    sessions_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with runtime_factory() as runtime:
            app.state.runtime = runtime
            yield

    app = FastAPI(title="Apple Chat", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        port = request.scope["server"][1]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if origin and origin not in allowed:
            return JSONResponse({"detail": "Only the local user interface is allowed."}, 403)
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Use application/json content."}, 415)
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8192:
                    return JSONResponse({"detail": "Request is too large."}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(PACKAGE_DIR / "chat" / "static" / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ready", "notice": NOTICE}

    @app.post("/api/conversations")
    def new_conversation() -> dict[str, str]:
        with sessions_lock:
            expired = [
                key
                for key, value in conversations.items()
                if time.monotonic() - value.created > 7200 and not value.lock.locked()
            ]
            for key in expired:
                del conversations[key]
            if len(conversations) >= 128:
                raise HTTPException(429, "Too many conversations. Close an old conversation.")
            identifier = uuid4()
            conversations[identifier] = Conversation()
        return {"conversation_id": str(identifier), "notice": NOTICE}

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: UUID) -> None:
        with sessions_lock:
            conversation = conversations.get(conversation_id)
            if conversation is not None:
                if conversation.lock.locked():
                    raise HTTPException(409, "A request in this conversation is still running.")
                del conversations[conversation_id]

    @app.post("/api/chat")
    def chat(body: ChatRequest, request: Request) -> ChatResponse:
        if not body.query.strip() or len(body.query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise HTTPException(422, f"Question is empty or exceeds {MAX_QUERY_BYTES} UTF-8 bytes.")
        with sessions_lock:
            conversation = conversations.get(body.conversation_id)
            if conversation is None or time.monotonic() - conversation.created > 7200:
                raise HTTPException(410, "Conversation expired. Start a new conversation.")
            if not conversation.lock.acquire(blocking=False):
                raise HTTPException(409, "The previous response is still running.")
        try:
            index = len(conversation.history) // 2 + 1
            context_size = len(json_text([m.model_dump() for m in conversation.history]).encode())
            if conversation.failed:
                raise HTTPException(409, "The previous turn failed. Start a new conversation.")
            if index > MAX_TURNS or context_size > MAX_CONTEXT_BYTES:
                raise HTTPException(
                    409,
                    "Context limit reached. History was not truncated; start a new conversation.",
                )
            runtime: Runtime = request.app.state.runtime
            with trace.get_tracer("evolutions.chat").start_as_current_span("chat_turn") as span:
                started = time.monotonic()
                answer = runtime.model.answer(body.query, conversation.history.copy())
                turn = ChatTurn(
                    timestamp=utc_now(),
                    conversation_id=body.conversation_id,
                    turn_id=uuid4(),
                    trace_id=f"{span.get_span_context().trace_id:032x}",
                    turn_index=index,
                    query=body.query,
                    response=answer.text,
                    context=conversation.history.copy(),
                    prompt_version=CHAT_PROMPT_VERSION,
                    model_deployment=runtime.deployment,
                    model_version=answer.model,
                    duration_ms=(time.monotonic() - started) * 1000,
                    usage=answer.usage,
                )
                runtime.telemetry.emit(turn)
            conversation.history.extend(
                [
                    Message(role="user", content=body.query),
                    Message(role="assistant", content=answer.text),
                ]
            )
            return ChatResponse(
                conversation_id=body.conversation_id,
                turn_id=turn.turn_id,
                trace_id=turn.trace_id,
                turn_index=index,
                response=answer.text,
            )
        except (AzureError, OpenAIError, ValueError, RuntimeError, TimeoutError) as error:
            conversation.failed = True
            LOG.error("Chat turn failed (%s): %s", type(error).__name__, error)
            raise HTTPException(
                502,
                "Model call, response completeness check or telemetry failed. "
                "The turn was not confirmed successful. Check the server error and start again.",
            ) from error
        finally:
            conversation.lock.release()

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "chat" / "static"), name="static")
    FastAPIInstrumentor.instrument_app(app)
    return app
