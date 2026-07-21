"use strict";

// AUTO actions are detected from live ChatGPT DOM. A React reconciliation can
// expose the same action more than once, and a Manifest V3 worker can fail after
// claiming an iteration but before publishing its canonical state. Keep a
// bounded durable claim lease so duplicates wait, failed executions release the
// claim, and abandoned claims can be reclaimed without weakening replay safety.
// The lease exceeds the extension's bounded initial wait, result polling and
// promotion-observation window, so a live action cannot be reclaimed early.
const BDB_AUTO_REPLAY_LEASE_MS = 180 * 1000;
const BDB_AUTO_REPLAY_STATUS_PROCESSING = "processing";
const BDB_AUTO_REPLAY_STATUS_COMPLETED = "completed";
const considerAutoBeforeReplayRecovery = considerAuto;

function bdbReplayRecordTimestamp(record) {
  if (Number.isFinite(record)) {
    return record;
  }
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    return 0;
  }
  if (Number.isFinite(record.completedAt)) {
    return record.completedAt;
  }
  return Number.isFinite(record.claimedAt) ? record.claimedAt : 0;
}

function bdbReplayRecordStatus(record) {
  if (Number.isFinite(record)) {
    return "legacy";
  }
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    return null;
  }
  if (record.status === BDB_AUTO_REPLAY_STATUS_PROCESSING) {
    return BDB_AUTO_REPLAY_STATUS_PROCESSING;
  }
  if (record.status === BDB_AUTO_REPLAY_STATUS_COMPLETED) {
    return BDB_AUTO_REPLAY_STATUS_COMPLETED;
  }
  return null;
}

function bdbReplayGuardObject(raw) {
  return raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
}

function bdbPrunedReplayGuard(guard) {
  const entries = Object.entries(guard)
    .filter(([entryKey, record]) => (
      typeof entryKey === "string" && bdbReplayRecordTimestamp(record) > 0
    ))
    .sort((left, right) => bdbReplayRecordTimestamp(left[1]) - bdbReplayRecordTimestamp(right[1]))
    .slice(-AUTO_REPLAY_GUARD_LIMIT);
  return Object.fromEntries(entries);
}

async function bdbReadReplayRecord(loopId, iteration) {
  const key = autoReplayKey(loopId, iteration);
  const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
  const guard = bdbReplayGuardObject(stored[AUTO_REPLAY_GUARD_KEY]);
  return guard[key];
}

claimAutoReplay = async function claimRecoverableAutoReplay(loopId, iteration) {
  const key = autoReplayKey(loopId, iteration);
  if (replayClaimsInFlight.has(key)) {
    return false;
  }

  replayClaimsInFlight.add(key);
  try {
    const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
    const guard = bdbReplayGuardObject(stored[AUTO_REPLAY_GUARD_KEY]);
    const existing = guard[key];
    const status = bdbReplayRecordStatus(existing);
    const now = Date.now();

    if (status === BDB_AUTO_REPLAY_STATUS_COMPLETED) {
      return false;
    }
    if (
      (status === BDB_AUTO_REPLAY_STATUS_PROCESSING || status === "legacy") &&
      now - bdbReplayRecordTimestamp(existing) < BDB_AUTO_REPLAY_LEASE_MS
    ) {
      return false;
    }

    guard[key] = {
      status: BDB_AUTO_REPLAY_STATUS_PROCESSING,
      claimedAt: now
    };
    await chrome.storage.local.set({
      [AUTO_REPLAY_GUARD_KEY]: bdbPrunedReplayGuard(guard)
    });
    return true;
  } finally {
    replayClaimsInFlight.delete(key);
  }
};

async function bdbCompleteReplayClaim(loopId, iteration) {
  const key = autoReplayKey(loopId, iteration);
  const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
  const guard = bdbReplayGuardObject(stored[AUTO_REPLAY_GUARD_KEY]);
  guard[key] = {
    status: BDB_AUTO_REPLAY_STATUS_COMPLETED,
    completedAt: Date.now()
  };
  await chrome.storage.local.set({
    [AUTO_REPLAY_GUARD_KEY]: bdbPrunedReplayGuard(guard)
  });
}

async function bdbReleaseReplayClaim(loopId, iteration) {
  const key = autoReplayKey(loopId, iteration);
  const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
  const guard = bdbReplayGuardObject(stored[AUTO_REPLAY_GUARD_KEY]);
  if (bdbReplayRecordStatus(guard[key]) !== BDB_AUTO_REPLAY_STATUS_PROCESSING) {
    return;
  }
  delete guard[key];
  await chrome.storage.local.set({
    [AUTO_REPLAY_GUARD_KEY]: bdbPrunedReplayGuard(guard)
  });
}

considerAuto = async function considerAutoWithReplayRecovery(action, tabId) {
  const metadata = automationMetadata(action);
  if (!metadata) {
    return considerAutoBeforeReplayRecovery(action, tabId);
  }

  try {
    const decision = await considerAutoBeforeReplayRecovery(action, tabId);
    if (decision.executed === true) {
      await bdbCompleteReplayClaim(metadata.loopId, metadata.iteration);
      return decision;
    }

    if (decision.reason !== "replay_guard") {
      return decision;
    }

    const state = decision.state;
    if (
      state &&
      Number.isInteger(state.lastIteration) &&
      metadata.iteration <= state.lastIteration
    ) {
      return {
        ...decision,
        reason: "iteration_already_processed",
        expectedIteration: state.lastIteration + 1
      };
    }

    const replayKey = autoReplayKey(metadata.loopId, metadata.iteration);
    if (replayClaimsInFlight.has(replayKey)) {
      return {
        ...decision,
        reason: "iteration_in_progress"
      };
    }

    const record = await bdbReadReplayRecord(metadata.loopId, metadata.iteration);
    const status = bdbReplayRecordStatus(record);
    if (status === BDB_AUTO_REPLAY_STATUS_PROCESSING || status === "legacy") {
      return {
        ...decision,
        reason: "iteration_in_progress"
      };
    }
    return decision;
  } catch (error) {
    try {
      await bdbReleaseReplayClaim(metadata.loopId, metadata.iteration);
    } catch (_releaseError) {
      // Preserve the original failure. A bounded lease still permits recovery.
    }
    throw error;
  }
};
