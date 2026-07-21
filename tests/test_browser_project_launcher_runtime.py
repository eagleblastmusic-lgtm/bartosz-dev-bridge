from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_all_extension_javascript_parses() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for extension syntax validation")
    for script in sorted(EXTENSION.glob("*.js")):
        completed = subprocess.run(
            [node, "--check", str(script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert completed.returncode == 0, f"{script.name}:\n{completed.stdout}{completed.stderr}"


def test_background_routes_project_launch_peek_claim_and_ack(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "project-launcher-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            let messageListener = null;
            const nativeRequests = [];
            const localStore = {};
            const sessionStore = {};
            const launchId = "11111111-1111-4111-8111-111111111111";
            const claimId = "22222222-2222-4222-8222-222222222222";

            function clone(value) {
              return JSON.parse(JSON.stringify(value));
            }

            function storageArea(store) {
              return {
                async get(keys) {
                  if (keys === null || keys === undefined) return clone(store);
                  if (typeof keys === "string") {
                    return Object.prototype.hasOwnProperty.call(store, keys)
                      ? { [keys]: clone(store[keys]) }
                      : {};
                  }
                  if (Array.isArray(keys)) {
                    const result = {};
                    for (const key of keys) {
                      if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = clone(store[key]);
                    }
                    return result;
                  }
                  const result = clone(keys || {});
                  for (const key of Object.keys(keys || {})) {
                    if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = clone(store[key]);
                  }
                  return result;
                },
                async set(values) { Object.assign(store, clone(values)); },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key];
                }
              };
            }

            const context = {
              console,
              TextEncoder,
              Uint8Array,
              Set,
              Map,
              Date,
              setTimeout,
              clearTimeout,
              crypto: {
                getRandomValues(buffer) {
                  for (let index = 0; index < buffer.length; index += 1) buffer[index] = index + 1;
                  return buffer;
                }
              },
              chrome: {
                storage: {
                  local: storageArea(localStore),
                  session: storageArea(sessionStore)
                },
                runtime: {
                  lastError: null,
                  onMessage: {
                    addListener(listener) { messageListener = listener; }
                  },
                  sendNativeMessage(_host, request, callback) {
                    nativeRequests.push(clone(request));
                    if (request.action === "project_launch_peek") {
                      callback({
                        schema: "bdb-native-response-v1",
                        request_id: request.request_id,
                        status: "project_launch",
                        launch: {
                          schema: "bdb-project-launch-v1",
                          launch_id: launchId,
                          repo_alias: "calculator",
                          prompt: "Create a calculator",
                          auto_send: true,
                          created_at: "2026-07-21T03:00:00Z",
                          expires_at: "2026-07-21T03:10:00Z"
                        }
                      });
                      return;
                    }
                    if (request.action === "project_launch_claim") {
                      callback({
                        schema: "bdb-native-response-v1",
                        request_id: request.request_id,
                        status: "claimed",
                        launch: { schema: "bdb-project-launch-v1", launch_id: launchId }
                      });
                      return;
                    }
                    if (request.action === "project_launch_ack") {
                      callback({
                        schema: "bdb-native-response-v1",
                        request_id: request.request_id,
                        status: "acknowledged"
                      });
                      return;
                    }
                    throw new Error(`unexpected native action: ${request.action}`);
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
                vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
              }
            };

            const entryPath = path.join(extensionDir, "background_full_entry.js");
            vm.runInContext(fs.readFileSync(entryPath, "utf8"), context, { filename: entryPath });
            assert.equal(typeof messageListener, "function");

            function send(message) {
              return new Promise((resolve, reject) => {
                const keepAlive = messageListener(message, { tab: { id: 7 } }, (response) => {
                  if (!response || response.ok !== true) {
                    reject(new Error(response && response.error ? response.error : "message failed"));
                    return;
                  }
                  resolve(response.response);
                });
                assert.equal(keepAlive, true);
              });
            }

            async function run() {
              const peek = await send({ type: "BDB_CONTEXT", repoAlias: "bdb-project-launch" });
              assert.equal(peek.status, "project_launch");
              assert.equal(nativeRequests.at(-1).action, "project_launch_peek");

              const claimed = await send({
                type: "BDB_SUBMIT_ACTION",
                action: {
                  schema: "bdb-action-v1",
                  operation: "project_launch_claim",
                  launch_id: launchId,
                  claim_id: claimId
                }
              });
              assert.equal(claimed.status, "claimed");
              assert.equal(nativeRequests.at(-1).action, "project_launch_claim");
              assert.equal(nativeRequests.at(-1).claim_id, claimId);

              const acknowledged = await send({
                type: "BDB_SUBMIT_ACTION",
                action: {
                  schema: "bdb-action-v1",
                  operation: "project_launch_ack",
                  launch_id: launchId,
                  claim_id: claimId
                }
              });
              assert.equal(acknowledged.status, "acknowledged");
              assert.equal(nativeRequests.at(-1).action, "project_launch_ack");
            }

            run().catch((error) => {
              console.error(error && error.stack ? error.stack : error);
              process.exitCode = 1;
            });
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
