"use strict";

const ACTION_SCHEMA = "bdb-action-v1";
const MAX_ACTION_TEXT = 1024 * 1024;
const processedBlocks = new WeakSet();

function parseAction(codeBlock) {
  const text = codeBlock.textContent || "";
  if (text.length === 0 || text.length > MAX_ACTION_TEXT || !text.includes(ACTION_SCHEMA)) {
    return null;
  }
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== ACTION_SCHEMA) {
      return null;
    }
    return value;
  } catch (_error) {
    return null;
  }
}

function compactAction(action) {
  const presentation = action && action.presentation;
  return Boolean(
    presentation &&
    typeof presentation === "object" &&
    !Array.isArray(presentation) &&
    presentation.mode === "compact"
  );
}


const BDB_ASSISTED_POLL_ATTEMPTS = 120;
const BDB_ASSISTED_POLL_DELAY_MS = 500;

function bdbAssistedSleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bdbAssistedResponsePending(response) {
  return Boolean(
    response &&
    (response.status === "accepted" || response.status === "pending")
  );
}

function bdbAssistedCommandId(response) {
  if (response && typeof response.command_id === "string") {
    return response.command_id;
  }
  const result = response && response.result;
  return result && typeof result.command_id === "string"
    ? result.command_id
    : null;
}

function bdbAssistedActionIdentity(action) {
  return JSON.stringify(action);
}

function bdbAssistedViews(actionIdentity) {
  const views = [];
  if (typeof actionIdentity !== "string" || actionIdentity.length === 0) {
    return views;
  }

  for (const block of document.querySelectorAll("pre code, code")) {
    if (!(block instanceof HTMLElement)) {
      continue;
    }
    const current = parseAction(block);
    if (!current || bdbAssistedActionIdentity(current) !== actionIdentity) {
      continue;
    }
    const host = block.closest("pre") || block.parentElement;
    if (!(host instanceof HTMLElement)) {
      continue;
    }
    const panel = host.querySelector(":scope > .bdb-assisted");
    if (!(panel instanceof HTMLElement)) {
      continue;
    }
    const viewButton = panel.querySelector(".bdb-execute");
    const viewOutput = panel.querySelector(".bdb-output");
    if (!(viewButton instanceof HTMLElement)) {
      continue;
    }
    views.push({
      button: viewButton,
      output: viewOutput instanceof HTMLElement ? viewOutput : null
    });
  }

  return views;
}

function bdbApplyAssistedViews(
  actionIdentity,
  callback,
  fallbackButton = null,
  fallbackOutput = null
) {
  const views = bdbAssistedViews(actionIdentity);
  if (views.length > 0) {
    for (const view of views) {
      callback(view);
    }
    return;
  }

  if (fallbackButton) {
    callback({
      button: fallbackButton,
      output: fallbackOutput
    });
  }
}

function bdbSetAssistedButtonState(
  actionIdentity,
  label,
  disabled,
  fallbackButton = null
) {
  bdbApplyAssistedViews(
    actionIdentity,
    ({ button: viewButton }) => {
      viewButton.textContent = label;
      viewButton.disabled = disabled;
    },
    fallbackButton
  );
}

function bdbRenderAssistedResult(
  actionIdentity,
  response,
  compact,
  fallbackButton,
  fallbackOutput
) {
  bdbApplyAssistedViews(
    actionIdentity,
    ({ output: viewOutput }) => {
      if (viewOutput) {
        renderResult(viewOutput, response, { compact });
      }
    },
    fallbackButton,
    fallbackOutput
  );
}

function bdbSetAssistedError(
  actionIdentity,
  message,
  label,
  disabled,
  fallbackButton,
  fallbackOutput
) {
  bdbApplyAssistedViews(
    actionIdentity,
    ({ button: viewButton, output: viewOutput }) => {
      if (viewOutput) {
        viewOutput.textContent = message;
      }
      viewButton.textContent = label;
      viewButton.disabled = disabled;
    },
    fallbackButton,
    fallbackOutput
  );
}

function bdbAssistedContextAvailable() {
  try {
    return Boolean(
      typeof chrome !== "undefined" &&
      chrome.runtime &&
      typeof chrome.runtime.id === "string" &&
      chrome.runtime.id.length > 0
    );
  } catch (_error) {
    return false;
  }
}

function bdbAssistedUncertainError(error) {
  const message = String(
    error && error.message ? error.message : error
  );
  return /Extension context invalidated|message port closed|Receiving end does not exist|before a response was received/i.test(
    message
  );
}

async function bdbAssistedMessage(message) {
  if (!bdbAssistedContextAvailable()) {
    throw new Error("Extension context invalidated");
  }

  const result = await chrome.runtime.sendMessage(message);
  if (!result || result.ok !== true) {
    throw new Error(
      result && result.error
        ? result.error
        : "Brak odpowiedzi rozszerzenia"
    );
  }
  return result.response;
}

