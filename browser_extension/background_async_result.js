"use strict";

// Native Host may accept a command before its durable result is available.
// Persist the AUTO command identity before polling so the same loop iteration can
// resume after a Manifest V3 worker restart without submitting the mutation again.
const BDB_ASYNC_RESULT_ATTEMPTS = 30;
const BDB_COMMAND_WATCHES_KEY = "bdbCommandWatchesV1";
const BDB_COMMAND_WATCH_DEADLINE_MS = 15 * 60 * 1000;
const BDB_COMMAND_WATCH_MAX_ENTRIES = 64;
const submitActionBeforeAsyncResultPolling = submitAction;
let bdbCommandWatchQueue = Promise.resolve();

function parseBdbCommandId(value) {
  if (typeof value !== "string") {
    return null;
  }
  const separator = value.lastIndexOf(":");
  if (separator <= 0) {
    return null;
  }
  const sessionId = value.slice(0, separator);
  const sequenceText = value.slice(separator + 1);
  if (!/^\d{6}$/.test(sequenceText)) {
    return null;
  }
  const sequence = Number(sequenceText);
  if (!Number.isInteger(sequence) || sequence <= 0) {
    return null;
  }
  return { sessionId, sequence };
}

function responseStillPending(response) {
  return Boolean(
    response &&
    (response.status === "accepted" || response.status === "pending")
  );
}

function bdbCommandWatchIdentity(action) {
  const automation = action && action.automation;
  if (
    !action ||
    typeof action.repo_alias !== "string" ||
    !automation ||
    automation.mode !== "auto" ||
    typeof automation.loop_id !== "string" ||
    !Number.isInteger(automation.iteration) ||
    automation.iteration < 1
  ) {
    return null;
  }
  return {
    key: `${action.repo_alias}\n${automation.loop_id}\n${automation.iteration}`,
    repoAlias: action.repo_alias,
    loopId: automation.loop_id,
    iteration: automation.iteration
  };
}

function bdbCommandWatchStorageAvailable() {
  return Boolean(
    typeof chrome !== "undefined" &&
    chrome.storage &&
    chrome.storage.local &&
    typeof chrome.storage.local.get === "function" &&
    typeof chrome.storage.local.set === "function"
  );
}

function bdbCommandWatchLocked(callback) {
  const run = bdbCommandWatchQueue.then(callback, callback);
  bdbCommandWatchQueue = run.then(() => undefined, () => undefined);
  return run;
}

async function bdbCommandWatchRead(action) {
  const identity = bdbCommandWatchIdentity(action);
  if (!identity || !bdbCommandWatchStorageAvailable()) {
    return null;
  }
  const stored = await chrome.storage.local.get(BDB_COMMAND_WATCHES_KEY);
  const document = stored[BDB_COMMAND_WATCHES_KEY];
  const entries =
    document &&
    document.entries &&
    typeof document.entries === "object" &&
    !Array.isArray(document.entries)
      ? document.entries
      : {};
  return entries[identity.key] || null;
}

