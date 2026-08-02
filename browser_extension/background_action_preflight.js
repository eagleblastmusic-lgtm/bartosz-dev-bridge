"use strict";

const BDB_PREFLIGHT_MUTATING_OPERATIONS = new Set([
  "replace_exact_and_test",
  "multi_file_patch"
]);
const submitActionBeforePreflight = submitAction;

function bdbPreflightError(code, detail) {
  const error = new Error(`${code}: ${detail}`);
  error.bdbCode = code;
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

async function bdbPreflightMultiFilePatch(action, allowedPaths) {
  const payload = bdbRequireObject(action.payload, "action.payload");
  const patch = bdbRequireObject(payload.patch, "action.payload.patch");
  if (patch.schema !== "bdb-multi-file-patch-v1") {
    throw bdbPreflightError("unsupported_schema", "action.payload.patch must use bdb-multi-file-patch-v1");
  }
  if (!Array.isArray(patch.operations) || patch.operations.length === 0) {
    throw bdbPreflightError("invalid_payload", "action.payload.patch.operations must be a non-empty list");
  }

  for (let index = 0; index < patch.operations.length; index += 1) {
    const operation = bdbRequireObject(patch.operations[index], `operations[${index}]`);
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

async function bdbPreflightAction(action) {
  if (!action || !BDB_PREFLIGHT_MUTATING_OPERATIONS.has(action.operation)) {
    return;
  }
  const repoAlias = validateRepoAlias(action.repo_alias);
  const allowedPaths = await bdbAllowedPaths(repoAlias);
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
