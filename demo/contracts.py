import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Final = "1"
SOURCE_APP: Final = "evolutions-demo"
EVENT_NAME: Final = "chat_turn_completed"
MAX_QUERY_BYTES = 1000
MAX_RESPONSE_BYTES = 2000
MAX_CONTEXT_BYTES = 3200
MAX_PAYLOAD_BYTES = 7500
MAX_TURNS = 8


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must be explicitly in UTC.")
    return value.isoformat().replace("+00:00", "Z")


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = SCHEMA_VERSION
    event_name: Literal["chat_turn_completed"] = EVENT_NAME
    source_app: Literal["evolutions-demo"] = SOURCE_APP
    timestamp: datetime
    conversation_id: UUID
    turn_id: UUID
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    turn_index: int = Field(ge=1, le=MAX_TURNS)
    query: str = Field(min_length=1)
    response: str = Field(min_length=1)
    context: list[Message]
    prompt_version: str = Field(min_length=1)
    model_deployment: str = Field(min_length=1)
    model_version: str | None
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    usage: Usage | None
    status: Literal["completed"] = "completed"
    content_complete: Literal[True] = True

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        utc_text(value)
        return value

    @model_validator(mode="after")
    def complete_payload(self) -> "ChatTurn":
        for name, value, limit in (
            ("query", self.query, MAX_QUERY_BYTES),
            ("response", self.response, MAX_RESPONSE_BYTES),
            ("context", json_text([m.model_dump() for m in self.context]), MAX_CONTEXT_BYTES),
        ):
            if not value.strip() or len(value.encode("utf-8")) > limit:
                raise ValueError(f"{name}: empty or oversized content (limit: {limit} bytes).")
        if len(self.context) != 2 * (self.turn_index - 1):
            raise ValueError("Context does not match the turn index.")
        for index, message in enumerate(self.context):
            if message.role != ("user" if index % 2 == 0 else "assistant"):
                raise ValueError("Invalid context role order.")
        if len(self.model_dump_json().encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("Turn exceeds the safe telemetry field size.")
        return self

    def payload(self) -> str:
        return self.model_dump_json()