async function bdbRunAssistedAction(action) {
  let latest = await bdbAssistedMessage({
    type: "BDB_SUBMIT_ASSISTED",
    action
  });

  if (!bdbAssistedResponsePending(latest)) {
    return latest;
  }

  const commandId = bdbAssistedCommandId(latest);
  if (!commandId) {
    return latest;
  }

  for (
    let attempt = 0;
    attempt < BDB_ASSISTED_POLL_ATTEMPTS;
    attempt += 1
  ) {
    await bdbAssistedSleep(BDB_ASSISTED_POLL_DELAY_MS);

    latest = await bdbAssistedMessage({
      type: "BDB_POLL_ASSISTED",
      action,
      commandId
    });

    if (!bdbAssistedResponsePending(latest)) {
      return latest;
    }
  }

  return {
    ...latest,
    async_poll_exhausted: true,
    command_id: bdbAssistedCommandId(latest) || commandId
  };
}

function bdbAssistedCompletionLabel(response) {
  const payload =
    response && response.result && typeof response.result === "object"
      ? response.result
      : response;
  return payload && payload.status === "success"
    ? "BDB: wykonano"
    : "BDB: zakończono";
}

function assistedElapsedLabel(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `BDB: wykonywanie… ${minutes}:${seconds}`;
}

function startAssistedProgress(button, actionIdentity = null) {
  const startedAt = Date.now();
  let active = true;

  const update = () => {
    if (!active) {
      return;
    }

    const label = assistedElapsedLabel(Date.now() - startedAt);
    if (actionIdentity) {
      bdbSetAssistedButtonState(
        actionIdentity,
        label,
        true,
        button
      );
      return;
    }

    button.textContent = label;
  };

  update();
  const timer = setInterval(update, 1000);

  return () => {
    if (!active) {
      return;
    }

    active = false;
    clearInterval(timer);
  };
}

function bdbResultPayload(response) {
  if (!response || typeof response !== "object" || Array.isArray(response)) {
    return response;
  }
  const result = response.result;
  const hasObjectResult = Boolean(
    result && typeof result === "object" && !Array.isArray(result)
  );
  return response.status === "failed" || !hasObjectResult ? response : result;
}

function resultText(response, marker = null) {
  const payload = bdbResultPayload(response);
  const prefix = marker ? `${marker}\n` : "";
  return `${prefix}BDB_RESULT:\n${JSON.stringify(payload, null, 2)}`;
}

const BDB_AUTO_CONTINUATION_TARGET_BYTES = 12 * 1024;
const BDB_AUTO_CONTINUATION_MAX_BYTES = 16 * 1024;
const BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES = 4 * 1024;
const BDB_COMPOSER_INSERT_MAX_BYTES = 64 * 1024;
const BDB_AUTO_TRACKED_PATH_LIMIT = 20;
const BDB_AUTO_SYMBOL_LIMIT = 8;
const BDB_AUTO_TEXT_TAIL_LIMIT = 1000;
const BDB_AUTO_READ_CONTENT_BYTES = 2200;
const BDB_AUTO_SEARCH_MATCH_LIMIT = 12;

function bdbUtf8ByteLength(value) {
  let bytes = 0;
  for (const character of String(value || "")) {
    const code = character.codePointAt(0);
    if (code <= 0x7f) bytes += 1;
    else if (code <= 0x7ff) bytes += 2;
    else if (code <= 0xffff) bytes += 3;
    else bytes += 4;
  }
  return bytes;
}

function bdbAutoTail(value, limit = BDB_AUTO_TEXT_TAIL_LIMIT) {
  if (typeof value !== "string") return value;
  return value.length <= limit ? value : value.slice(-limit);
}

function bdbAutoHeadBytes(value, maxBytes) {
  if (typeof value !== "string") return value;
  let bytes = 0;
  let output = "";
  for (const character of value) {
    const code = character.codePointAt(0);
    const width = code <= 0x7f ? 1 : (code <= 0x7ff ? 2 : (code <= 0xffff ? 3 : 4));
    if (bytes + width > maxBytes) break;
    output += character;
    bytes += width;
  }
  return output;
}

function bdbAutoOpenReadPayload(payload) {
  const data = payload && payload.data && typeof payload.data === "object"
    ? payload.data
    : {};
  const content = bdbAutoHeadBytes(data.content || "", BDB_AUTO_READ_CONTENT_BYTES);
  return {
    status: payload && payload.status,
    operation: "open_read",
    command_id: payload && payload.command_id,
    path: data.path,
    start_line: data.start_line,
    end_line: data.end_line,
    total_lines: data.total_lines,
    content,
    content_sha256: data.content_sha256,
    file_sha256: data.file_sha256,
    returned_bytes: bdbUtf8ByteLength(content),
    file_bytes: data.file_bytes,
    changed_files: [],
    mirror_sync: payload && payload.mirror_sync,
    auto_payload: {
      bounded: true,
      reason: "open_read_compacted",
      original_returned_bytes: data.returned_bytes,
      content_truncated_for_auto: bdbUtf8ByteLength(data.content || "") > BDB_AUTO_READ_CONTENT_BYTES,
      note: "AUTO preserved a bounded beginning of the requested local line range."
    }
  };
}

