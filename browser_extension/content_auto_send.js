"use strict";

// ChatGPT may accept insertion into the composer but ignore a synthetic click or
// keep the send button disabled briefly. AUTO must not report success until the
// composer has actually consumed the exact marker. Retry only while the marker
// is still present, which avoids duplicate submissions after a successful send.
const BDB_AUTO_SEND_BUTTON_ATTEMPTS = 80;
const BDB_AUTO_SEND_CONFIRM_POLLS = 24;
const BDB_AUTO_SEND_MAX_CLICKS = 3;
const BDB_AUTO_SEND_POLL_MS = 100;

function bdbAutoSendSleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bdbComposerContains(marker) {
  const current = findComposer();
  return Boolean(current && composerText(current).includes(marker));
}

async function bdbFindReadySendButton(composer) {
  const form = composer && composer.closest("form");
  if (!form) {
    return { form: null, button: null };
  }
  for (let attempt = 0; attempt < BDB_AUTO_SEND_BUTTON_ATTEMPTS; attempt += 1) {
    const local = form.querySelector("button[data-testid='send-button']");
    const global = document.querySelector("button[data-testid='send-button']");
    const candidate = local instanceof HTMLButtonElement ? local : global;
    if (candidate instanceof HTMLButtonElement && !candidate.disabled) {
      return { form, button: candidate };
    }
    await bdbAutoSendSleep(BDB_AUTO_SEND_POLL_MS);
  }
  return { form, button: null };
}

async function bdbWaitForComposerConsumption(marker) {
  let consecutiveMissing = 0;
  for (let poll = 0; poll < BDB_AUTO_SEND_CONFIRM_POLLS; poll += 1) {
    if (bdbComposerContains(marker)) {
      consecutiveMissing = 0;
    } else {
      consecutiveMissing += 1;
      if (consecutiveMissing >= 3) {
        return true;
      }
    }
    await bdbAutoSendSleep(BDB_AUTO_SEND_POLL_MS);
  }
  return false;
}

autoSend = async function autoSendWithConfirmation(response, loopId, iteration) {
  const marker = `BDB_AUTO_RESULT:${loopId}:${iteration}`;
  const text = resultText(response, marker);
  const composer = prepareContinuation(text, { requireEmpty: true });
  if (!composer || !composerText(composer).includes(marker)) {
    return { sent: false, reason: "composer_unavailable_or_not_empty" };
  }

  const ready = await bdbFindReadySendButton(composer);
  if (!ready.form || !ready.button || !bdbComposerContains(marker)) {
    return { sent: false, reason: ready.form ? "exact_send_button_unavailable" : "composer_form_missing" };
  }

  for (let clickAttempt = 0; clickAttempt < BDB_AUTO_SEND_MAX_CLICKS; clickAttempt += 1) {
    if (!bdbComposerContains(marker)) {
      return { sent: true, reason: null, confirmed: true, clickAttempts: clickAttempt };
    }

    const current = await bdbFindReadySendButton(findComposer());
    if (!current.button) {
      return { sent: false, reason: "exact_send_button_unavailable" };
    }
    current.button.click();

    if (await bdbWaitForComposerConsumption(marker)) {
      return {
        sent: true,
        reason: null,
        confirmed: true,
        clickAttempts: clickAttempt + 1
      };
    }
  }

  return {
    sent: false,
    reason: "send_not_confirmed",
    confirmed: false,
    markerStillPresent: bdbComposerContains(marker)
  };
};
