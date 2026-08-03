"use strict";

// A bounded, local-only control plane around the mature BDB transport. It keeps
// task progress and diagnostics out of the chat transcript, restores an
// undelivered AUTO result after a browser/service-worker restart, deduplicates
// exact actions against the same Git HEAD, and evaluates optional acceptance
// criteria after execution. It never enables AUTO and never bypasses policy,
// replay, iteration, time, promotion or high-risk gates.
const BDB_TASK_CONTROLLER_SCHEMA = "bdb-task-controller-v1";
const BDB_TASK_LEDGER_KEY = "bdbTaskLedgerV1";
const BDB_TASK_DIAGNOSTICS_KEY = "bdbAutoDiagnosticsV1";
const BDB_TASK_METRICS_KEY = "bdbTaskMetricsV1";
const BDB_TASK_CHECKPOINTS_KEY = "bdbTaskCheckpointsV1";
const BDB_TASK_CACHE_KEY = "bdbActionCacheV1";
const BDB_TASK_RELEASE_CHANNEL = "stable";
const BDB_TASK_MAX_LEDGER = 64;
const BDB_TASK_MAX_DIAGNOSTICS = 200;
const BDB_TASK_MAX_CHECKPOINTS = 16;
const BDB_TASK_MAX_CACHE_ENTRIES = 32;
const BDB_TASK_MAX_CHECKPOINT_BYTES = 160 * 1024;
const BDB_TASK_MAX_CACHE_BYTES = 160 * 1024;
const BDB_TASK_READ_CACHE_MS = 2 * 60 * 1000;
const BDB_TASK_MUTATION_DEDUP_MS = 5 * 60 * 1000;
const BDB_TASK_CHECKPOINT_MS = 24 * 60 * 60 * 1000;
const BDB_TASK_LOOP_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const BDB_TASK_READ_OPERATIONS = new Set([
  "workspace_context",
  "search_text",
  "inspect_bundle",
  "open_read"
]);
const BDB_TASK_MUTATING_OPERATIONS = new Set([
  "replace_exact_and_test",
  "multi_file_patch"
]);
const BDB_TASK_HIGH_RISK_KINDS = new Set([
  "delete_file",
  "move_file",
  "rename_file"
]);
const BDB_TASK_TERMINAL_STATUSES = new Set([
  "done",
  "needs_user",
  "policy_denied",
  "manual_reconciliation_required",
  "failed",
  "cancelled",
  "aborted"
]);
const BDB_TASK_BENIGN_REPEAT_STOPS = new Set([
  "iteration_already_processed",
  "iteration_in_progress",
  "replay_guard",
  "loop_not_running"
]);

const bdbSubmitActionBeforeTaskController = submitAction;
const bdbConsiderAutoBeforeTaskController = considerAuto;
const bdbMarkAutoResultDeliveredBeforeTaskController = markAutoResultDelivered;
let bdbTaskStorageChain = Promise.resolve();

