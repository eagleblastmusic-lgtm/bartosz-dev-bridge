from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


class AutoMutationSafetyRuntimeTests(unittest.TestCase):
    def test_uncertain_mutation_is_not_submitted_twice(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required")

        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "auto-mutation-safety.cjs"
            harness.write_text(
                textwrap.dedent(
                    r"""
                    "use strict";
                    const assert = require("node:assert/strict");
                    const fs = require("node:fs");
                    const path = require("node:path");
                    const vm = require("node:vm");

                    const extensionDir = process.argv[2];
                    const localStore = {};
                    const sessionStore = {};
                    let submitCalls = 0;

                    function clone(value) {
                      return value === undefined
                        ? undefined
                        : JSON.parse(JSON.stringify(value));
                    }

                    function storageArea(store) {
                      return {
                        async get(keys) {
                          if (typeof keys === "string") {
                            return Object.prototype.hasOwnProperty.call(store, keys)
                              ? { [keys]: clone(store[keys]) }
                              : {};
                          }
                          if (keys === null || keys === undefined) {
                            return clone(store);
                          }
                          throw new Error("unsupported storage query");
                        },
                        async set(values) {
                          Object.assign(store, clone(values));
                        }
                      };
                    }

                    const context = {
                      console,
                      Date,
                      Set,
                      Object,
                      Array,
                      Number,
                      Math,
                      String,
                      Boolean,
                      chrome: {
                        storage: {
                          local: storageArea(localStore),
                          session: storageArea(sessionStore)
                        }
                      },
                      AUTO_REPLAY_GUARD_KEY: "bdbAutoReplayGuard",
                      AUTO_REPLAY_GUARD_LIMIT: 512,
                      autoReplayKey(loopId, iteration) {
                        return `${loopId}:${iteration}`;
                      },
                      autoStateKey(_tabId, loopId) {
                        return `bdbAuto:${loopId}`;
                      },
                      automationMetadata(action) {
                        const automation = action && action.automation;
                        if (!automation) return null;
                        return {
                          loopId: automation.loop_id,
                          iteration: automation.iteration
                        };
                      },
                      async claimAutoReplay(loopId, iteration) {
                        const key = `${loopId}:${iteration}`;
                        const guard = localStore.bdbAutoReplayGuard || {};
                        if (Object.prototype.hasOwnProperty.call(guard, key)) {
                          return false;
                        }
                        localStore.bdbAutoReplayGuard = {
                          ...guard,
                          [key]: {
                            status: "processing",
                            claimedAt: Date.now()
                          }
                        };
                        return true;
                      },
                      async considerAuto(action, tabId) {
                        const metadata = context.automationMetadata(action);
                        if (!metadata) {
                          return { executed: false, reason: "not_auto" };
                        }
                        if (!await context.claimAutoReplay(
                          metadata.loopId,
                          metadata.iteration
                        )) {
                          const key = context.autoStateKey(tabId, metadata.loopId);
                          return {
                            executed: false,
                            reason: "replay_guard",
                            state: sessionStore[key] || null
                          };
                        }
                        submitCalls += 1;
                        throw new Error("simulated transport interruption");
                      }
                    };
                    context.globalThis = context;
                    vm.createContext(context);

                    const scriptPath = path.join(
                      extensionDir,
                      "background_auto_mutation_safety.js"
                    );
                    vm.runInContext(
                      fs.readFileSync(scriptPath, "utf8"),
                      context,
                      { filename: scriptPath }
                    );

                    function action(operation, loopId = "mutation-safety-runtime") {
                      return {
                        schema: "bdb-action-v1",
                        repo_alias: "bdb-self",
                        operation,
                        automation: {
                          mode: "auto",
                          loop_id: loopId,
                          iteration: 1
                        }
                      };
                    }

                    async function run() {
                      const first = await context.considerAuto(
                        action("multi_file_patch"),
                        77
                      );
                      assert.equal(first.executed, true, JSON.stringify(first));
                      assert.equal(first.shouldContinue, false);
                      assert.equal(
                        first.stopReason,
                        "manual_reconciliation_required"
                      );
                      assert.equal(first.uncertainExecution, true);
                      assert.equal(
                        first.response.error.code,
                        "manual_reconciliation_required"
                      );
                      assert.equal(submitCalls, 1);

                      const replayKey = "mutation-safety-runtime:1";
                      assert.equal(
                        localStore.bdbAutoReplayGuard[replayKey].status,
                        "uncertain"
                      );
                      const state = sessionStore["bdbAuto:mutation-safety-runtime"];
                      assert.equal(
                        state.status,
                        "manual_reconciliation_required"
                      );
                      assert.equal(state.lastIteration, 1);
                      assert.equal(state.lastResponseDelivered, false);

                      const second = await context.considerAuto(
                        action("multi_file_patch"),
                        77
                      );
                      assert.equal(second.executed, false, JSON.stringify(second));
                      assert.equal(
                        second.reason,
                        "manual_reconciliation_required"
                      );
                      assert.equal(second.uncertainExecution, true);
                      assert.equal(submitCalls, 1);

                      await assert.rejects(
                        context.considerAuto(
                          action("open_read", "read-runtime"),
                          77
                        ),
                        /simulated transport interruption/
                      );
                      assert.equal(submitCalls, 2);
                    }

                    run().catch((error) => {
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
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )


if __name__ == "__main__":
    unittest.main()