function bdbAutoSearchTextPayload(payload) {
  const matches = Array.isArray(payload && payload.matches)
    ? payload.matches.slice(0, BDB_AUTO_SEARCH_MATCH_LIMIT)
    : [];
  return {
    status: payload && payload.status,
    operation: "search_text",
    query: payload && payload.query,
    case_sensitive: payload && payload.case_sensitive,
    matches,
    returned_matches: matches.length,
    total_matches: payload && payload.total_matches,
    truncated: Boolean(payload && (payload.truncated || payload.matches && payload.matches.length > matches.length)),
    scanned_files: payload && payload.scanned_files,
    skipped_files: payload && payload.skipped_files,
    base_sha: payload && payload.base_sha,
    changed_files: [],
    mirror_sync: payload && payload.mirror_sync,
    auto_payload: {
      bounded: true,
      reason: "search_text_compacted",
      note: "AUTO preserved bounded local search matches with file paths and line numbers."
    }
  };
}

function bdbAutoWorkspaceContextPayload(payload) {
  const context = payload && payload.context && typeof payload.context === "object"
    ? payload.context
    : {};
  const reducedContext = {};
  const preservedKeys = [
    "repo_alias",
    "repository_id",
    "base_sha",
    "session_clean",
    "source_clean",
    "controlled_clean",
    "source_changes",
    "source_changes_outside_scope",
    "source_changes_truncated",
    "initial_revision",
    "initial_state_hash",
    "max_sequence",
    "capabilities",
    "latest_promotion",
    "mirror_sync",
    "limits",
    "snapshot_bytes",
    "snapshot_truncated",
    "tracked_paths_truncated",
    "symbols_truncated"
  ];
  for (const key of preservedKeys) {
    if (Object.prototype.hasOwnProperty.call(context, key)) {
      reducedContext[key] = context[key];
    }
  }

  const snapshotFiles = Array.isArray(context.snapshot_files) ? context.snapshot_files : [];
  reducedContext.snapshot_paths = snapshotFiles
    .slice(0, 8)
    .map((file) => (file && typeof file === "object" && !Array.isArray(file) ? file.path : null))
    .filter((path) => typeof path === "string" && path.length > 0);
  reducedContext.snapshot_file_count = snapshotFiles.length;
  reducedContext.snapshot_paths_omitted_for_auto = Math.max(
    0,
    snapshotFiles.length - reducedContext.snapshot_paths.length
  );
  reducedContext.snapshot_contents_omitted_for_auto = snapshotFiles.some(
    (file) => file && typeof file === "object" && typeof file.content === "string"
  );

  const trackedPaths = Array.isArray(context.tracked_paths) ? context.tracked_paths : [];
  reducedContext.tracked_paths = trackedPaths.slice(0, BDB_AUTO_TRACKED_PATH_LIMIT);
  reducedContext.tracked_paths_total = trackedPaths.length;
  reducedContext.tracked_paths_omitted_for_auto = Math.max(
    0,
    trackedPaths.length - reducedContext.tracked_paths.length
  );

  const symbols = Array.isArray(context.symbols) ? context.symbols : [];
  reducedContext.symbols = symbols.slice(0, BDB_AUTO_SYMBOL_LIMIT);
  reducedContext.symbols_total = symbols.length;
  reducedContext.symbols_omitted_for_auto = Math.max(
    0,
    symbols.length - reducedContext.symbols.length
  );

  if (Array.isArray(context.skipped_files)) {
    reducedContext.skipped_files = context.skipped_files;
  }

  const reduced = {
    status: payload && payload.status,
    operation: payload && payload.operation,
    context: reducedContext,
    auto_payload: {
      bounded: true,
      reason: "workspace_context_compacted",
      note: "AUTO omitted snapshot contents and excess paths/symbols. Use open_read for exact file content."
    }
  };
  if (payload && Object.prototype.hasOwnProperty.call(payload, "arm")) {
    reduced.arm = payload.arm;
  }
  return reduced;
}

