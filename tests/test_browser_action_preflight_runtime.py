from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_action_preflight_hash_scope_and_success_runtime(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser preflight runtime validation")

    harness = tmp_path / "action-preflight-runtime.cjs"
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
            const nativeRequests = [];

            function clone(value) {
              return JSON.parse(JSON.stringify(value));
            }

            function storageArea() {
              const store = {};
              return {
                async get(keys) {
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
              atob(value) { return Buffer.from(value, "base64").toString("binary"); },
              btoa(value) { return Buffer.from(value, "binary").toString("base64"); },
              chrome: {
                storage: {
                  local: storageArea(),
                  session: storageArea()
                },
                runtime: {
                  lastError: null,
                  onMessage: { addListener() {} },
                  sendNativeMessage(_host, request, callback) {
                    nativeRequests.push(clone(request));
                    if (request.action === "context") {
                      callback({
                        schema: "bdb-native-response-v1",
                        request_id: request.request_id,
                        status: "context",
                        context: {
                          allowed_paths: ["src/**", "tests/**"]
                        },
                        arm: { armed: true }
                      });
                      return;
                    }
                    if (request.action === "submit_action") {
                      callback({
                        schema: "bdb-native-response-v1",
                        request_id: request.request_id,
                        status: "accepted",
                        command_id: "11111111-1111-4111-8111-111111111111:000001"
                      });
                      return;
                    }
                    throw new Error(`unexpected native request: ${request.action}`);
                  }
                }
              }
            };
            context.globalThis = context;
            context.self = context;
            vm.createContext(context);

            for (const scriptName of ["background.js", "background_action_preflight.js"]) {
              const scriptPath = path.join(extensionDir, scriptName);
              vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
            }
            vm.runInContext("globalThis.__bdbSubmitAction = submitAction;", context);

            function replacement(pathValue, content, digest) {
              return {
                schema: "bdb-file-replacement-v1",
                kind: "replace_file",
                path: pathValue,
                expected_sha256: `sha256:${"0".repeat(64)}`,
                content_base64: Buffer.from(content, "utf8").toString("base64"),
                content_sha256: digest
              };
            }

            function action(operation) {
              return {
                schema: "bdb-action-v1",
                repo_alias: "synthetic",
                operation: "multi_file_patch",
                sequence: 1,
                expected_revision: 0,
                payload: {
                  profile_id: "poc_pytest",
                  patch: {
                    schema: "bdb-multi-file-patch-v1",
                    operations: [operation]
                  }
                }
              };
            }

            async function sha256(content) {
              const bytes = new TextEncoder().encode(content);
              const digest = await webcrypto.subtle.digest("SHA-256", bytes);
              return `sha256:${Buffer.from(digest).toString("hex")}`;
            }

            async function expectFailure(candidate, expectedCode, expectedText) {
              const before = nativeRequests.filter((item) => item.action === "submit_action").length;
              await assert.rejects(
                () => context.__bdbSubmitAction(candidate, 7),
                (error) => {
                  assert.equal(error.bdbCode, expectedCode);
                  assert.match(error.message, expectedText);
                  return true;
                }
              );
              const after = nativeRequests.filter((item) => item.action === "submit_action").length;
              assert.equal(after, before, "invalid action reached Native Host submission");
            }

            async function run() {
              await expectFailure(
                action(replacement("src/app.py", "print('ok')\n", `sha256:${"f".repeat(64)}`)),
                "invalid_payload",
                /src\/app\.py.*content_sha256 mismatch/
              );

              const validDigest = await sha256("echo ok\n");
              await expectFailure(
                action(replacement("START-APP.cmd", "echo ok\n", validDigest)),
                "policy_denied",
                /Path is not allowed by local policy: START-APP\.cmd/
              );

              const content = "print('green')\n";
              const response = await context.__bdbSubmitAction(
                action(replacement("src/app.py", content, await sha256(content))),
                7
              );
              assert.equal(response.status, "accepted");
              assert.equal(
                nativeRequests.filter((item) => item.action === "submit_action").length,
                1
              );
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
