"use strict";

// ChatGPT may replace the composer after an input event, accept insertion but
// ignore a synthetic click, or keep the send button disabled briefly. AUTO must
// operate on the current live composer and must not report success until the
// exact marker has been consumed or appears in a submitted user message.
const BDB_AUTO_SEND_BUTTON_ATTEMPTS = 80;
const BDB_AUTO_INSERTION_OBSERVE_POLLS = 40;
const BDB_AUTO_SEND_CONFIRM_POLLS = 30;
const BDB_AUTO_SEND_POLL_MS = 100;
const BDB_AUTO_SEND_STRATEGIES = Object.freeze([
  "button_click",
  "request_submit",
  "enter_key"
]);

function bdbAutoSendSleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bdbComposerContains(marker) {
  const current = findComposer();
  return Boolean(current && composerText(current).includes(marker));
}

function bdbUserMessageContains(marker) {
  if (!document || typeof document.querySelectorAll !== "function") {
    return false;
  }
  const messages = document.querySelectorAll("[data-message-author-role='user']");
  return Array.from(messages).some((message) => (
    typeof message.textContent === "string" && message.textContent.includes(marker)
  ));
}

function bdbInitialComposerState() {
  const composer = findComposer();
  if (!composer) {
    return { composer: null, reason: "composer_missing" };
  }
  if (composerText(composer).trim() !== "") {
    return { composer, reason: "composer_not_empty" };
  }
  return { composer, reason: null };
}

async function bdbWaitForLiveComposerMarker(marker) {
  for (let poll = 0; poll < BDB_AUTO_INSERTION_OBSERVE_POLLS; poll += 1) {
    const current = findComposer();
    if (current && composerText(current).includes(marker)) {
      return current;
    }
    await bdbAutoSendSleep(BDB_AUTO_SEND_POLL_MS);
  }
  return null;
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

async function bdbWaitForSendConfirmation(marker) {
  let consecutiveMissing = 0;
  for (let poll = 0; poll < BDB_AUTO_SEND_CONFIRM_POLLS; poll += 1) {
    if (bdbUserMessageContains(marker)) {
      return { confirmed: true, via: "user_message" };
    }
    if (bdbComposerContains(marker)) {
      consecutiveMissing = 0;
    } else {
      consecutiveMissing += 1;
      if (consecutiveMissing >= 3) {
        return { confirmed: true, via: "composer_consumed" };
      }
    }
    await bdbAutoSendSleep(BDB_AUTO_SEND_POLL_MS);
  }
  return { confirmed: false, via: null };
}

function bdbRequestSubmit(form, button) {
  if (!form || typeof form.requestSubmit !== "function") {
    return false;
  }
  try {
    form.requestSubmit(button);
    return true;
  } catch (_withButtonError) {
    try {
      form.requestSubmit();
      return true;
    } catch (_withoutButtonError) {
      return false;
    }
  }
}

function bdbDispatchEnter(composer) {
  if (!composer || typeof composer.dispatchEvent !== "function" || typeof KeyboardEvent !== "function") {
    return false;
  }
  const eventInit = {
    key: "Enter",
    code: "Enter",
    keyCode: 13,
    which: 13,
    bubbles: true,
    cancelable: true
  };
  composer.dispatchEvent(new KeyboardEvent("keydown", eventInit));
  composer.dispatchEvent(new KeyboardEvent("keypress", eventInit));
  composer.dispatchEvent(new KeyboardEvent("keyup", eventInit));
  return true;
}

async function bdbAttemptSend(marker, strategy) {
  const currentComposer = findComposer();
  if (!currentComposer || !composerText(currentComposer).includes(marker)) {
    return { attempted: false, reason: "live_composer_lost" };
  }

  const current = await bdbFindReadySendButton(currentComposer);
  if (!current.form || !current.button) {
    return {
      attempted: false,
      reason: current.form ? "exact_send_button_unavailable" : "composer_form_missing"
    };
  }

  if (strategy === "button_click") {
    current.button.click();
    return { attempted: true, reason: null };
  }
  if (strategy === "request_submit") {
    return {
      attempted: bdbRequestSubmit(current.form, current.button),
      reason: "request_submit_unavailable"
    };
  }
  if (strategy === "enter_key") {
    return {
      attempted: bdbDispatchEnter(currentComposer),
      reason: "enter_dispatch_unavailable"
    };
  }
  return { attempted: false, reason: "unknown_send_strategy" };
}

autoSend = async function autoSendWithConfirmedFallbacks(response, loopId, iteration) {
  const marker = `BDB_AUTO_RESULT:${loopId}:${iteration}`;
  const text = resultText(response, marker);
  const initial = bdbInitialComposerState();
  if (initial.reason) {
    return { sent: false, reason: initial.reason };
  }

  const prepared = prepareContinuation(text, { requireEmpty: true });
  if (!prepared) {
    return { sent: false, reason: "insertion_failed" };
  }

  // React may replace the contenteditable node after the input event. Reacquire
  // the live composer and wait until that exact node exposes the exact marker.
  const liveComposer = await bdbWaitForLiveComposerMarker(marker);
  if (!liveComposer) {
    return { sent: false, reason: "insertion_not_observed" };
  }

  const attempts = [];
  for (const strategy of BDB_AUTO_SEND_STRATEGIES) {
    if (!bdbComposerContains(marker)) {
      return {
        sent: true,
        reason: null,
        confirmed: true,
        confirmedVia: "composer_consumed",
        attempts
      };
    }

    const attempt = await bdbAttemptSend(marker, strategy);
    attempts.push({ strategy, ...attempt });
    if (!attempt.attempted) {
      continue;
    }

    const confirmation = await bdbWaitForSendConfirmation(marker);
    if (confirmation.confirmed) {
      return {
        sent: true,
        reason: null,
        confirmed: true,
        confirmedVia: confirmation.via,
        strategy,
        attempts
      };
    }
  }

  return {
    sent: false,
    reason: "send_not_confirmed",
    confirmed: false,
    markerStillPresent: bdbComposerContains(marker),
    attempts
  };
};
