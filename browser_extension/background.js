"use strict";

const HOST_NAME = "com.bartosz.dev_bridge";
const REQUEST_SCHEMA = "bdb-native-request-v1";
const ACTION_SCHEMA = "bdb-action-v1";
const WORKSPACE_CONTEXT_OPERATION = "workspace_context";
const MAX_SERIALIZED_BYTES = 1024 * 1024;
const DEFAULT_WAIT_SECONDS = 30;
const PROMOTION_WAIT_ATTEMPTS = 60;
const PROMOTION_WAIT_MILLISECONDS = 100;
const DEFAULT_AUTO_SETTINGS = Object.freeze({
  autoEnabled: false,
  autoMaxIterations: 4,
  autoMaxMinutes: 10
});
const AUTO_REPLAY_GUARD_KEY = "bdbAutoReplayGuard";
const AUTO_REPLAY_GUARD_LIMIT = 512;
const LOOP_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const REPO_ALIAS_RE = /^[a-z][a-z0-9-]{0,31}$/;
const TERMINAL_VALUES = new Set([
  "done",
  "needs_user",
  "policy_denied",
  "manual_reconciliation_required",
  "failed",
  "cancelled",
  "aborted"
]);
const inFlightTabs = new Set();
const replayClaimsInFlight = new Set();

