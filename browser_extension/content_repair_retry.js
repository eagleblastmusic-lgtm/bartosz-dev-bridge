"use strict";

const BDB_CONTENT_REPAIR_ACTIONS_KEY = "bdbRepairActionsV1";
const BDB_CONTENT_REPAIR_MARKER_PREFIX = "BDB_REPAIR_REQUEST";

function bdbContentRepairClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function bdbContentRepairCommandId(response) {
  if (response && typeof response.command_id === "string") {
    return response.command_id;
  }
  const result = response && response.result;
  return result && typeof result.command_id === "string" ? result.command_id : null;
}

function bdbContentRepairSessionId(commandId) {
  if (typeof commandId !== "string") {
    return null;
  }
  const separator = commandId.lastIndexOf(":");
  return separator > 0 ? commandId.slice(0, separator) : null;
}

function bdbContentRepairPayload(response) {
  return response && response.result && typeof response.result === "object"
    ? response.result
    : response;
}

function bdbContentRepairDetail(entry, response, output) {
  const payload = bdbContentRepairPayload(response);
  const data = payload && payload.data;
  if (data && typeof data.terminal_detail === "string" && data.terminal_detail.length > 0) {
    return data.terminal_detail;
  }
  if (payload && typeof payload.summary === "string" && payload.summary.length > 0) {
    return payload.summary;
  }
  if (entry && typeof entry.error === "string" && entry.error.length > 0) {
    return entry.error;
  }
  return (output.textContent || "Nieznany błąd BDB").trim().slice(0, 2000);
}

function bdbContentRepairIsFailure(entry, response, output) {
  if (entry && typeof entry.error === "string" && entry.error.length > 0) {
    return true;
  }
  const payload = bdbContentRepairPayload(response);
  const status = payload && typeof payload.status === "string" ? payload.status : null;
  if (status && !["success", "pending", "accepted", "running"].includes(status)) {
    return true;
  }
  return (output.textContent || "").startsWith("BDB error:");
}

async function bdbContentRepairLatest(repoAlias) {
  const result = await chrome.runtime.sendMessage({
    type: "BDB_SUBMIT_ACTION",
    action: {
      schema: ACTION_SCHEMA,
      operation: "repair_state_peek",
      repo_alias: repoAlias
    }
  });
  const response = result && result.ok === true ? result.response : null;
  if (
    !response ||
    response.status !== "repair_state" ||
    typeof response.key !== "string" ||
    !response.entry ||
    typeof response.entry !== "object"
  ) {
    return null;
  }
  return { key: response.key, entry: response.entry };
}

async function bdbContentRepairUpdateState(found, changes) {
  const stored = await chrome.storage.local.get(BDB_CONTENT_REPAIR_ACTIONS_KEY);
  const raw = stored[BDB_CONTENT_REPAIR_ACTIONS_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("Brak zapisanego stanu naprawy dla tej karty");
  }
  const current = raw[found.key];
  if (!current || typeof current !== "object") {
    throw new Error("Stan naprawy tej karty wygasł");
  }
  const nextEntry = {
    ...current,
    ...changes,
    updated_at: Date.now()
  };
  await chrome.storage.local.set({
    [BDB_CONTENT_REPAIR_ACTIONS_KEY]: {
      ...raw,
      [found.key]: nextEntry
    }
  });
  found.entry = nextEntry;
}

async function bdbContentRepairDigestBase64(value) {
  if (typeof value !== "string") {
    throw new Error("content_base64 must be a string");
  }
  let binary;
  try {
    binary = atob(value);
  } catch (_error) {
    throw new Error("content_base64 is not canonical base64");
  }
  if (btoa(binary) !== value) {
    throw new Error("content_base64 has noncanonical padding");
  }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return `sha256:${Array.from(new Uint8Array(digest), (item) => item.toString(16).padStart(2, "0")).join("")}`;
}

