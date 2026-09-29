const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

test("Enter uses form submission; Shift+Enter, composition and pending requests do not", async () => {
  const elements = new Map();
  function element() {
    return {
      value: "", textContent: "", handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
      append() {}, replaceChildren() {}, focus() {}, scrollIntoView() {},
    };
  }
  for (const id of ["messages", "error", "status", "send", "reset", "query", "chat-form"]) {
    elements.set(`#${id}`, element());
  }
  const query = elements.get("#query");
  const form = elements.get("#chat-form");
  let submissions = 0;
  let pendingSubmit;
  let finishChat;
  const calls = [];
  form.requestSubmit = () => {
    submissions++;
    pendingSubmit = form.handlers.submit({ preventDefault() {} });
  };
  const context = vm.createContext({
    document: {
      querySelector: (selector) => elements.get(selector),
      createElement: element,
    },
    TextEncoder,
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url === "/api/conversations") {
        return { ok: true, json: async () => ({ conversation_id: "conversation-1" }) };
      }
      return new Promise((resolve) => {
        finishChat = () => resolve({
          ok: true,
          json: async () => ({
            response: "Keep apples cool.", turn_index: 1, turn_id: "turn-1", trace_id: "trace-1",
          }),
        });
      });
    },
  });
  vm.runInContext(readFileSync(
    path.join(__dirname, "..", "demo", "chat", "static", "chat.js"), "utf8",
  ), context);
  function key(overrides = {}) {
    const event = {
      key: "Enter", shiftKey: false, isComposing: false, repeat: false,
      prevented: false, preventDefault() { this.prevented = true; }, ...overrides,
    };
    query.handlers.keydown(event);
    return event;
  }
  assert.equal(key().prevented, true);
  assert.equal(submissions, 0, "cannot send during conversation initialization");
  await new Promise(setImmediate);
  for (const override of [
    { shiftKey: true }, { isComposing: true }, { keyCode: 229 }, { key: "a" },
  ]) {
    assert.equal(key(override).prevented, false);
  }
  assert.equal(key({ repeat: true }).prevented, true);
  assert.equal(submissions, 0);
  query.value = "   ";
  key();
  await pendingSubmit;
  assert.equal(calls.length, 1, "existing empty-input validation is preserved");
  query.value = "How should I store apples?";
  key();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, "/api/chat");
  assert.equal(JSON.parse(calls[1].options.body).query, query.value);
  const before = submissions;
  key();
  assert.equal(submissions, before, "pending model request blocks duplicate sends");
  finishChat();
  await pendingSubmit;
  assert.equal(query.value, "");
  assert.equal(elements.get("#send").disabled, false);
  query.value = "Another apple question";
  const clicked = form.handlers.submit({ preventDefault() {} });
  assert.equal(calls.length, 3, "Send button retains the same submission handler");
  finishChat();
  await clicked;
});
