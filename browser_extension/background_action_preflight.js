"use strict";

const BDB_PREFLIGHT_MUTATING_OPERATIONS = new Set([
  "replace_exact_and_test",
  "multi_file_patch"
]);
const submitActionBeforePreflight = submitAction;

function bdbPreflightError(code, detail) {
  const error = new Error(`${code}: ${detail}`);
  error.bdbCode = code;
  error.bdbDetails = {
    rule_id: `action_preflight.${code}`,
    phase: "client_preflight",
    effect_started: false
  };
  return error;
}

function bdbRequireObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw bdbPreflightError("invalid_payload", `${label} must be an object`);
  }
  return value;
}

function bdbRequirePath(value, label) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.startsWith("./") ||
    value.startsWith("/") ||
    value.includes("\\") ||
    value.includes("\0") ||
    value.split("/").some((part) => part === "" || part === "." || part === "..")
  ) {
    throw bdbPreflightError("unsafe_path", `${label} is not a safe repository-relative POSIX path`);
  }
  return value;
}

function bdbFnmatchRegex(pattern) {
  let source = "";
  for (const character of pattern) {
    if (character === "*") {
      source += ".*";
    } else if (character === "?") {
      source += ".";
    } else {
      source += character.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
    }
  }
  return new RegExp(`^${source}$`);
}

function bdbPathMatches(path, patterns) {
  return patterns.some((pattern) => typeof pattern === "string" && bdbFnmatchRegex(pattern).test(path));
}

