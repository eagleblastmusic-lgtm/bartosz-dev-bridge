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
              let deliveryCalls = 0;
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
                    async sendMessage(message) {
                      if (message && message.type === "BDB_MARK_AUTO_RESULT_DELIVERED") {
                        deliveryCalls += 1;
                        return { ok: true, response: { marked: true } };
                      }
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
              return { calls, deliveryCalls, button, output, rendered };
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
                  loopId: "loop",
                  iteration: 3,
                  shouldContinue: false,
                  stopReason: "done"
                }
              ]);
              assert.equal(recovered.calls, 3, JSON.stringify(recovered));
              assert.equal(recovered.deliveryCalls, 1, JSON.stringify(recovered));
              assert.equal(
                recovered.button.textContent,
                "BDB AUTO: wynik wysłany; zatrzymano (done)"
              );
              assert.equal(recovered.rendered.length, 1);

              const delayedCompletion = await runScenario([
                ...Array.from({ length: 30 }, () => ({
                  executed: false,
                  reason: "iteration_in_progress",
                  expectedIteration: 3
                })),
                {
                  executed: true,
                  response: { status: "completed", recovered: true },
                  loopId: "loop",
                  iteration: 3,
                  recoveredResult: true,
                  durableCheckpoint: true,
                  shouldContinue: false,
                  stopReason: "needs_user"
                }
              ]);
              assert.equal(delayedCompletion.calls, 31, JSON.stringify(delayedCompletion));
              assert.equal(delayedCompletion.deliveryCalls, 1, JSON.stringify(delayedCompletion));
              assert.equal(delayedCompletion.rendered.length, 1);
              assert.equal(
                delayedCompletion.button.textContent,
                "BDB AUTO: wynik wysłany; zatrzymano (needs_user)"
              );

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
