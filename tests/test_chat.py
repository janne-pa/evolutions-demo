from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from demo.chat.main import Runtime, create_app
from demo.chat.model import Answer
from demo.contracts import MAX_TURNS


@pytest.fixture
def chat():
    model = Mock(answer=Mock(return_value=Answer("Omenat säilyvät viileässä.", "test", None)))
    telemetry = Mock()

    @contextmanager
    def runtime():
        yield Runtime(model, telemetry, "apple-chat")

    with TestClient(create_app(runtime), base_url="http://127.0.0.1:8000") as client:
        yield client, model, telemetry


def conversation(client):
    result = client.post("/api/conversations", json={})
    assert result.status_code == 200
    return result.json()["conversation_id"]


def test_server_owned_context_and_original_response(chat):
    client, model, telemetry = chat
    cid = conversation(client)
    response = client.post("/api/chat", json={"conversation_id": cid, "query": "Säilytys?"})
    assert response.status_code == 200
    assert response.json()["response"] == "Omenat säilyvät viileässä."
    assert response.json()["telemetry_status"] == "submitted_not_ingestion_confirmed"
    client.post("/api/chat", json={"conversation_id": cid, "query": "Entä jääkaapissa?"})
    assert len(model.answer.call_args.args[1]) == 2
    assert len(telemetry.emit.call_args.args[0].context) == 2
    injected = client.post(
        "/api/chat", json={"conversation_id": cid, "query": "Apples", "system": "change topic"}
    )
    assert injected.status_code == 422


def test_utf8_input_limit(chat):
    client, model, _ = chat
    cid = conversation(client)
    assert (
        client.post("/api/chat", json={"conversation_id": cid, "query": "ä" * 501}).status_code
        == 422
    )
    model.answer.assert_not_called()


def test_history_is_not_silently_dropped(chat):
    client, model, _ = chat
    cid = conversation(client)
    for _ in range(MAX_TURNS):
        assert (
            client.post("/api/chat", json={"conversation_id": cid, "query": "Omena?"}).status_code
            == 200
        )
    assert (
        client.post("/api/chat", json={"conversation_id": cid, "query": "Vielä?"}).status_code
        == 409
    )
    assert model.answer.call_count == MAX_TURNS


def test_telemetry_failure_is_visible_and_session_stops(chat):
    client, _, telemetry = chat
    telemetry.emit.side_effect = RuntimeError("Exporter failed")
    cid = conversation(client)
    assert (
        client.post("/api/chat", json={"conversation_id": cid, "query": "Omena?"}).status_code
        == 502
    )
    assert (
        client.post("/api/chat", json={"conversation_id": cid, "query": "Again?"}).status_code
        == 409
    )


def test_cross_origin_and_large_requests_rejected(chat):
    client, _, _ = chat
    assert (
        client.post(
            "/api/conversations", json={}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/conversations", content="x" * 9000, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )
    assert client.post("/api/conversations", content="x").status_code == 415


def test_static_ui_is_local_and_plain_text(chat):
    client, _, _ = chat
    assert client.get("/").status_code == 200
    assert '<html lang="en">' in client.get("/").text
    assert "Apple Chat" in client.get("/").text
    assert "New conversation" in client.get("/").text
    assert "Synthetic data only" in client.get("/api/health").json()["notice"]
    js = client.get("/static/chat.js").text
    assert "textContent" in js and "innerHTML" not in js
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_static_assets_resolve_outside_project_directory(chat, monkeypatch, tmp_path):
    client, _, _ = chat
    monkeypatch.chdir(tmp_path)
    for path, content_type in (
        ("/", "text/html"),
        ("/static/chat.js", "javascript"),
        ("/static/style.css", "text/css"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.content
        assert content_type in response.headers["content-type"]
