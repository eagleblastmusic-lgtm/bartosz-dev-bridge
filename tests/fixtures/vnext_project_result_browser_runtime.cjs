"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto, randomUUID } = require("node:crypto");

const [workerPath, adapterPath, mode] = process.argv.slice(2);
const extensionId = "mopnolkjddkmgojfjkenjobehhmmklll";
const nativeRequests = [];
let workerListener;
const nativeResponse = (request) => ({
  schema: "bdb-vnext-native-response-v1",
  request_id: request.request_id,
  generation_id: "bdb-vnext-g1",
  protocol_generation: "bdb-vnext-protocol-v1",
  native_host_name: "com.bartosz.dev_bridge.vnext",
  browser_extension_id: extensionId,
  status: request.action === "project_launch_peek" ? "empty" : "project_execution",
  receipt: request.action === "project_execution_submit" ? {
    accepted: true, result_status: "PASS", task_status: "completed", task_id: "P3-03"
  } : undefined
});

const workerChrome = {
  runtime: {
    id: extensionId,
    lastError: null,
    getURL(path) { return `chrome-extension://${extensionId}/${path}`; },
    onMessage: { addListener(listener) { workerListener = listener; } },
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    sendNativeMessage(_host, request, callback) {
      nativeRequests.push(JSON.parse(JSON.stringify(request)));
      if (mode === "native-error" && request.action === "project_execution_submit") {
        callback({ ...nativeResponse(request), status: "failed", receipt: undefined,
          error_code: "execution_field_invalid", error: "plan_version must be text" });
      } else {
        callback(nativeResponse(request));
      }
    }
  },
  storage: { local: { async get() { return {}; }, async set() {} } }
};
const worker = { chrome: workerChrome, crypto: webcrypto, TextEncoder, Uint8Array, Set, Map,
  fetch: async () => { throw new Error("bundle proof unavailable in fixture"); }, console };
vm.createContext(worker);
vm.runInContext(fs.readFileSync(workerPath, "utf8"), worker, { filename: workerPath });

const browserMessages = [];
const contentChrome = {
  runtime: {
    onMessage: { addListener() {} },
    sendMessage(message) {
      browserMessages.push(message);
      return new Promise((resolve) => {
        assert.equal(workerListener(message, { id: extensionId }, resolve), true);
      });
    }
  },
  storage: { local: { async get() { return {}; }, async set() {} } }
};
class Element {}
const document = {
  visibilityState: "visible", documentElement: { dataset: {} },
  querySelectorAll() { return []; }, querySelector() { return null; },
  createElement() { return new Element(); }
};
const content = {
  chrome: contentChrome, document, HTMLElement: Element, HTMLTextAreaElement: Element,
  HTMLInputElement: Element, MutationObserver: class { observe() {} },
  location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/abcdef12-3456-4789-abcd-abcdef123456" },
  setInterval: () => ({ unref() {} }), setTimeout, clearTimeout,
  TextEncoder, Set, Map, crypto: { randomUUID }, console
};
vm.createContext(content);
vm.runInContext(fs.readFileSync(adapterPath, "utf8"), content, { filename: adapterPath });

const result = {
  schema: "bdb-project-execution-submission-v1", project_id: "project-1",
  plan_version: mode === "numeric" ? 1 : "1", task_id: "P3-03",
  execution_binding_id: "binding-1", correlation_id: "corr-1", command_id: "command-1",
  repo_alias: "premium-calculator", head_before: "a".repeat(40), head_after: "b".repeat(40),
  execution_status: "PASS", validation_status: "PASS", promotion_status: "NOT_RUN",
  result_summary: "validated", evidence_refs: [], criteria: []
};
if (mode === "waiting") {
  result.execution_status = "WAITING_EXTERNAL";
  result.validation_status = "WAITING_EXTERNAL";
}
if (mode === "invalid") result.head_before = "invalid-head";

async function submit(value) {
  const parsed = content.parseProjectExecutionResult({ textContent: JSON.stringify(value) });
  assert.ok(parsed);
  const button = { disabled: false, textContent: "" };
  const output = { textContent: "", dataset: {} };
  await content.submitProjectExecutionResult(null, parsed, { button, output });
  return { parsed, button, output };
}

(async () => {
  if (mode === "direct-numeric") {
    const response = await new Promise((resolve) => workerListener({
      type: "bdb-vnext-project-execution-submit", result: { ...result, plan_version: 1 },
      conversation_id: "abcdef12-3456-4789-abcd-abcdef123456"
    }, { id: extensionId }, resolve));
    process.stdout.write(JSON.stringify({ response, submissions: nativeRequests.filter((request) => request.action === "project_execution_submit") }));
    return;
  }
  if (mode === "status-model") {
    const statuses = ["PASS", "SUCCEEDED", "SUCCESS", "FAIL", "FAILED", "BLOCKED", "REVIEW_REQUIRED", "WAITING_EXTERNAL", "PENDING", "RUNNING", "VALIDATING", "AWAITING_CI", "UNKNOWN", "NOT_RUN", "SKIPPED", "PROMOTED", "UNSUPPORTED"];
    const model = {};
    for (const field of ["execution_status", "validation_status", "promotion_status"]) {
      model[field] = Object.fromEntries(statuses.map((status) => {
        try { return [status, worker.finalStatus(status, field)]; }
        catch (error) { return [status, error.code]; }
      }));
    }
    process.stdout.write(JSON.stringify(model));
    return;
  }
  const first = await submit(result);
  const beforeFinal = nativeRequests.filter((request) => request.action === "project_execution_submit").length;
  let second = null;
  if (mode === "waiting") second = await submit({ ...result, execution_status: "PASS", validation_status: "PASS" });
  const submissions = nativeRequests.filter((request) => request.action === "project_execution_submit");
  process.stdout.write(JSON.stringify({
    first: { parsed: first.parsed, text: first.output.textContent, state: first.output.dataset.state },
    beforeFinal, second: second && { parsed: second.parsed, text: second.output.textContent, state: second.output.dataset.state },
    submissions, browserMessages: browserMessages.filter((message) => message.type === "bdb-vnext-project-execution-submit")
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
