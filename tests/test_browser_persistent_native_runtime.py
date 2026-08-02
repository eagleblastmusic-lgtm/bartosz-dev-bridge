from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_background_reuses_one_native_port_for_multiple_requests(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for persistent Native Messaging validation")
    harness = tmp_path / "persistent-native-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const { webcrypto } = require("node:crypto");
            const listeners = [];
            let connections = 0;
            let posts = 0;
            const context = {
              console, TextEncoder, Uint8Array, Set, Map, Date, JSON, Promise,
              setTimeout, clearTimeout, crypto: webcrypto,
              chrome: {
                storage: {
                  local: { async get() { return {}; }, async set() {} },
                  session: { async get() { return {}; }, async set() {}, async remove() {} }
                },
                runtime: {
                  lastError: null,
                  onMessage: { addListener() {} },
                  connectNative() {
                    connections += 1;
                    return {
                      onMessage: { addListener(listener) { listeners.push(listener); } },
                      onDisconnect: { addListener() {} },
                      postMessage(request) {
                        posts += 1;
                        Promise.resolve().then(() => listeners[0]({
                          schema: "bdb-native-response-v1",
                          request_id: request.request_id,
                          status: "ok"
                        }));
                      }
                    };
                  },
                  sendNativeMessage() { throw new Error("one-shot fallback must not run"); }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
            Promise.all([
              vm.runInContext('sendNative({schema: REQUEST_SCHEMA, request_id: "one", action: "status"})', context),
              vm.runInContext('sendNative({schema: REQUEST_SCHEMA, request_id: "two", action: "status"})', context)
            ]).then((responses) => {
              assert.equal(connections, 1);
              assert.equal(posts, 2);
              assert.deepEqual(responses.map((item) => item.request_id), ["one", "two"]);
            }).catch((error) => { console.error(error); process.exitCode = 1; });
            '''
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(EXTENSION / "background.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_background_routes_inspect_bundle() -> None:
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert 'const INSPECT_BUNDLE_OPERATION = "inspect_bundle";' in source
    assert 'action: "inspect_bundle"' in source
    assert "return await repositoryInspection(action);" in source
