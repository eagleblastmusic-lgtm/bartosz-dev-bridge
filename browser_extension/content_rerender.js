"use strict";

// ChatGPT may reconcile an assistant message by removing extension-owned children
// while preserving the original <code> node. content.js remembers processed nodes
// in a WeakSet, so release only nodes whose live BDB panel disappeared, then delegate
// to the mature scanner. Duplicate execution remains protected by the background
// replay guard keyed by <loop_id>:<iteration>.
const scanBeforeRerenderReconciliation = scan;

function bdbActionBlocks(root) {
  const blocks = [];
  if (root instanceof HTMLElement && root.matches("code")) {
    blocks.push(root);
  }
  if (root && typeof root.querySelectorAll === "function") {
    blocks.push(...root.querySelectorAll("pre code, code"));
  }
  return blocks;
}

function hasLiveBdbPanel(codeBlock) {
  const host = codeBlock.closest("pre") || codeBlock.parentElement;
  return Boolean(
    host instanceof HTMLElement &&
    host.querySelector(":scope > .bdb-assisted")
  );
}

scan = function scanWithRerenderReconciliation(root) {
  for (const block of bdbActionBlocks(root)) {
    if (
      block instanceof HTMLElement &&
      processedBlocks.has(block) &&
      !hasLiveBdbPanel(block)
    ) {
      processedBlocks.delete(block);
    }
  }
  scanBeforeRerenderReconciliation(root);
};

// Reconcile once immediately in case ChatGPT rerendered between content.js startup
// and this companion script loading.
scan(document);