function bdbTaskClone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function bdbTaskSerializedBytes(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function bdbTaskWithStorageLock(callback) {
  const run = bdbTaskStorageChain.then(callback, callback);
  bdbTaskStorageChain = run.catch(() => undefined);
  return run;
}

function bdbTaskSafeText(value, limit = 160) {
  if (typeof value !== "string") {
    return null;
  }
  const compact = value.replace(/[\r\n\t]+/g, " ").trim();
  return compact.length > limit ? `${compact.slice(0, limit - 1)}…` : compact;
}

function bdbTaskCanonical(value) {
  if (Array.isArray(value)) {
    return value.map(bdbTaskCanonical);
  }
  if (value && typeof value === "object") {
    const result = {};
    for (const key of Object.keys(value).sort()) {
      result[key] = bdbTaskCanonical(value[key]);
    }
    return result;
  }
  return value;
}

function bdbTaskFnv1a(value) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

async function bdbTaskFingerprint(value) {
  const text = JSON.stringify(bdbTaskCanonical(value));
  if (crypto.subtle && typeof crypto.subtle.digest === "function") {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return Array.from(new Uint8Array(digest), (item) => item.toString(16).padStart(2, "0")).join("");
  }
  return bdbTaskFnv1a(text);
}

function bdbTaskActionIdentity(action) {
  const copy = bdbTaskClone(action || {});
  for (const key of ["automation", "presentation", "acceptance", "task", "risk", "trace_id"] ) {
    delete copy[key];
  }
  return copy;
}

function bdbTaskNormalizeLoopId(raw, action) {
  if (typeof raw === "string" && BDB_TASK_LOOP_ID_RE.test(raw)) {
    return { loopId: raw, changed: false, reason: null };
  }
  const fallback = action && action.task && typeof action.task.id === "string"
    ? action.task.id
    : `${(action && action.repo_alias) || "bdb"}-${(action && action.operation) || "task"}-${bdbRandomUuid()}`;
  const source = typeof raw === "string" && raw.trim() ? raw : fallback;
  let normalized = source;
  try {
    normalized = normalized.normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
  } catch (_error) {
  }
  normalized = normalized
    .replace(/[^A-Za-z0-9._:-]+/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "")
    .replace(/-+/g, "-")
    .replace(/[-._:]+$/, "");
  if (!normalized) {
    normalized = `bdb-task-${bdbRandomUuid()}`;
  }
  if (normalized.length > 118) {
    normalized = `${normalized.slice(0, 109).replace(/[-._:]+$/, "")}-${bdbTaskFnv1a(source)}`;
  }
  if (!BDB_TASK_LOOP_ID_RE.test(normalized)) {
    normalized = `bdb-task-${bdbTaskFnv1a(source)}-${bdbRandomUuid().slice(0, 8)}`;
  }
  return {
    loopId: normalized,
    changed: normalized !== raw,
    reason: typeof raw === "string" && raw.trim() ? "unsafe_loop_id_normalized" : "loop_id_generated"
  };
}

function bdbTaskComplexity(action) {
  const operation = action && action.operation;
  const patch = action && action.payload && action.payload.patch;
  const operations = patch && Array.isArray(patch.operations) ? patch.operations.length : 0;
  const acceptance = action && action.acceptance;
  const assertions = acceptance && Array.isArray(acceptance.search_assertions)
    ? acceptance.search_assertions.length
    : 0;
  const searches = action && action.payload && Array.isArray(action.payload.searches)
    ? action.payload.searches.length
    : 0;
  const reads = action && action.payload && Array.isArray(action.payload.reads)
    ? action.payload.reads.length
    : 0;
  const iteration = action && action.automation && Number.isInteger(action.automation.iteration)
    ? action.automation.iteration
    : 1;
  let score = BDB_TASK_READ_OPERATIONS.has(operation) ? 1 : 3;
  score += Math.min(5, Math.ceil(operations / 3));
  score += Math.min(2, Math.ceil(assertions / 3));
  score += Math.min(2, Math.floor(searches / 4));
  score += Math.min(2, Math.floor(reads / 3));
  score += Math.min(6, Math.max(0, iteration - 2));
  return {
    score,
    class: score <= 2 ? "small" : (score <= 5 ? "medium" : "large"),
    suggested_iterations: score <= 2 ? 3 : (score <= 5 ? 6 : 8)
  };
}

function bdbTaskRisk(action) {
  if (!action || !BDB_TASK_MUTATING_OPERATIONS.has(action.operation)) {
    return { level: "read_only", reason: null };
  }
  const operations = action.payload && action.payload.patch && action.payload.patch.operations;
  const risky = Array.isArray(operations)
    ? operations.find((item) => item && BDB_TASK_HIGH_RISK_KINDS.has(item.kind))
    : null;
  if (risky) {
    return { level: "high", reason: `${risky.kind}_requires_assisted` };
  }
  return { level: "bounded_mutation", reason: null };
}

async function bdbTaskLedger() {
  const stored = await chrome.storage.local.get(BDB_TASK_LEDGER_KEY);
  const raw = stored[BDB_TASK_LEDGER_KEY];
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? bdbTaskClone(raw)
    : { schema: BDB_TASK_CONTROLLER_SCHEMA, tasks: {} };
}

async function bdbTaskUpsert(loopId, patch) {
  if (!BDB_TASK_LOOP_ID_RE.test(loopId)) {
    return null;
  }
  return bdbTaskWithStorageLock(async () => {
    const ledger = await bdbTaskLedger();
    const now = Date.now();
    const current = ledger.tasks[loopId] || {
      loop_id: loopId,
      created_at: now,
      last_iteration: 0,
      status: "running",
      operations: []
    };
    const requested = bdbTaskClone(patch);
    const forceStatus = requested.force_status === true;
    delete requested.force_status;
    if (Number.isInteger(requested.last_iteration)) {
      requested.last_iteration = Math.max(current.last_iteration || 0, requested.last_iteration);
    }
    if (Number.isInteger(requested.expected_iteration)) {
      requested.expected_iteration = Math.max(
        current.expected_iteration || 0,
        requested.expected_iteration
      );
    }
    const next = { ...current, ...requested, updated_at: now };
    if (
      !forceStatus &&
      BDB_TASK_TERMINAL_STATUSES.has(current.status) &&
      !BDB_TASK_TERMINAL_STATUSES.has(requested.status)
    ) {
      next.status = current.status;
    }
    if (requested.operation) {
      next.operations = [
        ...(Array.isArray(current.operations) ? current.operations : []),
        {
          iteration: requested.last_iteration || current.last_iteration || 0,
          operation: requested.operation,
          status: requested.last_operation_status || requested.status || "unknown",
          at: now
        }
      ].slice(-20);
      delete next.operation;
      delete next.last_operation_status;
    }
    ledger.tasks[loopId] = next;
    const retained = Object.entries(ledger.tasks)
      .sort((left, right) => (left[1].updated_at || 0) - (right[1].updated_at || 0))
      .slice(-BDB_TASK_MAX_LEDGER);
    ledger.tasks = Object.fromEntries(retained);
    await chrome.storage.local.set({ [BDB_TASK_LEDGER_KEY]: ledger });
    return next;
  });
}

async function bdbTaskRecordDiagnostic(event) {
  const safe = {
    at: Date.now(),
    event: bdbTaskSafeText(event.event, 64) || "unknown",
    loop_id: bdbTaskSafeText(event.loopId, 128),
    iteration: Number.isInteger(event.iteration) ? event.iteration : null,
    operation: bdbTaskSafeText(event.operation, 64),
    reason: bdbTaskSafeText(event.reason, 160),
    status: bdbTaskSafeText(event.status, 64),
    error_code: bdbTaskSafeText(event.errorCode, 64),
    detail: bdbTaskSafeText(event.detail, 300),
    duration_ms: Number.isFinite(event.durationMs) ? Math.max(0, Math.round(event.durationMs)) : null,
    tab_id: Number.isInteger(event.tabId) ? event.tabId : null,
    trace_id: bdbTaskSafeText(event.traceId, 160),
    extension_version: currentExtensionVersion(),
    cache: bdbTaskSafeText(event.cache, 32)
  };
  await bdbTaskWithStorageLock(async () => {
    const stored = await chrome.storage.local.get(BDB_TASK_DIAGNOSTICS_KEY);
    const diagnostics = Array.isArray(stored[BDB_TASK_DIAGNOSTICS_KEY])
      ? stored[BDB_TASK_DIAGNOSTICS_KEY]
      : [];
    diagnostics.push(safe);
    await chrome.storage.local.set({
      [BDB_TASK_DIAGNOSTICS_KEY]: diagnostics.slice(-BDB_TASK_MAX_DIAGNOSTICS)
    });
  });
}

function bdbTaskResponseError(response) {
  const result = response && response.result;
  const data = result && result.data;
  const error = response && response.error;
  const errorCode = (error && error.code) ||
    (data && data.terminal_error_code) ||
    (result && result.error_code) ||
    null;
  const detail = (data && data.terminal_detail) ||
    (error && error.message) ||
    (result && result.summary) ||
    null;
  if (!errorCode && !detail) {
    return null;
  }
  return {
    error_code: bdbTaskSafeText(errorCode, 64),
    detail: bdbTaskSafeText(detail, 300)
  };
}

async function bdbTaskMetric(name, amount = 1) {
  await bdbTaskWithStorageLock(async () => {
    const stored = await chrome.storage.local.get(BDB_TASK_METRICS_KEY);
    const metrics = stored[BDB_TASK_METRICS_KEY] && typeof stored[BDB_TASK_METRICS_KEY] === "object"
      ? { ...stored[BDB_TASK_METRICS_KEY] }
      : { schema: "bdb-task-metrics-v1", since: Date.now(), counters: {} };
    metrics.counters = metrics.counters && typeof metrics.counters === "object"
      ? { ...metrics.counters }
      : {};
    metrics.counters[name] = (Number(metrics.counters[name]) || 0) + amount;
    metrics.updated_at = Date.now();
    await chrome.storage.local.set({ [BDB_TASK_METRICS_KEY]: metrics });
  });
}

async function bdbTaskCompileAction(action) {
  if (!action || typeof action !== "object" || Array.isArray(action)) {
    return { action, compiler: { changed: false } };
  }
  const automation = action.automation;
  if (!automation || typeof automation !== "object" || automation.mode !== "auto") {
    return { action, compiler: { changed: false } };
  }
  const compiled = bdbTaskClone(action);
  const normalized = bdbTaskNormalizeLoopId(automation.loop_id, action);
  const ledger = await bdbTaskLedger();
  const task = ledger.tasks[normalized.loopId];
  const iteration = Number.isInteger(automation.iteration) && automation.iteration > 0
    ? automation.iteration
    : ((task && Number.isInteger(task.last_iteration) ? task.last_iteration : 0) + 1);
  compiled.automation = {
    ...compiled.automation,
    loop_id: normalized.loopId,
    iteration
  };
  const traceId = typeof compiled.trace_id === "string" && compiled.trace_id
    ? compiled.trace_id
    : `${normalized.loopId}:${iteration}`;
  compiled.trace_id = traceId;
  return {
    action: compiled,
    compiler: {
      changed: normalized.changed || iteration !== automation.iteration || traceId !== action.trace_id,
      loop_id_changed: normalized.changed,
      reason: normalized.reason,
      loop_id: normalized.loopId,
      iteration,
      trace_id: traceId
    }
  };
}

function bdbTaskResponseBaseSha(response) {
  const result = response && response.result;
  if (!result || typeof result !== "object") {
    return null;
  }
  if (typeof result.base_sha === "string") {
    return result.base_sha;
  }
  if (result.context && typeof result.context.base_sha === "string") {
    return result.context.base_sha;
  }
  if (result.data && typeof result.data.base_sha === "string") {
    return result.data.base_sha;
  }
  if (result.promotion && typeof result.promotion.source_commit === "string") {
    return result.promotion.source_commit;
  }
  if (result.verification && typeof result.verification.source_commit === "string") {
    return result.verification.source_commit;
  }
  return null;
}

async function bdbTaskCacheDocument() {
  const stored = await chrome.storage.session.get(BDB_TASK_CACHE_KEY);
  const raw = stored[BDB_TASK_CACHE_KEY];
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? bdbTaskClone(raw)
    : { schema: "bdb-action-cache-v1", entries: {} };
}

async function bdbTaskCacheLookup(action, fingerprint) {
  const cache = await bdbTaskCacheDocument();
  const entry = cache.entries[fingerprint];
  if (!entry || typeof entry !== "object") {
    return null;
  }
  const mutating = BDB_TASK_MUTATING_OPERATIONS.has(action.operation);
  const ttl = mutating ? BDB_TASK_MUTATION_DEDUP_MS : BDB_TASK_READ_CACHE_MS;
  if (Date.now() - entry.created_at > ttl || typeof entry.base_sha !== "string") {
    return null;
  }
  let current;
  try {
    current = await nativeContext(action.repo_alias);
  } catch (_error) {
    return null;
  }
  if (!current || !current.context || current.context.base_sha !== entry.base_sha) {
    return null;
  }
  const response = bdbTaskClone(entry.response);
  if (response && response.result && typeof response.result === "object") {
    response.result.execution_cache = {
      schema: "bdb-execution-cache-v1",
      status: "hit",
      deduplicated: mutating,
      base_sha: entry.base_sha,
      age_ms: Date.now() - entry.created_at
    };
  }
  return response;
}

async function bdbTaskCacheStore(action, fingerprint, response) {
  if (!response || response.status !== "completed") {
    return;
  }
  const baseSha = bdbTaskResponseBaseSha(response);
  if (typeof baseSha !== "string" || bdbTaskSerializedBytes(response) > BDB_TASK_MAX_CACHE_BYTES) {
    return;
  }
  const cache = await bdbTaskCacheDocument();
  cache.entries[fingerprint] = {
    created_at: Date.now(),
    base_sha: baseSha,
    operation: action.operation,
    response: bdbTaskClone(response)
  };
  cache.entries = Object.fromEntries(
    Object.entries(cache.entries)
      .sort((left, right) => left[1].created_at - right[1].created_at)
      .slice(-BDB_TASK_MAX_CACHE_ENTRIES)
  );
  await chrome.storage.session.set({ [BDB_TASK_CACHE_KEY]: cache });
}

function bdbTaskChangedFiles(response) {
  const result = response && response.result;
  if (!result || typeof result !== "object") {
    return [];
  }
  if (Array.isArray(result.changed_files)) {
    return result.changed_files.filter((item) => typeof item === "string");
  }
  if (result.promotion && Array.isArray(result.promotion.changed_files)) {
    return result.promotion.changed_files.filter((item) => typeof item === "string");
  }
  return [];
}

async function bdbTaskEvaluateAcceptance(action, response) {
  const acceptance = action && action.acceptance;
  if (!acceptance || typeof acceptance !== "object" || Array.isArray(acceptance)) {
    return null;
  }
  const checks = [];
  const add = (name, passed, detail) => checks.push({ name, passed, detail });
  if (acceptance.schema !== "bdb-acceptance-v1") {
    add("schema", false, "acceptance must use bdb-acceptance-v1");
  }
  const result = response && response.result && typeof response.result === "object"
    ? response.result
    : {};
  if (typeof acceptance.result_status === "string") {
    add(
      "result_status",
      result.status === acceptance.result_status,
      `expected=${acceptance.result_status} actual=${result.status || "missing"}`
    );
  }
  const changed = bdbTaskChangedFiles(response);
  if (Array.isArray(acceptance.changed_files_include)) {
    for (const path of acceptance.changed_files_include.slice(0, 32)) {
      add(`changed:${path}`, changed.includes(path), changed.includes(path) ? "present" : "missing");
    }
  }
  if (acceptance.promotion_required === true) {
    const promoted = Boolean(result.promotion && result.promotion.status === "promoted");
    add("promotion", promoted, promoted ? "promoted" : "promotion missing");
  }
  if (acceptance.tests_required === true) {
    const verified = Boolean(
      result.verification &&
      result.verification.tests &&
      result.verification.tests.status === "success"
    );
    add("tests", verified, verified ? "verified" : "verified successful tests missing");
  }
  const assertions = Array.isArray(acceptance.search_assertions)
    ? acceptance.search_assertions.slice(0, 8)
    : [];
  for (let index = 0; index < assertions.length; index += 1) {
    const assertion = assertions[index];
    if (!assertion || typeof assertion !== "object" || typeof assertion.query !== "string") {
      add(`search:${index}`, false, "invalid search assertion");
      continue;
    }
    const payload = {
      query: assertion.query,
      case_sensitive: assertion.case_sensitive === true,
      max_results: 20
    };
    if (typeof assertion.path === "string" && assertion.path) {
      payload.path_prefixes = [assertion.path];
    }
    try {
      const searchResponse = await repositorySearch({
        schema: ACTION_SCHEMA,
        repo_alias: action.repo_alias,
        operation: SEARCH_TEXT_OPERATION,
        payload,
        presentation: { mode: "compact" }
      });
      const search = searchResponse && searchResponse.result;
      const count = search && Number.isInteger(search.total_matches) ? search.total_matches : -1;
      const minimum = Number.isInteger(assertion.min_matches) ? assertion.min_matches : 0;
      const maximum = Number.isInteger(assertion.max_matches) ? assertion.max_matches : Number.MAX_SAFE_INTEGER;
      const passed = count >= minimum && count <= maximum;
      add(
        `search:${index}`,
        passed,
        `query=${JSON.stringify(assertion.query)} count=${count} expected=${minimum}..${maximum}`
      );
    } catch (error) {
      add(`search:${index}`, false, `search failed: ${bdbTaskSafeText(String(error), 120)}`);
    }
  }
  const passed = checks.length > 0 && checks.every((check) => check.passed);
  const needsVisualConfirmation = acceptance.manual_visual_confirmation_required === true && passed;
  return {
    schema: "bdb-acceptance-result-v1",
    status: needsVisualConfirmation ? "needs_confirmation" : (passed ? "passed" : "unmet"),
    checked_at: Date.now(),
    checks,
    recommended_operation: needsVisualConfirmation
      ? "manual_visual_confirmation"
      : (passed ? "complete" : "inspect_bundle_or_multi_file_patch"),
    ...(needsVisualConfirmation ? {
      confirmation: {
        kind: "visual",
        status: "required",
        instruction: "Sprawdź wynik w uruchomionej aplikacji. AUTO nie uzna zadania wizualnego za zakończone bez oceny człowieka."
      }
    } : {})
  };
}

function bdbTaskAttachGuidance(action, response, acceptance, cacheStatus) {
  const copy = bdbTaskClone(response);
  if (!copy || typeof copy !== "object") {
    return response;
  }
  const result = copy.result && typeof copy.result === "object" ? copy.result : {};
  const complexity = bdbTaskComplexity(action);
  const changedFiles = bdbTaskChangedFiles(copy);
  const nextOperation = acceptance
    ? acceptance.recommended_operation
    : (BDB_TASK_READ_OPERATIONS.has(action.operation)
      ? "multi_file_patch_or_focused_read"
      : "verify_acceptance");
  copy.result = {
    ...result,
    ...(acceptance ? { acceptance } : {}),
    task_guidance: {
      schema: "bdb-task-guidance-v1",
      trace_id: action.trace_id || null,
      phase: action.task && action.task.phase
        ? action.task.phase
        : (BDB_TASK_READ_OPERATIONS.has(action.operation) ? "analysis" : "implementation"),
      complexity,
      changed_files: changedFiles,
      next_operation: nextOperation,
      cache: cacheStatus
    }
  };
  return copy;
}

async function bdbTaskCheckpointStore(decision, action) {
  if (!decision || decision.executed !== true || !decision.response) {
    return;
  }
  if (bdbTaskSerializedBytes(decision.response) > BDB_TASK_MAX_CHECKPOINT_BYTES) {
    await bdbTaskRecordDiagnostic({
      event: "checkpoint_skipped",
      loopId: decision.loopId,
      iteration: decision.iteration,
      operation: action.operation,
      reason: "response_too_large"
    });
    return;
  }
  await bdbTaskWithStorageLock(async () => {
    const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
    const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY] && typeof stored[BDB_TASK_CHECKPOINTS_KEY] === "object"
      ? { ...stored[BDB_TASK_CHECKPOINTS_KEY] }
      : {};
    const key = `${decision.loopId}:${decision.iteration}`;
    checkpoints[key] = {
      created_at: Date.now(),
      loop_id: decision.loopId,
      iteration: decision.iteration,
      delivered: decision.resultDelivered === true,
      should_continue: decision.shouldContinue === true,
      stop_reason: decision.stopReason || null,
      state_status: decision.state && typeof decision.state.status === "string"
        ? decision.state.status
        : null,
      response: bdbTaskClone(decision.response)
    };
    const retained = Object.entries(checkpoints)
      .sort((left, right) => left[1].created_at - right[1].created_at)
      .slice(-BDB_TASK_MAX_CHECKPOINTS);
    await chrome.storage.local.set({ [BDB_TASK_CHECKPOINTS_KEY]: Object.fromEntries(retained) });
  });
}

