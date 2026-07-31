from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_assisted_uses_short_messages_and_controlled_scope() -> None:
    background = read("background.js")
    polling = read("background_async_result.js")
    content = read("content.js")

    assert 'case "BDB_SUBMIT_ASSISTED"' in background
    assert 'case "BDB_POLL_ASSISTED"' in background
    assert "globalThis.bdbSubmitAssistedAction" in background
    assert "globalThis.bdbPollAssistedActionResult" in background

    assert "context.controlled_clean === true" in background
    assert "context.source_clean === true" not in background

    assert "const BDB_ASSISTED_RESULT_WAIT_SECONDS = 5;" in polling
    assert 'request_id: requestId("assisted-result")' in polling
    assert "wait_seconds: BDB_ASSISTED_RESULT_WAIT_SECONDS" in polling
    assert (
        "globalThis.bdbPollAssistedActionResult = "
        "bdbPollAssistedActionResult;"
        in polling
    )

    assert 'type: "BDB_SUBMIT_ASSISTED"' in content
    assert 'type: "BDB_POLL_ASSISTED"' in content
    assert "bdbAssistedActionIdentity" in content
    assert "bdbAssistedViews" in content
    assert "BDB: sprawdź wynik — nie ponawiaj" in content


def test_repair_observer_is_context_safe_and_failure_gated() -> None:
    repair = read("content_repair_retry.js")

    assert "function bdbContentRepairRuntimeAvailable()" in repair
    assert "function bdbContentRepairContextInvalid(error)" in repair
    assert "bdbContentRepairObserver.disconnect();" in repair
    assert (
        "void bdbContentRepairEnhance(output).catch((error) => {"
        in repair
    )

    local_gate = repair.index(
        "if (!bdbContentRepairIsFailure(null, localResponse, output))"
    )
    remote_peek = repair.index(
        "const found = await bdbContentRepairLatest(action.repo_alias);"
    )

    assert local_gate < remote_peek


def test_assisted_background_poll_is_one_native_result_call(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")

    harness = tmp_path / "assisted-short-poll-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            const script = fs.readFileSync(
              path.join(extensionDir, "background_async_result.js"),
              "utf8"
            );

            const nativeRequests = [];
            let polls = 0;
            let promotionChecks = 0;

            const context = {
              console,
              ACTION_SCHEMA: "bdb-action-v1",
              REQUEST_SCHEMA: "bdb-native-request-v1",
              DEFAULT_WAIT_SECONDS: 30,
              validateJsonObject(value) {
                assert.ok(value && typeof value === "object");
              },
              validateRepoAlias(value) {
                assert.equal(value, "calculator2");
                return value;
              },
              requestId(prefix) {
                return `${prefix}-request`;
              },
              async submitAction() {
                return {
                  status: "accepted",
                  command_id:
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001"
                };
              },
              async sendNative(request) {
                nativeRequests.push(request);
                polls += 1;
                if (polls === 1) {
                  return {
                    status: "pending",
                    command_id:
                      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001"
                  };
                }
                return {
                  status: "completed",
                  command_id:
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001",
                  result: {
                    status: "success"
                  }
                };
              },
              async waitForRequiredPromotion(_action, response) {
                promotionChecks += 1;
                return {
                  ...response,
                  promotion_checked: true
                };
              }
            };

            context.globalThis = context;
            context.self = context;
            vm.createContext(context);
            vm.runInContext(script, context, {
              filename: "background_async_result.js"
            });

            async function run() {
              const action = {
                schema: "bdb-action-v1",
                repo_alias: "calculator2",
                operation: "replace_exact_and_test"
              };

              const submitted =
                await context.bdbSubmitAssistedAction(action, 1);
              assert.equal(submitted.status, "accepted");

              const pending =
                await context.bdbPollAssistedActionResult(
                  action,
                  submitted.command_id
                );
              assert.equal(pending.status, "pending");
              assert.equal(nativeRequests.length, 1);
              assert.equal(
                nativeRequests[0].wait_seconds,
                5
              );

              const completed =
                await context.bdbPollAssistedActionResult(
                  action,
                  submitted.command_id
                );
              assert.equal(completed.status, "completed");
              assert.equal(completed.promotion_checked, true);
              assert.equal(nativeRequests.length, 2);
              assert.equal(promotionChecks, 1);
            }

            run().catch((error) => {
              console.error(
                error && error.stack ? error.stack : error
              );
              process.exitCode = 1;
            });
            """
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

    assert completed.returncode == 0, (
        completed.stdout + completed.stderr
    )
