"use strict";

const BDB_REPAIR_ACTIONS_KEY = "bdbRepairActionsV1";
const BDB_REPAIR_ACTION_LIMIT = 8;
const BDB_REPAIR_MUTATING_OPERATIONS = new Set([
  "replace_exact_and_test",
  "multi_file_patch"
]);
const submitActionBeforeRepairCorrelation = submitAction;

function bdbRepairDeepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function bdbRepairEntryKey(tabId, repoAlias) {
  return `${tabId}:${repoAlias}`;
}

function bdbRepairCommandId(response) {
  if (response && typeof response.command_id === "string") {
    return response.command_id;
  }
  const result = response && response.result;
  return result && typeof result.command_id === "string" ? result.command_id : null;
}

function bdbRepairSessionId(commandId) {
  if (typeof commandId !== "string") {
    return null;
  }
  const separator = commandId.lastIndexOf(":");
  return separator > 0 ? commandId.slice(0, separator) : null;
}

async function bdbRepairStoreEntry(key, entry) {
  const stored = await chrome.storage.local.get(BDB_REPAIR_ACTIONS_KEY);
  const raw = stored[BDB_REPAIR_ACTIONS_KEY];
  const entries = raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
  entries[key] = entry;
  const bounded = Object.entries(entries)
    .filter(([, value]) => value && typeof value === "object" && Number.isFinite(value.updated_at))
    .sort((left, right) => left[1].updated_at - right[1].updated_at)
    .slice(-BDB_REPAIR_ACTION_LIMIT);
  await chrome.storage.local.set({ [BDB_REPAIR_ACTIONS_KEY]: Object.fromEntries(bounded) });
}

async function bdbRepairLatestForRepo(repoAlias) {
  const stored = await chrome.storage.local.get(BDB_REPAIR_ACTIONS_KEY);
  const raw = stored[BDB_REPAIR_ACTIONS_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const candidates = Object.entries(raw)
    .filter(([, entry]) => (
      entry &&
      typeof entry === "object" &&
      entry.repo_alias === repoAlias &&
      Number.isFinite(entry.updated_at)
    ))
    .sort((left, right) => right[1].updated_at - left[1].updated_at);
  return candidates.length > 0 ? { key: candidates[0][0], entry: candidates[0][1] } : null;
}

async function bdbRepairEnrichAction(action) {
  if (!action || !BDB_REPAIR_MUTATING_OPERATIONS.has(action.operation)) {
    return action;
  }
  const enriched = bdbRepairDeepClone(action);
  if (typeof enriched.session_id === "string" && enriched.session_id.length > 0) {
    return enriched;
  }

  const repoAlias = enriched.repo_alias;
  const pending = typeof repoAlias === "string" ? await bdbRepairLatestForRepo(repoAlias) : null;
  const previous = pending && pending.entry;
  const previousAction = previous && previous.action;
  const previousCorrelation = previousAction && previousAction.repair_correlation;
  const previousCommandId = previous && bdbRepairCommandId(previous.response);
  const predecessorSessionId = bdbRepairSessionId(previousCommandId);
  const awaitingRepair = Boolean(previous && previous.awaiting_corrected_action === true);

  enriched.sequence = 1;
  enriched.expected_revision = 0;
  delete enriched.expected_state_hash;

  if (
    awaitingRepair &&
    predecessorSessionId &&
    previousCorrelation &&
    typeof previousCorrelation.correlation_id === "string"
  ) {
    enriched.session_id = crypto.randomUUID();
    enriched.repair_correlation = {
      schema: "bdb-repair-correlation-v1",
      correlation_id: previousCorrelation.correlation_id,
      role: "repair",
      predecessor_session_id: predecessorSessionId
    };
    return enriched;
  }

  // A preflight failure never reached Native Host, so its generated initial
  // session is still unused. Reuse that initial identity for the corrected action.
  if (
    awaitingRepair &&
    !previousCommandId &&
    previousAction &&
    typeof previousAction.session_id === "string" &&
    previousCorrelation &&
    previousCorrelation.role === "initial"
  ) {
    enriched.session_id = previousAction.session_id;
    enriched.repair_correlation = bdbRepairDeepClone(previousCorrelation);
    return enriched;
  }

  enriched.session_id = crypto.randomUUID();
  enriched.repair_correlation = {
    schema: "bdb-repair-correlation-v1",
    correlation_id: crypto.randomUUID(),
    role: "initial",
    predecessor_session_id: null
  };
  return enriched;
}

submitAction = async function submitActionWithRepairCorrelation(action, tabId) {
  const enriched = await bdbRepairEnrichAction(action);
  const mutating = Boolean(enriched && BDB_REPAIR_MUTATING_OPERATIONS.has(enriched.operation));
  const repoAlias = enriched && enriched.repo_alias;
  const key = mutating && typeof repoAlias === "string" && Number.isInteger(tabId)
    ? bdbRepairEntryKey(tabId, repoAlias)
    : null;
  const started = Date.now();

  if (key) {
    await bdbRepairStoreEntry(key, {
      schema: "bdb-repair-action-state-v1",
      repo_alias: repoAlias,
      tab_id: tabId,
      action: enriched,
      response: null,
      error: null,
      awaiting_corrected_action: false,
      created_at: started,
      updated_at: started
    });
  }

  try {
    const response = await submitActionBeforeRepairCorrelation(enriched, tabId);
    if (key) {
      await bdbRepairStoreEntry(key, {
        schema: "bdb-repair-action-state-v1",
        repo_alias: repoAlias,
        tab_id: tabId,
        action: enriched,
        response,
        error: null,
        awaiting_corrected_action: false,
        created_at: started,
        updated_at: Date.now()
      });
    }
    return response;
  } catch (error) {
    if (key) {
      await bdbRepairStoreEntry(key, {
        schema: "bdb-repair-action-state-v1",
        repo_alias: repoAlias,
        tab_id: tabId,
        action: enriched,
        response: null,
        error: String(error && error.message ? error.message : error),
        awaiting_corrected_action: false,
        created_at: started,
        updated_at: Date.now()
      });
    }
    throw error;
  }
};
