"use strict";

// Native Host may accept a command before its durable result is available. Keep
// the AUTO decision open and poll the existing bounded `result` action instead
// of treating `accepted`/`pending` as a user-intervention terminal state.
const BDB_ASYNC_RESULT_ATTEMPTS = 4;
const submitActionBeforeAsyncResultPolling = submitAction;

function parseBdbCommandId(value) {
  if (typeof value !== "string") {
    return null;
  }
  const separator = value.lastIndexOf(":");
  if (separator <= 0) {
    return null;
  }
  const sessionId = value.slice(0, separator);
  const sequenceText = value.slice(separator + 1);
  if (!/^\d{6}$/.test(sequenceText)) {
    return null;
  }
  const sequence = Number(sequenceText);
  if (!Number.isInteger(sequence) || sequence <= 0) {
    return null;
  }
  return { sessionId, sequence };
}

function responseStillPending(response) {
  return Boolean(
    response &&
    (response.status === "accepted" || response.status === "pending")
  );
}

async function pollBdbCommandResult(action, initialResponse) {
  if (!responseStillPending(initialResponse)) {
    return initialResponse;
  }
  const parsed = parseBdbCommandId(initialResponse.command_id);
  if (!parsed) {
    return initialResponse;
  }

  let latest = initialResponse;
  for (let attempt = 0; attempt < BDB_ASYNC_RESULT_ATTEMPTS; attempt += 1) {
    latest = await sendNative({
      schema: REQUEST_SCHEMA,
      request_id: requestId("result"),
      action: "result",
      repo_alias: validateRepoAlias(action.repo_alias),
      session_id: parsed.sessionId,
      sequence: parsed.sequence,
      wait_seconds: DEFAULT_WAIT_SECONDS
    });
    if (latest.status === "completed") {
      return waitForRequiredPromotion(action, latest);
    }
    if (latest.status === "failed" || !responseStillPending(latest)) {
      return latest;
    }
  }
  return {
    ...latest,
    async_poll_exhausted: true,
    command_id: latest.command_id || initialResponse.command_id
  };
}

submitAction = async function submitActionWithAsyncResultPolling(action, tabId) {
  const response = await submitActionBeforeAsyncResultPolling(action, tabId);
  return pollBdbCommandResult(action, response);
};
