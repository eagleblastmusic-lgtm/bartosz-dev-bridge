from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_auto_ui_work_is_deduplicated_bounded_and_detachment_aware() -> None:
    retry = read("content_auto_retry.js")
    match = re.search(
        r"const BDB_AUTO_DECISION_RETRY_ATTEMPTS = ([0-9]+);",
        retry,
    )
    assert match is not None
    assert 4 <= int(match.group(1)) <= 40
    in_progress_match = re.search(
        r"const BDB_AUTO_IN_PROGRESS_RETRY_ATTEMPTS = ([0-9]+);",
        retry,
    )
    assert in_progress_match is not None
    assert 240 <= int(in_progress_match.group(1)) <= 360
    assert "function bdbRecoverDetachedAutoPanel(action, phase)" in retry
    assert '"auto_panel_detached"' in retry
    assert '"auto_panel_detachments"' in retry
    assert "scheduleBdbDocumentReconciliation()" in retry
    assert "const BDB_AUTO_ACTIVE_RUNS = new Map();" in retry
    assert "function bdbAutoRunKey(action)" in retry
    assert "function bdbAutoPanelDetached(button)" in retry
    assert "button.isConnected === false" in retry
    assert 'directParent.classList.contains("bdb-assisted")' in retry
    assert 'typeof directParent.className === "string"' in retry
    assert 'directParent.className.split(/\\s+/).includes("bdb-assisted")' in retry
    assert "directParent.parentElement === null" in retry
    assert "BDB_AUTO_ACTIVE_RUNS.get(runKey)" in retry
    assert "BDB_AUTO_ACTIVE_RUNS.set(runKey, entry)" in retry
    assert "active.button" in retry
    assert "active.promise" in retry
    assert "BDB_AUTO_ACTIVE_RUNS.delete(runKey)" in retry
    assert "BDB_AUTO_ACTIVE_RUNS.get(runKey) === entry" in retry
    assert "retryForReplacement" in retry


def test_rerender_observer_ignores_extension_owned_mutations_and_debounces() -> None:
    rerender = read("content_rerender.js")
    assert "function bdbMutationNodeBelongsToPanel(node)" in rerender
    assert 'element.closest(".bdb-assisted")' in rerender
    assert "!bdbMutationNodeBelongsToPanel(record.target)" in rerender
    assert "!bdbMutationNodeBelongsToPanel(node)" in rerender
    assert "clearTimeout(bdbDocumentReconciliationTimer)" in rerender
    delay = re.search(
        r"const BDB_DOCUMENT_RECONCILIATION_DELAY_MS = ([0-9]+);",
        rerender,
    )
    assert delay is not None
    assert 400 <= int(delay.group(1)) <= 1500


def test_duplicate_auto_panels_share_one_active_decision_run(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "auto-ui-freeze-dedupe-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const scriptPath = path.join(process.argv[2], "content_auto_retry.js");
            const script = fs.readFileSync(scriptPath, "utf8");

            let decisionCalls = 0;
            let releaseDecision = null;
            const context = {
              console,
              setTimeout,
              clearTimeout,
              chrome: {
                runtime: {
                  sendMessage(message) {
                    if (message.type === "BDB_MARK_AUTO_RESULT_DELIVERED") {
                      return Promise.resolve({ ok: true, response: { marked: true } });
                    }
                    assert.equal(message.type, "BDB_CONSIDER_AUTO");
                    decisionCalls += 1;
                    return new Promise((resolve) => {
                      releaseDecision = resolve;
                    });
                  }
                }
              },
              maybeAuto: async () => {},
              renderResult() {},
              autoSend: async () => ({ sent: false, reason: "not_needed" })
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(script, context, { filename: scriptPath });

            const action = {
              automation: {
                mode: "auto",
                loop_id: "same-loop",
                iteration: 4
              }
            };
            const buttonA = { disabled: false, textContent: "", isConnected: true };
            const buttonB = { disabled: false, textContent: "", isConnected: true };
            const outputA = { textContent: "" };
            const outputB = { textContent: "" };

            async function main() {
              const first = context.maybeAuto(action, buttonA, outputA, true);
              await Promise.resolve();
              const second = context.maybeAuto(action, buttonB, outputB, true);
              await Promise.resolve();

              assert.equal(decisionCalls, 1, "duplicate panels must share one active decision");
              assert.equal(typeof releaseDecision, "function");
              releaseDecision({
                ok: true,
                response: {
                  executed: false,
                  reason: "auto_disabled"
                }
              });
              await Promise.all([first, second]);
              assert.equal(decisionCalls, 1);
              assert.equal(buttonA.disabled, false);
              assert.equal(buttonB.disabled, false);
            }

            main().catch((error) => {
              console.error(error && error.stack ? error.stack : error);
              process.exitCode = 1;
            });
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_panel_text_mutation_does_not_schedule_document_scan(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "rerender-owned-mutation-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const scriptPath = path.join(process.argv[2], "content_rerender.js");
            const script = fs.readFileSync(scriptPath, "utf8");

            class FakeClassList {
              constructor(names = []) { this.values = new Set(names); }
              contains(name) { return this.values.has(name); }
            }

            class FakeElement {
              constructor(tagName = "div", classes = []) {
                this.tagName = String(tagName).toUpperCase();
                this.classList = new FakeClassList(classes);
                this.children = [];
                this.parentElement = null;
              }
              append(child) {
                child.parentElement = this;
                this.children.push(child);
              }
              matches(selector) {
                if (selector === "code") return this.tagName === "CODE";
                if (selector === "code, pre" || selector === "pre, code") {
                  return this.tagName === "CODE" || this.tagName === "PRE";
                }
                return false;
              }
              closest(selector) {
                let current = this;
                while (current) {
                  if (selector === ".bdb-assisted" && current.classList.contains("bdb-assisted")) {
                    return current;
                  }
                  if ((selector === "pre, code" || selector === "code, pre") &&
                      (current.tagName === "PRE" || current.tagName === "CODE")) {
                    return current;
                  }
                  current = current.parentElement;
                }
                return null;
              }
              querySelector(selector) {
                if (selector === ".bdb-assisted") {
                  return this.children.find((child) => child.classList.contains("bdb-assisted")) || null;
                }
                return null;
              }
              querySelectorAll() { return []; }
            }

            let observerCallback = null;
            class FakeMutationObserver {
              constructor(callback) { observerCallback = callback; }
              observe() {}
            }

            let scheduled = 0;
            const document = new FakeElement("document");
            document.documentElement = document;
            const context = {
              console,
              document,
              HTMLElement: FakeElement,
              MutationObserver: FakeMutationObserver,
              processedBlocks: new WeakSet(),
              scan() {},
              setTimeout(callback) {
                scheduled += 1;
                return scheduled;
              },
              clearTimeout() {}
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(script, context, { filename: scriptPath });
            assert.equal(typeof observerCallback, "function");

            const pre = new FakeElement("pre");
            const panel = new FakeElement("div", ["bdb-assisted"]);
            const button = new FakeElement("button");
            pre.append(panel);
            panel.append(button);

            observerCallback([{
              type: "characterData",
              target: { parentElement: button },
              addedNodes: [],
              removedNodes: []
            }]);
            assert.equal(scheduled, 0, "extension-owned text must not trigger a document scan");

            const code = new FakeElement("code");
            const streamingPre = new FakeElement("pre");
            streamingPre.append(code);
            observerCallback([{
              type: "characterData",
              target: { parentElement: code },
              addedNodes: [],
              removedNodes: []
            }]);
            assert.equal(scheduled, 1, "streaming code still requires one debounced scan");
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
