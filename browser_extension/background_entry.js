"use strict";

// Keep the mature Native Messaging and promotion implementation in background.js.
// This entrypoint adds only the AUTO loop-state synchronization contract.
importScripts("background.js");

const BDB_AUTO_STATE_PREFIX = "bdbAuto:";
const legacyConsiderAuto = considerAuto;

function canonicalAutoStateKey(_tabId, loopId) {
  return `${BDB_AUTO_STATE_PREFIX}${loopId}`;
}

function isStoredAutoState(value) {
  return Boolean(
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Number.isFinite(value.startedAt) &&
    Number.isInteger(value.lastIteration) &&
    value.lastIteration >= 0 &&
    typeof value.status === "string"
  );
}

function autoStateTimestamp(state) {
  if (Number.isFinite(state.updatedAt)) {
    return state.updatedAt;
  }
  return Number.isFinite(state.startedAt) ? state.startedAt : 0;
}

function isLegacyAutoStateKey(key, loopId, canonicalKey) {
  if (key === canonicalKey || !key.startsWith(BDB_AUTO_STATE_PREFIX)) {
    return false;
  }
  const remainder = key.slice(BDB_AUTO_STATE_PREFIX.length);
  const separator = remainder.indexOf(":");
  if (separator <= 0) {
    return false;
  }
  const tabIdPart = remainder.slice(0, separator);
  const storedLoopId = remainder.slice(separator + 1);
  return /^\d+$/.test(tabIdPart) && storedLoopId === loopId;
}

function legacyAutoStateEntries(snapshot, loopId, canonicalKey) {
  return Object.entries(snapshot)
    .filter(([key, value]) => (
      isLegacyAutoStateKey(key, loopId, canonicalKey) &&
      isStoredAutoState(value)
    ));
}

function newestSafeAutoState(entries) {
  if (entries.length === 0) {
    return null;
  }
  return [...entries].sort((left, right) => {
    const iterationDifference = left[1].lastIteration - right[1].lastIteration;
    if (iterationDifference !== 0) {
      return iterationDifference;
    }
    return autoStateTimestamp(left[1]) - autoStateTimestamp(right[1]);
  }).at(-1);
}

async function synchronizeAutoState(loopId, tabId) {
  const canonicalKey = canonicalAutoStateKey(tabId, loopId);
  const snapshot = await chrome.storage.session.get(null);
  const legacyEntries = legacyAutoStateEntries(snapshot, loopId, canonicalKey);
  const canonical = snapshot[canonicalKey];

  let state = isStoredAutoState(canonical) ? { ...canonical } : null;
  if (!state) {
    const selected = newestSafeAutoState(legacyEntries);
    if (selected) {
      state = {
        ...selected[1],
        migratedFromTabState: true
      };
    }
  }

  if (!state) {
    return null;
  }

  const synchronized = {
    ...state,
    lastTabId: tabId,
    updatedAt: Date.now()
  };
  await chrome.storage.session.set({ [canonicalKey]: synchronized });

  const obsoleteKeys = legacyEntries.map(([key]) => key);
  if (obsoleteKeys.length > 0) {
    await chrome.storage.session.remove(obsoleteKeys);
  }
  return synchronized;
}

// background.js resolves this binding at execution time. Replacing only the key
// function keeps all existing bounds, terminal-state handling and replay claims.
autoStateKey = canonicalAutoStateKey;

considerAuto = async function synchronizedConsiderAuto(action, tabId) {
  const metadata = automationMetadata(action);
  if (metadata && Number.isInteger(tabId) && tabId >= 0) {
    await synchronizeAutoState(metadata.loopId, tabId);
  }

  const decision = await legacyConsiderAuto(action, tabId);
  if (!metadata) {
    return decision;
  }

  const key = canonicalAutoStateKey(tabId, metadata.loopId);
  const stored = await chrome.storage.session.get(key);
  const state = isStoredAutoState(stored[key])
    ? stored[key]
    : (isStoredAutoState(decision.state) ? decision.state : null);
  const expectedIteration = state
    ? state.lastIteration + 1
    : metadata.iteration;

  let reason = decision.reason;
  if (
    decision.executed === false &&
    reason === "non_sequential_iteration" &&
    state &&
    metadata.iteration <= state.lastIteration
  ) {
    reason = "iteration_already_processed";
  }

  return {
    ...decision,
    ...(reason ? { reason } : {}),
    expectedIteration
  };
};
