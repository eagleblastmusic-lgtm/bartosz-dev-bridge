"use strict";

// Reuse the reviewed BDB_CONTEXT and BDB_SUBMIT_ACTION paths. A short Native
// Host lease makes one ChatGPT tab the owner before it touches the composer.
const BDB_PROJECT_LAUNCH_ALIAS = "bdb-project-launch";
const BDB_PROJECT_LAUNCH_OPERATIONS = new Set([
  "project_launch_claim",
  "project_launch_ack",
  "project_conversation_bind"
]);
const BDB_PROJECT_LAUNCH_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const BDB_PROJECT_CONVERSATION_ID_RE = /^[A-Za-z0-9-]{8,128}$/;
const BDB_PROJECT_BINDINGS_STORAGE_KEY = "bdbConversationBindingsV1";
const BDB_PROJECT_BINDINGS_LIMIT = 128;
const nativeContextBeforeProjectLauncher = nativeContext;
const submitActionBeforeProjectLauncher = submitAction;

function bdbValidateLaunchUuid(value, field) {
  if (typeof value !== "string" || !BDB_PROJECT_LAUNCH_ID_RE.test(value)) {
    throw new Error(`Project ${field} must be a UUID`);
  }
  return value;
}

function bdbValidateConversationId(value) {
  if (typeof value !== "string" || !BDB_PROJECT_CONVERSATION_ID_RE.test(value)) {
    throw new Error("Project conversation_id has an unsafe format");
  }
  return value;
}

async function bdbBindProjectConversationToTab(action, tabId) {
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("Project conversation binding requires a concrete sender tab");
  }
  const launchId = bdbValidateLaunchUuid(action.launch_id, "launch_id");
  const conversationId = bdbValidateConversationId(action.conversation_id);
  const repoAlias = validateRepoAlias(action.repo_alias);
  const stored = await chrome.storage.local.get(BDB_PROJECT_BINDINGS_STORAGE_KEY);
  const raw = stored[BDB_PROJECT_BINDINGS_STORAGE_KEY];
  const bindings = raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
  const previous = bindings[conversationId];
  const now = Date.now();
  bindings[conversationId] = {
    schema: "bdb-conversation-binding-v1",
    conversation_id: conversationId,
    tab_id: tabId,
    repo_alias: repoAlias,
    launch_id: launchId,
    session_id: previous && typeof previous.session_id === "string" ? previous.session_id : null,
    command_id: previous && typeof previous.command_id === "string" ? previous.command_id : null,
    bound_at: previous && Number.isFinite(previous.bound_at) ? previous.bound_at : now,
    updated_at: now
  };
  const entries = Object.entries(bindings)
    .filter(([, value]) => value && typeof value === "object" && Number.isFinite(value.updated_at))
    .sort((left, right) => left[1].updated_at - right[1].updated_at)
    .slice(-BDB_PROJECT_BINDINGS_LIMIT);
  await chrome.storage.local.set({ [BDB_PROJECT_BINDINGS_STORAGE_KEY]: Object.fromEntries(entries) });
  return {
    schema: "bdb-native-response-v1",
    request_id: requestId("project-conversation-bind"),
    status: "conversation_bound",
    launch_id: launchId,
    conversation_id: conversationId,
    repo_alias: repoAlias,
    tab_id: tabId
  };
}

nativeContext = async function nativeContextWithProjectLauncher(repoAlias) {
  if (repoAlias !== BDB_PROJECT_LAUNCH_ALIAS) {
    return nativeContextBeforeProjectLauncher(repoAlias);
  }
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("project-launch"),
    action: "project_launch_peek"
  });
};

submitAction = async function submitActionWithProjectLauncher(action, tabId) {
  if (!action || !BDB_PROJECT_LAUNCH_OPERATIONS.has(action.operation)) {
    return submitActionBeforeProjectLauncher(action, tabId);
  }
  validateJsonObject(action, "project launch handoff");
  if (action.schema !== ACTION_SCHEMA) {
    throw new Error(`Only ${ACTION_SCHEMA} is supported`);
  }
  if (action.operation === "project_conversation_bind") {
    return bdbBindProjectConversationToTab(action, tabId);
  }
  const launchId = bdbValidateLaunchUuid(action.launch_id, "launch_id");
  const claimId = bdbValidateLaunchUuid(action.claim_id, "claim_id");
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId(action.operation),
    action: action.operation,
    launch_id: launchId,
    claim_id: claimId
  });
};