async function bdbTaskCheckpointRestore(loopId, iteration) {
  const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
  const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY];
  const checkpoint = checkpoints && checkpoints[`${loopId}:${iteration}`];
  if (
    !checkpoint ||
    checkpoint.delivered === true ||
    Date.now() - checkpoint.created_at > BDB_TASK_CHECKPOINT_MS ||
    !checkpoint.response
  ) {
    return null;
  }
  return {
    executed: true,
    response: bdbTaskClone(checkpoint.response),
    loopId,
    iteration,
    recoveredResult: true,
    durableCheckpoint: true,
    resultDelivered: false,
    shouldContinue: checkpoint.should_continue === true,
    stopReason: checkpoint.stop_reason,
    state_status: checkpoint.state_status
  };
}

function bdbTaskCheckpointRuntimeStatus(checkpoint) {
  if (
    checkpoint &&
    typeof checkpoint.state_status === "string" &&
    checkpoint.state_status
  ) {
    return checkpoint.state_status;
  }
  const stopReason = checkpoint && typeof checkpoint.stop_reason === "string"
    ? checkpoint.stop_reason
    : (checkpoint && typeof checkpoint.stopReason === "string" ? checkpoint.stopReason : null);
  if (BDB_TASK_TERMINAL_STATUSES.has(stopReason)) {
    return stopReason;
  }
  if (stopReason === "result_not_completed") {
    return "needs_user";
  }
  return "running";
}

