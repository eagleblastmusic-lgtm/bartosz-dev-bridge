"use strict";

const aliasInput = document.getElementById("alias");
const output = document.getElementById("output");
const autoEnabled = document.getElementById("auto-enabled");
const autoIterations = document.getElementById("auto-iterations");
const autoMinutes = document.getElementById("auto-minutes");

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

loadSettings();
