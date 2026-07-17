"use strict";

const HOST_NAME = "com.bartosz.dev_bridge";
const REQUEST_SCHEMA = "bdb-native-request-v1";
const ACTION_SCHEMA = "bdb-action-v1";
const MAX_SERIALIZED_BYTES = 1024 * 1024;
const DEFAULT_WAIT_SECONDS = 30;
const inFlightTabs = new Set();

function requestId(prefix) {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return `${prefix}-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function serializedSize(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function validateJsonObject(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  if (serializedSize(value) > MAX_SERIALIZED_BYTES) {
    throw new Error(`${field} exceeds the 1 MiB limit`);
  }
}

function sendNative(request) {
  validateJsonObject(request, "native request");
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, request, (response) => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError) {
        reject(new Error(runtimeError.message || "Native host unavailable"));
        return;
      }
      try {
        validateJsonObject(response, "native response");
        resolve(response);
      } catch (error) {
        reject(error);
      }
    });
  });
}

async function submitAction(action, tabId) {
  validateJsonObject(action, "BDB action");
  if (action.schema !== ACTION_SCHEMA) {
    throw new Error(`Only ${ACTION_SCHEMA} is supported`);
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("A concrete sender tab is required");
  }
  if (inFlightTabs.has(tabId)) {
    throw new Error("This tab already has a BDB action in progress");
  }
  inFlightTabs.add(tabId);
  try {
    return await sendNative({
      schema: REQUEST_SCHEMA,
      request_id: requestId("submit"),
      action: "submit_action",
      wait_seconds: DEFAULT_WAIT_SECONDS,
      bdb_action: action
    });
  } finally {
    inFlightTabs.delete(tabId);
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const handle = async () => {
    validateJsonObject(message, "extension message");
    switch (message.type) {
      case "BDB_SUBMIT_ACTION":
        return submitAction(message.action, sender.tab && sender.tab.id);
      case "BDB_STATUS":
        return sendNative({
          schema: REQUEST_SCHEMA,
          request_id: requestId("status"),
          action: "status"
        });
      case "BDB_CONTEXT":
        if (typeof message.repoAlias !== "string" || !/^[a-z][a-z0-9-]{0,31}$/.test(message.repoAlias)) {
          throw new Error("Repository alias has an unsafe format");
        }
        return sendNative({
          schema: REQUEST_SCHEMA,
          request_id: requestId("context"),
          action: "context",
          repo_alias: message.repoAlias
        });
      default:
        throw new Error("Unsupported extension message");
    }
  };

  handle()
    .then((response) => sendResponse({ ok: true, response }))
    .catch((error) => sendResponse({ ok: false, error: String(error && error.message ? error.message : error) }));
  return true;
});