async function bdbTaskRestoreCheckpointState(loopId, iteration, tabId, checkpoint) {
  const status = bdbTaskCheckpointRuntimeStatus(checkpoint);
  if (Number.isInteger(tabId) && tabId >= 0) {
    const key = autoStateKey(tabId, loopId);
    const stored = await chrome.storage.session.get(key);
    const current = stored[key] && typeof stored[key] === "object"
      ? stored[key]
      : {};
    await chrome.storage.session.set({
      [key]: {
        ...current,
        lastIteration: Math.max(current.lastIteration || 0, iteration),
        lastResponse: bdbTaskClone(checkpoint.response),
        lastResponseIteration: iteration,
        lastResponseDelivered: false,
        status,
        updatedAt: Date.now()
      }
    });
  }
  await bdbTaskUpsert(loopId, {
    status,
    last_iteration: iteration,
    expected_iteration: iteration + 1,
    force_status: true
  });
  return status;
}

async function bdbTaskLatestPendingCheckpoint(loopId) {
  const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
  const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY];
  if (!checkpoints || typeof checkpoints !== "object" || Array.isArray(checkpoints)) {
    return null;
  }
  const pending = Object.values(checkpoints)
    .filter((checkpoint) => (
      checkpoint &&
      checkpoint.loop_id === loopId &&
      checkpoint.delivered !== true &&
      checkpoint.response &&
      Number.isInteger(checkpoint.iteration) &&
      Number.isFinite(checkpoint.created_at) &&
      Date.now() - checkpoint.created_at <= BDB_TASK_CHECKPOINT_MS
    ))
    .sort((left, right) => (
      (right.iteration - left.iteration) || (right.created_at - left.created_at)
    ));
  return pending.length > 0 ? bdbTaskClone(pending[0]) : null;
}

