"use strict";

const AUTO_STATE_PREFIX = "bdbAuto:";
const aliasInput = document.getElementById("alias");
const output = document.getElementById("output");
const autoEnabled = document.getElementById("auto-enabled");
const autoShadow = document.getElementById("auto-shadow");
const autoIterations = document.getElementById("auto-iterations");
const autoMinutes = document.getElementById("auto-minutes");
const autoState = document.getElementById("auto-state");
const versionLabel = document.getElementById("version");
const taskState = document.getElementById("task-state");
const resumeTaskButton = document.getElementById("resume-task");
const cancelTaskButton = document.getElementById("cancel-task");
let latestTaskLoopId = null;

function isAutoState(value) {
  return Boolean(
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Number.isInteger(value.lastIteration) &&
    value.lastIteration >= 0
  );
}

function stateTimestamp(state) {
  if (Number.isFinite(state.updatedAt)) {
    return state.updatedAt;
  }
  return Number.isFinite(state.startedAt) ? state.startedAt : 0;
}

async function loadAutoState() {
  try {
    const snapshot = await chrome.storage.session.get(null);
    const entries = Object.entries(snapshot)
      .filter(([key, value]) => key.startsWith(AUTO_STATE_PREFIX) && isAutoState(value))
      .sort((left, right) => stateTimestamp(right[1]) - stateTimestamp(left[1]));
    if (entries.length === 0) {
      autoState.textContent = "Brak aktywnej pętli AUTO.";
      return;
    }

    const [key, state] = entries[0];
    const loopId = key.slice(AUTO_STATE_PREFIX.length);
    const expectedIteration = state.lastIteration + 1;
    autoState.textContent = [
      `Pętla: ${loopId}`,
      `Status: ${state.status || "nieznany"}`,
      `Ostatnia iteracja: ${state.lastIteration}`,
      `Oczekiwana iteracja: ${expectedIteration}`
    ].join("\n");
  } catch (error) {
    autoState.textContent = `Stan AUTO niedostępny: ${String(error && error.message ? error.message : error)}`;
  }
}

async function loadTasks() {
  try {
    const result = await chrome.runtime.sendMessage({ type: "BDB_TASKS" });
    const tasks = result && result.ok === true && Array.isArray(result.response.tasks)
      ? result.response.tasks
      : [];
    if (tasks.length === 0) {
      latestTaskLoopId = null;
      taskState.textContent = "Brak trwałych zadań.";
      resumeTaskButton.disabled = true;
      cancelTaskButton.disabled = true;
      return;
    }
    const task = tasks[0];
    latestTaskLoopId = task.loop_id;
    taskState.textContent = [
      `Zadanie: ${task.title || task.loop_id}`,
      `Faza: ${task.phase || "nieznana"}`,
      `Status: ${task.status || "nieznany"}`,
      `Ostatnia iteracja: ${task.last_iteration || 0}`,
      `Następna iteracja: ${task.expected_iteration || ((task.last_iteration || 0) + 1)}`,
      `Złożoność: ${(task.complexity && task.complexity.class) || "nieznana"}`
    ].join("\n");
    resumeTaskButton.disabled = task.status === "running";
    cancelTaskButton.disabled = task.status === "cancelled";
  } catch (error) {
    taskState.textContent = `Rejestr zadań niedostępny: ${String(error && error.message ? error.message : error)}`;
  }
}

async function loadSettings() {
  versionLabel.textContent = `Wersja rozszerzenia: ${chrome.runtime.getManifest().version}`;
  const aliasStored = await chrome.storage.local.get("repoAlias");
  if (typeof aliasStored.repoAlias === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(aliasStored.repoAlias)) {
    aliasInput.value = aliasStored.repoAlias;
  }
  const result = await chrome.runtime.sendMessage({ type: "BDB_GET_AUTO_SETTINGS" });
  if (result && result.ok === true) {
    autoEnabled.checked = result.response.autoEnabled === true;
    autoShadow.checked = result.response.autoShadowMode === true;
    autoIterations.value = String(result.response.autoMaxIterations);
    autoMinutes.value = String(result.response.autoMaxMinutes);
  }
  await loadAutoState();
  await loadTasks();
}