function bdbAutoFallbackPayload(payload, originalBytes) {
  const reduced = {};
  const keys = [
    "status",
    "operation",
    "command_id",
    "session_id",
    "sequence",
    "changed_files",
    "workspace_revision",
    "workspace_state_hash",
    "revision_after",
    "state_hash_after",
    "reason",
    "error",
    "error_code",
    "message",
    "stopReason",
    "arm",
    "mirror_sync",
    "verification",
    "acceptance",
    "task_guidance",
    "execution_cache"
  ];
  for (const key of keys) {
    if (payload && Object.prototype.hasOwnProperty.call(payload, key)) {
      reduced[key] = payload[key];
    }
  }
  if (payload && payload.promotion && typeof payload.promotion === "object") {
    const promotion = payload.promotion;
    reduced.promotion = {
      status: promotion.status,
      command_id: promotion.command_id,
      source_commit: promotion.source_commit,
      changed_files: promotion.changed_files,
      mirror_sync: promotion.mirror_sync
    };
  }
  for (const key of ["stdout_tail", "stderr_tail", "stdout", "stderr"]) {
    if (payload && Object.prototype.hasOwnProperty.call(payload, key)) {
      reduced[key] = bdbAutoTail(payload[key]);
    }
  }
  reduced.auto_payload = {
    bounded: true,
    reason: "generic_result_compacted",
    original_bytes: originalBytes,
    note: "AUTO compacted an oversized result. Request a focused read for omitted details."
  };
  return reduced;
}

function bdbAutoMirrorSummary(mirror) {
  if (!mirror || typeof mirror !== "object" || Array.isArray(mirror)) {
    return undefined;
  }
  return {
    status: mirror.status,
    phase: mirror.phase,
    pushed: mirror.pushed,
    local_head: mirror.local_head,
    remote_head_after: mirror.remote_head_after
  };
}

function bdbAutoPromotionSummary(promotion) {
  if (!promotion || typeof promotion !== "object" || Array.isArray(promotion)) {
    return undefined;
  }
  return {
    status: promotion.status,
    source_commit: promotion.source_commit,
    changed_files: promotion.changed_files,
    mirror_sync: bdbAutoMirrorSummary(promotion.mirror_sync)
  };
}

function bdbAutoTaskGuidanceSummary(guidance) {
  if (!guidance || typeof guidance !== "object" || Array.isArray(guidance)) {
    return undefined;
  }
  return {
    trace_id: guidance.trace_id,
    phase: guidance.phase,
    complexity: guidance.complexity,
    next_operation: guidance.next_operation,
    cache: guidance.cache
  };
}

