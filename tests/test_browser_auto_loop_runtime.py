from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_loop_state_survives_tab_change_and_worker_restart(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "auto-loop-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            const manifest = JSON.parse(
              fs.readFileSync(path.join(extensionDir, "manifest.json"), "utf8")
            );

            function storageArea(store) {
              return {
                async get(keys) {
                  if (keys === null || keys === undefined) {
                    return { ...store };
                  }
                  if (typeof keys === "string") {
                    return Object.prototype.hasOwnProperty.call(store, keys)
                      ? { [keys]: store[keys] }
                      : {};
                  }
                  if (Array.isArray(keys)) {
                    const result = {};
                    for (const key of keys) {
                      if (Object.prototype.hasOwnProperty.call(store, key)) {
                        result[key] = store[key];
                      }
                    }
                    return result;
                  }
                  const result = { ...keys };
                  for (const key of Object.keys(keys)) {
                    if (Object.prototype.hasOwnProperty.call(store, key)) {
                      result[key] = store[key];
                    }
                  }
                  return result;
                },
                async set(values) {
                  Object.assign(store, values);
                },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) {
                    delete store[key];
                  }
                }
              };
            }

            function createWorker(shared) {
              let messageListener = null;
              const context = {
                console,
                TextEncoder,
                Uint8Array,
                setTimeout,
                clearTimeout,
                crypto: {
                  getRandomValues(buffer) {
                    for (let index = 0; index < buffer.length; index += 1) {
                      buffer[index] = (index * 17 + shared.randomSeed) % 256;
                    }
                    shared.randomSeed += 1;
                    return buffer;
                  }
                },
                chrome: {
                  storage: {
                    local: storageArea(shared.local),
                    session: storageArea(shared.session)
                  },
                  runtime: {
                    lastError: null,
                    onMessage: {
                      addListener(listener) {
                        messageListener = listener;
                      }
                    },
                    sendNativeMessage(_host, request, callback) {
                      shared.nativeRequests.push(request);
                      if (request.action === "context") {
                        callback({
                          schema: "bdb-native-response-v1",
                          request_id: request.request_id,
                          context: {
                            source_clean: true,
                            latest_promotion: null
                          },
                          arm: { armed: true }
                        });
                        return;
                      }
                      if (request.action === "submit_action") {
                        shared.commandCounter += 1;
                        callback({
                          schema: "bdb-native-response-v1",
                          request_id: request.request_id,
                          command_id: `command-${shared.commandCounter}`,
                          status: "completed",
                          result: {
                            status: "success",
                            data: {
                              operation: request.bdb_action.operation
                            }
                          }
                        });
                        return;
                      }
                      throw new Error(`Unexpected native action: ${request.action}`);
                    }
                  }
                }
              };
              context.globalThis = context;
              context.self = context;
              vm.createContext(context);
              context.importScripts = (...scriptNames) => {
                for (const scriptName of scriptNames) {
                  const scriptPath = path.join(extensionDir, scriptName);
                  vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, {
                    filename: scriptPath
                  });
                }
              };

              const workerPath = path.join(
                extensionDir,
                manifest.background.service_worker
              );
              vm.runInContext(fs.readFileSync(workerPath, "utf8"), context, {
                filename: workerPath
              });
              assert.equal(typeof messageListener, "function");

              return {
                async send(action, tabId) {
                  return new Promise((resolve, reject) => {
                    const keepChannelOpen = messageListener(
                      { type: "BDB_CONSIDER_AUTO", action },
                      { tab: { id: tabId } },
                      (message) => {
                        if (!message || message.ok !== true) {
                          reject(new Error(message && message.error ? message.error : "AUTO failed"));
                          return;
                        }
                        resolve(message.response);
                      }
                    );
                    assert.equal(keepChannelOpen, true);
                  });
                }
              };
            }

            function autoAction(loopId, iteration, operation = "open_read") {
              return {
                schema: "bdb-action-v1",
                repo_alias: "calculator",
                operation,
                payload: operation === "open_read" ? { path: "calculator.py" } : {},
                automation: {
                  mode: "auto",
                  loop_id: loopId,
                  iteration
                },
                presentation: { mode: "compact" }
              };
            }

            async function main() {
              const shared = {
                local: {
                  autoEnabled: true,
                  autoMaxIterations: 4,
                  autoMaxMinutes: 10
                },
                session: {},
                nativeRequests: [],
                commandCounter: 0,
                randomSeed: 1
              };
              const loopId = "calculator-history-20260717-2302";

              let worker = createWorker(shared);
              const first = await worker.send(
                autoAction(loopId, 1, "workspace_context"),
                101
              );
              assert.equal(first.executed, true, JSON.stringify(first));

              // A fresh worker context simulates an MV3 service-worker restart. A new
              // sender tab simulates refresh/navigation that changes the tab identity.
              worker = createWorker(shared);
              const second = await worker.send(autoAction(loopId, 2), 202);
              assert.equal(second.executed, true, JSON.stringify(second));
              assert.equal(second.expectedIteration, 3, JSON.stringify(second));

              worker = createWorker(shared);
              const third = await worker.send(autoAction(loopId, 3), 303);
              assert.equal(third.executed, true, JSON.stringify(third));
              assert.equal(third.expectedIteration, 4, JSON.stringify(third));

              const requestsBeforeDuplicate = shared.nativeRequests.length;
              const duplicate = await worker.send(autoAction(loopId, 3), 404);
              assert.equal(duplicate.executed, false, JSON.stringify(duplicate));
              assert.equal(duplicate.reason, "iteration_already_processed", JSON.stringify(duplicate));
              assert.equal(duplicate.expectedIteration, 4, JSON.stringify(duplicate));
              assert.equal(shared.nativeRequests.length, requestsBeforeDuplicate);

              const canonicalKey = `bdbAuto:${loopId}`;
              assert.ok(shared.session[canonicalKey], JSON.stringify(shared.session));
              assert.equal(shared.session[canonicalKey].lastIteration, 3);
              assert.equal(
                Object.keys(shared.session).some(
                  (key) => (
                    key !== canonicalKey &&
                    key.startsWith("bdbAuto:") &&
                    key.endsWith(`:${loopId}`)
                  )
                ),
                false,
                JSON.stringify(shared.session)
              );

              const freshLoop = await worker.send(
                autoAction("calculator-fresh-loop-20260718", 1, "workspace_context"),
                505
              );
              assert.equal(freshLoop.executed, true, JSON.stringify(freshLoop));
              assert.equal(freshLoop.expectedIteration, 2, JSON.stringify(freshLoop));
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
