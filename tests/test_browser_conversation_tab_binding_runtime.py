from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_conversation_binding_is_scoped_to_exact_sender_tab(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for conversation-tab runtime validation")

    harness = tmp_path / "conversation-tab-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");
            const { webcrypto } = require("node:crypto");

            const extensionDir = process.argv[2];
            const localStore = {};
            const sessionStore = {};
            let commandCounter = 0;

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
                  return {};
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
              JSON,
              Promise,
              setTimeout,
              clearTimeout,
              crypto: webcrypto,
              chrome: {
                storage: {
                  local: storageArea(localStore),
                  session: storageArea(sessionStore)
                },
                runtime: {
                  lastError: null,
                  onMessage: { addListener() {} },
                  sendNativeMessage(_host, request, callback) {
                    if (request.action !== "submit_action") {
                      throw new Error(`unexpected native action ${request.action}`);
                    }
                    commandCounter += 1;
                    const session = commandCounter === 1
                      ? "11111111-1111-4111-8111-111111111111"
                      : "22222222-2222-4222-8222-222222222222";
                    callback({
                      schema: "bdb-native-response-v1",
                      request_id: request.request_id,
                      status: "accepted",
                      command_id: `${session}:000001`
                    });
                  }
                }
              }
            };
            context.globalThis = context;
            context.self = context;
            vm.createContext(context);

            for (const scriptName of [
              "background.js",
              "background_project_launcher.js",
              "background_conversation_binding.js"
            ]) {
              const scriptPath = path.join(extensionDir, scriptName);
              vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
            }
            vm.runInContext("globalThis.__submit = submitAction;", context);

            const launchId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
            const conversationId = "conversation-12345678";

            function normalAction() {
              return {
                schema: "bdb-action-v1",
                repo_alias: "synthetic",
                operation: "open_read",
                payload: { path: "src/app.py" }
              };
            }

            async function run() {
              const bound = await context.__submit({
                schema: "bdb-action-v1",
                operation: "project_conversation_bind",
                launch_id: launchId,
                conversation_id: conversationId,
                repo_alias: "synthetic"
              }, 7);
              assert.equal(bound.status, "conversation_bound");
              assert.equal(localStore.bdbConversationBindingsV1[conversationId].tab_id, 7);

              await context.__submit(normalAction(), 8);
              let binding = localStore.bdbConversationBindingsV1[conversationId];
              assert.equal(binding.command_id, null, "foreign tab overwrote conversation command");
              assert.equal(binding.session_id, null, "foreign tab overwrote conversation session");

              await context.__submit(normalAction(), 7);
              binding = localStore.bdbConversationBindingsV1[conversationId];
              assert.equal(
                binding.command_id,
                "22222222-2222-4222-8222-222222222222:000001"
              );
              assert.equal(binding.session_id, "22222222-2222-4222-8222-222222222222");
              assert.equal(binding.tab_id, 7);
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
