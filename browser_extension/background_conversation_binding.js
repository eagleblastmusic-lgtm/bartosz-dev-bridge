"use strict";

const BDB_CONVERSATION_BINDINGS_KEY = "bdbConversationBindingsV1";
const BDB_CONVERSATION_BINDING_LIMIT = 128;
const submitActionBeforeConversationBinding = submitAction;

function bdbBindingCommandId(response) {
  if (response && typeof response.command_id === "string" && response.command_id.length > 0) {
    return response.command_id;
  }
  const result = response && response.result;
  if (result && typeof result.command_id === "string" && result.command_id.length > 0) {
    return result.command_id;
  }
  return null;
}

function bdbBindingSessionId(commandId) {
  if (typeof commandId !== "string") {
    return null;
  }
  const separator = commandId.lastIndexOf(":");
  return separator > 0 ? commandId.slice(0, separator) : null;
}

async function bdbRecordConversationCommand(action, response) {
  if (!action || typeof action.repo_alias !== "string") {
    return;
  }
  const commandId = bdbBindingCommandId(response);
  if (!commandId) {
    return;
  }
  const stored = await chrome.storage.local.get(BDB_CONVERSATION_BINDINGS_KEY);
  const raw = stored[BDB_CONVERSATION_BINDINGS_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return;
  }
  const candidates = Object.entries(raw)
    .filter(([, binding]) => (
      binding &&
      typeof binding === "object" &&
      binding.repo_alias === action.repo_alias &&
      Number.isFinite(binding.updated_at)
    ))
    .sort((left, right) => right[1].updated_at - left[1].updated_at);
  if (candidates.length === 0) {
    return;
  }
  const [conversationId, binding] = candidates[0];
  const now = Date.now();
  const updated = {
    ...raw,
    [conversationId]: {
      ...binding,
      schema: "bdb-conversation-binding-v1",
      conversation_id: conversationId,
      repo_alias: action.repo_alias,
      session_id: bdbBindingSessionId(commandId),
      command_id: commandId,
      updated_at: now
    }
  };
  const entries = Object.entries(updated)
    .filter(([, value]) => value && typeof value === "object" && Number.isFinite(value.updated_at))
    .sort((left, right) => left[1].updated_at - right[1].updated_at)
    .slice(-BDB_CONVERSATION_BINDING_LIMIT);
  await chrome.storage.local.set({ [BDB_CONVERSATION_BINDINGS_KEY]: Object.fromEntries(entries) });
}

submitAction = async function submitActionWithConversationBinding(action, tabId) {
  const response = await submitActionBeforeConversationBinding(action, tabId);
  try {
    await bdbRecordConversationCommand(action, response);
  } catch (_error) {
    // A storage failure must not turn a successful, already-submitted command into
    // a duplicate retry. The command remains recoverable by command_id.
  }
  return response;
};