async function run(message) {
  output.textContent = "Łączenie…";
  try {
    const result = await chrome.runtime.sendMessage(message);
    output.textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    output.textContent = String(error && error.message ? error.message : error);
  }
}

document.getElementById("status").addEventListener("click", () => run({ type: "BDB_STATUS" }));
document.getElementById("health").addEventListener("click", () => run({
  type: "BDB_HEALTH",
  probeNative: true
}));
document.getElementById("context").addEventListener("click", async () => {
  const repoAlias = aliasInput.value.trim();
  if (!/^[a-z][a-z0-9-]{0,31}$/.test(repoAlias)) {
    output.textContent = "Nieprawidłowy alias.";
    return;
  }
  await chrome.storage.local.set({ repoAlias });
  await run({ type: "BDB_CONTEXT", repoAlias });
});
document.getElementById("save-auto").addEventListener("click", async () => {
  const settings = {
    autoEnabled: autoEnabled.checked,
    autoShadowMode: autoShadow.checked,
    autoMaxIterations: Number(autoIterations.value),
    autoMaxMinutes: Number(autoMinutes.value)
  };
  await run({ type: "BDB_SET_AUTO_SETTINGS", settings });
});

document.getElementById("test-auto").addEventListener("click", async () => {
  const repoAlias = aliasInput.value.trim();
  if (!/^[a-z][a-z0-9-]{0,31}$/.test(repoAlias)) {
    output.textContent = "Nieprawidłowy alias.";
    return;
  }
  if (!window.confirm("Test wykona bezpieczny odczyt workspace_context i wyśle jego wynik do pustego pola rozmowy ChatGPT. Kontynuować?")) {
    return;
  }
  output.textContent = "Testowanie całego łańcucha AUTO…";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !Number.isInteger(tab.id)) {
      throw new Error("Nie znaleziono aktywnej karty ChatGPT");
    }
    const result = await chrome.tabs.sendMessage(tab.id, {
      type: "BDB_CONTENT_SELF_TEST",
      repoAlias
    });
    output.textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    output.textContent = `Test AUTO nieudany: ${String(error && error.message ? error.message : error)}`;
  }
});

resumeTaskButton.addEventListener("click", async () => {
  if (latestTaskLoopId) {
    await run({ type: "BDB_RESUME_TASK", loopId: latestTaskLoopId });
    await loadTasks();
  }
});

cancelTaskButton.addEventListener("click", async () => {
  if (latestTaskLoopId) {
    await run({ type: "BDB_CANCEL_TASK", loopId: latestTaskLoopId });
    await loadTasks();
  }
});

document.getElementById("clear-cache").addEventListener("click", async () => {
  await run({ type: "BDB_CLEAR_READ_CACHE" });
});

document.getElementById("export-diagnostics").addEventListener("click", async () => {
  output.textContent = "Przygotowywanie diagnostyki…";
  try {
    const result = await chrome.runtime.sendMessage({ type: "BDB_AUTO_DIAGNOSTICS" });
    if (!result || result.ok !== true) {
      throw new Error(result && result.error ? result.error : "Brak diagnostyki");
    }
    globalThis.bdbDownloadDiagnosticsZip(result.response);
    output.textContent = "Zapisano bezpieczną diagnostykę ZIP.";
  } catch (error) {
    output.textContent = `Eksport nieudany: ${String(error && error.message ? error.message : error)}`;
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (
    areaName === "session" &&
    Object.keys(changes).some((key) => key.startsWith(AUTO_STATE_PREFIX))
  ) {
    loadAutoState();
  }
  if (areaName === "local") {
    loadTasks();
  }
});

loadSettings();
