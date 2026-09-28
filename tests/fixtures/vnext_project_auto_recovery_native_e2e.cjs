"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const vm = require("node:vm");

const [adapterPath, workerPath, bridgePath, runtimeRoot, pythonExe, repoRoot] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const trace = { nativeRequests: [], sends: [] };
const localStorage = {
  bdbVnextProjectLaunchBindingsV1: {
    [input.launch.launch_id]: {
      schema: "bdb-vnext-project-launch-binding-v1",
      launch_id: input.launch.launch_id,
      conversation_id: input.conversation_id,
      tab_instance_id: "4b8f08b0-4c25-47c2-a19c-c476b58a8862",
      claim_id: "b2e5f1e9-9dd7-4d30-8916-572b1a3d733c",
      repo_alias: input.launch.repo_alias,
      project_id: input.launch.project_id,
      plan_version: String(input.launch.plan_version),
      task_id: input.launch.task_id,
      execution_binding_id: input.launch.execution_binding_id,
      correlation_id: input.launch.correlation_id,
      command_id: input.launch.command_id,
      expected_repo_head_before: input.launch.expected_repo_head_before,
      auto_send: true,
      state: "SEND_ATTEMPTED",
      send_baseline_count: 0,
      send_attempt_token: "historical-send-attempt-token",
      updated_at: Date.now()
    }
  }
};

const nativeActionFor = new Map([
  ["bdb-vnext-project-launch-peek", "project_launch_peek"],
  ["bdb-vnext-project-launch-claim", "project_launch_claim"],
  ["bdb-vnext-project-launch-ack", "project_launch_ack"],
  ["bdb-vnext-project-execution-status", "project_execution_status"],
  ["bdb-vnext-project-execution-submit", "project_execution_submit"]
]);
let nativeRequestNumber = 0;

function callCanonicalNative(browserMessage) {
  const action = nativeActionFor.get(browserMessage.type);
  assert.ok(action, "unexpected Browser message: " + browserMessage.type);
  const extra = {};
  if (action === "project_launch_claim") {
    Object.assign(extra, {
      launch_id: browserMessage.launch_id,
      claim_id: browserMessage.claim_id,
      conversation_id: browserMessage.conversation_id
    });
  } else if (action === "project_launch_ack") {
    Object.assign(extra, {
      launch_id: browserMessage.launch_id,
      claim_id: browserMessage.claim_id,
      conversation_id: browserMessage.handoff?.conversation_id || browserMessage.conversation_id,
      handoff_status: browserMessage.handoff ? "SENT" : undefined,
      project_id: browserMessage.handoff?.project_id,
      execution_binding_id: browserMessage.handoff?.execution_binding_id
    });
  } else if (action === "project_execution_status") {
    Object.assign(extra, {
      project_id: browserMessage.project_id,
      execution_binding_id: browserMessage.execution_binding_id,
      conversation_id: browserMessage.conversation_id
    });
  } else if (action === "project_execution_submit") {
    Object.assign(extra, {
      result: browserMessage.result,
      conversation_id: browserMessage.conversation_id
    });
  }
  const request = {
    schema: "bdb-vnext-native-request-v1",
    request_id: "browser-e2e-" + (++nativeRequestNumber),
    action,
    protocol_generation: "bdb-vnext-protocol-v1",
    browser_extension_id: "mopnolkjddkmgojfjkenjobehhmmklll",
    ...Object.fromEntries(Object.entries(extra).filter(([, value]) => value !== undefined))
  };
  const child = childProcess.spawnSync(pythonExe, [bridgePath, runtimeRoot], {
    input: JSON.stringify(request),
    encoding: "utf8",
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: repoRoot },
    timeout: 15000
  });
  if (child.error) {
    trace.nativeRequests.push({ action, browser_type: browserMessage.type, request, response: { status: "bridge_error", error: String(child.error), stdout: child.stdout || "", stderr: child.stderr || "" } });
    throw child.error;
  }
  if (child.status !== 0) {
    trace.nativeRequests.push({ action, browser_type: browserMessage.type, request, response: { status: "bridge_error", exit_code: child.status, stdout: child.stdout || "", stderr: child.stderr || "" } });
    throw new Error("canonical Native bridge failed: " + (child.stderr || child.stdout));
  }
  const response = JSON.parse(child.stdout);
  trace.nativeRequests.push({ action, browser_type: browserMessage.type, request, response });
  if (response.status === "failed") {
    return { ok: false, error_code: response.error_code, error: response.error };
  }
  if (action === "project_execution_submit") {
    return { ok: response.status === "project_execution", receipt: response.receipt || null };
  }
  const expectedStatus = action === "project_launch_claim" ? "claimed"
    : action === "project_launch_ack" ? "acknowledged"
      : action === "project_launch_peek" ? ["project_launch", "empty"].includes(response.status)
        : action === "project_execution_status" ? "project_execution_status"
          : "";
  return { ok: Array.isArray(expectedStatus) ? expectedStatus.includes(response.status) : response.status === expectedStatus, response };
}

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.nodeValue = String(text);
    this.parentNode = null;
    this.parentElement = null;
  }
  get textContent() { return this.nodeValue; }
  set textContent(value) { this.nodeValue = String(value); }
}

