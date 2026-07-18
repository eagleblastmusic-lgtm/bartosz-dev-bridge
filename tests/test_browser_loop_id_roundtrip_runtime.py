from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_loop_identifier_is_preserved_character_for_character(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "loop-id-roundtrip-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            let listener = null;
            const session = {};
            const local = {};

            function storageArea(store) {
              return {
                async get(keys) {
                  if (keys === null || keys === undefined) return { ...store };
                  if (typeof keys === "string") {
                    return Object.prototype.hasOwnProperty.call(store, keys)
                      ? { [keys]: store[keys] }
                      : {};
                  }
                  return {};
                },
                async set(values) {
                  Object.assign(store, values);
                },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key];
                }
              };
            }

            const context = {
              console,
              TextEncoder,
              Uint8Array,
              setTimeout,
              clearTimeout,
              crypto: {
                getRandomValues(buffer) {
                  buffer.fill(7);
                  return buffer;
                }
              },
              chrome: {
                storage: {
                  local: storageArea(local),
                  session: storageArea(session)
                },
                runtime: {
                  lastError: null,
                  onMessage: {
                    addListener(callback) {
                      listener = callback;
                    }
                  },
                  sendNativeMessage() {
                    throw new Error("Native Messaging must not be used in this identity test");
                  }
                }
              }
            };
            context.globalThis = context;
            context.self = context;
            vm.createContext(context);
            context.importScripts = (...names) => {
              for (const name of names) {
                const scriptPath = path.join(extensionDir, name);
                vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, {
                  filename: scriptPath
                });
              }
            };

            const entry = path.join(extensionDir, "background_full_entry.js");
            vm.runInContext(fs.readFileSync(entry, "utf8"), context, { filename: entry });
            assert.equal(typeof listener, "function");

            const loopId = "calculator2-p01_auto:2026.07.18";
            const metadata = vm.runInContext(
              `automationMetadata(${JSON.stringify({
                automation: {
                  mode: "auto",
                  loop_id: "calculator2-p01_auto:2026.07.18",
                  iteration: 3
                }
              })})`,
              context
            );
            assert.equal(metadata.loopId, loopId);
            assert.equal(metadata.iteration, 3);

            const stateKey = vm.runInContext(
              `canonicalAutoStateKey(998, ${JSON.stringify(loopId)})`,
              context
            );
            assert.equal(stateKey, `bdbAuto:${loopId}`);

            const replayKey = vm.runInContext(
              `autoReplayKey(${JSON.stringify(loopId)}, 3)`,
              context
            );
            assert.equal(replayKey, `${loopId}:3`);

            const marker = `BDB_AUTO_RESULT:${metadata.loopId}:${metadata.iteration}`;
            assert.equal(marker, "BDB_AUTO_RESULT:calculator2-p01_auto:2026.07.18:3");
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
