from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_send_reacquires_live_composer_and_confirms_consumption(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "auto-send-confirmation-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const scriptPath = path.join(process.argv[2], "content_auto_send.js");
            const script = fs.readFileSync(scriptPath, "utf8");

            async function runScenario({
              clearAfterClick = 1,
              initialText = "",
              composerAvailable = true,
              insertionMode = "stable"
            } = {}) {
              let clicks = 0;
              let prepareCalls = 0;

              const form = {
                querySelector(selector) {
                  return selector === "button[data-testid='send-button']" ? button : null;
                }
              };
              function makeComposer(value = "") {
                return {
                  value,
                  closest(selector) {
                    return selector === "form" ? form : null;
                  }
                };
              }

              let currentComposer = composerAvailable ? makeComposer(initialText) : null;
              const replacementComposer = makeComposer("");

              class FakeButton {
                constructor() {
                  this.disabled = false;
                }
                click() {
                  clicks += 1;
                  if (
                    clearAfterClick !== null &&
                    clicks >= clearAfterClick &&
                    currentComposer
                  ) {
                    currentComposer.value = "";
                  }
                }
              }
              const button = new FakeButton();
              const document = {
                querySelector(selector) {
                  return selector === "button[data-testid='send-button']" ? button : null;
                }
              };
              const context = {
                console,
                document,
                HTMLButtonElement: FakeButton,
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
              vm.runInContext(script, context, { filename: scriptPath });
              const result = await context.autoSend({}, "calculator2-p01_auto:2026.07.18", 2);
              return { result, clicks, prepareCalls, currentComposer, replacementComposer };
            }

            async function main() {
              const retried = await runScenario({ clearAfterClick: 2 });
              assert.equal(retried.result.sent, true, JSON.stringify(retried.result));
              assert.equal(retried.result.confirmed, true, JSON.stringify(retried.result));
              assert.equal(retried.clicks, 2, JSON.stringify(retried.result));

              const rerendered = await runScenario({
                clearAfterClick: 1,
                insertionMode: "rerender"
              });
              assert.equal(rerendered.result.sent, true, JSON.stringify(rerendered.result));
              assert.equal(rerendered.result.confirmed, true, JSON.stringify(rerendered.result));
              assert.equal(rerendered.clicks, 1, JSON.stringify(rerendered.result));
              assert.equal(rerendered.currentComposer, rerendered.replacementComposer);

              const blocked = await runScenario({ clearAfterClick: null });
              assert.equal(blocked.result.sent, false, JSON.stringify(blocked.result));
              assert.equal(blocked.result.reason, "send_not_confirmed", JSON.stringify(blocked.result));
              assert.equal(blocked.result.markerStillPresent, true, JSON.stringify(blocked.result));
              assert.equal(blocked.clicks, 3, JSON.stringify(blocked.result));

              const occupied = await runScenario({ initialText: "draft from user" });
              assert.equal(occupied.result.sent, false, JSON.stringify(occupied.result));
              assert.equal(occupied.result.reason, "composer_not_empty", JSON.stringify(occupied.result));
              assert.equal(occupied.prepareCalls, 0);
              assert.equal(occupied.clicks, 0);

              const missing = await runScenario({ composerAvailable: false });
              assert.equal(missing.result.sent, false, JSON.stringify(missing.result));
              assert.equal(missing.result.reason, "composer_missing", JSON.stringify(missing.result));
              assert.equal(missing.prepareCalls, 0);

              const unobserved = await runScenario({ insertionMode: "unobserved" });
              assert.equal(unobserved.result.sent, false, JSON.stringify(unobserved.result));
              assert.equal(unobserved.result.reason, "insertion_not_observed", JSON.stringify(unobserved.result));
              assert.equal(unobserved.clicks, 0);
            }

            main().catch((error) => {
              console.error(error && error.stack ? error.stack : error);
              process.exitCode = 1;
            });
            """
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