submitAction = async function submitActionWithTaskController(action, tabId) {
  const started = Date.now();
  const fingerprint = await bdbTaskFingerprint(bdbTaskActionIdentity(action));
  const cached = await bdbTaskCacheLookup(action, fingerprint);
  if (cached) {
    await bdbTaskMetric(BDB_TASK_MUTATING_OPERATIONS.has(action.operation) ? "deduplicated_mutations" : "cache_hits");
    await bdbTaskRecordDiagnostic({
      event: "action_reused",
      operation: action.operation,
      status: "completed",
      durationMs: Date.now() - started,
      tabId,
      traceId: action.trace_id,
      cache: "hit"
    });
    return bdbTaskAttachGuidance(action, cached, cached.result && cached.result.acceptance, "hit");
  }

  await bdbTaskMetric("cache_misses");
  let response = await bdbSubmitActionBeforeTaskController(action, tabId);
  const acceptance = await bdbTaskEvaluateAcceptance(action, response);
  response = bdbTaskAttachGuidance(action, response, acceptance, "miss");
  await bdbTaskCacheStore(action, fingerprint, response);
  await bdbTaskMetric("actions_executed");
  await bdbTaskRecordDiagnostic({
    event: "action_completed",
    operation: action.operation,
    status: response && response.status,
    errorCode: bdbTaskResponseError(response) && bdbTaskResponseError(response).error_code,
    detail: bdbTaskResponseError(response) && bdbTaskResponseError(response).detail,
    durationMs: Date.now() - started,
    tabId,
    traceId: action.trace_id,
    cache: "miss"
  });
  return response;
};

