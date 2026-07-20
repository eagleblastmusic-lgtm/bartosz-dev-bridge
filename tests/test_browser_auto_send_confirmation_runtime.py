from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_send_confirms_click_request_submit_and_enter_fallbacks(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "auto-send-confirmation-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const script = fs.readFileSync(process.argv[2], "utf8");

            async function runScenario({
              clearOn = null,
              initialText = "",
              composerAvailable = true,
              insertionMode = "stable",
              userMessageOn = null
            } = {}) {
              const actions = [];
              let prepareCalls = 0;
              let currentComposer = null;
              const userMessages = [];

              function maybeComplete(strategy) {
                actions.push(strategy);
                if (userMessageOn === strategy) {
                  userMessages.push({ textContent: currentComposer ? currentComposer.value : "" });
                }
                if (clearOn === strategy && currentComposer) {
                  currentComposer.value = "";
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
                  maybeComplete("button_click");
                }
              }
              const button = new FakeButton();
              const form = {
                querySelector(selector) {
                  return selector === "button[data-testid='send-button']" ? button : null;
                },
                requestSubmit() {
                  maybeComplete("request_submit");
                }
              };
              function makeComposer(value = "") {
                return {
                  value,
                  closest(selector) {
                    return selector === "form" ? form : null;
                  },
                  dispatchEvent(event) {
                    if (event.type === "keydown" && event.key === "Enter") {
                      maybeComplete("enter_key");
                    }
                    return true;
                  }
                };
              }
              currentComposer = composerAvailable ? makeComposer(initialText) : null;
              const replacementComposer = makeComposer("");
              const document = {
                querySelector(selector) {
                  return selector === "button[data-testid='send-button']" ? button : null;
                },
                querySelectorAll(selector) {
                  return selector === "[data-message-author-role='user']" ? userMessages : [];
                }
              };
              const context = {
                console,
                document,
                HTMLButtonElement: FakeButton,
                KeyboardEvent: FakeKeyboardEvent,
                setTimeout(callback) {
                  callback();
                  return 1;
                },
                autoSend: async () => ({ sent: false }),
                findComposer() {
                  return currentComposer;
                },
                composerText(value) {
                  return value.value;
                },
                prepareContinuation(text, options = {}) {
                  prepareCalls += 1;
                  if (!currentComposer) {
                    return null;
                  }
                  if (options.requireEmpty && currentComposer.value.trim() !== "") {
                    return null;
                  }
                  const preparedComposer = currentComposer;
                  if (insertionMode === "unobserved") {
                    return preparedComposer;
                  }
                  preparedComposer.value = text;
                  if (insertionMode === "rerender") {
                    replacementComposer.value = text;
                    currentComposer = replacementComposer;
                  }
                  return preparedComposer;
                },
                resultText(_response, marker) {
                  return `${marker}\nBDB_RESULT:\n{}`;
                }
              };
              context.globalThis = context;
              vm.createContext(context);
              vm.runInContext(script, context);
              const result = await context.autoSend({}, "loop", 3);
              return { result, actions, prepareCalls, currentComposer, replacementComposer };
            }

            async function main() {
              const clicked = await runScenario({ clearOn: "button_click" });
              assert.equal(clicked.result.sent, true, JSON.stringify(clicked.result));
              assert.equal(clicked.result.strategy, "button_click");
              assert.deepEqual(clicked.actions, ["button_click"]);

              const submitted = await runScenario({ clearOn: "request_submit" });
              assert.equal(submitted.result.sent, true, JSON.stringify(submitted.result));
              assert.equal(submitted.result.strategy, "request_submit");
              assert.deepEqual(submitted.actions, ["button_click", "request_submit"]);

              const entered = await runScenario({ clearOn: "enter_key" });
              assert.equal(entered.result.sent, true, JSON.stringify(entered.result));
              assert.equal(entered.result.strategy, "enter_key");
              assert.deepEqual(entered.actions, ["button_click", "request_submit", "enter_key"]);

              const messageConfirmed = await runScenario({ userMessageOn: "button_click" });
              assert.equal(messageConfirmed.result.sent, true, JSON.stringify(messageConfirmed.result));
              assert.equal(messageConfirmed.result.confirmedVia, "user_message");

              const rerendered = await runScenario({
                clearOn: "button_click",
                insertionMode: "rerender"
              });
              assert.equal(rerendered.result.sent, true, JSON.stringify(rerendered.result));
              assert.equal(rerendered.currentComposer, rerendered.replacementComposer);

              const blocked = await runScenario();
              assert.equal(blocked.result.sent, false, JSON.stringify(blocked.result));
              assert.equal(blocked.result.reason, "send_not_confirmed");
              assert.equal(blocked.result.markerStillPresent, true);
              assert.deepEqual(blocked.actions, ["button_click", "request_submit", "enter_key"]);

              const occupied = await runScenario({ initialText: "draft" });
              assert.equal(occupied.result.reason, "composer_not_empty");
              assert.equal(occupied.prepareCalls, 0);

              const missing = await runScenario({ composerAvailable: false });
              assert.equal(missing.result.reason, "composer_missing");

              const unobserved = await runScenario({ insertionMode: "unobserved" });
              assert.equal(unobserved.result.reason, "insertion_not_observed");
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
