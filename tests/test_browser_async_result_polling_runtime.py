from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_async_result_companion_polls_accepted_command_until_completed(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "async-result-polling-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            const scriptPath = path.join(extensionDir, "background_async_result.js");
            const script = fs.readFileSync(scriptPath, "utf8");

            const nativeRequests = [];
            let polls = 0;
            let promotionChecks = 0;
            const context = {
              console,
              REQUEST_SCHEMA: "bdb-native-request-v1",
              DEFAULT_WAIT_SECONDS: 30,
              requestId(prefix) {
                return `${prefix}-request`;
              },
              validateRepoAlias(value) {
                assert.equal(value, "calculator2");
                return value;
              },
              async submitAction(_action, _tabId) {
                return {
                  status: "accepted",
                  command_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001"
                };
              },
              async sendNative(request) {
                nativeRequests.push(request);
                polls += 1;
                if (polls === 1) {
                  return {
                    status: "pending",
                    command_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001"
                  };
                }
                return {
                  status: "completed",
                  command_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001",
                  result: {
                    status: "success",
                    data: { operation: "multi_file_patch" }
                  }
                };
              },
              async waitForRequiredPromotion(_action, response) {
                promotionChecks += 1;
                return { ...response, promotion_checked: true };
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(script, context, { filename: scriptPath });

            async function main() {
              const result = await context.submitAction(
                {
                  repo_alias: "calculator2",
                  operation: "multi_file_patch",
                  promotion: { mode: "required" }
                },
                17
              );
              assert.equal(result.status, "completed", JSON.stringify(result));
              assert.equal(result.promotion_checked, true, JSON.stringify(result));
              assert.equal(polls, 2);
              assert.equal(promotionChecks, 1);
              for (const request of nativeRequests) {
                assert.equal(request.action, "result");
                assert.equal(request.repo_alias, "calculator2");
                assert.equal(request.session_id, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
                assert.equal(request.sequence, 1);
                assert.equal(request.wait_seconds, 30);
              }

              polls = 0;
              context.submitActionBeforeAsyncResultPolling = async () => ({
                status: "completed",
                result: { status: "success" }
              });
              const alreadyCompleted = await context.submitAction(
                { repo_alias: "calculator2" },
                18
              );
              assert.equal(alreadyCompleted.status, "completed");
              assert.equal(polls, 0);
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
