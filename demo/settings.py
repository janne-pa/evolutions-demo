import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing setting: {name}. See .env.example.")
    return value


@dataclass(frozen=True)
class Settings:
    endpoint: str
    chat_deployment: str
    judge_deployment: str
    workspace_id: str
    connection_string: str | None

    @classmethod
    def load(cls, *, chat: bool = False) -> "Settings":
        load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
        endpoint = required("AZURE_AI_PROJECT_ENDPOINT")
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".services.ai.azure.com")
            or not parsed.path.startswith("/api/projects/")
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("AZURE_AI_PROJECT_ENDPOINT must be a Foundry project HTTPS URL.")
        chat_name = required("CHAT_DEPLOYMENT") if chat else os.environ.get("CHAT_DEPLOYMENT", "")
        judge_name = required("JUDGE_DEPLOYMENT")
        if chat_name == judge_name:
            raise ValueError("Chat and judge require separate deployments.")
        return cls(
            endpoint,
            chat_name,
            judge_name,
            required("LOG_ANALYTICS_WORKSPACE_ID"),
            required("APPLICATIONINSIGHTS_CONNECTION_STRING") if chat else None,
        )


def credential() -> DefaultAzureCredential:
    os.environ.setdefault("AZURE_TOKEN_CREDENTIALS", "AzureCliCredential")
    return DefaultAzureCredential(require_envvar=True)
