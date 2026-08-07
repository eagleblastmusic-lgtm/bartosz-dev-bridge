from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


class AutoDecisionRetryRuntimeTests(unittest.TestCase):
    def test_marks_only_confirmed_user_message_delivery(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required")

        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "retry.cjs"
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

                    async function run(responses, sendResult, { resume = false } = {}) {
                      let calls = 0;
                      let delivered = 0;
                      const button = { disabled: false, textContent: "" };
                      const output = { textContent: "" };
                      const context = {
                        console,
                        setTimeout(callback) { callback(); return 1; },
                        chrome: {
                          runtime: {
                            async sendMessage(message) {
                              if (message.type === "BDB_MARK_AUTO_RESULT_DELIVERED") {
                                delivered += 1;
                                return { ok: true };
                              }
                              const response = responses[Math.min(calls, responses.length - 1)];
                              calls += 1;
                              return { ok: true, response };
                            }
                          }
                        },
                        maybeAuto: async () => {},
                        renderResult() {},
                        autoSend: async () => typeof sendResult === "function" ? sendResult() : sendResult
                      };
                      context.globalThis = context;
                      vm.createContext(context);
                      vm.runInContext(script, context, { filename: scriptPath });
                      let resumeResult = null;
                      if (resume) {
                        resumeResult = await context.bdbRetryResumedTask(
                          "loop",
                          3,
                          null,
                          { status: "recovered" }
                        );
                      } else {
                        await context.maybeAuto(
                          { automation: { mode: "auto", loop_id: "loop", iteration: 3 } },
                          button,
                          output,
                          true
                        );
                      }
                      return { calls, delivered, button, resumeResult };
                    }

                    const completed = {
                      executed: true,
                      response: { status: "completed" },
                      loopId: "loop",
                      iteration: 3,
                      shouldContinue: false,
                      stopReason: "done"
                    };

                    async function main() {
                      const confirmed = await run([
                        {
                          executed: false,
                          reason: "non_sequential_iteration",
                          expectedIteration: 3
                        },
                        completed
                      ], {
                        sent: true,
                        confirmed: true,
                        confirmedVia: "user_message"
                      });
                      assert.equal(confirmed.calls, 2);
                      assert.equal(confirmed.delivered, 1);

                      const recoveredResume = await run(
                        [],
                        {
                          sent: true,
                          confirmed: true,
                          confirmedVia: "user_message"
                        },
                        { resume: true }
                      );
                      assert.equal(recoveredResume.delivered, 1);
                      assert.equal(recoveredResume.resumeResult.retried, true);
                      assert.equal(recoveredResume.resumeResult.recovered, true);
                      assert.equal(recoveredResume.resumeResult.iteration, 3);

                      let handoffSendCalls = 0;
                      const terminalHandoff = await run([
                        {
                          executed: false,
                          reason: "loop_not_running",
                          state: { status: "done" }
                        }
                      ], () => {
                        handoffSendCalls += 1;
                        return {
                          sent: true,
                          confirmed: true,
                          confirmedVia: "user_message"
                        };
                      });
                      assert.equal(terminalHandoff.calls, 1);
                      assert.equal(handoffSendCalls, 1);
                      assert.equal(terminalHandoff.delivered, 0);
                      assert.equal(
                        terminalHandoff.button.textContent,
                        "BDB AUTO: przekazano zakończenie pętli do ChatGPT"
                      );
                      assert.equal(terminalHandoff.button.disabled, true);

                      let handoffRetryCalls = 0;
                      const terminalHandoffRetry = await run([
                        {
                          executed: false,
                          reason: "loop_not_running",
                          state: { status: "done" }
                        }
                      ], () => {
                        handoffRetryCalls += 1;
                        if (handoffRetryCalls === 1) {
                          return {
                            sent: false,
                            confirmed: false,
                            confirmedVia: null,
                            reason: "composer_missing"
                          };
                        }
                        return {
                          sent: true,
                          confirmed: true,
                          confirmedVia: "user_message"
                        };
                      });
                      assert.equal(handoffRetryCalls, 2);
                      assert.equal(terminalHandoffRetry.delivered, 0);
                      assert.equal(
                        terminalHandoffRetry.button.textContent,
                        "BDB AUTO: przekazano zakończenie pętli do ChatGPT"
                      );

                      let transientSendCalls = 0;
                      const transient = await run([completed], () => {
                        transientSendCalls += 1;
                        if (transientSendCalls === 1) {
                          return {
                            sent: false,
                            confirmed: false,
                            confirmedVia: null,
                            reason: "composer_missing"
                          };
                        }
                        return {
                          sent: true,
                          confirmed: true,
                          confirmedVia: "user_message"
                        };
                      });
                      assert.equal(transientSendCalls, 2);
                      assert.equal(transient.delivered, 1);

                      const unconfirmed = await run([completed], {
                        sent: true,
                        confirmed: false,
                        confirmedVia: null,
                        reason: "send_not_confirmed"
                      });
                      assert.equal(unconfirmed.delivered, 0);
                      assert.equal(
                        unconfirmed.button.textContent,
                        "BDB AUTO: zatrzymano; wynik oczekuje na ponowienie (send_not_confirmed)"
                      );
                    }

                    main().catch((error) => {
                      console.error(error.stack || error);
                      process.exitCode = 1;
                    });
                    '''
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [node, str(harness), str(EXTENSION)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
