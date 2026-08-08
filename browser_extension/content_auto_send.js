"use strict";

// ChatGPT may replace the composer after an input event, accept insertion but
// ignore a synthetic click, or keep the send button disabled briefly. AUTO must
// operate on the current live composer and must not report success until the
// exact marker appears in a submitted user message.
const BDB_AUTO_SEND_BUTTON_ATTEMPTS = 80;
const BDB_AUTO_INSERTION_OBSERVE_POLLS = 40;
const BDB_AUTO_SEND_CONFIRM_POLLS = 30;
const BDB_AUTO_SEND_POLL_MS = 100;
const BDB_AUTO_SEND_STRATEGIES = Object.freeze([
  "button_click",
  "request_submit",
  "enter_key"
]);

async function bdbAutoSendSleep(milliseconds) {
  if (
    typeof chrome === "object" &&
    chrome.runtime &&
    typeof chrome.runtime.sendMessage === "function"
  ) {
    try {
      const waited = await chrome.runtime.sendMessage({
        type: "BDB_AUTO_WAIT",
        milliseconds
      });
      if (waited && waited.ok === true) {
        return;
      }
    } catch (_error) {
      // Fall back only when the background wait broker is unavailable.
    }
  }
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bdbAutoUtf8ByteLength(value) {
  let bytes = 0;
  for (const character of String(value || "")) {
    const code = character.codePointAt(0);
    if (code <= 0x7f) bytes += 1;
    else if (code <= 0x7ff) bytes += 2;
    else if (code <= 0xffff) bytes += 3;
    else bytes += 4;
  }
  return bytes;
}

function bdbAutoFastInsertionAvailable(composer) {
  return Boolean(
    composer &&
    composer.isContentEditable &&
    typeof composer.replaceChildren === "function" &&
    typeof document !== "undefined" &&
    document &&
    typeof document.createElement === "function"
  );
}

function bdbPrepareAutoContinuation(text, composer, maxBytes) {
  if (!composer || bdbAutoUtf8ByteLength(text) > maxBytes) {
    return null;
  }

  // ChatGPT currently exposes a contenteditable composer.  execCommand inserts
  // long strings character-by-character through Blink editing and repeatedly
  // forces style/layout.  Replace the editor contents once and emit one input
  // event instead.  Non-contenteditable/fake harnesses keep the proven legacy
  // path so existing fallbacks and runtime contracts remain intact.
  if (bdbAutoFastInsertionAvailable(composer)) {
    try {
      composer.focus();
      const paragraph = document.createElement("p");
      paragraph.textContent = text;
      composer.replaceChildren(paragraph);
      if (typeof composer.dispatchEvent === "function" && typeof InputEvent === "function") {
        composer.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          inputType: "insertText",
          data: text
        }));
      }
      return composer;
    } catch (_directInsertError) {
      // Fall through to the existing insertion path.  AUTO is capped at 4 KiB,
      // when the legacy path is selected, so even the compatibility fallback
      // cannot reproduce the former 426 KiB or 15 KiB renderer stall.
    }
  }

  const legacyMaxBytes = typeof BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES === "number"
    ? BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES
    : 4 * 1024;
  if (bdbAutoUtf8ByteLength(text) > legacyMaxBytes) {
    return null;
  }
  return prepareContinuation(text, {
    requireEmpty: true,
    maxBytes: Math.min(maxBytes, legacyMaxBytes)
  });
}

function bdbComposerContains(marker) {
  const current = findComposer();
  return Boolean(current && composerText(current).includes(marker));
}

async function bdbPrepareManualContinuation(text) {
  const maxBytes = typeof BDB_AUTO_CONTINUATION_MAX_BYTES === "number"
    ? BDB_AUTO_CONTINUATION_MAX_BYTES
    : 16 * 1024;
  if (bdbAutoUtf8ByteLength(text) > maxBytes) {
    return false;
  }
  const initial = findComposer();
  if (!initial) {
    return false;
  }
  const prepared = bdbAutoFastInsertionAvailable(initial)
    ? bdbPrepareAutoContinuation(text, initial, maxBytes)
    : prepareContinuation(text, { requireEmpty: false, maxBytes });
  if (!prepared) {
    return false;
  }
  for (let poll = 0; poll < BDB_AUTO_INSERTION_OBSERVE_POLLS; poll += 1) {
    const current = findComposer();
    if (current && composerText(current).includes(text)) {
      return true;
    }
    await bdbAutoSendSleep(BDB_AUTO_SEND_POLL_MS);
  }
  return false;
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
  for (let poll = 0; poll < BDB_AUTO_SEND_CONFIRM_POLLS; poll += 1) {
    if (bdbUserMessageContains(marker)) {
      return { confirmed: true, via: "user_message" };
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
  const fastContinuationMaxBytes = typeof BDB_AUTO_CONTINUATION_MAX_BYTES === "number"
    ? BDB_AUTO_CONTINUATION_MAX_BYTES
    : 16 * 1024;
  const legacyContinuationMaxBytes = typeof BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES === "number"
    ? BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES
    : 4 * 1024;
  const initial = bdbInitialComposerState();
  if (initial.reason) {
    return { sent: false, reason: initial.reason };
  }
  const fastInsertion = bdbAutoFastInsertionAvailable(initial.composer);
  const autoContinuationMaxBytes = fastInsertion
    ? fastContinuationMaxBytes
    : legacyContinuationMaxBytes;
  let text = typeof autoResultText === "function"
    ? autoResultText(response, marker, autoContinuationMaxBytes)
    : resultText(response, marker);

  let prepared = bdbPrepareAutoContinuation(
    text,
    initial.composer,
    autoContinuationMaxBytes
  );
  if (!prepared && fastInsertion && typeof autoResultText === "function") {
    text = autoResultText(response, marker, legacyContinuationMaxBytes);
    prepared = bdbPrepareAutoContinuation(
      text,
      initial.composer,
      legacyContinuationMaxBytes
    );
  }
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
    if (bdbUserMessageContains(marker)) {
      return {
        sent: true,
        reason: null,
        confirmed: true,
        confirmedVia: "user_message",
        attempts
      };
    }
    if (!bdbComposerContains(marker)) {
      return {
        sent: false,
        reason: "send_not_confirmed",
        confirmed: false,
        markerStillPresent: false,
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

  if (bdbUserMessageContains(marker)) {
    return {
      sent: true,
      reason: null,
      confirmed: true,
      confirmedVia: "user_message",
      attempts
    };
  }
  return {
    sent: false,
    reason: "send_not_confirmed",
    confirmed: false,
    markerStillPresent: bdbComposerContains(marker),
    attempts
  };
};
