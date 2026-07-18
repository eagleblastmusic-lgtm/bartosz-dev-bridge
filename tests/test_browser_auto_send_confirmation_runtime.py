from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_send_retries_until_composer_is_consumed(tmp_path: Path) -> None:
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

            async function runScenario(clearAfterClick) {
              let clicks = 0;
              const composer = {
                value: "",
                closest(selector) {
                  return selector === "form" ? form : null;
                }
              };
              class FakeButton {
                constructor() {
                  this.disabled = false;
                }
                click() {
                  clicks += 1;
                  if (clearAfterClick !== null && clicks >= clearAfterClick) {
                    composer.value = "";
                  }
                }
              }
              const button = new FakeButton();
              const form = {
                querySelector(selector) {
                  return selector === "button[data-testid='send-button']" ? button : null;
                }
              };
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
                  return composer;
                },
                composerText(value) {
                  return value.value;
                },
                prepareContinuation(text) {
                  composer.value = text;
                  return composer;
                },
                resultText(_response, marker) {
                  return `${marker}\nBDB_RESULT:\n{}`;
                }
              };
              context.globalThis = context;
              vm.createContext(context);
              vm.runInContext(script, context, { filename: scriptPath });
              const result = await context.autoSend({}, "loop", 2);
              return { result, clicks };
            }

            async function main() {
              const retried = await runScenario(2);
              assert.equal(retried.result.sent, true, JSON.stringify(retried));
              assert.equal(retried.result.confirmed, true, JSON.stringify(retried));
              assert.equal(retried.clicks, 2, JSON.stringify(retried));

              const blocked = await runScenario(null);
              assert.equal(blocked.result.sent, false, JSON.stringify(blocked));
              assert.equal(blocked.result.reason, "send_not_confirmed", JSON.stringify(blocked));
              assert.equal(blocked.result.markerStillPresent, true, JSON.stringify(blocked));
              assert.equal(blocked.clicks, 3, JSON.stringify(blocked));
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