function bdbAutoInspectBundlePayload(payload, profile = "rich") {
  const context = payload && payload.context && typeof payload.context === "object"
    ? payload.context
    : {};
  const searches = Array.isArray(payload && payload.searches) ? payload.searches : [];
  const reads = Array.isArray(payload && payload.reads) ? payload.reads : [];
  const profiles = {
    rich: {
      searchLimit: 8,
      matchesPerSearch: 3,
      totalMatches: 12,
      matchTextBytes: 220,
      readLimit: 6,
      readContentCount: 6,
      readContentBytes: 1200,
      treeLimit: 10,
      symbolLimit: 6
    },
    compact: {
      searchLimit: 8,
      matchesPerSearch: 2,
      totalMatches: 8,
      matchTextBytes: 120,
      readLimit: 6,
      readContentCount: 4,
      readContentBytes: 800,
      treeLimit: 0,
      symbolLimit: 4
    },
    tight: {
      searchLimit: 8,
      matchesPerSearch: 2,
      totalMatches: 8,
      matchTextBytes: 120,
      readLimit: 4,
      readContentCount: 1,
      readContentBytes: 320,
      treeLimit: 0,
      symbolLimit: 0
    },
    minimal: {
      searchLimit: 8,
      matchesPerSearch: 1,
      totalMatches: 8,
      matchTextBytes: 0,
      readLimit: 6,
      readContentCount: 0,
      readContentBytes: 0,
      treeLimit: 0,
      symbolLimit: 0
    }
  };
  const limits = profiles[profile] || profiles.tight;
  let matchesRemaining = limits.totalMatches;
  const renderedSearches = searches.slice(0, limits.searchLimit).map((search) => {
    const available = Array.isArray(search && search.matches) ? search.matches : [];
    const selected = available.slice(
      0,
      Math.max(0, Math.min(limits.matchesPerSearch, matchesRemaining))
    );
    matchesRemaining -= selected.length;
    return {
      query: search && search.query,
      total_matches: search && search.total_matches,
      truncated: Boolean(search && search.truncated),
      matches: selected.map((match) => ({
        kind: match && match.kind,
        path: match && match.path,
        line: match && match.line,
        ...(limits.matchTextBytes > 0 && typeof (match && match.text) === "string"
          ? { text: bdbAutoHeadBytes(match.text, limits.matchTextBytes) }
          : {})
      })),
      matches_omitted_for_auto: Math.max(0, available.length - selected.length)
    };
  });
  const renderedReads = reads.slice(0, limits.readLimit).map((read, index) => ({
    path: read && read.path,
    source: read && read.source,
    start_line: read && read.start_line,
    end_line: read && read.end_line,
    total_lines: read && read.total_lines,
    ...(index < limits.readContentCount && typeof (read && read.content) === "string"
      ? { content: bdbAutoHeadBytes(read.content, limits.readContentBytes) }
      : {}),
    content_sha256: read && read.content_sha256,
    file_sha256: read && read.file_sha256,
    returned_bytes: read && read.returned_bytes,
    requested_end_line: read && read.requested_end_line,
    range_complete: read && read.range_complete,
    file_has_more: read && read.file_has_more,
    truncated: read && read.truncated,
    error: read && read.error
  }));
  const symbols = Array.isArray(context.symbols)
    ? context.symbols.slice(0, limits.symbolLimit)
    : [];
  const tree = Array.isArray(payload && payload.tree)
    ? payload.tree.slice(0, limits.treeLimit)
    : [];
  if (profile === "minimal") {
    return {
      status: payload && payload.status,
      operation: "inspect_bundle",
      base_sha: payload && payload.base_sha,
      context: {
        source_clean: context.source_clean,
        controlled_clean: context.controlled_clean,
        source_changes_outside_scope: context.source_changes_outside_scope
      },
      mirror_sync: bdbAutoMirrorSummary(payload && payload.mirror_sync),
      searches: renderedSearches,
      searches_total: searches.length,
      reads: renderedReads.map((read) => ({
        path: read.path,
        start_line: read.start_line,
        end_line: read.end_line,
        truncated: read.truncated
      })),
      reads_total: reads.length,
      reads_truncated: Boolean(payload && payload.reads_truncated) || reads.length > renderedReads.length,
      task_guidance: bdbAutoTaskGuidanceSummary(payload && payload.task_guidance),
      auto_payload: {
        bounded: true,
        reason: "inspect_bundle_compacted",
        profile: "minimal",
        note: "AUTO preserved repository identity, query totals and bounded path locations."
      }
    };
  }
  return {
    status: payload && payload.status,
    operation: "inspect_bundle",
    base_sha: payload && payload.base_sha,
    response_profile: payload && payload.response_profile,
    result_bytes: payload && payload.result_bytes,
    performance: payload && payload.performance,
    mirror_sync: bdbAutoMirrorSummary(payload && payload.mirror_sync),
    context: {
      source_clean: context.source_clean,
      controlled_clean: context.controlled_clean,
      source_changes: Array.isArray(context.source_changes)
        ? context.source_changes.slice(0, 12)
        : context.source_changes,
      source_changes_truncated: context.source_changes_truncated,
      source_changes_outside_scope: context.source_changes_outside_scope,
      symbols,
      symbols_total: Array.isArray(context.symbols) ? context.symbols.length : 0,
      latest_promotion: bdbAutoPromotionSummary(context.latest_promotion)
    },
    tree,
    tree_total: Array.isArray(payload && payload.tree) ? payload.tree.length : 0,
    tree_truncated: Boolean(payload && payload.tree_truncated),
    tree_summary: payload && payload.tree_summary,
    searches: renderedSearches,
    searches_total: searches.length,
    searches_omitted_for_auto: Math.max(0, searches.length - renderedSearches.length),
    reads: renderedReads,
    reads_total: reads.length,
    reads_truncated: Boolean(payload && payload.reads_truncated) || reads.length > renderedReads.length,
    task_guidance: bdbAutoTaskGuidanceSummary(payload && payload.task_guidance),
    auto_payload: {
      bounded: true,
      reason: "inspect_bundle_compacted",
      profile,
      note: "One bounded reconnaissance result combines repository state, searches and exact file excerpts."
    }
  };
}

function bdbAutoResultCandidate(prefix, projected, pretty = true) {
  return `${prefix}BDB_RESULT:\n${JSON.stringify(projected, null, pretty ? 2 : 0)}`;
}