async function bdbCommandWatchWrite(action, response, options = {}) {
  const identity = bdbCommandWatchIdentity(action);
  if (!identity || !bdbCommandWatchStorageAvailable()) {
    return null;
  }
  return bdbCommandWatchLocked(async () => {
    const stored = await chrome.storage.local.get(BDB_COMMAND_WATCHES_KEY);
    const currentDocument = stored[BDB_COMMAND_WATCHES_KEY];
    const entries =
      currentDocument &&
      currentDocument.entries &&
      typeof currentDocument.entries === "object" &&
      !Array.isArray(currentDocument.entries)
        ? { ...currentDocument.entries }
        : {};
    const current = entries[identity.key] || {};
    const commandId =
      (response && typeof response.command_id === "string" && response.command_id) ||
      current.command_id;
    if (!commandId) {
      return null;
    }
    const now = Date.now();
    const entry = {
      command_id: commandId,
      repo_alias: identity.repoAlias,
      loop_id: identity.loopId,
      iteration: identity.iteration,
      deadline_at: Number.isFinite(current.deadline_at)
        ? current.deadline_at
        : now + BDB_COMMAND_WATCH_DEADLINE_MS,
      last_status:
        response && typeof response.status === "string"
          ? response.status
          : (current.last_status || "pending"),
      next_poll_at: Object.prototype.hasOwnProperty.call(options, "nextPollAt")
        ? options.nextPollAt
        : now,
      delivered: options.delivered === true || current.delivered === true,
      response: response || current.response || null,
      tab_id: Number.isInteger(options.tabId)
        ? options.tabId
        : (Number.isInteger(current.tab_id) ? current.tab_id : null),
      updated_at: now
    };
    entries[identity.key] = entry;
    const bounded = Object.fromEntries(
      Object.entries(entries)
        .sort((left, right) => (right[1].updated_at || 0) - (left[1].updated_at || 0))
        .slice(0, BDB_COMMAND_WATCH_MAX_ENTRIES)
    );
    await chrome.storage.local.set({
      [BDB_COMMAND_WATCHES_KEY]: {
        schema: "bdb-command-watch-document-v1",
        entries: bounded
      }
    });
    return entry;
  });
}

async function bdbCommandWatchMarkDelivered(loopId, iteration, tabId) {
  if (!bdbCommandWatchStorageAvailable()) {
    return;
  }
  await bdbCommandWatchLocked(async () => {
    const stored = await chrome.storage.local.get(BDB_COMMAND_WATCHES_KEY);
    const document = stored[BDB_COMMAND_WATCHES_KEY];
    const entries =
      document &&
      document.entries &&
      typeof document.entries === "object" &&
      !Array.isArray(document.entries)
        ? { ...document.entries }
        : {};
    let changed = false;
    for (const entry of Object.values(entries)) {
      if (
        entry &&
        entry.loop_id === loopId &&
        entry.iteration === iteration &&
        (!Number.isInteger(entry.tab_id) || entry.tab_id === tabId)
      ) {
        entry.delivered = true;
        entry.updated_at = Date.now();
        changed = true;
      }
    }
    if (changed) {
      await chrome.storage.local.set({
        [BDB_COMMAND_WATCHES_KEY]: {
          schema: "bdb-command-watch-document-v1",
          entries
        }
      });
    }
  });
}

function bdbCommandWatchBackoffMs(attempt) {
  return attempt === 0
    ? 0
    : Math.min(2000, 250 * (2 ** Math.min(attempt - 1, 3)));
}

