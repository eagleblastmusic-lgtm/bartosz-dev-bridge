"use strict";

const aliasInput = document.getElementById("alias");
const output = document.getElementById("output");

async function loadAlias() {
  const stored = await chrome.storage.local.get("repoAlias");
  if (typeof stored.repoAlias === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(stored.repoAlias)) {
    aliasInput.value = stored.repoAlias;
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

loadAlias();
