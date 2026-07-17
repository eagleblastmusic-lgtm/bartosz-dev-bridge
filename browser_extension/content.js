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

function resultText(response) {
  const payload = response && response.result ? response.result : response;
  return `BDB_RESULT:\n${JSON.stringify(payload, null, 2)}`;
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

function prepareContinuation(text) {
  const composer = findComposer();
  if (!composer) {
    return false;
  }
  composer.focus();
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    const prefix = composer.value ? `${composer.value}\n\n` : "";
    composer.value = `${prefix}${text}`;
    composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    return true;
  }
  if (composer.isContentEditable) {
    const selection = window.getSelection();
    if (selection) {
      selection.selectAllChildren(composer);
      selection.collapseToEnd();
    }
    const inserted = typeof document.execCommand === "function" && document.execCommand("insertText", false, `\n\n${text}`);
    if (!inserted) {
      composer.append(document.createTextNode(`\n\n${text}`));
      composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    }
    return true;
  }
  return false;
}

function renderResult(container, response) {
  container.textContent = "";
  const status = document.createElement("div");
  status.className = "bdb-status";
  status.textContent = `BDB: ${response.status || "wynik"}`;

  const pre = document.createElement("pre");
  pre.className = "bdb-result";
  pre.textContent = JSON.stringify(response, null, 2);

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
  container.append(status, pre, controls);
}

function enhance(codeBlock, action) {
  const host = codeBlock.closest("pre") || codeBlock.parentElement;
  if (!(host instanceof HTMLElement) || host.querySelector(":scope > .bdb-assisted")) {
    return;
  }
  const panel = document.createElement("div");
  panel.className = "bdb-assisted";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-execute";
  button.textContent = "BDB: Wykonaj";

  const output = document.createElement("div");
  output.className = "bdb-output";

  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "BDB: wykonywanie…";
    output.textContent = "";
    try {
      const result = await chrome.runtime.sendMessage({ type: "BDB_SUBMIT_ACTION", action });
      if (!result || result.ok !== true) {
        throw new Error(result && result.error ? result.error : "Brak odpowiedzi rozszerzenia");
      }
      renderResult(output, result.response);
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
}

function scan(root) {
  const blocks = root.querySelectorAll ? root.querySelectorAll("pre code, code") : [];
  for (const block of blocks) {
    if (!(block instanceof HTMLElement) || processedBlocks.has(block)) {
      continue;
    }
    processedBlocks.add(block);
    const action = parseAction(block);
    if (action) {
      enhance(block, action);
    }
  }
}

scan(document);
const observer = new MutationObserver((records) => {
  for (const record of records) {
    for (const node of record.addedNodes) {
      if (node instanceof HTMLElement) {
        scan(node);
      }
    }
  }
});
observer.observe(document.documentElement, { childList: true, subtree: true });
