"use strict";

// Reuse the already reviewed BDB_CONTEXT and BDB_SUBMIT_ACTION message paths.
// The existing background listener resolves these globals at call time, so no
// second competing onMessage listener is needed.
const BDB_PROJECT_LAUNCH_ALIAS = "bdb-project-launch";
const BDB_PROJECT_LAUNCH_ACK_OPERATION = "project_launch_ack";
const BDB_PROJECT_LAUNCH_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const nativeContextBeforeProjectLauncher = nativeContext;
const submitActionBeforeProjectLauncher = submitAction;

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
  if (!action || action.operation !== BDB_PROJECT_LAUNCH_ACK_OPERATION) {
    return submitActionBeforeProjectLauncher(action, tabId);
  }
  validateJsonObject(action, "project launch acknowledgement");
  if (action.schema !== ACTION_SCHEMA) {
    throw new Error(`Only ${ACTION_SCHEMA} is supported`);
  }
  if (typeof action.launch_id !== "string" || !BDB_PROJECT_LAUNCH_ID_RE.test(action.launch_id)) {
    throw new Error("Project launch_id must be a UUID");
  }
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("project-launch-ack"),
    action: "project_launch_ack",
    launch_id: action.launch_id
  });
};