function autoResultText(
  response,
  marker = null,
  requestedMaxBytes = BDB_AUTO_CONTINUATION_MAX_BYTES
) {
  const payload = bdbResultPayload(response);
  const prefix = marker ? `${marker}\n` : "";
  const originalJson = JSON.stringify(payload, null, 2);
  const hardLimit = Number.isInteger(requestedMaxBytes)
    ? Math.max(1024, Math.min(requestedMaxBytes, BDB_AUTO_CONTINUATION_MAX_BYTES))
    : BDB_AUTO_CONTINUATION_MAX_BYTES;
  const targetLimit = Math.min(BDB_AUTO_CONTINUATION_TARGET_BYTES, hardLimit);
  let projected = payload;
  if (payload && payload.operation === "workspace_context" && payload.context) {
    projected = bdbAutoWorkspaceContextPayload(payload);
  } else if (payload && payload.data && payload.data.operation === "open_read") {
    projected = bdbAutoOpenReadPayload(payload);
  } else if (payload && payload.operation === "search_text") {
    projected = bdbAutoSearchTextPayload(payload);
  } else if (payload && payload.operation === "inspect_bundle") {
    projected = bdbAutoInspectBundlePayload(payload, "rich");
  }

  let text = bdbAutoResultCandidate(prefix, projected);
  if (bdbUtf8ByteLength(text) <= targetLimit) {
    return text;
  }

  if (payload && payload.operation === "inspect_bundle") {
    text = bdbAutoResultCandidate(prefix, projected, false);
    if (bdbUtf8ByteLength(text) <= hardLimit) {
      return text;
    }
    for (const profile of ["compact", "tight", "minimal"]) {
      const candidate = bdbAutoInspectBundlePayload(payload, profile);
      text = bdbAutoResultCandidate(prefix, candidate);
      if (bdbUtf8ByteLength(text) <= targetLimit) {
        return text;
      }
      text = bdbAutoResultCandidate(prefix, candidate, false);
      if (bdbUtf8ByteLength(text) <= hardLimit) {
        return text;
      }
    }
  }

  projected = bdbAutoFallbackPayload(payload, bdbUtf8ByteLength(originalJson));
  text = bdbAutoResultCandidate(prefix, projected);
  if (bdbUtf8ByteLength(text) <= hardLimit) {
    return text;
  }
  text = bdbAutoResultCandidate(prefix, projected, false);
  if (bdbUtf8ByteLength(text) <= hardLimit) {
    return text;
  }

  const minimal = {
    status: payload && payload.status,
    operation: payload && payload.operation,
    auto_payload: {
      bounded: true,
      reason: "minimal_result",
      original_bytes: bdbUtf8ByteLength(originalJson),
      note: "Result exceeded the AUTO composer limit. Request a focused read for details."
    }
  };
  return bdbAutoResultCandidate(prefix, minimal, false);
}

function resultSummary(response) {
  const payload = bdbResultPayload(response);
  if (response && response.status === "failed") {
    const code = response.error && typeof response.error.code === "string"
      ? response.error.code
      : "unknown_error";
    const message = response.error && typeof response.error.message === "string"
      ? response.error.message
      : null;
    return `BDB zakończył operację błędem: ${code}${message ? ` — ${message}` : ""}`;
  }
  if (payload && payload.acceptance && payload.acceptance.status === "unmet") {
    const failed = Array.isArray(payload.acceptance.checks)
      ? payload.acceptance.checks.filter((check) => check && check.passed !== true).length
      : 0;
    return `Operacja wykonana, ale ${failed || "niektóre"} kryteria ukończenia nie zostały spełnione.`;
  }
  if (payload && payload.acceptance && payload.acceptance.status === "passed") {
    return "Operacja i kryteria ukończenia zostały zweryfikowane.";
  }
  if (payload && payload.acceptance && payload.acceptance.status === "needs_confirmation") {
    return "Testy automatyczne przeszły. Sprawdź zmianę wizualnie w uruchomionej aplikacji.";
  }
  if (payload && payload.operation === "workspace_context" && payload.context) {
    const context = payload.context;
    const files = Array.isArray(context.tracked_paths) ? context.tracked_paths.length : 0;
    const snapshots = Array.isArray(context.snapshot_files) ? context.snapshot_files.length : 0;
    const symbols = Array.isArray(context.symbols) ? context.symbols.length : 0;
    return `Odczytano kontekst: ${files} plików, ${snapshots} treści, ${symbols} symboli.`;
  }
  if (payload && payload.status === "success") {
    const changed = Array.isArray(payload.changed_files) ? payload.changed_files.length : 0;
    const tests = payload.stdout_tail && typeof payload.stdout_tail === "string"
      ? payload.stdout_tail.trim().split("\n").slice(-1)[0]
      : null;
    if (changed > 0 && tests) {
      return `Zmieniono ${changed} plików. ${tests}`;
    }
    if (changed > 0) {
      return `Zmieniono ${changed} plików.`;
    }
    return "Operacja zakończona powodzeniem.";
  }
  if (response && response.status === "pending") {
    return "Operacja została przyjęta i nadal trwa.";
  }
  return `Stan: ${(response && response.status) || (payload && payload.status) || "wynik"}`;
}

async function writeClipboard(text) {
  if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_error) {
    return false;
  }
}

function findComposer() {
  const selectors = [
    "#prompt-textarea",
    "[data-testid='prompt-textarea']",
    "textarea[placeholder]"
  ];
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (node instanceof HTMLElement) {
      return node;
    }
  }
  return null;
}

function composerText(composer) {
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    return composer.value;
  }
  return composer.textContent || "";
}

