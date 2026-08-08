from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_failed_result_transport_and_task_guidance_preserve_native_error(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser failure transport validation")

    harness = tmp_path / "failed-result-transport.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const { webcrypto } = require("node:crypto");

            class FakeElement {}
            class FakeTextArea extends FakeElement {}
            class FakeInput extends FakeElement {}
            class FakeButton extends FakeElement {}
            class FakeMutationObserver { observe() {} }
            class FakeInputEvent {}

            const contentContext = {
              console,
              document: {
                documentElement: {},
                querySelector() { return null; },
                querySelectorAll() { return []; }
              },
              navigator: {},
              window: { getSelection() { return null; } },
              HTMLElement: FakeElement,
              HTMLTextAreaElement: FakeTextArea,
              HTMLInputElement: FakeInput,
              HTMLButtonElement: FakeButton,
              MutationObserver: FakeMutationObserver,
              InputEvent: FakeInputEvent,
              setTimeout,
              clearTimeout
            };
            contentContext.globalThis = contentContext;
            vm.createContext(contentContext);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), contentContext);

            const legacyMaskedFailure = {
              status: "failed",
              error: {
                code: "dirty_source_checkout",
                message: "Native request failed: dirty_source_checkout"
              },
              result: {
                task_guidance: {
                  schema: "bdb-task-guidance-v1",
                  next_operation: "multi_file_patch_or_focused_read"
                }
              }
            };
            for (const builder of ["resultText", "autoResultText"]) {
              const text = vm.runInContext(
                `${builder}(${JSON.stringify(legacyMaskedFailure)}, "BDB_AUTO_RESULT:error-loop:1")`,
                contentContext
              );
              const parsed = JSON.parse(text.split("BDB_RESULT:\n", 2)[1]);
              assert.equal(parsed.status, "failed");
              assert.equal(parsed.error.code, "dirty_source_checkout");
              assert.equal(parsed.result.task_guidance.schema, "bdb-task-guidance-v1");
            }
            const summary = vm.runInContext(
              `resultSummary(${JSON.stringify(legacyMaskedFailure)})`,
              contentContext
            );
            assert.match(summary, /dirty_source_checkout/);

            const emptyArea = {
              async get() { return {}; },
              async set() {},
              async remove() {}
            };
            const taskContext = {
              console,
              TextEncoder,
              crypto: webcrypto,
              chrome: { storage: { local: emptyArea, session: emptyArea } },
              submitAction: async () => null,
              considerAuto: async () => null,
              markAutoResultDelivered: async () => ({ marked: true })
            };
            taskContext.globalThis = taskContext;
            vm.createContext(taskContext);
            vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), taskContext);

            const attached = vm.runInContext(
              `bdbTaskAttachGuidance(
                {schema: "bdb-action-v1", repo_alias: "gicleeapp", operation: "inspect_bundle", trace_id: "error-loop:1"},
                ${JSON.stringify({
                  status: "failed",
                  error: {
                    code: "invalid_payload",
                    message: "Native request failed: invalid_payload"
                  }
                })},
                null,
                "miss"
              )`,
              taskContext
            );
            assert.equal(attached.status, "failed");
            assert.equal(attached.error.code, "invalid_payload");
            assert.equal(Object.prototype.hasOwnProperty.call(attached, "result"), false);
            assert.equal(attached.task_guidance.next_operation, "recover_from_error");
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            node,
            str(harness),
            str(EXTENSION / "content.js"),
            str(EXTENSION / "background_task_controller.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_auto_read_failure_continues_when_continue_on_failure_is_enabled(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for AUTO read-failure recovery validation")

    harness = tmp_path / "auto-read-failure-continuation.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const { webcrypto, randomUUID } = require("node:crypto");

            const local = {
              autoEnabled: true,
              autoMaxIterations: 4,
              autoMaxMinutes: 10,
              autoShadowMode: false
            };
            const session = {};
            let inspectNativeCalls = 0;

            function area(store) {
              return {
                async get(keys) {
                  if (keys === null || keys === undefined) return { ...store };
                  if (typeof keys === "string") {
                    return Object.prototype.hasOwnProperty.call(store, keys)
                      ? { [keys]: store[keys] }
                      : {};
                  }
                  if (Array.isArray(keys)) {
                    const result = {};
                    for (const key of keys) {
                      if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = store[key];
                    }
                    return result;
                  }
                  const result = { ...(keys || {}) };
                  for (const key of Object.keys(keys || {})) {
                    if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = store[key];
                  }
                  return result;
                },
                async set(values) { Object.assign(store, values); },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key];
                }
              };
            }

            const context = {
              console, TextEncoder, Uint8Array, Set, Map, Date, JSON, Promise,
              setTimeout, clearTimeout,
              crypto: {
                subtle: webcrypto.subtle,
                getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
                randomUUID
              },
              chrome: {
                storage: { local: area(local), session: area(session) },
                runtime: {
                  lastError: null,
                  getManifest() { return { version: "0.4.7" }; },
                  onMessage: { addListener() {} },
                  sendNativeMessage(_host, request, callback) {
                    if (request.action === "context") {
                      callback({
                        schema: "bdb-native-response-v1",
                        host_version: "0.4.7",
                        request_id: request.request_id,
                        status: "context",
                        context: { allowed_paths: ["**"] },
                        arm: { armed: true }
                      });
                      return;
                    }
                    assert.equal(request.action, "inspect_bundle");
                    inspectNativeCalls += 1;
                    callback({
                      schema: "bdb-native-response-v1",
                      host_version: "0.4.7",
                      request_id: request.request_id,
                      status: "failed",
                      error: {
                        code: "policy_denied",
                        message: "Path is not allowed by local policy"
                      }
                    });
                  }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);

            const action = {
              schema: "bdb-action-v1",
              repo_alias: "gicleeapp",
              operation: "inspect_bundle",
              payload: {
                searches: [],
                reads: [],
                read_top_matches: 20
              },
              automation: {
                mode: "auto",
                loop_id: "recover-invalid-read",
                iteration: 1,
                continue_on_failure: true
              }
            };

            context.considerAuto(action, 7).then(async (decision) => {
              assert.equal(decision.executed, true, JSON.stringify(decision));
              assert.equal(decision.recoverableReadFailure, true, JSON.stringify(decision));
              assert.equal(decision.shouldContinue, true, JSON.stringify(decision));
              assert.equal(decision.stopReason, null, JSON.stringify(decision));
              assert.equal(decision.state.status, "running", JSON.stringify(decision));
              assert.equal(decision.response.error.code, "invalid_payload");
              assert.equal(decision.response.client_preflight, true);
              assert.equal(decision.response.error.details.rule_id, "inspect_bundle.client_preflight");
              assert.equal(decision.response.error.details.phase, "client_preflight");
              assert.equal(decision.response.error.details.effect_started, false);
              assert.equal(inspectNativeCalls, 0, "invalid inspect_bundle must stop before Native Host");
              assert.equal(
                decision.response.error.message,
                "inspect_bundle read_top_matches must be boolean or 0-12"
              );

              const invalidSearchAction = {
                ...action,
                payload: {
                  searches: [{ query: "bad\nquery" }],
                  reads: [],
                  read_top_matches: 1
                },
                automation: {
                  ...action.automation,
                  loop_id: "recover-invalid-search"
                }
              };
              const invalidSearchDecision = await context.considerAuto(invalidSearchAction, 8);
              assert.equal(invalidSearchDecision.executed, true, JSON.stringify(invalidSearchDecision));
              assert.equal(invalidSearchDecision.recoverableReadFailure, true, JSON.stringify(invalidSearchDecision));
              assert.equal(invalidSearchDecision.shouldContinue, true, JSON.stringify(invalidSearchDecision));
              assert.equal(invalidSearchDecision.response.error.code, "invalid_payload");
              assert.equal(invalidSearchDecision.response.client_preflight, true);
              assert.equal(inspectNativeCalls, 0, "invalid inspect_bundle search must stop before Native Host");
              assert.equal(
                invalidSearchDecision.response.error.message,
                "search_text payload.query must contain 1-200 characters on one line"
              );

              const invalidFlagsAction = {
                ...action,
                payload: {
                  searches: [],
                  reads: [],
                  read_top_matches: 1,
                  include_tree: "yes"
                },
                automation: {
                  ...action.automation,
                  loop_id: "recover-invalid-inspect-flags"
                }
              };
              const invalidFlagsDecision = await context.considerAuto(invalidFlagsAction, 9);
              assert.equal(invalidFlagsDecision.executed, true, JSON.stringify(invalidFlagsDecision));
              assert.equal(invalidFlagsDecision.recoverableReadFailure, true, JSON.stringify(invalidFlagsDecision));
              assert.equal(invalidFlagsDecision.shouldContinue, true, JSON.stringify(invalidFlagsDecision));
              assert.equal(invalidFlagsDecision.response.error.code, "invalid_payload");
              assert.equal(invalidFlagsDecision.response.client_preflight, true);
              assert.equal(inspectNativeCalls, 0, "invalid inspect_bundle flags must stop before Native Host");
              assert.equal(
                invalidFlagsDecision.response.error.message,
                "inspect_bundle include_tree/include_symbols must be boolean"
              );

              const policyAction = {
                ...action,
                payload: {
                  searches: [],
                  reads: [],
                  read_top_matches: 1
                },
                automation: {
                  ...action.automation,
                  loop_id: "recover-policy-read"
                }
              };
              const policyDecision = await context.considerAuto(policyAction, 9);
              assert.equal(policyDecision.executed, true, JSON.stringify(policyDecision));
              assert.equal(policyDecision.recoverableReadFailure, true, JSON.stringify(policyDecision));
              assert.equal(policyDecision.shouldContinue, true, JSON.stringify(policyDecision));
              assert.equal(policyDecision.stopReason, null, JSON.stringify(policyDecision));
              assert.equal(policyDecision.state.status, "running", JSON.stringify(policyDecision));
              assert.equal(policyDecision.response.error.code, "policy_denied");
              assert.equal(policyDecision.response.client_preflight, undefined);
              assert.equal(inspectNativeCalls, 1, "valid inspect_bundle must reach Native Host");
              assert.equal(
                policyDecision.response.error.message,
                "Path is not allowed by local policy"
              );
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