considerAuto = async function considerAutoWithTaskController(action, tabId) {
  const started = Date.now();
  const compiled = await bdbTaskCompileAction(action);
  const effective = compiled.action;
  const metadata = effective && effective.automation;
  const loopId = metadata && metadata.loop_id;
  const iteration = metadata && metadata.iteration;
  const operation = effective && effective.operation;
  const traceId = effective && effective.trace_id;

  if (metadata && metadata.mode === "auto") {
    const checkpoint = await bdbTaskCheckpointRestore(loopId, iteration);
    if (checkpoint) {
      const restoredStatus = await bdbTaskRestoreCheckpointState(
        loopId,
        iteration,
        tabId,
        checkpoint
      );
      await bdbTaskMetric("checkpoints_restored");
      await bdbTaskRecordDiagnostic({
        event: "checkpoint_restored",
        loopId,
        iteration,
        operation,
        status: restoredStatus,
        tabId,
        traceId
      });
      return { ...checkpoint, compiler: compiled.compiler };
    }

    const settings = await getAutoSettings();
    if (settings.autoEnabled && settings.autoShadowMode) {
      const shadow = {
        executed: false,
        reason: "shadow_mode",
        expectedIteration: iteration,
        shadow: {
          would_execute: true,
          risk: bdbTaskRisk(effective),
          complexity: bdbTaskComplexity(effective)
        },
        compiler: compiled.compiler
      };
      await bdbTaskMetric("shadow_decisions");
      await bdbTaskRecordDiagnostic({
        event: "auto_shadow_decision",
        loopId,
        iteration,
        operation,
        reason: "shadow_mode",
        tabId,
        traceId
      });
      return shadow;
    }

    const risk = bdbTaskRisk(effective);
    if (settings.autoEnabled && risk.level === "high") {
      await bdbTaskMetric("high_risk_stops");
      await bdbTaskRecordDiagnostic({
        event: "auto_stopped",
        loopId,
        iteration,
        operation,
        reason: "high_risk_requires_assisted",
        tabId,
        traceId
      });
      return {
        executed: false,
        reason: "high_risk_requires_assisted",
        expectedIteration: iteration,
        risk,
        compiler: compiled.compiler
      };
    }
  }

  try {
    const ledgerBefore = loopId ? await bdbTaskLedger() : null;
    const taskBefore = ledgerBefore && ledgerBefore.tasks
      ? ledgerBefore.tasks[loopId]
      : null;
    const decision = await bdbConsiderAutoBeforeTaskController(effective, tabId);
    const replayed = Boolean(decision && decision.executed && decision.recoveredResult);
    const lastError = bdbTaskResponseError(decision && decision.response);
    const stopReason = decision && decision.reason;
    const repeatedBenignStop = Boolean(
      decision &&
      decision.executed !== true &&
      taskBefore &&
      (
        (
          BDB_TASK_BENIGN_REPEAT_STOPS.has(stopReason) &&
          Number.isInteger(iteration) &&
          iteration <= (taskBefore.last_iteration || 0)
        ) ||
        taskBefore.status === stopReason ||
        BDB_TASK_TERMINAL_STATUSES.has(taskBefore.status)
      )
    );
    if (decision && decision.executed === true) {
      await bdbTaskCheckpointStore(decision, effective);
      await bdbTaskMetric(replayed ? "auto_results_replayed" : "auto_executed");
    } else if (!repeatedBenignStop) {
      await bdbTaskMetric(`auto_stop_${(decision && decision.reason) || "unknown"}`);
    }
    if (loopId && !repeatedBenignStop) {
      const taskPatch = {
        title: bdbTaskSafeText(effective.task && effective.task.title, 120),
        phase: bdbTaskSafeText(effective.task && effective.task.phase, 64) || (BDB_TASK_READ_OPERATIONS.has(operation) ? "analysis" : "implementation"),
        repo_alias: effective.repo_alias,
        status: decision && decision.executed
          ? ((decision.state && decision.state.status) || (decision.shouldContinue ? "running" : "stopped"))
          : ((decision && decision.reason) || "stopped"),
        expected_iteration: decision && Number.isInteger(decision.expectedIteration)
          ? decision.expectedIteration
          : ((Number.isInteger(iteration) ? iteration : 0) + 1),
        trace_id: traceId,
        complexity: bdbTaskComplexity(effective),
        risk: bdbTaskRisk(effective),
        last_error: lastError,
        ...((decision && decision.executed === true && !replayed) ? {
          last_iteration: Number.isInteger(iteration) ? iteration : 0,
          operation,
          last_operation_status: "executed"
        } : {})
      };
      await bdbTaskUpsert(loopId, taskPatch);
    }
    if (!repeatedBenignStop) {
      await bdbTaskRecordDiagnostic({
        event: replayed ? "auto_result_replayed" : (decision && decision.executed ? "auto_executed" : "auto_stopped"),
        loopId,
        iteration,
        operation,
        reason: decision && decision.reason,
        status: decision && decision.executed ? "executed" : "assisted",
        errorCode: lastError && lastError.error_code,
        detail: lastError && lastError.detail,
        durationMs: Date.now() - started,
        tabId,
        traceId
      });
    }
    return { ...decision, compiler: compiled.compiler };
  } catch (error) {
    await bdbTaskMetric("auto_errors");
    await bdbTaskRecordDiagnostic({
      event: "auto_error",
      loopId,
      iteration,
      operation,
      reason: String(error && error.message ? error.message : error),
      status: "error",
      durationMs: Date.now() - started,
      tabId,
      traceId
    });
    throw error;
  }
};