function requestId(prefix) {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return `${prefix}-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function serializedSize(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function validateJsonObject(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  if (serializedSize(value) > MAX_SERIALIZED_BYTES) {
    throw new Error(`${field} exceeds the 1 MiB limit`);
  }
}

function validateRepoAlias(value) {
  if (typeof value !== "string" || !REPO_ALIAS_RE.test(value)) {
    throw new Error("Repository alias has an unsafe format");
  }
  return value;
}

function sendNative(request) {
  validateJsonObject(request, "native request");
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, request, (response) => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError) {
        reject(new Error(runtimeError.message || "Native host unavailable"));
        return;
      }
      try {
        validateJsonObject(response, "native response");
        resolve(response);
      } catch (error) {
        reject(error);
      }
    });
  });
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function nativeContext(repoAlias) {
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("workspace-context"),
    action: "context",
    repo_alias: validateRepoAlias(repoAlias)
  });
}

async function workspaceContext(action) {
  const repoAlias = validateRepoAlias(action.repo_alias);
  const native = await nativeContext(repoAlias);
  return {
    schema: native.schema,
    request_id: native.request_id,
    status: "completed",
    repo_alias: repoAlias,
    result: {
      status: "success",
      operation: WORKSPACE_CONTEXT_OPERATION,
      context: native.context,
      arm: native.arm
    }
  };
}

function requiresPromotion(action) {
  const promotion = action && action.promotion;
  return Boolean(
    promotion &&
    typeof promotion === "object" &&
    !Array.isArray(promotion) &&
    promotion.mode === "required"
  );
}

function withPromotion(response, promotion) {
  const result = response && response.result && typeof response.result === "object"
    ? response.result
    : {};
  return { ...response, result: { ...result, promotion } };
}

async function waitForRequiredPromotion(action, response) {
  if (!requiresPromotion(action)) {
    return response;
  }
  const result = response && response.result;
  const successfulPatch = Boolean(
    response &&
    response.status === "completed" &&
    result &&
    result.status === "success" &&
    result.data &&
    result.data.operation === "multi_file_patch"
  );
  if (!successfulPatch) {
    return response;
  }

  const commandId = response.command_id || result.command_id;
  if (typeof commandId !== "string" || commandId.length === 0) {
    return withPromotion(response, {
      status: "needs_user",
      reason: "completed_result_has_no_command_id"
    });
  }

  for (let attempt = 0; attempt < PROMOTION_WAIT_ATTEMPTS; attempt += 1) {
    const contextResponse = await nativeContext(action.repo_alias);
    const context = contextResponse && contextResponse.context;
    const receipt = context && context.latest_promotion;
    if (
      receipt &&
      receipt.status === "promoted" &&
      receipt.command_id === commandId &&
      context.source_clean === true
    ) {
      return withPromotion(response, receipt);
    }
    await sleep(PROMOTION_WAIT_MILLISECONDS);
  }

  return withPromotion(response, {
    status: "needs_user",
    reason: "promotion_not_observed",
    command_id: commandId
  });
}

async function submitAction(action, tabId) {
  validateJsonObject(action, "BDB action");
  if (action.schema !== ACTION_SCHEMA) {
    throw new Error(`Only ${ACTION_SCHEMA} is supported`);
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("A concrete sender tab is required");
  }
  if (inFlightTabs.has(tabId)) {
    throw new Error("This tab already has a BDB action in progress");
  }
  inFlightTabs.add(tabId);
  try {
    if (action.operation === WORKSPACE_CONTEXT_OPERATION) {
      return await workspaceContext(action);
    }
    const response = await sendNative({
      schema: REQUEST_SCHEMA,
      request_id: requestId("submit"),
      action: "submit_action",
      wait_seconds: DEFAULT_WAIT_SECONDS,
      bdb_action: action
    });
    return await waitForRequiredPromotion(action, response);
  } finally {
    inFlightTabs.delete(tabId);
  }
}

function normalizeAutoSettings(raw) {
  const enabled = raw.autoEnabled === true;
  const iterations = Number.isInteger(raw.autoMaxIterations) ? raw.autoMaxIterations : DEFAULT_AUTO_SETTINGS.autoMaxIterations;
  const minutes = Number.isInteger(raw.autoMaxMinutes) ? raw.autoMaxMinutes : DEFAULT_AUTO_SETTINGS.autoMaxMinutes;
  if (iterations < 1 || iterations > 8 || minutes < 1 || minutes > 30) {
    throw new Error("AUTO limits are outside the allowed range");
  }
  return { autoEnabled: enabled, autoMaxIterations: iterations, autoMaxMinutes: minutes };
}

async function getAutoSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULT_AUTO_SETTINGS));
  return normalizeAutoSettings({ ...DEFAULT_AUTO_SETTINGS, ...stored });
}

async function setAutoSettings(settings) {
  const normalized = normalizeAutoSettings(settings);
  await chrome.storage.local.set(normalized);
  return normalized;
}

function automationMetadata(action) {
  const metadata = action && action.automation;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata) || metadata.mode !== "auto") {
    return null;
  }
  if (typeof metadata.loop_id !== "string" || !LOOP_ID_RE.test(metadata.loop_id)) {
    throw new Error("AUTO loop_id has an unsafe format");
  }
  if (!Number.isInteger(metadata.iteration) || metadata.iteration < 1) {
    throw new Error("AUTO iteration must be a positive integer");
  }
  return { loopId: metadata.loop_id, iteration: metadata.iteration };
}

function autoStateKey(tabId, loopId) {
  return `bdbAuto:${tabId}:${loopId}`;
}

function autoReplayKey(loopId, iteration) {
  return `${loopId}:${iteration}`;
}

async function claimAutoReplay(loopId, iteration) {
  const key = autoReplayKey(loopId, iteration);
  if (replayClaimsInFlight.has(key)) {
    return false;
  }
  replayClaimsInFlight.add(key);
  try {
    const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
    const raw = stored[AUTO_REPLAY_GUARD_KEY];
    const guard = raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
    if (Object.prototype.hasOwnProperty.call(guard, key)) {
      return false;
    }
    guard[key] = Date.now();
    const entries = Object.entries(guard)
      .filter(([entryKey, timestamp]) => typeof entryKey === "string" && Number.isFinite(timestamp))
      .sort((left, right) => left[1] - right[1])
      .slice(-AUTO_REPLAY_GUARD_LIMIT);
    await chrome.storage.local.set({ [AUTO_REPLAY_GUARD_KEY]: Object.fromEntries(entries) });
    return true;
  } finally {
    replayClaimsInFlight.delete(key);
  }
}

function containsTerminalValue(value, depth = 0) {
  if (depth > 8) {
    return "needs_user";
  }
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    return TERMINAL_VALUES.has(normalized) ? normalized : null;
  }
  if (Array.isArray(value)) {
    for (const item of value.slice(0, 100)) {
      const terminal = containsTerminalValue(item, depth + 1);
      if (terminal) {
        return terminal;
      }
    }
    return null;
  }
  if (value && typeof value === "object") {
    for (const item of Object.values(value).slice(0, 100)) {
      const terminal = containsTerminalValue(item, depth + 1);
      if (terminal) {
        return terminal;
      }
    }
  }
  return null;
}

async function considerAuto(action, tabId) {
  const metadata = automationMetadata(action);
  if (!metadata) {
    return { executed: false, reason: "action_not_auto" };
  }
  const settings = await getAutoSettings();
  if (!settings.autoEnabled) {
    return { executed: false, reason: "auto_disabled" };
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("AUTO requires a concrete sender tab");
  }
  if (metadata.iteration > settings.autoMaxIterations) {
    return { executed: false, reason: "iteration_limit" };
  }

  const key = autoStateKey(tabId, metadata.loopId);
  const stored = await chrome.storage.session.get(key);
  const now = Date.now();
  const state = stored[key] || {
    startedAt: now,
    lastIteration: 0,
    status: "running"
  };
  if (state.status !== "running") {
    return { executed: false, reason: "loop_not_running", state };
  }
  if (now - state.startedAt > settings.autoMaxMinutes * 60 * 1000) {
    state.status = "time_limit";
    await chrome.storage.session.set({ [key]: state });
    return { executed: false, reason: "time_limit", state };
  }
  if (metadata.iteration !== state.lastIteration + 1) {
    return { executed: false, reason: "non_sequential_iteration", state };
  }
  if (!await claimAutoReplay(metadata.loopId, metadata.iteration)) {
    return { executed: false, reason: "replay_guard", state };
  }

  const response = await submitAction(action, tabId);
  const terminal = containsTerminalValue(response.result || response);
  const completed = response.status === "completed";
  state.lastIteration = metadata.iteration;
  state.lastCommandId = response.command_id || null;
  state.updatedAt = Date.now();
  state.status = terminal || (completed ? "running" : "needs_user");
  await chrome.storage.session.set({ [key]: state });

  const shouldContinue = completed && !terminal && metadata.iteration < settings.autoMaxIterations;
  return {
    executed: true,
    response,
    loopId: metadata.loopId,
    iteration: metadata.iteration,
    shouldContinue,
    stopReason: terminal || (completed ? null : "result_not_completed"),
    state
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const handle = async () => {
    validateJsonObject(message, "extension message");
    switch (message.type) {
      case "BDB_SUBMIT_ACTION":
        return submitAction(message.action, sender.tab && sender.tab.id);
      case "BDB_CONSIDER_AUTO":
        return considerAuto(message.action, sender.tab && sender.tab.id);
      case "BDB_GET_AUTO_SETTINGS":
        return getAutoSettings();
      case "BDB_SET_AUTO_SETTINGS":
        validateJsonObject(message.settings, "AUTO settings");
        return setAutoSettings(message.settings);
      case "BDB_STATUS":
        return sendNative({
          schema: REQUEST_SCHEMA,
          request_id: requestId("status"),
          action: "status"
        });
      case "BDB_CONTEXT":
        return nativeContext(message.repoAlias);
      default:
        throw new Error("Unsupported extension message");
    }
  };

  handle()
    .then((response) => sendResponse({ ok: true, response }))
    .catch((error) => sendResponse({ ok: false, error: String(error && error.message ? error.message : error) }));
  return true;
});
