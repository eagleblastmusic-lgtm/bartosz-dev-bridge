from __future__ import annotations

import runpy
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"
_LEGACY = runpy.run_path(
    str(Path(__file__).with_name("_browser_auto_composer_fast_path_runtime_legacy.py"))
)
test_auto_payload_cap_and_composer_read_avoid_live_layout_triggers = _LEGACY[
    "test_auto_payload_cap_and_composer_read_avoid_live_layout_triggers"
]


def _run_confirmed_fast_path(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("Node.js is required for the browser runtime contract")

    harness = tmp_path / "confirmed-fast-path.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const script = fs.readFileSync(process.argv[2], "utf8");

            const actions = [];
            const inputs = [];
            const limits = [];
            const messages = [];
            let replacements = 0;
            let legacyCalls = 0;
            let failDirect = false;

            function submit(strategy) {
              actions.push(strategy);
              messages.push({ textContent: composer.textContent });
              composer.textContent = "";
            }

            class Input {
              constructor(type, init) {
                this.type = type;
                this.init = init;
              }
            }
            class Keyboard {
              constructor(type, init) {
                this.type = type;
                this.key = init.key;
              }
            }
            class Button {
              constructor() {
                this.disabled = false;
              }
              click() {
                submit("button_click");
              }
            }

            const button = new Button();
            const form = {
              querySelector: (selector) => (
                selector === "button[data-testid='send-button']" ? button : null
              ),
              requestSubmit: () => submit("request_submit")
            };
            const composer = {
              isContentEditable: true,
              textContent: "",
              focus() {},
              closest: (selector) => selector === "form" ? form : null,
              replaceChildren(node) {
                if (failDirect) throw new Error("direct insertion failure");
                replacements += 1;
                this.textContent = node.textContent || "";
              },
              dispatchEvent(event) {
                inputs.push(event);
                if (event.type === "keydown" && event.key === "Enter") {
                  submit("enter_key");
                }
                return true;
              }
            };
            const document = {
              createElement(tag) {
                assert.equal(tag, "p");
                return { textContent: "" };
              },
              querySelector: (selector) => (
                selector === "button[data-testid='send-button']" ? button : null
              ),
              querySelectorAll: (selector) => (
                selector === "[data-message-author-role='user']" ? messages : []
              )
            };
            const context = {
              console,
              document,
              InputEvent: Input,
              KeyboardEvent: Keyboard,
              HTMLButtonElement: Button,
              setTimeout(callback) {
                callback();
                return 1;
              },
              autoSend: async () => ({ sent: false }),
              findComposer: () => composer,
              composerText: (value) => value.textContent || "",
              prepareContinuation(text) {
                legacyCalls += 1;
                composer.textContent = text;
                return composer;
              },
              autoResultText(_response, marker, maxBytes) {
                limits.push(maxBytes);
                return `${marker}\nBDB_RESULT:\n${JSON.stringify({ status: "success" })}`;
              },
              resultText: (_response, marker) => `${marker}\nBDB_RESULT:\n{}`
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(script, context, { filename: process.argv[2] });

            async function main() {
              const first = await context.autoSend({}, "fast-loop", 1);
              assert.equal(first.sent, true, JSON.stringify(first));
              assert.equal(first.confirmed, true, JSON.stringify(first));
              assert.equal(first.confirmedVia, "user_message");
              assert.equal(first.strategy, "button_click");
              assert.deepEqual(actions, ["button_click"]);
              assert.equal(replacements, 1);
              assert.equal(legacyCalls, 0);
              assert.equal(inputs.length, 1);
              assert.equal(inputs[0].type, "input");
              assert.equal(inputs[0].init.inputType, "insertText");
              assert.deepEqual(limits, [16 * 1024]);

              context.autoResultText = (_response, marker, maxBytes) => {
                limits.push(maxBytes);
                return `${marker}\nBDB_RESULT:\n${"x".repeat(12 * 1024)}`;
              };
              const large = await context.autoSend({}, "fast-loop", 2);
              assert.equal(large.sent, true, JSON.stringify(large));
              assert.equal(large.confirmedVia, "user_message");
              assert.equal(replacements, 2);

              const manual = `BDB_RESULT:\n${"m".repeat(12 * 1024)}`;
              assert.equal(await context.bdbPrepareManualContinuation(manual), true);
              assert.equal(composer.textContent, manual);
              assert.equal(replacements, 3);
              assert.equal(legacyCalls, 0);
              composer.textContent = "";

              failDirect = true;
              context.autoResultText = (_response, marker, maxBytes) => {
                limits.push(maxBytes);
                const body = maxBytes <= 4 * 1024
                  ? "safe-fallback"
                  : "z".repeat(12 * 1024);
                return `${marker}\nBDB_RESULT:\n${body}`;
              };
              const fallback = await context.autoSend({}, "fast-loop", 3);
              assert.equal(fallback.sent, true, JSON.stringify(fallback));
              assert.equal(fallback.confirmedVia, "user_message");
              assert.equal(legacyCalls, 1);
              assert.deepEqual(limits, [
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


def test_auto_contenteditable_uses_single_dom_replacement_without_legacy_insertion(
    tmp_path: Path,
) -> None:
    _run_confirmed_fast_path(tmp_path)


class AutoComposerConfirmedFastPathTests(unittest.TestCase):
    def test_confirmed_fast_path_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _run_confirmed_fast_path(Path(tempdir))

    def test_payload_cap_and_composer_read_contract(self) -> None:
        test_auto_payload_cap_and_composer_read_avoid_live_layout_triggers()


if __name__ == "__main__":
    unittest.main()