function selectorMatch(element, selector) {
  if (!element || element.nodeType !== 1) return false;
  if (selector === "#prompt-textarea") return element.id === "prompt-textarea";
  if (selector === "[data-virtualized-turn-content]") return element.getAttribute("data-virtualized-turn-content") !== null;
  if (selector === "[data-message-author-role]") return element.getAttribute("data-message-author-role") !== null;
  if (selector === "[data-testid^='conversation-turn-']") return (element.getAttribute("data-testid") || "").startsWith("conversation-turn-");
  const roleMatch = selector.match(/^\[data-message-author-role='(user|assistant)'\]$/);
  if (roleMatch) return element.getAttribute("data-message-author-role") === roleMatch[1];
  if (selector === "button[data-testid='send-button']") return element.tagName === "BUTTON" && element.getAttribute("data-testid") === "send-button";
  if (selector === "button[aria-label='Send prompt']") return element.tagName === "BUTTON" && element.getAttribute("aria-label") === "Send prompt";
  if (selector === "button[aria-label*='Send' i]") return element.tagName === "BUTTON" && /send/i.test(element.getAttribute("aria-label") || "");
  if (selector === "button[type='submit']") return element.tagName === "BUTTON" && element.getAttribute("type") === "submit";
  if (selector === "pre code") {
    if (element.tagName !== "CODE") return false;
    for (let current = element.parentElement; current; current = current.parentElement) {
      if (current.tagName === "PRE") return true;
    }
    return false;
  }
  if (selector.startsWith(".") && !selector.includes(" ")) return element.className.split(/\s+/).includes(selector.slice(1));
  if (selector === "[contenteditable='true']" || selector === "[contenteditable='true'][role='textbox']") {
    return element.getAttribute("contenteditable") === "true" &&
      (selector !== "[contenteditable='true'][role='textbox']" || element.getAttribute("role") === "textbox");
  }
  if (selector.includes("input[type='file']") || selector.includes("[data-testid*='attachment']") ||
      selector.includes("[data-file-id]") || selector.includes("[aria-label*='attachment' i]") ||
      selector.includes("[aria-label*='file' i]")) return false;
  return false;
}

