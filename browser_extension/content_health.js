"use strict";

// This hard-coded build version intentionally differs from runtime manifest
// lookup. A content script survives an extension reload until the ChatGPT tab is
// refreshed, so comparing both values detects the otherwise invisible stale-tab
// state that previously forced AUTO into ASSISTED.
const BDB_CONTENT_BUILD_VERSION = "0.4.6";
const BDB_CONTENT_VERSION_WARNING_ID = "bdb-version-warning";

function bdbContentHealthWarning(message) {
  let warning = document.getElementById(BDB_CONTENT_VERSION_WARNING_ID);
  if (!warning) {
    warning = document.createElement("div");
    warning.id = BDB_CONTENT_VERSION_WARNING_ID;
    warning.setAttribute("role", "alert");
    document.documentElement.append(warning);
  }
  warning.textContent = message;
}

function bdbContentClearHealthWarning() {
  const warning = document.getElementById(BDB_CONTENT_VERSION_WARNING_ID);
  if (warning) {
    warning.remove();
  }
}

async function bdbContentRecord(event) {
  try {
    await chrome.runtime.sendMessage({ type: "BDB_CONTENT_EVENT", event });
  } catch (_error) {
  }
}

globalThis.bdbContentRecord = bdbContentRecord;

async function bdbContentHandshake({ probeNative = false } = {}) {
  const response = await chrome.runtime.sendMessage({
    type: "BDB_HEALTH",
    probeNative,
    contentVersion: BDB_CONTENT_BUILD_VERSION
  });
  if (!response || response.ok !== true) {
    throw new Error(response && response.error ? response.error : "BDB health handshake failed");
  }
  const health = response.response;
  if (health.content_version_match === false) {
    bdbContentHealthWarning(
      `BDB: karta ChatGPT używa wersji ${BDB_CONTENT_BUILD_VERSION}, a rozszerzenie ${health.extension_version}. Przeładuj kartę.`
    );
    await bdbContentRecord({
      event: "content_version_mismatch",
      reason: `${BDB_CONTENT_BUILD_VERSION}->${health.extension_version}`,
      metric: "content_version_mismatches"
    });
  } else {
    bdbContentClearHealthWarning();
  }
  return health;
}

function bdbContentSelfTestAction(repoAlias) {
  return {
    schema: ACTION_SCHEMA,
    repo_alias: repoAlias,
    operation: "workspace_context",
    payload: {},
    automation: {
      mode: "auto",
      loop_id: `bdb-auto-self-test-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
      iteration: 1,
      continue_on_failure: false
    },
    task: {
      id: `bdb-auto-self-test-${Date.now()}`,
      title: "BDB AUTO self-test",
      phase: "health_check"
    },
    presentation: { mode: "compact" }
  };
}

async function bdbContentRunAutoSelfTest(repoAlias) {
  if (typeof repoAlias !== "string" || !/^[a-z][a-z0-9-]{0,31}$/.test(repoAlias)) {
    throw new Error("Self-test requires a safe repository alias");
  }
  const started = Date.now();
  const health = await bdbContentHandshake({ probeNative: true });
  const composer = findComposer();
  if (!composer) {
    throw new Error("ChatGPT composer is unavailable");
  }
  if (composerText(composer).trim() !== "") {
    throw new Error("ChatGPT composer must be empty before AUTO self-test");
  }
  const action = bdbContentSelfTestAction(repoAlias);
  const decision = await chrome.runtime.sendMessage({ type: "BDB_CONSIDER_AUTO", action });
  if (!decision || decision.ok !== true) {
    throw new Error(decision && decision.error ? decision.error : "AUTO self-test decision failed");
  }
  const auto = decision.response;
  if (!auto.executed) {
    await bdbContentRecord({
      event: "auto_self_test_stopped",
      loopId: action.automation.loop_id,
      iteration: 1,
      operation: action.operation,
      reason: auto.reason,
      status: "assisted",
      durationMs: Date.now() - started,
      metric: "auto_self_test_failures"
    });
    return {
      schema: "bdb-auto-self-test-v1",
      status: "failed",
      stage: "auto_decision",
      reason: auto.reason,
      health
    };
  }
  const sent = await autoSend(auto.response, auto.loopId, auto.iteration);
  if (sent.sent) {
    await chrome.runtime.sendMessage({
      type: "BDB_MARK_AUTO_RESULT_DELIVERED",
      loopId: auto.loopId,
      iteration: auto.iteration
    });
  }
  await bdbContentRecord({
    event: sent.sent ? "auto_self_test_passed" : "auto_self_test_failed",
    loopId: auto.loopId,
    iteration: auto.iteration,
    operation: action.operation,
    reason: sent.reason,
    status: sent.sent ? "passed" : "failed",
    durationMs: Date.now() - started,
    metric: sent.sent ? "auto_self_test_passes" : "auto_self_test_failures"
  });
  return {
    schema: "bdb-auto-self-test-v1",
    status: sent.sent ? "passed" : "failed",
    stage: sent.sent ? "result_delivered" : "composer_send",
    reason: sent.reason,
    loop_id: auto.loopId,
    iteration: auto.iteration,
    duration_ms: Date.now() - started,
    health
  };
}

if (
  chrome.runtime.onMessage &&
  typeof chrome.runtime.onMessage.addListener === "function"
) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "BDB_CONTENT_SELF_TEST") {
      return false;
    }
    bdbContentRunAutoSelfTest(message.repoAlias)
      .then((result) => sendResponse({ ok: true, response: result }))
      .catch((error) => sendResponse({
        ok: false,
        error: String(error && error.message ? error.message : error)
      }));
    return true;
  });
}

if (typeof chrome.runtime.getManifest === "function") {
  bdbContentHandshake().catch((error) => {
    bdbContentHealthWarning(
      `BDB: nie udało się sprawdzić zgodności rozszerzenia. ${String(error && error.message ? error.message : error)}`
    );
  });
}
