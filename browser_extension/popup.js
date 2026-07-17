"use strict";

const AUTO_STATE_PREFIX = "bdbAuto:";
const aliasInput = document.getElementById("alias");
const output = document.getElementById("output");
const autoEnabled = document.getElementById("auto-enabled");
const autoIterations = document.getElementById("auto-iterations");
const autoMinutes = document.getElementById("auto-minutes");
const autoState = document.getElementById("auto-state");

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

async function loadSettings() {
  const aliasStored = await chrome.storage.local.get("repoAlias");
  if (typeof aliasStored.repoAlias === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(aliasStored.repoAlias)) {
    aliasInput.value = aliasStored.repoAlias;
  }
  const result = await chrome.runtime.sendMessage({ type: "BDB_GET_AUTO_SETTINGS" });
  if (result && result.ok === true) {
    autoEnabled.checked = result.response.autoEnabled === true;
    autoIterations.value = String(result.response.autoMaxIterations);
    autoMinutes.value = String(result.response.autoMaxMinutes);
  }
  await loadAutoState();
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
    autoMaxIterations: Number(autoIterations.value),
    autoMaxMinutes: Number(autoMinutes.value)
  };
  await run({ type: "BDB_SET_AUTO_SETTINGS", settings });
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (
    areaName === "session" &&
    Object.keys(changes).some((key) => key.startsWith(AUTO_STATE_PREFIX))
  ) {
    loadAutoState();
  }
});

loadSettings();
