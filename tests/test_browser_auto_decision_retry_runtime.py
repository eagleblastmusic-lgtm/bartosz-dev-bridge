from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_decision_retries_only_transient_sequence_gaps(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "auto-decision-retry-runtime.cjs"
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

            async function runScenario(responses, iteration = 3) {
              let calls = 0;
              const button = { disabled: false, textContent: "" };
              const output = { textContent: "" };
              const rendered = [];
              const context = {
                console,
                setTimeout(callback) {
                  callback();
                  return 1;
                },
                chrome: {
                  runtime: {
                    async sendMessage() {
                      const response = responses[Math.min(calls, responses.length - 1)];
                      calls += 1;
                      return { ok: true, response };
                    }
                  }
                },
                maybeAuto: async () => {},
                renderResult(_output, response) {
                  rendered.push(response);
                },
                autoSend: async () => ({ sent: true })
              };
              context.globalThis = context;
              vm.createContext(context);
              vm.runInContext(script, context, { filename: scriptPath });
              await context.maybeAuto(
                {
                  automation: {
                    mode: "auto",
                    loop_id: "loop",
                    iteration
                  }
                },
                button,
                output,
                true
              );
              return { calls, button, output, rendered };
            }

            async function main() {
              const recovered = await runScenario([
                {
                  executed: false,
                  reason: "non_sequential_iteration",
                  expectedIteration: 2
                },
                {
                  executed: false,
                  reason: "non_sequential_iteration",
                  expectedIteration: 3
                },
                {
                  executed: true,
                  response: { status: "completed" },
                  shouldContinue: false,
                  stopReason: "done"
                }
              ]);
              assert.equal(recovered.calls, 3, JSON.stringify(recovered));
              assert.equal(recovered.button.textContent, "BDB AUTO: zatrzymano (done)");
              assert.equal(recovered.rendered.length, 1);

              const stale = await runScenario([
                {
                  executed: false,
                  reason: "non_sequential_iteration",
                  expectedIteration: 4
                }
              ]);
              assert.equal(stale.calls, 1, JSON.stringify(stale));
              assert.equal(
                stale.button.textContent,
                "BDB: Wykonaj (non_sequential_iteration)"
              );

              const disabled = await runScenario([
                {
                  executed: false,
                  reason: "auto_disabled",
                  expectedIteration: 3
                }
              ]);
              assert.equal(disabled.calls, 1, JSON.stringify(disabled));
              assert.equal(disabled.button.textContent, "BDB: Wykonaj (auto_disabled)");
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
