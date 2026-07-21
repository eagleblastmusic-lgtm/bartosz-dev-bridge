"use strict";

const BDB_PROJECT_LAUNCH_ALIAS = "bdb-project-launch";
const BDB_PROJECT_LAUNCH_POLL_MS = 1000;
const BDB_PROJECT_LAUNCH_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
let bdbProjectLaunchPolling = false;

function bdbProjectLaunchMarker(launchId) {
  return `BDB_PROJECT_LAUNCH:${launchId}`;
}

function bdbValidProjectLaunch(value) {
  return Boolean(
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    value.schema === "bdb-project-launch-v1" &&
    typeof value.launch_id === "string" &&
    BDB_PROJECT_LAUNCH_ID_RE.test(value.launch_id) &&
    typeof value.repo_alias === "string" &&
    typeof value.prompt === "string" &&
    value.prompt.trim().length > 0 &&
    value.prompt.length <= 50000 &&
    typeof value.auto_send === "boolean"
  );
}

async function bdbFetchProjectLaunch() {
  const result = await chrome.runtime.sendMessage({
    type: "BDB_CONTEXT",
    repoAlias: BDB_PROJECT_LAUNCH_ALIAS
  });
  if (!result || result.ok !== true) {
    return null;
  }
  const response = result.response;
  if (!response || response.status !== "project_launch" || !bdbValidProjectLaunch(response.launch)) {
    return null;
  }
  return response.launch;
}

async function bdbAcknowledgeProjectLaunch(launchId) {
  const result = await chrome.runtime.sendMessage({
    type: "BDB_SUBMIT_ACTION",
    action: {
      schema: ACTION_SCHEMA,
      operation: "project_launch_ack",
      launch_id: launchId
    }
  });
  return Boolean(
    result &&
    result.ok === true &&
    result.response &&
    result.response.status === "acknowledged"
  );
}

async function bdbSubmitProjectLaunch(marker) {
  if (bdbUserMessageContains(marker)) {
    return true;
  }
  for (const strategy of BDB_AUTO_SEND_STRATEGIES) {
    if (!bdbComposerContains(marker)) {
      return bdbUserMessageContains(marker);
    }
    const attempt = await bdbAttemptSend(marker, strategy);
    if (!attempt.attempted) {
      continue;
    }
    const confirmation = await bdbWaitForSendConfirmation(marker);
    if (confirmation.confirmed) {
      return true;
    }
  }
  return false;
}

async function bdbHandleProjectLaunch(launch) {
  const marker = bdbProjectLaunchMarker(launch.launch_id);
  if (bdbUserMessageContains(marker)) {
    return bdbAcknowledgeProjectLaunch(launch.launch_id);
  }

  let composer = findComposer();
  if (!composer) {
    return false;
  }
  const currentText = composerText(composer);
  if (!currentText.includes(marker)) {
    if (currentText.trim() !== "") {
      return false;
    }
    const inserted = prepareContinuation(`${marker}\n${launch.prompt}`, { requireEmpty: true });
    if (!inserted) {
      return false;
    }
    composer = await bdbWaitForLiveComposerMarker(marker);
    if (!composer) {
      return false;
    }
  }

  if (!launch.auto_send) {
    return bdbAcknowledgeProjectLaunch(launch.launch_id);
  }
  const sent = await bdbSubmitProjectLaunch(marker);
  if (!sent) {
    return false;
  }
  return bdbAcknowledgeProjectLaunch(launch.launch_id);
}

async function bdbPollProjectLaunch() {
  if (bdbProjectLaunchPolling) {
    return;
  }
  bdbProjectLaunchPolling = true;
  try {
    const launch = await bdbFetchProjectLaunch();
    if (launch) {
      await bdbHandleProjectLaunch(launch);
    }
  } catch (_error) {
    // The queue is optional and bounded. Native Host availability, an occupied
    // composer or a transient ChatGPT rerender simply leaves the launch pending.
  } finally {
    bdbProjectLaunchPolling = false;
  }
}

bdbPollProjectLaunch();
setInterval(bdbPollProjectLaunch, BDB_PROJECT_LAUNCH_POLL_MS);
