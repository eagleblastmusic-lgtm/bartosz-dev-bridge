"use strict";

const ACTION_SCHEMA = "bdb-action-v1";
const MAX_ACTION_TEXT = 1024 * 1024;
const processedBlocks = new WeakSet();

function parseAction(codeBlock) {
  const text = codeBlock.textContent || "";
  if (text.length === 0 || text.length > MAX_ACTION_TEXT || !text.includes(ACTION_SCHEMA)) {
    return null;
  }
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== ACTION_SCHEMA) {
      return null;
    }
    return value;
  } catch (_error) {
    return null;
  }
}

function compactAction(action) {
  const presentation = action && action.presentation;
  return Boolean(
    presentation &&
    typeof presentation === "object" &&
    !Array.isArray(presentation) &&
    presentation.mode === "compact"
  );
}

function resultText(response, marker = null) {
  const payload = response && response.result ? response.result : response;
  const prefix = marker ? `${marker}\n` : "";
  return `${prefix}BDB_RESULT:\n${JSON.stringify(payload, null, 2)}`;
}

function resultSummary(response) {
  const payload = response && response.result ? response.result : response;
  if (payload && payload.operation === "workspace_context" && payload.context) {
    const context = payload.context;
    const files = Array.isArray(context.tracked_paths) ? context.tracked_paths.length : 0;
    const snapshots = Array.isArray(context.snapshot_files) ? context.snapshot_files.length : 0;
    const symbols = Array.isArray(context.symbols) ? context.symbols.length : 0;
    return `Odczytano kontekst: ${files} plików, ${snapshots} treści, ${symbols} symboli.`;
  }
  if (payload && payload.status === "success") {
    const changed = Array.isArray(payload.changed_files) ? payload.changed_files.length : 0;
    const tests = payload.stdout_tail && typeof payload.stdout_tail === "string"
      ? payload.stdout_tail.trim().split("\n").slice(-1)[0]
      : null;
    if (changed > 0 && tests) {
      return `Zmieniono ${changed} plików. ${tests}`;
    }
    if (changed > 0) {
      return `Zmieniono ${changed} plików.`;
    }
    return "Operacja zakończona powodzeniem.";
  }
  if (response && response.status === "pending") {
    return "Operacja została przyjęta i nadal trwa.";
  }
  return `Stan: ${(response && response.status) || (payload && payload.status) || "wynik"}`;
}

async function writeClipboard(text) {
  if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_error) {
    return false;
  }
}

function findComposer() {
  const selectors = [
    "#prompt-textarea",
    "[data-testid='prompt-textarea']",
    "textarea[placeholder]"
  ];
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (node instanceof HTMLElement) {
      return node;
    }
  }
  return null;
}

function composerText(composer) {
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    return composer.value;
  }
  return composer.innerText || composer.textContent || "";
}

function prepareContinuation(text, { requireEmpty = false } = {}) {
  const composer = findComposer();
  if (!composer) {
    return null;
  }
  if (requireEmpty && composerText(composer).trim() !== "") {
    return null;
  }
  composer.focus();
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    const prefix = composer.value ? `${composer.value}\n\n` : "";
    composer.value = `${prefix}${text}`;
    composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    return composer;
  }
  if (composer.isContentEditable) {
    if (requireEmpty) {
      composer.textContent = "";
    }
    const selection = window.getSelection();
    if (selection) {
      selection.selectAllChildren(composer);
      selection.collapseToEnd();
    }
    const insertion = requireEmpty ? text : `\n\n${text}`;
    const inserted = typeof document.execCommand === "function" && document.execCommand("insertText", false, insertion);
    if (!inserted) {
      composer.append(document.createTextNode(insertion));
      composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    }
    return composer;
  }
  return null;
}

