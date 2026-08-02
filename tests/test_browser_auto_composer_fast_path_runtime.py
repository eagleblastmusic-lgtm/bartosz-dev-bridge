from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_contenteditable_uses_single_dom_replacement_without_legacy_insertion(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "auto-composer-fast-path-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const script = fs.readFileSync(process.argv[2], "utf8");

            const actions = [];
            const inputEvents = [];
            const requestedLimits = [];
            let replacementCount = 0;
            let legacyCalls = 0;
            let failDirectInsertion = false;

            class FakeInputEvent {
              constructor(type, init) {
                this.type = type;
                this.init = init;
              }
            }
            class FakeKeyboardEvent {
              constructor(type, init) {
                this.type = type;
                this.key = init.key;
              }
            }
            class FakeButton {
              constructor() {
                this.disabled = false;
              }
              click() {
                actions.push("button_click");
                composer.textContent = "";
              }
            }

            const button = new FakeButton();
            const form = {
              querySelector(selector) {
                return selector === "button[data-testid='send-button']" ? button : null;
              },
              requestSubmit() {
                actions.push("request_submit");
                composer.textContent = "";
              }
            };
            const composer = {
              isContentEditable: true,
              textContent: "",
              focus() {},
              closest(selector) {
                return selector === "form" ? form : null;
              },
              replaceChildren(node) {
                if (failDirectInsertion) {
                  throw new Error("synthetic direct insertion failure");
                }
                replacementCount += 1;
                this.textContent = node.textContent || "";
              },
              dispatchEvent(event) {
                inputEvents.push(event);
                return true;
              }
            };
            const document = {
              createElement(tag) {
                assert.equal(tag, "p");
                return { textContent: "" };
              },
              querySelector(selector) {
                return selector === "button[data-testid='send-button']" ? button : null;
              },
              querySelectorAll(selector) {
                return selector === "[data-message-author-role='user']" ? [] : [];
              }
            };
            const context = {
              console,
              document,
              InputEvent: FakeInputEvent,
              KeyboardEvent: FakeKeyboardEvent,
              HTMLButtonElement: FakeButton,
              setTimeout(callback) {
                callback();
                return 1;
              },
              autoSend: async () => ({ sent: false }),
              findComposer() {
                return composer;
              },
              composerText(value) {
                return value.textContent || "";
              },
              prepareContinuation(text) {
                legacyCalls += 1;
                composer.textContent = text;
                return composer;
              },
              autoResultText(_response, marker, maxBytes) {
                requestedLimits.push(maxBytes);
                return `${marker}\nBDB_RESULT:\n${JSON.stringify({ status: "success", operation: "workspace_context" })}`;
              },
              resultText(_response, marker) {
                return `${marker}\nBDB_RESULT:\n{}`;
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(script, context, { filename: process.argv[2] });

            async function main() {
              const result = await context.autoSend({}, "fast-loop", 1);
              assert.equal(result.sent, true, JSON.stringify(result));
              assert.equal(result.strategy, "button_click");
              assert.deepEqual(actions, ["button_click"]);
              assert.equal(replacementCount, 1);
              assert.equal(legacyCalls, 0);
              assert.equal(inputEvents.length, 1);
              assert.equal(inputEvents[0].type, "input");
              assert.equal(inputEvents[0].init.inputType, "insertText");
              assert.deepEqual(requestedLimits, [16 * 1024]);

              context.autoResultText = (_response, marker, maxBytes) => {
                requestedLimits.push(maxBytes);
                return `${marker}\nBDB_RESULT:\n${"x".repeat(12 * 1024)}`;
              };
              const large = await context.autoSend({}, "fast-loop", 2);
              assert.equal(large.sent, true, JSON.stringify(large));
              assert.equal(replacementCount, 2);
              assert.deepEqual(requestedLimits, [16 * 1024, 16 * 1024]);

              const manualText = `BDB_RESULT:\n${"m".repeat(12 * 1024)}`;
              const manualPrepared = await context.bdbPrepareManualContinuation(manualText);
              assert.equal(manualPrepared, true);
              assert.equal(composer.textContent, manualText);
              assert.equal(replacementCount, 3);
              assert.equal(legacyCalls, 0);
              composer.textContent = "";

              failDirectInsertion = true;
              context.autoResultText = (_response, marker, maxBytes) => {
                requestedLimits.push(maxBytes);
                const body = maxBytes <= 4 * 1024 ? "safe-fallback" : "z".repeat(12 * 1024);
                return `${marker}\nBDB_RESULT:\n${body}`;
              };
              const fallback = await context.autoSend({}, "fast-loop", 3);
              assert.equal(fallback.sent, true, JSON.stringify(fallback));
              assert.equal(legacyCalls, 1);
              assert.deepEqual(requestedLimits, [
                16 * 1024,
                16 * 1024,
                16 * 1024,
                4 * 1024
              ]);
            }

            main().catch((error) => {
              console.error(error.stack || error);
              process.exitCode = 1;
            });
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION / "content_auto_send.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_auto_payload_cap_and_composer_read_avoid_live_layout_triggers() -> None:
    content = (EXTENSION / "content.js").read_text(encoding="utf-8")
    auto_send = (EXTENSION / "content_auto_send.js").read_text(encoding="utf-8")

    assert "const BDB_AUTO_CONTINUATION_TARGET_BYTES = 12 * 1024;" in content
    assert "const BDB_AUTO_CONTINUATION_MAX_BYTES = 16 * 1024;" in content
    assert "const BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES = 4 * 1024;" in content
    assert "const BDB_AUTO_TRACKED_PATH_LIMIT = 20;" in content
    assert "const BDB_AUTO_SYMBOL_LIMIT = 8;" in content
    assert "snapshot_paths_omitted_for_auto" in content

    composer_start = content.index("function composerText(composer)")
    composer_end = content.index("function prepareContinuation", composer_start)
    composer_body = content[composer_start:composer_end]
    assert "innerText" not in composer_body
    assert "textContent" in composer_body

    assert "function bdbPrepareAutoContinuation" in auto_send
    assert "composer.replaceChildren(paragraph)" in auto_send
    assert "let prepared = bdbPrepareAutoContinuation(" in auto_send
    assert "initial.composer" in auto_send
    assert ": 16 * 1024;" in auto_send
    assert "legacyContinuationMaxBytes" in auto_send
