"use strict";

// ChatGPT may reconcile an assistant message by removing extension-owned children
// while preserving the original <code> node. content.js remembers processed nodes
// in a WeakSet, so release only nodes whose live BDB panel disappeared, then delegate
// to the mature scanner. Duplicate execution remains protected by the background
// replay guard keyed by <loop_id>:<iteration>.
const scanBeforeRerenderReconciliation = scan;
const BDB_DOCUMENT_RECONCILIATION_DELAY_MS = 200;
let bdbDocumentReconciliationTimer = null;

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

function containsRemovedBdbPanel(node) {
  return Boolean(
    node instanceof HTMLElement &&
    (
      node.classList.contains("bdb-assisted") ||
      node.querySelector(".bdb-assisted")
    )
  );
}

function elementTouchesCode(node) {
  return Boolean(
    node instanceof HTMLElement &&
    (
      node.matches("code, pre") ||
      node.closest("pre, code") ||
      node.querySelector("pre code, code")
    )
  );
}

function mutationMayAffectBdbAction(record) {
  if (record.type === "characterData") {
    return Boolean(
      record.target &&
      record.target.parentElement &&
      elementTouchesCode(record.target.parentElement)
    );
  }
  if (record.type !== "childList") {
    return false;
  }
  if (elementTouchesCode(record.target)) {
    return true;
  }
  if (Array.from(record.addedNodes).some(elementTouchesCode)) {
    return true;
  }
  return Array.from(record.removedNodes).some(containsRemovedBdbPanel);
}

function scheduleBdbDocumentReconciliation() {
  if (bdbDocumentReconciliationTimer !== null) {
    return;
  }
  bdbDocumentReconciliationTimer = setTimeout(() => {
    bdbDocumentReconciliationTimer = null;
    scan(document);
  }, BDB_DOCUMENT_RECONCILIATION_DELAY_MS);
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

// The original observer scans additions and character changes immediately. This
// companion observer covers two complementary cases:
// 1. React removes an extension-owned panel while keeping the same <code> node.
// 2. A streaming block is invalid during the immediate local scan but becomes a
//    complete action before a delayed whole-document reconciliation runs.
// It never submits an action directly; replay protection remains in background.js.
const bdbRemovedPanelObserver = new MutationObserver((records) => {
  let shouldReconcileDocument = false;
  for (const record of records) {
    if (
      record.type === "childList" &&
      record.target instanceof HTMLElement &&
      Array.from(record.removedNodes).some(containsRemovedBdbPanel)
    ) {
      scan(record.target);
    }
    if (mutationMayAffectBdbAction(record)) {
      shouldReconcileDocument = true;
    }
  }
  if (shouldReconcileDocument) {
    scheduleBdbDocumentReconciliation();
  }
});
bdbRemovedPanelObserver.observe(document.documentElement, {
  childList: true,
  subtree: true,
  characterData: true
});

// Reconcile once immediately in case ChatGPT rerendered between content.js startup
// and this companion script loading.
scan(document);