async function bdbContentRepairHashes(action) {
  const repaired = bdbContentRepairClone(action);
  const operations = repaired && repaired.payload && repaired.payload.patch && repaired.payload.patch.operations;
  if (!Array.isArray(operations) || operations.length === 0) {
    throw new Error("Akcja nie zawiera wieloplikowego patcha do naprawy hashy");
  }
  let changed = 0;
  for (const operation of operations) {
    if (!operation || typeof operation !== "object" || typeof operation.content_base64 !== "string") {
      continue;
    }
    const actual = await bdbContentRepairDigestBase64(operation.content_base64);
    if (operation.content_sha256 !== actual) {
      operation.content_sha256 = actual;
      changed += 1;
    }
  }
  if (changed === 0) {
    throw new Error("Nie znaleziono rozbieżności content_sha256 do automatycznej naprawy");
  }
  return repaired;
}

function bdbContentRepairAsNewAttempt(entry, corrected) {
  const action = bdbContentRepairClone(corrected);
  const commandId = bdbContentRepairCommandId(entry.response);
  const predecessor = bdbContentRepairSessionId(commandId);
  const previous = entry.action;
  const correlation = previous && previous.repair_correlation;

  action.sequence = 1;
  action.expected_revision = 0;
  delete action.expected_state_hash;

  if (!predecessor) {
    // Client preflight failed before Native Host, therefore the initial session was
    // never bound and may safely be used for its first real submission.
    action.session_id = previous.session_id;
    action.repair_correlation = bdbContentRepairClone(previous.repair_correlation);
    return action;
  }

  if (!correlation || typeof correlation.correlation_id !== "string") {
    throw new Error("Poprzednia sesja nie ma jawnej korelacji naprawczej");
  }
  action.session_id = crypto.randomUUID();
  action.repair_correlation = {
    schema: "bdb-repair-correlation-v1",
    correlation_id: correlation.correlation_id,
    role: "repair",
    predecessor_session_id: predecessor
  };
  return action;
}

