from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_assisted_progress_counter_and_cleanup(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")

    harness = tmp_path / "assisted-progress-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const script = fs.readFileSync(
              path.join(process.argv[2], "content.js"),
              "utf8"
            );

            const start = script.indexOf(
              "function assistedElapsedLabel(milliseconds)"
            );
            const end = script.indexOf(
              "function resultText(response, marker = null)"
            );

            assert.notEqual(start, -1);
            assert.notEqual(end, -1);
            assert.ok(end > start);

            let now = 1000;
            let intervalCallback = null;
            let clearedTimer = null;

            const context = {
              Date: {
                now() {
                  return now;
                }
              },
              setInterval(callback, milliseconds) {
                assert.equal(milliseconds, 1000);
                intervalCallback = callback;
                return 17;
              },
              clearInterval(timer) {
                clearedTimer = timer;
              }
            };

            vm.createContext(context);
            vm.runInContext(script.slice(start, end), context);

            const button = { textContent: "" };
            const stop = context.startAssistedProgress(button);

            assert.equal(
              button.textContent,
              "BDB: wykonywanie… 0:00"
            );

            now = 66000;
            intervalCallback();

            assert.equal(
              button.textContent,
              "BDB: wykonywanie… 1:05"
            );

            stop();
            assert.equal(clearedTimer, 17);

            const finalText = button.textContent;
            now = 126000;
            intervalCallback();

            assert.equal(button.textContent, finalText);

            assert.match(
              script,
              /let stopProgress = \(\) => \{\};[\s\S]*stopProgress = startAssistedProgress\(button, actionIdentity\);/s
            );

            assert.match(
              script,
              /finally\s*\{\s*stopProgress\(\);[\s\S]*viewButton\.disabled = keepDisabled;/s
            );
            """
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, (
        completed.stdout + "\n" + completed.stderr
    )