async function autoSend(response, loopId, iteration) {
  const marker = `BDB_AUTO_RESULT:${loopId}:${iteration}`;
  const text = resultText(response, marker);
  const composer = prepareContinuation(text, { requireEmpty: true });
  if (!composer || !composerText(composer).includes(marker)) {
    return { sent: false, reason: "composer_unavailable_or_not_empty" };
  }
  const form = composer.closest("form");
  if (!form) {
    return { sent: false, reason: "composer_form_missing" };
  }
  let button = null;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const candidate = form.querySelector("button[data-testid='send-button']");
    if (candidate instanceof HTMLButtonElement && !candidate.disabled) {
      button = candidate;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (!button || !composerText(composer).includes(marker)) {
    return { sent: false, reason: "exact_send_button_unavailable" };
  }
  button.click();
  return { sent: true, reason: null };
}

function renderResult(container, response, { compact = false } = {}) {
  container.textContent = "";
  const status = document.createElement("div");
  status.className = "bdb-status";
  status.textContent = compact ? resultSummary(response) : `BDB: ${response.status || "wynik"}`;

  const pre = document.createElement("pre");
  pre.className = "bdb-result";
  pre.textContent = JSON.stringify(response, null, 2);

  const details = document.createElement("details");
  details.className = "bdb-details";
  const detailsSummary = document.createElement("summary");
  detailsSummary.textContent = "Szczegóły techniczne";
  details.append(detailsSummary, pre);
  if (!compact) {
    details.open = true;
  }

  const controls = document.createElement("div");
  controls.className = "bdb-controls";

  const continuation = resultText(response);
  const continueButton = document.createElement("button");
  continueButton.type = "button";
  continueButton.textContent = "Przygotuj kontynuację";
  continueButton.addEventListener("click", async () => {
    if (prepareContinuation(continuation)) {
      continueButton.textContent = "Wstawiono — wyślij ręcznie";
      return;
    }
    const copied = await writeClipboard(continuation);
    continueButton.textContent = copied ? "Skopiowano — wklej ręcznie" : "Nie udało się wstawić";
  });

  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.textContent = "Kopiuj wynik";
  copyButton.addEventListener("click", async () => {
    const copied = await writeClipboard(continuation);
    copyButton.textContent = copied ? "Skopiowano" : "Kopiowanie niedostępne";
  });

  controls.append(continueButton, copyButton);
  container.append(status, details, controls);
}

async function maybeAuto(action, button, output, compact) {
  const automation = action && action.automation;
  if (!automation || automation.mode !== "auto") {
    return;
  }
  button.disabled = true;
  button.textContent = "BDB AUTO: sprawdzanie…";
  try {
    const decision = await chrome.runtime.sendMessage({ type: "BDB_CONSIDER_AUTO", action });
    if (!decision || decision.ok !== true) {
      throw new Error(decision && decision.error ? decision.error : "Brak decyzji AUTO");
    }
    const auto = decision.response;
    if (!auto.executed) {
      button.textContent = `BDB: Wykonaj (${auto.reason || "ASSISTED"})`;
      return;
    }
    renderResult(output, auto.response, { compact });
    if (!auto.shouldContinue) {
      button.textContent = `BDB AUTO: zatrzymano (${auto.stopReason || "limit"})`;
      return;
    }
    const sent = await autoSend(auto.response, auto.loopId, auto.iteration);
    if (sent.sent) {
      button.textContent = `BDB AUTO: wysłano ${auto.iteration}`;
      return;
    }
    button.textContent = `BDB AUTO → ASSISTED (${sent.reason})`;
  } catch (error) {
    output.textContent = `BDB AUTO error: ${String(error && error.message ? error.message : error)}`;
    button.textContent = "BDB AUTO → ASSISTED";
  } finally {
    button.disabled = false;
  }
}

function enhance(codeBlock, action) {
  const host = codeBlock.closest("pre") || codeBlock.parentElement;
  if (!(host instanceof HTMLElement) || host.querySelector(":scope > .bdb-assisted")) {
    return;
  }
  const compact = compactAction(action);
  if (compact) {
    codeBlock.classList.add("bdb-action-source-hidden");
    host.classList.add("bdb-compact-host");
  }

  const panel = document.createElement("div");
  panel.className = compact ? "bdb-assisted bdb-compact" : "bdb-assisted";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-execute";
  button.textContent = compact ? "BDB: uruchom zadanie" : "BDB: Wykonaj";

  const output = document.createElement("div");
  output.className = "bdb-output";

  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "BDB: wykonywanie…";
    output.textContent = "";
    try {
      const currentAction = parseAction(codeBlock);
      if (!currentAction) {
        throw new Error("Blok BDB zmienił się lub nie jest już prawidłowym bdb-action-v1 JSON");
      }
      const result = await chrome.runtime.sendMessage({ type: "BDB_SUBMIT_ACTION", action: currentAction });
      if (!result || result.ok !== true) {
        throw new Error(result && result.error ? result.error : "Brak odpowiedzi rozszerzenia");
      }
      renderResult(output, result.response, { compact });
      button.textContent = "BDB: wykonano";
    } catch (error) {
      output.textContent = `BDB error: ${String(error && error.message ? error.message : error)}`;
      button.textContent = "BDB: ponów";
    } finally {
      button.disabled = false;
    }
  });

  panel.append(button, output);
  host.append(panel);
  maybeAuto(action, button, output, compact);
}

function scan(root) {
  const blocks = [];
  if (root instanceof HTMLElement && root.matches("code")) {
    blocks.push(root);
  }
  if (root.querySelectorAll) {
    blocks.push(...root.querySelectorAll("pre code, code"));
  }
  for (const block of blocks) {
    if (!(block instanceof HTMLElement) || processedBlocks.has(block)) {
      continue;
    }
    const action = parseAction(block);
    if (!action) {
      continue;
    }
    processedBlocks.add(block);
    enhance(block, action);
  }
}

scan(document);
const observer = new MutationObserver((records) => {
  for (const record of records) {
    if (record.type === "characterData" && record.target.parentElement) {
      scan(record.target.parentElement);
    }
    for (const node of record.addedNodes) {
      if (node instanceof HTMLElement) {
        scan(node);
      } else if (node.parentElement) {
        scan(node.parentElement);
      }
    }
  }
});
observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
