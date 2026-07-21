"use strict";

const BDB_PROJECT_LAUNCH_ALIAS = "bdb-project-launch";
const BDB_PROJECT_LAUNCH_POLL_MS = 1000;
const BDB_PROJECT_LAUNCH_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
let bdbProjectLaunchPolling = false;
const bdbProjectClaims = new Map();

function bdbProjectLaunchMarker(launchId) {
  return `BDB_PROJECT_LAUNCH:${launchId}`;
}

function bdbProjectClaimId(launchId) {
  let claimId = bdbProjectClaims.get(launchId);
  if (!claimId) {
    claimId = crypto.randomUUID();
    bdbProjectClaims.set(launchId, claimId);
  }
  return claimId;
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

function bdbProjectComposerEligible(marker) {
  if (bdbUserMessageContains(marker)) {
    return true;
  }
  const composer = findComposer();
  if (!composer) {
    return false;
  }
  const text = composerText(composer);
  return text.includes(marker) || text.trim() === "";
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

async function bdbProjectLaunchAction(operation, launchId, claimId) {
  const result = await chrome.runtime.sendMessage({
    type: "BDB_SUBMIT_ACTION",
    action: {
      schema: ACTION_SCHEMA,
      operation,
      launch_id: launchId,
      claim_id: claimId
    }
  });
  return result && result.ok === true ? result.response : null;
}

async function bdbClaimProjectLaunch(launch) {
  const claimId = bdbProjectClaimId(launch.launch_id);
  const response = await bdbProjectLaunchAction(
    "project_launch_claim",
    launch.launch_id,
    claimId
  );
  if (!response || response.status !== "claimed" || !bdbValidProjectLaunch(response.launch)) {
    return null;
  }
  return { launch: response.launch, claimId };
}

async function bdbAcknowledgeProjectLaunch(launchId, claimId) {
  const response = await bdbProjectLaunchAction(
    "project_launch_ack",
    launchId,
    claimId
  );
  if (response && response.status === "acknowledged") {
    bdbProjectClaims.delete(launchId);
    return true;
  }
  return false;
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

async function bdbHandleProjectLaunch(candidate) {
  const candidateMarker = bdbProjectLaunchMarker(candidate.launch_id);
  // Tabs with an unrelated draft never acquire the cross-process claim. An
  // empty tab or the tab already holding this exact marker may become owner.
  if (!bdbProjectComposerEligible(candidateMarker)) {
    return false;
  }

  const ownership = await bdbClaimProjectLaunch(candidate);
  if (!ownership) {
    return false;
  }
  const launch = ownership.launch;
  const claimId = ownership.claimId;
  const marker = bdbProjectLaunchMarker(launch.launch_id);
  if (bdbUserMessageContains(marker)) {
    return bdbAcknowledgeProjectLaunch(launch.launch_id, claimId);
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
    return bdbAcknowledgeProjectLaunch(launch.launch_id, claimId);
  }
  const sent = await bdbSubmitProjectLaunch(marker);
  if (!sent) {
    return false;
  }
  return bdbAcknowledgeProjectLaunch(launch.launch_id, claimId);
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
    // Native Host unavailability, a busy composer or a transient rerender leaves
    // the launch pending. The cross-process claim expires and can be retried.
  } finally {
    bdbProjectLaunchPolling = false;
  }
}

const bdbProjectRuntimeReady = Boolean(
  typeof chrome === "object" &&
  chrome.runtime &&
  typeof chrome.runtime.id === "string" &&
  chrome.runtime.id.length > 0 &&
  typeof chrome.runtime.sendMessage === "function"
);
if (bdbProjectRuntimeReady) {
  void bdbPollProjectLaunch();
  if (typeof setInterval === "function") {
    const bdbProjectPollTimer = setInterval(
      bdbPollProjectLaunch,
      BDB_PROJECT_LAUNCH_POLL_MS
    );
    // Browser timers are numeric. Node-based contract harnesses expose unref(),
    // so release the synthetic timer without changing browser behavior.
    if (bdbProjectPollTimer && typeof bdbProjectPollTimer.unref === "function") {
      bdbProjectPollTimer.unref();
    }
  }
}
