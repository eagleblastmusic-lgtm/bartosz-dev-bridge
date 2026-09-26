"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const [mode, adapterPath] = process.argv.slice(2);
const conversationId = "abcdef12-3456-4789-abcd-abcdef123456";
const otherConversationId = "fedcba98-7654-4321-8765-abcdef123456";
const launchId = "11111111-1111-4111-8111-111111111111";
const claimId = "22222222-2222-4222-8222-222222222222";
const tabId = "33333333-3333-4333-8333-333333333333";
const bindingId = "binding-current";
const projectId = "project-current";
const taskId = "P3-03";
const prompt = "Canonical P3-03 prompt\r\nwith exact multiline content";

class TextNode {
  constructor(value = "") {
    this.nodeType = 3;
    this.nodeValue = String(value);
    this.parentElement = null;
    this.parentNode = null;
  }
  get textContent() { return this.nodeValue; }
  set textContent(value) { this.nodeValue = String(value); }
}

class Element {
  constructor(tagName = "div") {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.childNodes = [];
    this.parentElement = null;
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this.style = {};
    this.listeners = {};
    this.disabled = false;
    this.value = "";
    this.id = "";
    this._text = "";
  }
  get isConnected() { return Boolean(this.parentElement); }
  get textContent() {
    return this.childNodes.length ? this.childNodes.map((node) => node.textContent || "").join("") : this._text;
  }
  set textContent(value) {
    this.children = [];
    this.childNodes = [];
    this._text = String(value);
  }
  get innerText() { return this.textContent; }
  set innerText(value) { this.textContent = value; }
  getBoundingClientRect() { return { width: 600, height: 30 }; }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name === "id") this.id = text;
    if (name === "class") this.className = text;
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[key] = text;
    }
  }
  append(...nodes) {
    for (const node of nodes) {
      this.childNodes.push(node);
      if (node.nodeType === 1) this.children.push(node);
      node.parentElement = this;
      node.parentNode = this;
    }
  }
  appendChild(node) { this.append(node); return node; }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.childNodes = this.parentElement.childNodes.filter((node) => node !== this);
    this.parentElement.children = this.parentElement.children.filter((node) => node !== this);
    this.parentElement = null;
    this.parentNode = null;
  }
  insertAdjacentElement(_position, element) { this.append(element); }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  focus() {}
  dispatchEvent() { return true; }
  click() { if (typeof this.onClick === "function") this.onClick(); }
  matches(selector) {
    return selector.split(",").some((part) => matchesSimple(this, part.trim()));
  }
  closest(selector) {
    let current = this;
    while (current) {
      if (selector.split(",").some((part) => matchesSimple(current, part.trim()))) return current;
      current = current.parentElement;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => {
      for (const child of node.childNodes || []) {
        if (child.nodeType === 1) {
          if (selector.split(",").some((part) => matchesSimple(child, part.trim()))) found.push(child);
          visit(child);
        }
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function matchesSimple(element, selector) {
  if (!element || element.nodeType !== 1) return false;
  if (selector === "*") return true;
  if (selector === "pre code") return element.tagName === "CODE" && element.parentElement?.tagName === "PRE";
  if (selector.startsWith("#")) return element.id === selector.slice(1);
  if (selector.startsWith(".")) return String(element.className || "").split(/\s+/).includes(selector.slice(1));
  const roleMatch = selector.match(/^\[data-message-author-role=['"]([^'"]+)['"]\]$/);
  if (roleMatch) return element.getAttribute("data-message-author-role") === roleMatch[1];
  const attrMatch = selector.match(/^\[([^=\]]+)=['"]([^'"]+)['"]\]$/);
  if (attrMatch) return element.getAttribute(attrMatch[1]) === attrMatch[2];
  if (selector === "button, [role='button']" || selector === "button, [role='button'], [role='group'], [role='toolbar'], textarea, input, select, [contenteditable='true'], .bdb-vnext-project-launch-status, .bdb-vnext-project-execution-panel") {
    return element.tagName === "BUTTON" || element.getAttribute("role") === "button";
  }
  if (/^[a-z][a-z0-9-]*$/i.test(selector)) return element.tagName.toLowerCase() === selector.toLowerCase();
  return false;
}

class Composer extends Element {
  constructor() { super("textarea"); this.id = "prompt-textarea"; }
}

function resultPayload(overrides = {}) {
  return {
    schema: "bdb-project-execution-submission-v1",
    project_id: projectId,
    plan_version: "1",
    task_id: taskId,
    execution_binding_id: bindingId,
    correlation_id: "correlation-current",
    command_id: "command-current",
    repo_alias: "premium-calculator",
    head_before: "a".repeat(40),
    head_after: "b".repeat(40),
    execution_status: "PASS",
    validation_status: "PASS",
    promotion_status: "NOT_RUN",
    result_summary: "complete canonical result",
    evidence_refs: [],
    criteria: [],
    ...overrides
  };
}

function assistantMessage(root, value, shape = "interactive") {
  const owner = new Element("article");
  owner.setAttribute("data-message-author-role", "assistant");
  if (shape === "legacy") {
    const pre = new Element("pre");
    const code = new Element("code");
    code.append(new TextNode(value));
    pre.append(code);
    owner.append(pre);
  } else if (shape === "prose") {
    owner.append(new TextNode("Here is the result: "));
    const pre = new Element("pre");
    const code = new Element("code");
    code.append(new TextNode(value));
    pre.append(code);
    owner.append(pre);
  } else {
    const codeContainer = new Element("div");
    codeContainer.setAttribute("data-language", "json");
    const split = Math.floor(value.length / 2);
    const first = new Element("span");
    const second = new Element("span");
    first.append(new TextNode(value.slice(0, split)));
    second.append(new TextNode(value.slice(split)));
    codeContainer.append(first, second);
    const copy = new Element("button");
    copy.setAttribute("aria-label", "Copy code");
    copy.append(new TextNode("Copy"));
    codeContainer.append(copy);
    owner.append(codeContainer);
  }
  root.append(owner);
  return owner;
}

const localStore = {};
const runtimeMessages = [];
const nativeSubmissions = [];
let sweepCallback = null;
let mutationCallback = null;
let sendClicks = 0;
const composer = new Composer();
const documentElement = new Element("html");
documentElement.dataset = {};
documentElement.append(composer);

const activeBindingId = mode === "wrong-binding" ? "binding-another" : bindingId;
const activeConversation = mode === "wrong-conversation" ? otherConversationId : conversationId;
const canonicalStatus = {
  status: "project_execution_status",
  current_binding_id: activeBindingId,
  current_task_id: taskId,
  binding: {
    project_id: projectId,
    execution_binding_id: activeBindingId,
    task_id: taskId,
    launch_id: launchId,
    conversation_id: activeConversation,
    status: "ACTIVE",
    superseded: false,
    plan_version: "1",
    correlation_id: "correlation-current",
    command_id: "command-current",
    repo_alias: "premium-calculator",
    expected_repo_head_before: "a".repeat(40)
  },
  milestone_auto: { status: "RUNNABLE", current_task_id: taskId, milestone_run_id: "milestone-run-1" },
  launch_handoff: { status: "PENDING" },
  launch_outbox_status: "PUBLISHED"
};
const launch = {
  schema: "bdb-project-launch-v1",
  launch_id: launchId,
  repo_alias: "premium-calculator",
  prompt,
  auto_send: true,
  project_id: projectId,
  plan_version: "1",
  task_id: taskId,
  execution_binding_id: bindingId,
  correlation_id: "correlation-current",
  command_id: "command-current",
  expected_repo_head_before: "a".repeat(40),
  created_at: "2026-09-26T00:00:00Z",
  expires_at: "2999-09-26T00:00:00Z"
};

const userMessages = [];
let userOwner;
if (mode.startsWith("proof-") || mode === "recovery-collapsed" || mode === "recovery-uncertain" || mode === "combined") {
  userOwner = new Element("article");
  userOwner.setAttribute("data-message-author-role", "user");
  if (mode === "proof-attribute" || mode === "recovery-collapsed" || mode === "combined") {
    userOwner.setAttribute("data-message-content", prompt.replace(/\r\n/g, "\n"));
    const truncated = new Element("div");
    truncated.append(new TextNode("Canonical P3-03 prompt"));
    const show = new Element("button");
    show.append(new TextNode("Pokaż więcej"));
    userOwner.append(truncated, show);
    if (mode === "proof-attribute") {
      const bdbPanel = new Element("div");
      bdbPanel.className = "bdb-vnext-project-execution-panel";
      bdbPanel.append(new TextNode("BDB panel must not be prompt evidence"));
      userOwner.append(bdbPanel);
    }
  } else if (mode === "proof-show-more") {
    const body = new Element("div");
    body.append(new TextNode(prompt.replace(/\r\n/g, "\n")));
    const show = new Element("button");
    show.append(new TextNode("Pokaż więcej"));
    userOwner.append(body, show);
  } else if (mode === "proof-hidden-body") {
    const hiddenBody = new Element("div");
    hiddenBody.style.visibility = "hidden";
    hiddenBody.append(new TextNode(prompt.replace(/\r\n/g, "\n")));
    userOwner.append(hiddenBody);
  } else if (mode === "proof-altered") {
    userOwner.append(new TextNode(prompt.replace("exact", "changed")));
  } else if (mode === "proof-prefix") {
    userOwner.append(new TextNode(prompt.slice(0, 18)));
  } else {
    userOwner.append(new TextNode(prompt.replace(/\r\n/g, "\n")));
  }
  userMessages.push(userOwner);
  documentElement.append(userOwner);
}

if (["legacy", "interactive", "initial-scan", "active", "rerender", "wrong-binding", "wrong-conversation", "combined"].includes(mode)) {
  assistantMessage(documentElement, JSON.stringify(resultPayload()), mode === "legacy" ? "legacy" : "interactive");
}
if (mode === "partial") {
  assistantMessage(documentElement, '{"schema":"bdb-project-execution-submission-v1","project_id":"partial"', "interactive");
}
if (mode === "prose-json") assistantMessage(documentElement, JSON.stringify(resultPayload()), "prose");
if (mode === "malformed") assistantMessage(documentElement, '{"schema":"bdb-project-execution-submission-v1",}', "interactive");
if (mode === "wrong-schema") assistantMessage(documentElement, JSON.stringify(resultPayload({ schema: "bdb-project-execution-submission-v2" })), "interactive");
if (mode === "assistant-prose") assistantMessage(documentElement, "The bdb-project-execution-submission-v1 schema is used for results.", "interactive");
if (mode === "oversized") assistantMessage(documentElement, JSON.stringify(resultPayload({ result_summary: "x".repeat(256 * 1024) })), "interactive");
if (mode === "unrelated-page-text") {
  const unrelated = new Element("div");
  unrelated.append(new TextNode(JSON.stringify(resultPayload())));
  documentElement.append(unrelated);
}

const pageConversationId = mode === "proof-wrong-conversation" ? otherConversationId : conversationId;
const document = {
  visibilityState: "visible",
  documentElement,
  activeElement: composer,
  querySelectorAll(selector) {
    if (selector === "[data-message-author-role='user']") return userMessages;
    if (selector === "#prompt-textarea") return [composer];
    return documentElement.querySelectorAll(selector);
  },
  querySelector(selector) { return documentElement.querySelector(selector); },
  createElement(tag) { return new Element(tag); },
  execCommand() { return false; }
};

const persistedState = ["recovery-collapsed", "recovery-uncertain", "combined"].includes(mode);
if (persistedState) {
  localStore.bdbVnextProjectLaunchBindingsV1 = {
    [launchId]: {
      launch_id: launchId,
      conversation_id: conversationId,
      tab_instance_id: tabId,
      claim_id: claimId,
      project_id: projectId,
      task_id: taskId,
      execution_binding_id: bindingId,
      auto_send: true,
      state: "SEND_ATTEMPTED",
      send_baseline_count: 0,
      send_attempt_token: "attempt-token-1",
      updated_at: Date.now()
    }
  };
}

const context = {
  console,
  HTMLElement: Element,
  HTMLTextAreaElement: Composer,
  HTMLInputElement: class extends Element {},
  InputEvent: class {},
  Event: class {},
  TextEncoder,
  Set,
  Map,
  URL,
  crypto: { randomUUID: () => tabId },
  sessionStorage: { getItem: () => tabId, setItem() {} },
  window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
  location: { protocol: "https:", hostname: "chatgpt.com", pathname: `/c/${pageConversationId}` },
  document,
  MutationObserver: class {
    constructor(callback) { mutationCallback = callback; }
    observe() {}
  },
  setTimeout,
  clearTimeout,
  setInterval(callback, interval) {
    if (interval === 750) sweepCallback = callback;
    return { unref() {} };
  },
  chrome: {
    storage: { local: {
      async get(key) { return Object.prototype.hasOwnProperty.call(localStore, key) ? { [key]: localStore[key] } : {}; },
      async set(values) { Object.assign(localStore, values); }
    } },
    runtime: {
      onMessage: { addListener() {} },
      async sendMessage(message) {
        runtimeMessages.push(message);
        if (message.type === "bdb-vnext-project-launch-peek") return { ok: true, response: { status: "empty" } };
        if (message.type === "bdb-vnext-project-execution-status") return { ok: true, response: canonicalStatus };
        if (message.type === "bdb-vnext-project-launch-claim") return { ok: true, response: { status: "claimed", launch } };
        if (message.type === "bdb-vnext-project-launch-ack") return { ok: true, response: { status: "acknowledged" } };
        if (message.type === "bdb-vnext-project-execution-submit") {
          nativeSubmissions.push(message);
          return { ok: true, receipt: {
            accepted: true,
            result_status: "PASS",
            task_status: "completed",
            task_id: taskId,
            replayed: false,
            milestone_status: "MILESTONE_COMPLETED"
          } };
        }
        return { ok: false, error: "unexpected fixture message" };
      }
    }
  }
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(adapterPath, "utf8"), context, { filename: adapterPath });

function panels(owner = null) {
  const root = owner || documentElement;
  return root.querySelectorAll(".bdb-vnext-project-execution-panel");
}

function exactSendProof() {
  const expectedConversation = conversationId;
  if (mode === "proof-nonempty") composer.value = "draft still present";
  return context.projectSendEffectObserved(prompt, mode === "proof-old-identical" ? 1 : 0, expectedConversation);
}

async function main() {
  if (mode.startsWith("proof-")) {
    const expected = !["proof-altered", "proof-prefix", "proof-wrong-conversation", "proof-nonempty", "proof-old-identical"].includes(mode);
    assert.equal(exactSendProof(), expected);
    return;
  }
  if (mode === "recovery-collapsed" || mode === "recovery-uncertain") {
    if (mode === "recovery-uncertain") userMessages.splice(0, userMessages.length);
    const recovery = await context.projectHandleLaunch(launch, { automatic: true });
    assert.equal(sendClicks, 0, "SEND_ATTEMPTED recovery must never click Send again");
    if (mode === "recovery-collapsed") {
      assert.equal(recovery.ok, true);
      assert.equal(localStore.bdbVnextProjectLaunchBindingsV1[launchId].state, "ACKED");
      assert.equal(runtimeMessages.filter((item) => item.type === "bdb-vnext-project-launch-ack").length, 1);
    } else {
      assert.equal(recovery.code, "project_auto_send_uncertain");
      assert.equal(runtimeMessages.filter((item) => item.type === "bdb-vnext-project-launch-ack").length, 0);
    }
    return;
  }
  if (mode === "sweep") {
    const owner = assistantMessage(documentElement, JSON.stringify(resultPayload()), "interactive");
    assert.equal(panels(owner).length, 0);
    assert.equal(typeof sweepCallback, "function");
    sweepCallback();
    assert.equal(panels(owner).length, 1, "periodic bounded sweep discovers an existing result");
    return;
  }
  if (mode === "rerender") {
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(nativeSubmissions.length, 1);
    const second = assistantMessage(documentElement, JSON.stringify(resultPayload()), "interactive");
    context.scan(second);
    sweepCallback();
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(nativeSubmissions.length, 1, "same semantic result remains at-most-once after rerender");
    return;
  }
  if (["legacy", "interactive", "initial-scan", "active", "wrong-binding", "wrong-conversation", "combined"].includes(mode)) {
    if (mode === "combined") {
      assert.equal(panels().length, 1, "initial page scan finds the already-existing assistant result");
      const recovery = await context.projectHandleLaunch(launch, { automatic: true });
      assert.equal(recovery.ok, true);
      assert.equal(sendClicks, 0, "combined failure recovers without another Send");
      await new Promise((resolve) => setTimeout(resolve, 10));
      assert.equal(nativeSubmissions.length, 1, "existing result uses normal canonical execution submit transport");
      assert.equal(nativeSubmissions[0].conversation_id, conversationId);
      assert.equal(nativeSubmissions[0].result.execution_binding_id, bindingId);
      assert.equal(runtimeMessages.filter((item) => item.type === "bdb-vnext-project-launch-ack").length, 1);
      return;
    }
    if (mode === "wrong-binding" || mode === "wrong-conversation") {
      await new Promise((resolve) => setTimeout(resolve, 10));
      assert.equal(panels().length, 1, "canonical result remains visible for manual review");
      assert.equal(nativeSubmissions.length, 0, "automatic gate rejects a mismatched binding or conversation");
      return;
    }
    assert.equal(panels().length, 1, "initial scan discovers the already-rendered canonical result");
    if (mode === "legacy" || mode === "interactive" || mode === "initial-scan" || mode === "active") {
      await new Promise((resolve) => setTimeout(resolve, 10));
      assert.equal(nativeSubmissions.length, 1);
    }
    return;
  }
  if (["partial", "prose-json", "malformed", "wrong-schema", "oversized", "assistant-prose", "unrelated-page-text"].includes(mode)) {
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(panels().length, 0, "noncanonical, partial, prose-mixed, or unrelated text is not decorated");
    assert.equal(nativeSubmissions.length, 0);
    return;
  }
  throw new Error(`unsupported mode: ${mode}`);
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
