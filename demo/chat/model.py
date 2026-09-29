from dataclasses import dataclass

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from opentelemetry import trace

from demo.contracts import Message, Usage
from demo.settings import PACKAGE_DIR

CHAT_PROMPT_VERSION = "chat-en-v2"
CHAT_PROMPT = (PACKAGE_DIR / "prompts" / f"{CHAT_PROMPT_VERSION}.txt").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Answer:
    text: str
    model: str | None
    usage: Usage | None


class ChatModel:
    def __init__(self, client: OpenAI, deployment: str) -> None:
        self.client = client
        self.deployment = deployment

    def answer(self, query: str, history: list[Message]) -> Answer:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": CHAT_PROMPT},
        ]
        for message in history:
            if message.role == "user":
                messages.append({"role": "user", "content": message.content})
            else:
                messages.append({"role": "assistant", "content": message.content})
        messages.append({"role": "user", "content": query})
        with trace.get_tracer("evolutions.chat").start_as_current_span("foundry.chat"):
            result = self.client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                max_completion_tokens=450,
            )
        if len(result.choices) != 1:
            raise ValueError("The model response did not contain exactly one result.")
        choice = result.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            raise ValueError(f"Model response incomplete or blocked: {choice.finish_reason}.")
        usage = None
        if result.usage is not None:
            usage = Usage(
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
            )
        return Answer(choice.message.content, result.model, usage)