function bdbCanonicalBase64Bytes(value, label) {
  if (typeof value !== "string") {
    throw bdbPreflightError("invalid_payload", `${label} must be a string`);
  }
  let decoded;
  try {
    decoded = atob(value);
  } catch (_error) {
    throw bdbPreflightError("invalid_payload", `${label} is not canonical base64`);
  }
  if (btoa(decoded) !== value) {
    throw bdbPreflightError("invalid_payload", `${label} has noncanonical padding`);
  }
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

async function bdbSha256(bytes) {
  if (!crypto.subtle || typeof crypto.subtle.digest !== "function") {
    throw bdbPreflightError("preflight_unavailable", "Web Crypto SHA-256 is unavailable");
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return `sha256:${Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function bdbOperationPaths(operation, index) {
  const paths = [];
  for (const key of ["path", "source_path", "destination_path"]) {
    if (operation[key] !== undefined && operation[key] !== null) {
      paths.push({ key, path: bdbRequirePath(operation[key], `operations[${index}].${key}`) });
    }
  }
  if (paths.length === 0) {
    throw bdbPreflightError("invalid_payload", `operations[${index}] has no repository path`);
  }
  return paths;
}

async function bdbPreflightEncodedContent(operation, index, path) {
  if (operation.content_base64 === undefined && operation.content_sha256 === undefined) {
    return;
  }
  const bytes = bdbCanonicalBase64Bytes(
    operation.content_base64,
    `operations[${index}].content_base64`
  );
  const declared = operation.content_sha256;
  if (typeof declared !== "string" || !/^sha256:[0-9a-f]{64}$/.test(declared)) {
    throw bdbPreflightError(
      "invalid_payload",
      `operations[${index}] (${path}) has an invalid content_sha256`
    );
  }
  const actual = await bdbSha256(bytes);
  if (actual !== declared) {
    throw bdbPreflightError(
      "invalid_payload",
      `operations[${index}] (${path}) content_sha256 mismatch; declared=${declared} actual=${actual}`
    );
  }
}

async function bdbAllowedPaths(repoAlias) {
  const response = await nativeContext(repoAlias);
  const context = response && response.context;
  const allowed = context && context.allowed_paths;
  if (!Array.isArray(allowed) || !allowed.every((item) => typeof item === "string")) {
    throw bdbPreflightError("preflight_unavailable", "Native context has no valid allowed_paths");
  }
  return allowed;
}

function bdbUtf8TextBytes(value, label) {
  let bytes = 0;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code <= 0x7f) {
      bytes += 1;
    } else if (code <= 0x7ff) {
      bytes += 2;
    } else if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (index + 1 >= value.length || next < 0xdc00 || next > 0xdfff) {
        throw bdbPreflightError("invalid_payload", `${label} must contain valid UTF-8 text`);
      }
      bytes += 4;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      throw bdbPreflightError("invalid_payload", `${label} must contain valid UTF-8 text`);
    } else {
      bytes += 3;
    }
  }
  return bytes;
}

function bdbPreflightTextReplacement(operation, index) {
  const label = `operations[${index}]`;
  const allowedKeys = new Set(["schema", "kind", "path", "expected_sha256", "replacements"]);
  for (const key of Object.keys(operation)) {
    if (!allowedKeys.has(key)) {
      throw bdbPreflightError("invalid_payload", `${label} has unsupported field: ${key}`);
    }
  }
  if (operation.schema !== "bdb-text-replacement-v1") {
    throw bdbPreflightError("unsupported_schema", `${label} must use bdb-text-replacement-v1`);
  }
  if (operation.kind !== "replace_exact_text") {
    throw bdbPreflightError("invalid_payload", `${label}.kind must be replace_exact_text`);
  }
  if (typeof operation.expected_sha256 !== "string" || !/^sha256:[0-9a-f]{64}$/.test(operation.expected_sha256)) {
    throw bdbPreflightError("invalid_payload", `${label} has an invalid expected_sha256`);
  }
  if (!Array.isArray(operation.replacements) || operation.replacements.length < 1 || operation.replacements.length > 64) {
    throw bdbPreflightError("invalid_payload", `${label}.replacements must contain 1-64 items`);
  }

  const seen = new Set();
  let suppliedTextBytes = 0;
  for (let replacementIndex = 0; replacementIndex < operation.replacements.length; replacementIndex += 1) {
    const replacementLabel = `${label}.replacements[${replacementIndex}]`;
    const replacement = bdbRequireObject(operation.replacements[replacementIndex], replacementLabel);
    const replacementKeys = Object.keys(replacement);
    if (replacementKeys.length !== 2 || !replacementKeys.includes("old") || !replacementKeys.includes("new")) {
      throw bdbPreflightError("invalid_payload", `${replacementLabel} must contain only old and new`);
    }
    if (typeof replacement.old !== "string" || replacement.old.length === 0) {
      throw bdbPreflightError("invalid_payload", `${replacementLabel}.old must be a non-empty string`);
    }
    if (typeof replacement.new !== "string") {
      throw bdbPreflightError("invalid_payload", `${replacementLabel}.new must be a string`);
    }
    if (seen.has(replacement.old)) {
      throw bdbPreflightError("invalid_payload", `${replacementLabel}.old duplicates an earlier replacement`);
    }
    seen.add(replacement.old);
    suppliedTextBytes += bdbUtf8TextBytes(replacement.old, `${replacementLabel}.old`);
    suppliedTextBytes += bdbUtf8TextBytes(replacement.new, `${replacementLabel}.new`);
  }
  if (suppliedTextBytes > 256 * 1024) {
    throw bdbPreflightError("invalid_payload", `${label} exceeds the 256 KiB supplied-text limit`);
  }
  return operation.replacements.length;
}

async function bdbPreflightMultiFilePatch(action, allowedPaths) {
  const payload = bdbRequireObject(action.payload, "action.payload");
  const patch = bdbRequireObject(payload.patch, "action.payload.patch");
  if (patch.schema !== "bdb-multi-file-patch-v1") {
    throw bdbPreflightError("unsupported_schema", "action.payload.patch must use bdb-multi-file-patch-v1");
  }
  if (!Array.isArray(patch.operations) || patch.operations.length === 0 || patch.operations.length > 100) {
    throw bdbPreflightError("invalid_payload", "action.payload.patch.operations must contain 1-100 items");
  }

  let textEditOperations = 0;
  let textReplacementCount = 0;
  for (let index = 0; index < patch.operations.length; index += 1) {
    const operation = bdbRequireObject(patch.operations[index], `operations[${index}]`);
    if (operation.schema === "bdb-text-replacement-v1" || operation.kind === "replace_exact_text") {
      textEditOperations += 1;
      textReplacementCount += bdbPreflightTextReplacement(operation, index);
      if (textEditOperations > 32) {
        throw bdbPreflightError("invalid_payload", "A patch may contain at most 32 text edit operations");
      }
      if (textReplacementCount > 64) {
        throw bdbPreflightError("invalid_payload", "A patch may contain at most 64 exact text replacements");
      }
    }

    const paths = bdbOperationPaths(operation, index);
    for (const item of paths) {
      if (!bdbPathMatches(item.path, allowedPaths)) {
        throw bdbPreflightError(
          "policy_denied",
          `Path is not allowed by local policy: ${item.path}`
        );
      }
    }
    await bdbPreflightEncodedContent(operation, index, paths[0].path);
  }
}

async function bdbPreflightReplaceExact(action, allowedPaths) {
  const payload = bdbRequireObject(action.payload, "action.payload");
  const path = bdbRequirePath(payload.path, "action.payload.path");
  if (!bdbPathMatches(path, allowedPaths)) {
    throw bdbPreflightError("policy_denied", `Path is not allowed by local policy: ${path}`);
  }

  const hasBatch = payload.replacements !== undefined;
  if (hasBatch && (payload.old !== undefined || payload.new !== undefined)) {
    throw bdbPreflightError("invalid_payload", "Use either old/new or replacements, not both");
  }
  const replacements = hasBatch
    ? payload.replacements
    : [{ old: payload.old, new: payload.new }];
  if (!Array.isArray(replacements) || replacements.length < 1 || replacements.length > 16) {
    throw bdbPreflightError("invalid_payload", "replacements must contain 1-16 items");
  }
  for (let index = 0; index < replacements.length; index += 1) {
    const replacement = bdbRequireObject(replacements[index], `replacements[${index}]`);
    if (typeof replacement.old !== "string" || replacement.old.length === 0) {
      throw bdbPreflightError("invalid_payload", `replacements[${index}].old must be a non-empty string`);
    }
    if (typeof replacement.new !== "string") {
      throw bdbPreflightError("invalid_payload", `replacements[${index}].new must be a string`);
    }
  }

  for (let replacementIndex = 0; replacementIndex < replacements.length; replacementIndex += 1) {
    const oldText = replacements[replacementIndex].old;
    if (
      oldText.length > 200 ||
      oldText.includes("\0") ||
      oldText.includes("\r") ||
      oldText.includes("\n")
    ) {
      continue;
    }

    const searchResponse = await repositorySearch({
      schema: ACTION_SCHEMA,
      repo_alias: action.repo_alias,
      operation: SEARCH_TEXT_OPERATION,
      payload: {
        query: oldText,
        case_sensitive: true,
        max_results: 20
      },
      presentation: { mode: "compact" }
    });
    const searchResult = searchResponse && searchResponse.result;
    if (
      !searchResponse ||
      searchResponse.status !== "completed" ||
      !searchResult ||
      searchResult.status !== "success" ||
      !Array.isArray(searchResult.matches)
    ) {
      throw bdbPreflightError(
        "preflight_unavailable",
        "Exact-text scope search did not return a complete result"
      );
    }

    const contentMatches = searchResult.matches.filter((match) => (
      match && match.kind === "content" && typeof match.path === "string"
    ));
    const completeSingleTarget = Boolean(
      searchResult.truncated !== true &&
      searchResult.total_matches === 1 &&
      contentMatches.length === 1 &&
      contentMatches[0].path === path
    );
    if (searchResult.total_matches === 0 || completeSingleTarget) {
      continue;
    }

    const candidatePaths = [...new Set(contentMatches.map((match) => match.path))];
    return {
      schema: searchResponse.schema,
      host_version: searchResponse.host_version,
      request_id: searchResponse.request_id,
      status: "completed",
      repo_alias: action.repo_alias,
      result: {
        status: "scope_incomplete",
        operation: "replace_exact_scope_preflight",
        action_executed: false,
        target_path: path,
        replacement_index: replacementIndex,
        exact_occurrences: searchResult.total_matches,
        candidate_paths: candidatePaths,
        matches: contentMatches,
        base_sha: searchResult.base_sha,
        mirror_sync: searchResult.mirror_sync,
        changed_files: [],
        recommended_operation: "multi_file_patch",
        summary: "Exact old text exists in more than one repository location; inspect the candidates and patch every relevant runtime source in one action."
      }
    };
  }
  return null;
}

function bdbAcceptanceTouchedPaths(action) {
  const touched = new Set();
  if (action.operation === "multi_file_patch") {
    const patch = action && action.payload && action.payload.patch;
    const operations = patch && Array.isArray(patch.operations) ? patch.operations : [];
    for (let index = 0; index < operations.length; index += 1) {
      const operation = bdbRequireObject(operations[index], `operations[${index}]`);
      for (const item of bdbOperationPaths(operation, index)) {
        touched.add(item.path);
      }
    }
  } else if (action.operation === "replace_exact_and_test") {
    const payload = bdbRequireObject(action.payload, "action.payload");
    touched.add(bdbRequirePath(payload.path, "action.payload.path"));
  }
  return touched;
}

function bdbPreflightAcceptance(action, allowedPaths) {
  if (action.acceptance === undefined) {
    return;
  }
  const acceptance = bdbRequireObject(action.acceptance, "action.acceptance");
  const allowedKeys = new Set([
    "schema",
    "result_status",
    "changed_files_include",
    "promotion_required",
    "tests_required",
    "search_assertions",
    "manual_visual_confirmation_required"
  ]);
  for (const key of Object.keys(acceptance)) {
    if (!allowedKeys.has(key)) {
      throw bdbPreflightError("invalid_payload", `action.acceptance contains unsupported key: ${key}`);
    }
  }
  if (acceptance.schema !== "bdb-acceptance-v1") {
    throw bdbPreflightError("invalid_payload", "action.acceptance must use bdb-acceptance-v1");
  }
  if (acceptance.result_status !== undefined && acceptance.result_status !== "success") {
    throw bdbPreflightError("invalid_payload", "action.acceptance.result_status must be success");
  }
  for (const key of ["promotion_required", "tests_required", "manual_visual_confirmation_required"]) {
    if (acceptance[key] !== undefined && typeof acceptance[key] !== "boolean") {
      throw bdbPreflightError("invalid_payload", `action.acceptance.${key} must be boolean`);
    }
  }

  const touched = bdbAcceptanceTouchedPaths(action);
  const changed = acceptance.changed_files_include === undefined
    ? []
    : acceptance.changed_files_include;
  if (
    !Array.isArray(changed) ||
    changed.length > 32 ||
    !changed.every((value) => typeof value === "string")
  ) {
    throw bdbPreflightError(
      "invalid_payload",
      "action.acceptance.changed_files_include must contain at most 32 path strings"
    );
  }
  for (let index = 0; index < changed.length; index += 1) {
    const path = bdbRequirePath(changed[index], `action.acceptance.changed_files_include[${index}]`);
    if (!bdbPathMatches(path, allowedPaths)) {
      throw bdbPreflightError("policy_denied", `Acceptance path is not allowed by local policy: ${path}`);
    }
    if (!touched.has(path)) {
      throw bdbPreflightError(
        "invalid_payload",
        `action.acceptance.changed_files_include cannot be satisfied by this mutation: ${path}`
      );
    }
  }

  const assertions = acceptance.search_assertions === undefined
    ? []
    : acceptance.search_assertions;
  if (!Array.isArray(assertions) || assertions.length > 8) {
    throw bdbPreflightError(
      "invalid_payload",
      "action.acceptance.search_assertions must contain at most 8 items"
    );
  }
  for (let index = 0; index < assertions.length; index += 1) {
    const assertion = bdbRequireObject(
      assertions[index],
      `action.acceptance.search_assertions[${index}]`
    );
    const assertionKeys = new Set(["query", "path", "min_matches", "max_matches", "case_sensitive"]);
    for (const key of Object.keys(assertion)) {
      if (!assertionKeys.has(key)) {
        throw bdbPreflightError(
          "invalid_payload",
          `action.acceptance.search_assertions[${index}] contains unsupported key: ${key}`
        );
      }
    }
    if (
      typeof assertion.query !== "string" ||
      !assertion.query.trim() ||
      assertion.query.length > 200 ||
      assertion.query.includes("\0") ||
      assertion.query.includes("\r") ||
      assertion.query.includes("\n")
    ) {
      throw bdbPreflightError(
        "invalid_payload",
        `action.acceptance.search_assertions[${index}].query must contain 1-200 characters on one line`
      );
    }
    if (assertion.path !== undefined) {
      const path = bdbRequirePath(
        assertion.path,
        `action.acceptance.search_assertions[${index}].path`
      );
      if (!bdbPathMatches(path, allowedPaths)) {
        throw bdbPreflightError("policy_denied", `Acceptance search path is not allowed by local policy: ${path}`);
      }
    }
    if (assertion.case_sensitive !== undefined && typeof assertion.case_sensitive !== "boolean") {
      throw bdbPreflightError(
        "invalid_payload",
        `action.acceptance.search_assertions[${index}].case_sensitive must be boolean`
      );
    }
    for (const key of ["min_matches", "max_matches"]) {
      if (
        assertion[key] !== undefined &&
        (!Number.isSafeInteger(assertion[key]) || assertion[key] < 0)
      ) {
        throw bdbPreflightError(
          "invalid_payload",
          `action.acceptance.search_assertions[${index}].${key} must be a non-negative safe integer`
        );
      }
    }
    const minimum = assertion.min_matches === undefined ? 0 : assertion.min_matches;
    const maximum = assertion.max_matches === undefined ? Number.MAX_SAFE_INTEGER : assertion.max_matches;
    if (minimum > maximum) {
      throw bdbPreflightError(
        "invalid_payload",
        `action.acceptance.search_assertions[${index}] has min_matches greater than max_matches`
      );
    }
  }

  const hasMachineCheck = (
    acceptance.result_status !== undefined ||
    changed.length > 0 ||
    acceptance.promotion_required === true ||
    acceptance.tests_required === true ||
    assertions.length > 0
  );
  if (!hasMachineCheck) {
    throw bdbPreflightError(
      "invalid_payload",
      "action.acceptance must contain at least one machine-checkable completion criterion"
    );
  }
}

async function bdbPreflightAction(action) {
  if (!action || !BDB_PREFLIGHT_MUTATING_OPERATIONS.has(action.operation)) {
    return;
  }
  const repoAlias = validateRepoAlias(action.repo_alias);
  const allowedPaths = await bdbAllowedPaths(repoAlias);
  bdbPreflightAcceptance(action, allowedPaths);
  if (action.operation === "multi_file_patch") {
    await bdbPreflightMultiFilePatch(action, allowedPaths);
  } else if (action.operation === "replace_exact_and_test") {
    return bdbPreflightReplaceExact(action, allowedPaths);
  }
  return null;
}

submitAction = async function submitActionWithPreflight(action, tabId) {
  const preflightResponse = await bdbPreflightAction(action);
  if (preflightResponse) {
    return preflightResponse;
  }
  return submitActionBeforePreflight(action, tabId);
};