markAutoResultDelivered = async function markAutoResultDeliveredWithTaskCheckpoint(loopId, iteration, tabId) {
  const result = await bdbMarkAutoResultDeliveredBeforeTaskController(loopId, iteration, tabId);
  if (result && result.marked) {
    await bdbTaskWithStorageLock(async () => {
      const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
      const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY] && typeof stored[BDB_TASK_CHECKPOINTS_KEY] === "object"
        ? { ...stored[BDB_TASK_CHECKPOINTS_KEY] }
        : {};
      const key = `${loopId}:${iteration}`;
      if (checkpoints[key]) {
        checkpoints[key] = { ...checkpoints[key], delivered: true, delivered_at: Date.now() };
        await chrome.storage.local.set({ [BDB_TASK_CHECKPOINTS_KEY]: checkpoints });
      }
    });
    await bdbTaskMetric("results_delivered");
    await bdbTaskRecordDiagnostic({
      event: "auto_result_delivered",
      loopId,
      iteration,
      status: "delivered",
      tabId
    });
  }
  return result;
};

async function bdbTaskSnapshot() {
  const ledger = await bdbTaskLedger();
  const tasks = Object.values(ledger.tasks)
    .sort((left, right) => (right.updated_at || 0) - (left.updated_at || 0));
  return {
    schema: "bdb-task-snapshot-v1",
    tasks,
    total: tasks.length
  };
}

async function bdbDiagnosticsSnapshot() {
  const stored = await chrome.storage.local.get([
    BDB_TASK_DIAGNOSTICS_KEY,
    BDB_TASK_METRICS_KEY
  ]);
  return {
    schema: "bdb-sanitized-browser-diagnostics-v1",
    generated_at: Date.now(),
    extension_version: currentExtensionVersion(),
    release_channel: BDB_TASK_RELEASE_CHANNEL,
    metrics: stored[BDB_TASK_METRICS_KEY] || null,
    events: Array.isArray(stored[BDB_TASK_DIAGNOSTICS_KEY])
      ? stored[BDB_TASK_DIAGNOSTICS_KEY].slice(-100)
      : [],
    tasks: (await bdbTaskSnapshot()).tasks.slice(0, 20),
    privacy: {
      source_code_included: false,
      action_payloads_included: false,
      credentials_included: false
    }
  };
}

async function bdbHealthSnapshot({ probeNative = false, contentVersion = null } = {}) {
  const settings = await getAutoSettings();
  let native = null;
  if (probeNative) {
    try {
      const response = await sendNative({
        schema: REQUEST_SCHEMA,
        request_id: requestId("health"),
        action: "status"
      });
      native = {
        status: response.status,
        host_version: response.host_version,
        armed: Boolean(response.arm && response.arm.armed)
      };
    } catch (error) {
      native = { status: "unavailable", error: bdbTaskSafeText(String(error), 180) };
    }
  }
  const extensionVersion = currentExtensionVersion();
  return {
    schema: "bdb-health-v1",
    status: native && native.status === "unavailable" ? "degraded" : "ready",
    extension_version: extensionVersion,
    content_version: typeof contentVersion === "string" ? contentVersion : null,
    content_version_match: typeof contentVersion === "string" ? contentVersion === extensionVersion : null,
    release_channel: BDB_TASK_RELEASE_CHANNEL,
    auto: settings,
    native,
    capabilities: {
      action_compiler: true,
      acceptance_checks: true,
      durable_resume: true,
      duplicate_guard: true,
      read_cache: true,
      risk_tiers: true,
      shadow_mode: true,
      sanitized_diagnostics: true
    }
  };
}

