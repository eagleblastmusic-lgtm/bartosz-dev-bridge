from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_terminal_auto_result_is_cached_recovered_and_delivery_is_remembered(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "auto-terminal-result-cache.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];

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
                      buffer[index] = (index * 19 + shared.randomSeed) % 256;
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
                              status: "context",
                              context: { allowed_paths: ["**"] },
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
                            status: "needs_user",
                            error_code: "path_not_found",
                            summary: "Synthetic terminal result"
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

              vm.runInContext(
                fs.readFileSync(path.join(extensionDir, "background_entry.js"), "utf8"),
                context,
                { filename: "background_entry.js" }
              );
              assert.equal(typeof messageListener, "function");

              async function dispatch(message, tabId) {
                return new Promise((resolve, reject) => {
                  const keepOpen = messageListener(
                    message,
                    { tab: { id: tabId } },
                    (reply) => {
                      if (!reply || reply.ok !== true) {
                        reject(new Error(reply && reply.error ? reply.error : "message failed"));
                        return;
                      }
                      resolve(reply.response);
                    }
                  );
                  assert.equal(keepOpen, true);
                });
              }

              return {
                consider(action, tabId) {
                  return dispatch({ type: "BDB_CONSIDER_AUTO", action }, tabId);
                },
                mark(loopId, iteration, tabId) {
                  return dispatch(
                    {
                      type: "BDB_MARK_AUTO_RESULT_DELIVERED",
                      loopId,
                      iteration
                    },
                    tabId
                  );
                }
              };
            }

            function terminalAction(loopId) {
              return {
                schema: "bdb-action-v1",
                repo_alias: "calculator",
                operation: "open_read",
                payload: { path: "missing.txt" },
                automation: {
                  mode: "auto",
                  loop_id: loopId,
                  iteration: 1
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
              const loopId = "terminal-cache-test-20260731";
              const action = terminalAction(loopId);

              let worker = createWorker(shared);
              const first = await worker.consider(action, 101);
              assert.equal(first.executed, true, JSON.stringify(first));
              assert.equal(first.shouldContinue, false, JSON.stringify(first));
              assert.equal(first.stopReason, "needs_user", JSON.stringify(first));
                  assert.equal(shared.nativeRequests.length, 2);

              worker = createWorker(shared);
              const recoveredUndelivered = await worker.consider(action, 202);
              assert.equal(recoveredUndelivered.executed, true, JSON.stringify(recoveredUndelivered));
              assert.equal(recoveredUndelivered.recoveredResult, true, JSON.stringify(recoveredUndelivered));
              assert.equal(recoveredUndelivered.resultDelivered, false, JSON.stringify(recoveredUndelivered));
                  assert.equal(shared.nativeRequests.length, 2);

              const marked = await worker.mark(loopId, 1, 202);
              assert.equal(marked.marked, true, JSON.stringify(marked));

              worker = createWorker(shared);
              const recoveredDelivered = await worker.consider(action, 303);
              assert.equal(recoveredDelivered.executed, true, JSON.stringify(recoveredDelivered));
              assert.equal(recoveredDelivered.recoveredResult, true, JSON.stringify(recoveredDelivered));
              assert.equal(recoveredDelivered.resultDelivered, true, JSON.stringify(recoveredDelivered));
                  assert.equal(shared.nativeRequests.length, 2);
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
