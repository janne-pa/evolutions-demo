"use strict";
const messages = document.querySelector("#messages");
const errorBox = document.querySelector("#error");
const statusBox = document.querySelector("#status");
const send = document.querySelector("#send");
const reset = document.querySelector("#reset");
const query = document.querySelector("#query");
const form = document.querySelector("#chat-form");
let conversationId = null;
let busy = false;

function setBusy(value) {
  busy = value;
  send.disabled = value || !conversationId;
  reset.disabled = value;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json" },
  });
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
  }
  return body;
}

function append(role, text, identifiers = "") {
  const article = document.createElement("article");
  article.className = role;
  const label = document.createElement("strong");
  label.textContent = role === "user" ? "You" : "Apple Chat";
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  article.append(label, paragraph);
  if (identifiers) {
    const details = document.createElement("small");
    details.textContent = identifiers;
    article.append(details);
  }
  messages.append(article);
  article.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function newConversation() {
  setBusy(true);
  errorBox.textContent = "";
  try {
    if (conversationId) await request(`/api/conversations/${conversationId}`, { method: "DELETE" });
    conversationId = null;
    const result = await request("/api/conversations", { method: "POST", body: "{}" });
    conversationId = result.conversation_id;
    messages.replaceChildren();
    query.value = "";
    statusBox.textContent = `Conversation ${conversationId}`;
    query.focus();
  } catch (error) {
    errorBox.textContent = `Could not open a conversation: ${error.message}`;
  } finally {
    setBusy(false);
  }
}

query.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (!event.repeat && !busy && conversationId) form.requestSubmit();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !conversationId) return;
  const text = query.value;
  errorBox.textContent = "";
  if (!text.trim() || new TextEncoder().encode(text).length > 1000) {
    errorBox.textContent = "Question is empty or exceeds 1,000 UTF-8 bytes.";
    return;
  }
  setBusy(true);
  statusBox.textContent = "Waiting for the model response and telemetry submission…";
  append("user", text);
  try {
    const result = await request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ conversation_id: conversationId, query: text }),
    });
    append("assistant", result.response, `Turn ${result.turn_index} · turn_id ${result.turn_id} · trace_id ${result.trace_id}`);
    query.value = "";
    statusBox.textContent = "Response complete. Telemetry submitted; Azure ingestion not yet confirmed.";
  } catch (error) {
    errorBox.textContent = error.message;
    statusBox.textContent = "Turn failed. The response was not marked successful.";
  } finally {
    setBusy(false);
  }
});
reset.addEventListener("click", newConversation);
newConversation();