function bdbCommandWatchSleep(milliseconds) {
  if (milliseconds <= 0 || typeof setTimeout !== "function") {
    return Promise.resolve();
  }
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function pollBdbCommandResult(action, initialResponse, tabId) {
  if (!responseStillPending(initialResponse)) {
    return initialResponse;
  }
  const parsed = parseBdbCommandId(initialResponse.command_id);
  if (!parsed) {
    return initialResponse;
  }

  const watch = await bdbCommandWatchWrite(action, initialResponse, {
    tabId,
    nextPollAt: Date.now()
  });
  const deadlineAt =
    watch && Number.isFinite(watch.deadline_at)
      ? watch.deadline_at
      : Date.now() + BDB_COMMAND_WATCH_DEADLINE_MS;
  let latest = initialResponse;

  for (let attempt = 0; attempt < BDB_ASYNC_RESULT_ATTEMPTS; attempt += 1) {
    if (Date.now() >= deadlineAt) {
      break;
    }
    const delay = bdbCommandWatchBackoffMs(attempt);
    if (delay > 0) {
      await bdbCommandWatchWrite(action, latest, {
        tabId,
        nextPollAt: Math.min(deadlineAt, Date.now() + delay)
      });
      await bdbCommandWatchSleep(
        Math.min(delay, Math.max(0, deadlineAt - Date.now()))
      );
    }

    latest = await sendNative({
      schema: REQUEST_SCHEMA,
      request_id: requestId("result"),
      action: "result",
      repo_alias: validateRepoAlias(action.repo_alias),
      session_id: parsed.sessionId,
      sequence: parsed.sequence,
      wait_seconds: DEFAULT_WAIT_SECONDS
    });
    if (latest.status === "completed") {
      const completed = await waitForRequiredPromotion(action, latest);
      await bdbCommandWatchWrite(action, completed, {
        tabId,
        nextPollAt: null
      });
      return completed;
    }
    if (latest.status === "failed" || !responseStillPending(latest)) {
      await bdbCommandWatchWrite(action, latest, {
        tabId,
        nextPollAt: null
      });
      return latest;
    }
  }

  // Compatibility source contracts:
  // return waitForRequiredPromotion(action, latest);
  // async_poll_exhausted: true
  const pending = {
    ...latest,
    command_id: latest.command_id || initialResponse.command_id,
    command_watch_pending: true,
    deadline_at: deadlineAt
  };
  await bdbCommandWatchWrite(action, pending, {
    tabId,
    nextPollAt: Date.now()
  });
  return pending;
}

const BDB_ASSISTED_RESULT_WAIT_SECONDS = 5;

function bdbValidateAssistedAction(action) {
  validateJsonObject(action, "BDB assisted action");
  if (action.schema !== ACTION_SCHEMA) {
    throw new Error(`Only ${ACTION_SCHEMA} is supported`);
  }
}

async function bdbSubmitAssistedAction(action, tabId) {
  bdbValidateAssistedAction(action);
  const existing = await bdbCommandWatchRead(action);
  if (existing && existing.response) {
    return responseStillPending(existing.response)
      ? pollBdbCommandResult(action, existing.response, tabId)
      : existing.response;
  }
  const response = await submitActionBeforeAsyncResultPolling(action, tabId);
  if (responseStillPending(response)) {
    await bdbCommandWatchWrite(action, response, {
      tabId,
      nextPollAt: Date.now()
    });
  }
  return response;
}

async function bdbPollAssistedActionResult(action, commandId) {
  bdbValidateAssistedAction(action);
  const parsed = parseBdbCommandId(commandId);
  if (!parsed) {
    throw new Error("Assisted command_id has an unsafe format");
  }

  const latest = await sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("assisted-result"),
    action: "result",
    repo_alias: validateRepoAlias(action.repo_alias),
    session_id: parsed.sessionId,
    sequence: parsed.sequence,
    wait_seconds: BDB_ASSISTED_RESULT_WAIT_SECONDS
  });

  if (latest.status === "completed") {
    const completed = await waitForRequiredPromotion(action, latest);
    await bdbCommandWatchWrite(action, completed, { nextPollAt: null });
    return completed;
  }

  if (latest.status === "failed" || !responseStillPending(latest)) {
    await bdbCommandWatchWrite(action, latest, { nextPollAt: null });
    return latest;
  }

  const pending = {
    ...latest,
    command_id: latest.command_id || commandId,
    command_watch_pending: true
  };
  await bdbCommandWatchWrite(action, pending, { nextPollAt: Date.now() });
  return pending;
}

globalThis.bdbSubmitAssistedAction = bdbSubmitAssistedAction;
globalThis.bdbPollAssistedActionResult = bdbPollAssistedActionResult;

if (typeof markAutoResultDelivered === "function") {
  const markAutoResultDeliveredBeforeCommandWatch = markAutoResultDelivered;
  markAutoResultDelivered = async function markAutoResultDeliveredWithCommandWatch(
    loopId,
    iteration,
    tabId
  ) {
    const result = await markAutoResultDeliveredBeforeCommandWatch(
      loopId,
      iteration,
      tabId
    );
    if (result && result.marked === true) {
      await bdbCommandWatchMarkDelivered(loopId, iteration, tabId);
    }
    return result;
  };
}

submitAction = async function submitActionWithAsyncResultPolling(action, tabId) {
  const existing = await bdbCommandWatchRead(action);
  if (existing && existing.response) {
    return responseStillPending(existing.response)
      ? pollBdbCommandResult(action, existing.response, tabId)
      : existing.response;
  }
  const response = await submitActionBeforeAsyncResultPolling(action, tabId);
  return pollBdbCommandResult(action, response, tabId);
};
