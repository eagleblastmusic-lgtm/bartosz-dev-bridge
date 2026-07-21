from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_state_synchronization_never_regresses_a_newer_canonical_iteration(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "auto-state-monotonic-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            const loopId = "auto-state-monotonic-race";
            const canonicalKey = `bdbAuto:${loopId}`;
            const localStore = {
              autoEnabled: true,
              autoMaxIterations: 6,
              autoMaxMinutes: 15
            };
            const sessionStore = {
              [canonicalKey]: {
                startedAt: Date.now(),
                lastIteration: 1,
                status: "running",
                updatedAt: 100
              }
            };
            const sessionWrites = [];
            let injectConcurrentAdvance = true;

            function clone(value) {
              return JSON.parse(JSON.stringify(value));
            }

            function standardGet(store, keys) {
              if (keys === null || keys === undefined) return { ...store };
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
            }

            const localArea = {
              async get(keys) {
                return standardGet(localStore, keys);
              },
              async set(values) {
                Object.assign(localStore, values);
              },
              async remove(keys) {
                for (const key of Array.isArray(keys) ? keys : [keys]) delete localStore[key];
              }
            };

            const sessionArea = {
              async get(keys) {
                if (keys === null && injectConcurrentAdvance) {
                  injectConcurrentAdvance = false;
                  const staleSnapshot = clone(sessionStore);
                  sessionStore[canonicalKey] = {
                    ...sessionStore[canonicalKey],
                    lastIteration: 2,
                    updatedAt: 200
                  };
                  return staleSnapshot;
                }
                return standardGet(sessionStore, keys);
              },
              async set(values) {
                sessionWrites.push(clone(values));
                Object.assign(sessionStore, values);
              },
              async remove(keys) {
                for (const key of Array.isArray(keys) ? keys : [keys]) delete sessionStore[key];
              }
            };

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
                    buffer[index] = index + 1;
                  }
                  return buffer;
                }
              },
              chrome: {
                storage: {
                  local: localArea,
                  session: sessionArea
                },
                runtime: {
                  lastError: null,
                  onMessage: {
                    addListener(listener) {
                      messageListener = listener;
                    }
                  },
                  sendNativeMessage(_host, request, callback) {
                    assert.equal(request.action, "submit_action");
                    callback({
                      schema: "bdb-native-response-v1",
                      request_id: request.request_id,
                      command_id: "command-monotonic-3",
                      status: "completed",
                      result: {
                        status: "success",
                        data: { operation: request.bdb_action.operation }
                      }
                    });
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

            const entryPath = path.join(extensionDir, "background_entry.js");
            vm.runInContext(fs.readFileSync(entryPath, "utf8"), context, {
              filename: entryPath
            });
            assert.equal(typeof messageListener, "function");
            assert.equal(typeof context.considerAuto, "function");

            const action = {
              schema: "bdb-action-v1",
              repo_alias: "calculator",
              operation: "open_read",
              payload: { path: "calculator.py" },
              automation: {
                mode: "auto",
                loop_id: loopId,
                iteration: 3
              },
              presentation: { mode: "compact" }
            };

            context.considerAuto(action, 101).then((decision) => {
              assert.equal(decision.executed, true, JSON.stringify(decision));
              assert.equal(decision.expectedIteration, 4, JSON.stringify(decision));
              assert.equal(sessionStore[canonicalKey].lastIteration, 3);
              assert.equal(
                sessionWrites.some((write) => (
                  write[canonicalKey] && write[canonicalKey].lastIteration === 1
                )),
                false,
                JSON.stringify(sessionWrites)
              );
            }).catch((error) => {
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
