"use strict";

// Reuse the reviewed BDB_CONTEXT and BDB_SUBMIT_ACTION paths. A short Native
// Host lease makes one ChatGPT tab the owner before it touches the composer.
const BDB_PROJECT_LAUNCH_ALIAS = "bdb-project-launch";
const BDB_PROJECT_LAUNCH_OPERATIONS = new Set([
  "project_launch_claim",
  "project_launch_ack"
]);
const BDB_PROJECT_LAUNCH_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const nativeContextBeforeProjectLauncher = nativeContext;
const submitActionBeforeProjectLauncher = submitAction;

function bdbValidateLaunchUuid(value, field) {
  if (typeof value !== "string" || !BDB_PROJECT_LAUNCH_ID_RE.test(value)) {
    throw new Error(`Project ${field} must be a UUID`);
  }
  return value;
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