class HTMLElement {
  constructor(tagName = "div") {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.parentNode = null;
    this.parentElement = null;
    this.childNodes = [];
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.id = "";
    this.disabled = false;
    this._value = "";
    this._text = null;
    this.listeners = new Map();
  }
  get children() { return this.childNodes.filter((item) => item.nodeType === 1); }
  get textContent() {
    return this._text !== null ? this._text : this.childNodes.map((item) => item.textContent || "").join("");
  }
  set textContent(value) {
    this._text = String(value);
    this.childNodes = [];
  }
  get innerText() { return this.textContent; }
  set innerText(value) { this.textContent = value; }
  getAttribute(name) {
    if (name === "id") return this.id || null;
    if (name === "class") return this.className || null;
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name === "id") this.id = text;
    if (name === "class") this.className = text;
  }
  appendChild(child) {
    this._text = null;
    child.parentNode = this;
    child.parentElement = child.nodeType === 1 ? this : null;
    this.childNodes.push(child);
    return child;
  }
  append(...children) {
    for (const child of children) this.appendChild(child);
  }
  remove() {
    if (!this.parentNode) return;
    const parent = this.parentNode;
    parent.childNodes = parent.childNodes.filter((child) => child !== this);
    this.parentNode = null;
    this.parentElement = null;
  }
  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) || [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  }
  dispatchEvent() { return true; }
  focus() {}
  getBoundingClientRect() { return { width: 100, height: 24 }; }
  insertAdjacentElement(_position, element) {
    if (this.parentNode) this.parentNode.appendChild(element);
  }
  matches(selector) { return selectorMatch(this, selector); }
  closest(selector) {
    for (let current = this; current; current = current.parentElement) {
      if (selector === "form" && current.tagName === "FORM") return current;
      if (selectorMatch(current, selector.replace(/,\s*\[data-message-author-role='user'\]/, ""))) return current;
      if (selector.startsWith(".") && selectorMatch(current, selector)) return current;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => {
      for (const child of node.children || []) {
        if (selectorMatch(child, selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  click() {
    if (this.tagName === "BUTTON" && this.getAttribute("data-testid") === "send-button") {
      const prompt = composer.value;
      trace.sends.push(prompt);
      conversationRoot.appendChild(virtualizedTurn(prompt, "send-turn-" + trace.sends.length).wrapper);
      composer.value = "";
      return;
    }
    for (const callback of this.listeners.get("click") || []) void callback();
  }
}

class HTMLTextAreaElement extends HTMLElement {
  constructor() { super("textarea"); this._value = ""; }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
}
class HTMLInputElement extends HTMLElement {
  constructor() { super("input"); this._value = ""; }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
}
class FakeMutationObserver {
  constructor(callback) { this.callback = callback; }
  observe() {}
  disconnect() {}
}

const conversationRoot = new HTMLElement("main");
function virtualizedTurn(text, id) {
  const wrapper = new HTMLElement("div");
  wrapper.className = "group flex flex-col pb-2 pt-2";
  const content = new HTMLElement("div");
  content.className = "flex flex-col";
  content.setAttribute("data-virtualized-turn-content", "");
  content.setAttribute("data-turn-id", id);
  if (text) content.appendChild(new TextNode(text));
  wrapper.appendChild(content);
  return { wrapper, content };
}
const composer = new HTMLTextAreaElement();
composer.id = "prompt-textarea";
const form = new HTMLElement("form");
form.appendChild(composer);
const send = new HTMLElement("button");
send.setAttribute("data-testid", "send-button");
send.setAttribute("aria-label", "Send prompt");
form.appendChild(send);

if (input.dom_mode !== "ambiguous-turn") {
  conversationRoot.appendChild(virtualizedTurn(input.launch.prompt, "existing-user-turn-4").wrapper);
}
const assistantTurn = virtualizedTurn("", "assistant-result-turn-1");
const assistant = assistantTurn.content;
const virtualizedLayout = new HTMLElement("div");
virtualizedLayout.className = "[&_[data-virtualized-turn-content]]:[content-visibility:visible]";
const turnBody = new HTMLElement("div");
const messageBody = new HTMLElement("div");
messageBody.className = "flex flex-col gap-1.5";
const contentsA = new HTMLElement("div");
contentsA.className = "contents";
const contentsB = new HTMLElement("div");
contentsB.className = "contents";
const messageGroup = new HTMLElement("div");
messageGroup.className = "group flex flex-col pb-2 pt-2";
const markdownGroup = new HTMLElement("div");
markdownGroup.className = "group flex min-w-0 flex-col";
const markdownRoot = new HTMLElement("div");
markdownRoot.className = "MarkdownRoot-rZKhxa [&>*:first-child]:mt-0 [&>*:last-child]:mb-0";
const codeBlock = new HTMLElement("div");
codeBlock.className = "CodeBlock-zu1QM3";
const inlineCodePane = new HTMLElement("div");
inlineCodePane.className = "InlineCodePane-EYx4bd w-full overflow-x-hidden";
const scrollport = new HTMLElement("div");
scrollport.className = "chatgpt-code-scrollport flex w-full items-stretch overscroll-x-contain overflow-x-auto";
const pre = new HTMLElement("pre");
pre.className = "m-0 flex-1 cursor-text px-4 pb-3 font-mono text-size-code text-default md:px-5 ViewerContent-Cg8AgN pt-0 min-w-max whitespace-pre";
const code = new HTMLElement("code");
code.appendChild(new TextNode(JSON.stringify(input.result)));
pre.appendChild(code);
scrollport.appendChild(pre);
inlineCodePane.appendChild(scrollport);
codeBlock.appendChild(inlineCodePane);
markdownRoot.appendChild(codeBlock);
markdownGroup.appendChild(markdownRoot);
messageGroup.appendChild(markdownGroup);
contentsB.appendChild(messageGroup);
contentsA.appendChild(contentsB);
messageBody.appendChild(contentsA);
turnBody.appendChild(messageBody);
virtualizedLayout.appendChild(turnBody);
assistantTurn.content.appendChild(virtualizedLayout);
conversationRoot.appendChild(assistantTurn.wrapper);
conversationRoot.appendChild(form);

const runtimeListeners = [];
const sessionStorage = new Map();
const runtimeId = "mopnolkjddkmgojfjkenjobehhmmklll";

const runtime = {
  id: runtimeId,
  lastError: null,
  onMessage: { addListener(callback) { runtimeListeners.push(callback); } },
  onInstalled: { addListener() {} },
  onStartup: { addListener() {} },
  getURL(path) { return "chrome-extension://" + runtimeId + "/" + path; },
  sendMessage(message) {
    return new Promise((resolve) => {
      let settled = false;
      const sendResponse = (response) => {
        if (settled) return;
        settled = true;
        resolve(response);
      };
      for (const listener of runtimeListeners) {
        const keepAlive = listener(message, { id: runtimeId }, sendResponse);
        if (keepAlive === true || settled) return;
      }
      resolve({ ok: false, error: "unhandled Browser message " + message.type });
    });
  },
  sendNativeMessage(_host, message, callback) {
    try {
      const response = callCanonicalNative({
        type: message.action === "project_launch_peek" ? "bdb-vnext-project-launch-peek"
          : message.action === "project_launch_claim" ? "bdb-vnext-project-launch-claim"
            : message.action === "project_launch_ack" ? "bdb-vnext-project-launch-ack"
              : message.action === "project_execution_status" ? "bdb-vnext-project-execution-status"
                : "bdb-vnext-project-execution-submit",
        ...message,
        handoff: message.handoff_status === "SENT" ? {
          project_id: message.project_id,
          execution_binding_id: message.execution_binding_id,
          conversation_id: message.conversation_id
        } : undefined
      });
      callback(response.response || (response.ok ? {
        schema: "bdb-vnext-native-response-v1", status: "project_execution",
        request_id: message.request_id, generation_id: "bdb-vnext-g1",
        protocol_generation: "bdb-vnext-protocol-v1",
        native_host_name: "com.bartosz.dev_bridge.vnext",
        browser_extension_id: runtimeId,
        receipt: response.receipt
      } : {
        schema: "bdb-vnext-native-response-v1", status: "failed",
        request_id: message.request_id, generation_id: "bdb-vnext-g1",
        protocol_generation: "bdb-vnext-protocol-v1",
        native_host_name: "com.bartosz.dev_bridge.vnext",
        browser_extension_id: runtimeId,
        error_code: response.error_code || "native_failed",
        error: response.error || "canonical Native request failed"
      }));
    } catch (error) {
      callback({
        schema: "bdb-vnext-native-response-v1", status: "failed",
        request_id: message.request_id, generation_id: "bdb-vnext-g1",
        protocol_generation: "bdb-vnext-protocol-v1",
        native_host_name: "com.bartosz.dev_bridge.vnext",
        browser_extension_id: runtimeId,
        error_code: "native_bridge_failed",
        error: String(error)
      });
    }
  }
};

const document = {
  visibilityState: "visible",
  activeElement: null,
  hasFocus: () => true,
  documentElement: conversationRoot,
  querySelectorAll(selector) {
    if (selector === "#prompt-textarea") return [composer];
    return conversationRoot.querySelectorAll(selector);
  },
  querySelector(selector) {
    if (selector === "#prompt-textarea") return composer;
    return conversationRoot.querySelector(selector);
  },
  createElement(tagName) { return new HTMLElement(tagName); },
  execCommand() { return false; }
};

const context = {
  console,
  chrome: { runtime, storage: { local: {
    async get(key) { return { [key]: localStorage[key] }; },
    async set(values) { Object.assign(localStorage, values); }
  }}},
  HTMLElement,
  HTMLTextAreaElement,
  HTMLInputElement,
  InputEvent: class InputEvent { constructor(type) { this.type = type; } },
  Event: class Event { constructor(type) { this.type = type; } },
  MutationObserver: FakeMutationObserver,
  document,
  window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
  location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/g/example/premium-calculator/c/" + input.conversation_id },
  sessionStorage: {
    getItem(key) { return sessionStorage.get(key) || null; },
    setItem(key, value) { sessionStorage.set(key, String(value)); }
  },
  crypto: require("node:crypto").webcrypto,
  TextEncoder,
  TextDecoder,
  Uint8Array,
  Map,
  Set,
  WeakSet,
  WeakMap,
  URL,
  fetch: async () => ({ ok: false }),
  setTimeout,
  clearTimeout,
  setInterval: () => ({ unref() {} })
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(workerPath, "utf8"), context, { filename: "transport_worker.js" });
vm.runInContext(fs.readFileSync(adapterPath, "utf8"), context, { filename: "content_adapter.js" });

const deadline = Date.now() + 20000;
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
(async () => {
  const p3Binding = input.launch.execution_binding_id;
  while (Date.now() < deadline) {
    const p3Ack = trace.nativeRequests.some((item) => item.action === "project_launch_ack" && item.request.launch_id === input.launch.launch_id);
    const p3Submit = trace.nativeRequests.some((item) => item.action === "project_execution_submit" && item.request.result?.execution_binding_id === p3Binding);
    const p4Ack = trace.nativeRequests.some((item) => item.action === "project_launch_ack" && item.request.project_id === input.launch.project_id && item.request.execution_binding_id !== p3Binding);
    if (p3Ack && p3Submit && p4Ack) break;
    await wait(25);
  }
  await wait(200);
  assert.equal(document.querySelectorAll("[data-message-author-role]").length, 0, "live route fixture has no legacy message owner markers");
  assert.equal(document.querySelectorAll("[data-testid^='conversation-turn-']").length, 0, "live route fixture has no legacy conversation-turn test IDs");
  assert.equal(document.querySelectorAll("pre code").length, 1, "the existing result is discoverable only through its pre/code block");
  assert.equal(document.querySelectorAll("pre code")[0].textContent, JSON.stringify(input.result));
  if (input.dom_mode === "ambiguous-turn") {
    assert.equal(trace.nativeRequests.filter((item) => item.action === "project_launch_ack" && item.request.launch_id === input.launch.launch_id).length, 0, "an unpaired structural result cannot ACK the prior launch");
    assert.equal(trace.nativeRequests.filter((item) => item.action === "project_execution_submit" && item.request.result?.execution_binding_id === p3Binding).length, 0, "an unpaired structural result cannot be auto-submitted");
    assert.equal(trace.sends.length, 0, "ambiguous recovery cannot resend");
    process.stdout.write(JSON.stringify(trace));
    return;
  }
  const p3Submit = trace.nativeRequests.find((item) => item.action === "project_execution_submit" && item.request.result?.execution_binding_id === p3Binding);
  const firstStatusResponse = trace.nativeRequests.find((item) => item.action === "project_execution_status")?.response || null;
  const autoGateMatches = firstStatusResponse && typeof context.projectAutoGateMatches === "function"
    ? context.projectAutoGateMatches(firstStatusResponse, input.result, input.conversation_id)
    : null;
  const lifecycle = vm.runInContext("({ autoState: projectAutoState, pollActive: projectPollActive, insertionActive: projectInsertionActive, submissions: Array.from(projectAutoSubmissions.values()).map((item) => item.status), claims: Array.from(projectClaims.entries()) })", context);
  const requestSummary = trace.nativeRequests.map((item) => ({
    action: item.action,
    task: item.request.task_id || item.request.result?.task_id,
    binding: item.request.execution_binding_id || item.request.result?.execution_binding_id,
    launch: item.request.launch_id,
    status: item.response.status,
    error: item.response.error_code || item.response.error,
    bridge_error: item.response.status === "bridge_error" ? { exit_code: item.response.exit_code, error: item.response.error, stdout: item.response.stdout, stderr: item.response.stderr } : undefined,
    state: item.action === "project_execution_status" ? {
      current_binding_id: item.response.current_binding_id,
      current_task_id: item.response.current_task_id,
      binding: item.response.binding,
      milestone_auto: item.response.milestone_auto,
      launch_handoff: item.response.launch_handoff,
      launch_outbox_status: item.response.launch_outbox_status
    } : undefined,
    receipt: item.response.receipt && {
      accepted: item.response.receipt.accepted,
      result_status: item.response.receipt.result_status,
      task_status: item.response.receipt.task_status,
      current_task_id: item.response.receipt.current_task_id
    }
  }));
  assert.ok(p3Submit, "Browser AUTO must submit the existing assistant result through Native; autoGateMatches=" + autoGateMatches + "; resultIdentity=" + JSON.stringify({ project_id: input.result.project_id, task_id: input.result.task_id, execution_binding_id: input.result.execution_binding_id, plan_version: input.result.plan_version }) + "; lifecycle=" + JSON.stringify(lifecycle) + "; requests=" + JSON.stringify(requestSummary) + "; sends=" + JSON.stringify(trace.sends));
  assert.equal(p3Submit.request.result.plan_version, "1");
  assert.equal(Object.prototype.hasOwnProperty.call(p3Submit.request.result, "canonical_refs"), false);
  assert.equal(trace.sends.length, 1, "only the newly authorized P3-04 launch may send; P3-03 recovery must not resend; requests=" + JSON.stringify(trace.nativeRequests.map((item) => ({action:item.action, task:item.request.task_id || item.request.result?.task_id, binding:item.request.execution_binding_id || item.request.result?.execution_binding_id, status:item.response.status, error:item.response.error, receipt:item.response.receipt ? {accepted:item.response.receipt.accepted, result_status:item.response.receipt.result_status, task_status:item.response.receipt.task_status, current_task_id:item.response.receipt.current_task_id, milestone_status:item.response.receipt.milestone_status, next_launch_status:item.response.receipt.next_launch_status, next_task:item.response.receipt.next_launch?.task_id} : undefined}))) + " sends=" + JSON.stringify(trace.sends));
  assert.match(trace.sends[0], /Task ID \(copy exactly\): P3-04/);
  const p3AckIndex = trace.nativeRequests.findIndex((item) => item.action === "project_launch_ack" && item.request.launch_id === input.launch.launch_id);
  const p3SubmitIndex = trace.nativeRequests.findIndex((item) => item.action === "project_execution_submit" && item.request.result?.execution_binding_id === p3Binding);
  assert.ok(p3AckIndex >= 0 && p3SubmitIndex > p3AckIndex, "P3-03 canonical ACK must precede result submission");
  assert.equal(trace.nativeRequests.filter((item) => item.action === "project_launch_ack" && item.request.launch_id === input.launch.launch_id).length, 1);
  process.stdout.write(JSON.stringify(trace));
})().catch((error) => {
  process.stderr.write(error.stack || String(error));
  process.exitCode = 1;
});