function prepareContinuation(text, { requireEmpty = false, maxBytes = BDB_COMPOSER_INSERT_MAX_BYTES } = {}) {
  if (bdbUtf8ByteLength(text) > maxBytes) {
    return null;
  }
  const composer = findComposer();
  if (!composer) {
    return null;
  }
  if (requireEmpty && composerText(composer).trim() !== "") {
    return null;
  }
  composer.focus();
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    const prefix = composer.value ? `${composer.value}\n\n` : "";
    composer.value = `${prefix}${text}`;
    composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    return composer;
  }
  if (composer.isContentEditable) {
    if (requireEmpty) {
      composer.textContent = "";
    }
    const selection = window.getSelection();
    if (selection) {
      selection.selectAllChildren(composer);
      selection.collapseToEnd();
    }
    const insertion = requireEmpty ? text : `\n\n${text}`;
    const inserted = typeof document.execCommand === "function" && document.execCommand("insertText", false, insertion);
    if (!inserted) {
      composer.append(document.createTextNode(insertion));
      composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    }
    return composer;
  }
  return null;
}

async function autoSend(response, loopId, iteration) {
  const marker = `BDB_AUTO_RESULT:${loopId}:${iteration}`;
  const text = resultText(response, marker);
  const composer = prepareContinuation(text, { requireEmpty: true });
  if (!composer || !composerText(composer).includes(marker)) {
    return { sent: false, reason: "composer_unavailable_or_not_empty" };
  }
  const form = composer.closest("form");
  if (!form) {
    return { sent: false, reason: "composer_form_missing" };
  }
  let button = null;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const candidate = form.querySelector("button[data-testid='send-button']");
    if (candidate instanceof HTMLButtonElement && !candidate.disabled) {
      button = candidate;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (!button || !composerText(composer).includes(marker)) {
    return { sent: false, reason: "exact_send_button_unavailable" };
  }
  button.click();
  return { sent: true, reason: null };
}

function renderResult(container, response, { compact = false } = {}) {
  container.textContent = "";
  const status = document.createElement("div");
  status.className = "bdb-status";
  status.textContent = compact ? resultSummary(response) : `BDB: ${response.status || "wynik"}`;

  const pre = document.createElement("pre");
  pre.className = "bdb-result";
  pre.textContent = JSON.stringify(response, null, 2);

  const details = document.createElement("details");
  details.className = "bdb-details";
  const detailsSummary = document.createElement("summary");
  detailsSummary.textContent = "Szczegóły techniczne";
  details.append(detailsSummary, pre);
  if (!compact) {
    details.open = true;
  }

  const controls = document.createElement("div");
  controls.className = "bdb-controls";

  const continuation = typeof autoResultText === "function"
    ? autoResultText(response, null, BDB_AUTO_CONTINUATION_MAX_BYTES)
    : resultText(response);
  const continueButton = document.createElement("button");
  continueButton.type = "button";
  continueButton.textContent = "Przygotuj kontynuację";
  continueButton.addEventListener("click", async () => {
    const prepared = typeof bdbPrepareManualContinuation === "function"
      ? await bdbPrepareManualContinuation(continuation)
      : Boolean(prepareContinuation(continuation, {
        maxBytes: BDB_AUTO_CONTINUATION_MAX_BYTES
      }));
    if (prepared) {
      continueButton.textContent = "Wstawiono — wyślij ręcznie";
      return;
    }
    const copied = await writeClipboard(continuation);
    continueButton.textContent = copied ? "Skopiowano — wklej ręcznie" : "Nie udało się wstawić";
  });

  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.textContent = "Kopiuj wynik";
  copyButton.addEventListener("click", async () => {
    const copied = await writeClipboard(continuation);
    copyButton.textContent = copied ? "Skopiowano" : "Kopiowanie niedostępne";
  });

  controls.append(continueButton, copyButton);
  container.append(status, details, controls);
}

async function maybeAuto(action, button, output, compact) {
  const automation = action && action.automation;
  if (!automation || automation.mode !== "auto") {
    return;
  }
  button.disabled = true;
  button.textContent = "BDB AUTO: sprawdzanie…";
  try {
    const decision = await chrome.runtime.sendMessage({ type: "BDB_CONSIDER_AUTO", action });
    if (!decision || decision.ok !== true) {
      throw new Error(decision && decision.error ? decision.error : "Brak decyzji AUTO");
    }
    const auto = decision.response;
    if (!auto.executed) {
      button.textContent = `BDB: Wykonaj (${auto.reason || "ASSISTED"})`;
      return;
    }
    renderResult(output, auto.response, { compact });
    if (auto.resultDelivered === true) {
      button.textContent = `BDB AUTO: wynik odtworzony (${auto.stopReason || "zakończono"})`;
      return;
    }
    const sent = await autoSend(auto.response, auto.loopId, auto.iteration);
    if (sent.sent) {
      try {
        await chrome.runtime.sendMessage({
          type: "BDB_MARK_AUTO_RESULT_DELIVERED",
          loopId: auto.loopId,
          iteration: auto.iteration
        });
      } catch (_error) {
      }
      button.textContent = auto.shouldContinue
        ? `BDB AUTO: wysłano ${auto.iteration}`
        : `BDB AUTO: wynik wysłany; zatrzymano (${auto.stopReason || "zakończono"})`;
      return;
    }
    button.textContent = auto.shouldContinue
      ? `BDB AUTO → ASSISTED (${sent.reason})`
      : `BDB AUTO: zatrzymano; wynik oczekuje na ponowienie (${sent.reason})`;
  } catch (error) {
    output.textContent = `BDB AUTO error: ${String(error && error.message ? error.message : error)}`;
    button.textContent = "BDB AUTO → ASSISTED";
  } finally {
    button.disabled = false;
  }
}

function enhance(codeBlock, action) {
  const host = codeBlock.closest("pre") || codeBlock.parentElement;
  if (!(host instanceof HTMLElement) || host.querySelector(":scope > .bdb-assisted")) {
    return;
  }
  const compact = compactAction(action);
  if (compact) {
    codeBlock.classList.add("bdb-action-source-hidden");
    host.classList.add("bdb-compact-host");
  }

  const panel = document.createElement("div");
  panel.className = compact ? "bdb-assisted bdb-compact" : "bdb-assisted";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-execute";
  button.textContent = compact ? "BDB: uruchom zadanie" : "BDB: Wykonaj";

  const output = document.createElement("div");
  output.className = "bdb-output";

  button.addEventListener("click", async () => {
    button.disabled = true;
    output.textContent = "";

    let actionIdentity = null;
    let keepDisabled = false;
    let stopProgress = () => {};

    try {
      const currentAction = parseAction(codeBlock);
      if (!currentAction) {
        throw new Error("Blok BDB zmienił się lub nie jest już prawidłowym bdb-action-v1 JSON");
      }

      actionIdentity = bdbAssistedActionIdentity(currentAction);
      stopProgress = startAssistedProgress(button, actionIdentity);

      const response = await bdbRunAssistedAction(currentAction);

      if (bdbAssistedResponsePending(response)) {
        keepDisabled = true;
        bdbRenderAssistedResult(
          actionIdentity,
          response,
          compact,
          button,
          output
        );
        bdbSetAssistedButtonState(
          actionIdentity,
          "BDB: nadal trwa — nie ponawiaj",
          true,
          button
        );
        return;
      }

      bdbRenderAssistedResult(
        actionIdentity,
        response,
        compact,
        button,
        output
      );
      bdbSetAssistedButtonState(
        actionIdentity,
        bdbAssistedCompletionLabel(response),
        false,
        button
      );
    } catch (error) {
      const detail = String(
        error && error.message ? error.message : error
      );

      if (bdbAssistedUncertainError(error)) {
        keepDisabled = true;
        bdbSetAssistedError(
          actionIdentity,
          "BDB: połączenie z rozszerzeniem zostało przerwane. Operacja mogła zostać wykonana. Nie uruchamiaj jej ponownie; odśwież kartę i sprawdź wynik.",
          "BDB: sprawdź wynik — nie ponawiaj",
          true,
          button,
          output
        );
      } else {
        bdbSetAssistedError(
          actionIdentity,
          `BDB error: ${detail}`,
          "BDB: ponów",
          false,
          button,
          output
        );
      }
    } finally {
      stopProgress();
      bdbApplyAssistedViews(
        actionIdentity,
        ({ button: viewButton }) => {
          viewButton.disabled = keepDisabled;
        },
        button,
        output
      );
    }
  });

  panel.append(button, output);
  host.append(panel);
  maybeAuto(action, button, output, compact);
}

function scan(root) {
  const blocks = [];
  if (root instanceof HTMLElement && root.matches("code")) {
    blocks.push(root);
  }
  if (root.querySelectorAll) {
    blocks.push(...root.querySelectorAll("pre code, code"));
  }
  for (const block of blocks) {
    if (!(block instanceof HTMLElement) || processedBlocks.has(block)) {
      continue;
    }
    const action = parseAction(block);
    if (!action) {
      continue;
    }
    processedBlocks.add(block);
    enhance(block, action);
  }
}

scan(document);
const observer = new MutationObserver((records) => {
  for (const record of records) {
    if (record.type === "characterData" && record.target.parentElement) {
      scan(record.target.parentElement);
    }
    for (const node of record.addedNodes) {
      if (node instanceof HTMLElement) {
        scan(node);
      } else if (node.parentElement) {
        scan(node.parentElement);
      }
    }
  }
});
observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