function bdbContentRepairResponseFromOutput(output) {
  const pre = output.querySelector(".bdb-result");
  if (!(pre instanceof HTMLElement)) {
    return null;
  }
  try {
    const parsed = JSON.parse(pre.textContent || "");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch (_error) {
    return null;
  }
}

async function bdbContentRepairAutoSend(text, marker) {
  if (
    document.visibilityState !== "visible" ||
    typeof document.hasFocus !== "function" ||
    !document.hasFocus()
  ) {
    return { sent: false, reason: "rozmowa_nie_jest_aktywna" };
  }
  const composer = prepareContinuation(text, { requireEmpty: true });
  if (!composer || !composerText(composer).includes(marker)) {
    return { sent: false, reason: "pole_wiadomosci_jest_zajete" };
  }
  for (const strategy of BDB_AUTO_SEND_STRATEGIES) {
    const attempt = await bdbAttemptSend(marker, strategy);
    if (!attempt.attempted) {
      continue;
    }
    const confirmation = await bdbWaitForSendConfirmation(marker);
    if (confirmation.confirmed) {
      return { sent: true, reason: null };
    }
  }
  return { sent: false, reason: "wyslanie_niepotwierdzone" };
}

async function bdbContentRepairRequestCorrection(found, detail, button) {
  await bdbContentRepairUpdateState(found, { awaiting_corrected_action: true });
  const marker = `${BDB_CONTENT_REPAIR_MARKER_PREFIX}:${crypto.randomUUID()}`;
  const actionText = JSON.stringify(found.entry.action, null, 2);
  const boundedAction = actionText.length <= 100000
    ? actionText
    : "Akcja jest dostępna w bezpośrednio poprzednim bloku BDB; nie kopiuję jej ponownie ze względu na rozmiar.";
  const prompt = `${marker}\nNapraw poprzednią akcję Bartosz Dev Bridge w tej samej rozmowie.\n\nDokładny błąd:\n${detail}\n\nOryginalna akcja:\n${boundedAction}\n\nZachowaj cel zadania, nie omijaj polityk i nie rozszerzaj zakresu. Wygeneruj dokładnie jeden poprawiony obiekt bdb-action-v1. Nie używaj zakończonego session_id; rozszerzenie automatycznie utworzy albo przypnie bezpieczną sesję naprawczą i uruchomi preflight przed wykonaniem.`;
  const sent = await bdbContentRepairAutoSend(prompt, marker);
  button.textContent = sent.sent
    ? "Naprawa wysłana w tej rozmowie"
    : `Naprawa oczekuje (${sent.reason})`;
  return sent.sent;
}

async function bdbContentRepairRun(found, response, output, button, compact) {
  button.disabled = true;
  button.textContent = "BDB: naprawianie…";
  try {
    const detail = bdbContentRepairDetail(found.entry, response, output);
    if (/content_sha256/i.test(detail) && /mismatch|does not match|nie zgadza/i.test(detail)) {
      const corrected = await bdbContentRepairHashes(found.entry.action);
      const repairAction = bdbContentRepairAsNewAttempt(found.entry, corrected);
      const result = await chrome.runtime.sendMessage({
        type: "BDB_SUBMIT_ACTION",
        action: repairAction
      });
      if (!result || result.ok !== true) {
        throw new Error(result && result.error ? result.error : "Brak odpowiedzi rozszerzenia po naprawie");
      }
      renderResult(output, result.response, { compact });
      button.textContent = "BDB: naprawiono i uruchomiono";
      return;
    }
    await bdbContentRepairRequestCorrection(found, detail, button);
  } catch (error) {
    button.textContent = `Naprawa zatrzymana: ${String(error && error.message ? error.message : error)}`;
  } finally {
    button.disabled = false;
  }
}

async function bdbContentRepairEnhance(output) {
  if (!(output instanceof HTMLElement) || output.dataset.bdbRepairReady === "true") {
    return;
  }
  const host = output.closest("pre") || output.parentElement && output.parentElement.parentElement;
  if (!(host instanceof HTMLElement)) {
    return;
  }
  const code = host.querySelector("code");
  if (!(code instanceof HTMLElement)) {
    return;
  }
  const action = parseAction(code);
  if (!action || typeof action.repo_alias !== "string") {
    return;
  }
  const found = await bdbContentRepairLatest(action.repo_alias);
  if (!found) {
    return;
  }
  const response = bdbContentRepairResponseFromOutput(output) || found.entry.response;
  if (!bdbContentRepairIsFailure(found.entry, response, output)) {
    return;
  }

  output.dataset.bdbRepairReady = "true";
  let controls = output.querySelector(".bdb-controls");
  if (!(controls instanceof HTMLElement)) {
    controls = document.createElement("div");
    controls.className = "bdb-controls";
    output.append(controls);
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-repair-retry";
  button.textContent = "Napraw i uruchom ponownie";
  const compact = Boolean(output.closest(".bdb-compact"));
  button.addEventListener("click", () => {
    void bdbContentRepairRun(found, response, output, button, compact);
  });
  controls.append(button);
}

function bdbContentRepairScan(root = document) {
  const outputs = [];
  if (root instanceof HTMLElement && root.matches(".bdb-output")) {
    outputs.push(root);
  }
  if (root.querySelectorAll) {
    outputs.push(...root.querySelectorAll(".bdb-output"));
  }
  for (const output of outputs) {
    void bdbContentRepairEnhance(output);
  }
}

bdbContentRepairScan(document);
const bdbContentRepairObserver = new MutationObserver((records) => {
  for (const record of records) {
    if (record.target instanceof HTMLElement) {
      bdbContentRepairScan(record.target);
    }
    for (const node of record.addedNodes) {
      if (node instanceof HTMLElement) {
        bdbContentRepairScan(node);
      }
    }
  }
});
bdbContentRepairObserver.observe(document.documentElement, {
  childList: true,
  subtree: true,
  characterData: true
});