async function bdbCancelTask(loopId, tabId = null) {
  if (!BDB_TASK_LOOP_ID_RE.test(loopId || "")) {
    throw new Error("Task loop_id has an unsafe format");
  }
  const stored = await chrome.storage.session.get(null);
  const matching = Object.entries(stored).filter(([key, value]) => (
    key.startsWith("bdbAuto:") &&
    key.endsWith(`:${loopId}`) &&
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (!Number.isInteger(tabId) || key === autoStateKey(tabId, loopId))
  ));
  if (matching.length > 0) {
    await chrome.storage.session.set(Object.fromEntries(matching.map(([key, value]) => [
      key,
      { ...value, status: "cancelled", updatedAt: Date.now() }
    ])));
  }
  await bdbTaskUpsert(loopId, { status: "cancelled", force_status: true });
  await bdbTaskRecordDiagnostic({ event: "task_cancelled", loopId, status: "cancelled" });
  return { schema: "bdb-task-control-v1", loop_id: loopId, status: "cancelled" };
}

async function bdbResumeTask(loopId, tabId) {
  if (!BDB_TASK_LOOP_ID_RE.test(loopId || "")) {
    throw new Error("Task loop_id has an unsafe format");
  }
  const ledger = await bdbTaskLedger();
  const task = ledger.tasks[loopId];
  if (!task) {
    throw new Error("Task is not present in the durable ledger");
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("Task resume requires the active ChatGPT tab");
  }
  const pendingCheckpoint = await bdbTaskLatestPendingCheckpoint(loopId);
  if (pendingCheckpoint) {
    const restoredStatus = await bdbTaskRestoreCheckpointState(
      loopId,
      pendingCheckpoint.iteration,
      tabId,
      pendingCheckpoint
    );
    await bdbTaskMetric("checkpoint_recovery_requests");
    await bdbTaskRecordDiagnostic({
      event: "task_result_recovery_requested",
      loopId,
      iteration: pendingCheckpoint.iteration,
      status: restoredStatus,
      tabId
    });
    return {
      schema: "bdb-task-control-v1",
      loop_id: loopId,
      status: "recovering_result",
      task_status: restoredStatus,
      expected_iteration: pendingCheckpoint.iteration,
      recovery_only: true,
      instruction: "BDB odzyska zapisany wynik bez ponownego wykonania operacji."
    };
  }
  const allSession = await chrome.storage.session.get(null);
  const matchingStates = Object.entries(allSession)
    .filter(([key, value]) => (
      key.startsWith("bdbAuto:") &&
      key.endsWith(`:${loopId}`) &&
      value &&
      typeof value === "object" &&
      !Array.isArray(value)
    ))
    .map(([, value]) => value);
  const key = autoStateKey(tabId, loopId);
  const current = allSession[key] && typeof allSession[key] === "object"
    ? allSession[key]
    : {};
  const observedIterations = matchingStates
    .map((value) => value.lastIteration)
    .filter((value) => Number.isInteger(value));
  const lastIteration = Math.max(
    Number.isInteger(task.last_iteration) ? task.last_iteration : 0,
    ...observedIterations,
    0
  );
  const settings = await getAutoSettings();
  const iterationCeiling = lastIteration + settings.autoMaxIterations;
  await chrome.storage.session.set({
    [key]: {
      ...current,
      startedAt: Date.now(),
      lastIteration,
      status: "running",
      iterationCeiling,
      restoredFromTaskLedger: true,
      updatedAt: Date.now()
    }
  });
  await bdbTaskUpsert(loopId, {
    status: "running",
    expected_iteration: lastIteration + 1,
    force_status: true
  });
  await bdbTaskRecordDiagnostic({ event: "task_resumed", loopId, status: "running" });
  return {
    schema: "bdb-task-control-v1",
    loop_id: loopId,
    status: "running",
    expected_iteration: lastIteration + 1,
    allowed_through_iteration: iterationCeiling,
    instruction: "Reload the ChatGPT tab if its action panel is not visible."
  };
}

async function bdbClearReadCache() {
  await chrome.storage.session.remove(BDB_TASK_CACHE_KEY);
  await bdbTaskRecordDiagnostic({ event: "cache_cleared", status: "completed" });
  return { schema: "bdb-cache-control-v1", status: "cleared" };
}

async function bdbRecordContentEvent(event, tabId) {
  const value = event && typeof event === "object" && !Array.isArray(event) ? event : {};
  await bdbTaskRecordDiagnostic({
    event: value.event || "content_event",
    loopId: value.loopId,
    iteration: value.iteration,
    operation: value.operation,
    reason: value.reason,
    status: value.status,
    durationMs: value.durationMs,
    tabId,
    traceId: value.traceId
  });
  if (typeof value.metric === "string" && /^[a-z0-9_]{1,64}$/.test(value.metric)) {
    await bdbTaskMetric(value.metric);
  }
  return { recorded: true };
}

globalThis.bdbTaskSnapshot = bdbTaskSnapshot;
globalThis.bdbDiagnosticsSnapshot = bdbDiagnosticsSnapshot;
globalThis.bdbHealthSnapshot = bdbHealthSnapshot;
globalThis.bdbCancelTask = bdbCancelTask;
globalThis.bdbResumeTask = bdbResumeTask;
globalThis.bdbClearReadCache = bdbClearReadCache;
globalThis.bdbRecordContentEvent = bdbRecordContentEvent;
