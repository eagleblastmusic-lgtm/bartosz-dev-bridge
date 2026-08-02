from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_workspace_context_is_bounded_before_composer_insertion(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "auto-payload-bound-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");

            const script = fs.readFileSync(process.argv[2], "utf8");
            class FakeElement {}
            class FakeTextArea extends FakeElement {}
            class FakeInput extends FakeElement {}
            class FakeButton extends FakeElement {}
            class FakeMutationObserver { observe() {} }
            class FakeInputEvent { constructor(type, init) { this.type = type; this.init = init; } }

            const document = {
              documentElement: {},
              querySelector() { return null; },
              querySelectorAll() { return []; },
              execCommand() { throw new Error("execCommand must not run in payload builder"); }
            };
            const context = {
              console,
              document,
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
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(script, context, { filename: process.argv[2] });

            const snapshots = Array.from({ length: 40 }, (_, index) => ({
              path: `assets/file-${index}.js`,
              bytes: 12000,
              sha256: `sha256:${String(index).padStart(64, "0")}`,
              content: "x".repeat(12000)
            }));
            const payload = {
              status: "success",
              operation: "workspace_context",
              context: {
                repo_alias: "gicleeapp",
                base_sha: "a".repeat(40),
                session_clean: true,
                tracked_paths: Array.from({ length: 2000 }, (_, index) => `assets/path-${index}.js`),
                symbols: Array.from({ length: 500 }, (_, index) => ({
                  path: `assets/path-${index}.js`,
                  line: index + 1,
                  text: `function symbol${index}() {}`
                })),
                snapshot_files: snapshots,
                skipped_files: []
              },
              arm: { armed: true }
            };
            const marker = "BDB_AUTO_RESULT:test-loop:1";
            const text = vm.runInContext(
              `autoResultText(${JSON.stringify(payload)}, ${JSON.stringify(marker)})`,
              context
            );
            assert.ok(Buffer.byteLength(text, "utf8") <= 8192, Buffer.byteLength(text, "utf8"));
            assert.ok(text.startsWith(`${marker}\nBDB_RESULT:\n`));
            const parsed = JSON.parse(text.split("BDB_RESULT:\n", 2)[1]);
            assert.equal(parsed.operation, "workspace_context");
            assert.equal(parsed.auto_payload.bounded, true);
            assert.equal(parsed.auto_payload.reason, "workspace_context_compacted");
            assert.equal(parsed.context.snapshot_paths.length, 8);
            assert.equal(parsed.context.snapshot_file_count, 40);
            assert.equal(parsed.context.snapshot_paths_omitted_for_auto, 32);
            assert.equal(parsed.context.snapshot_contents_omitted_for_auto, true);
            assert.equal(parsed.context.tracked_paths.length, 20);
            assert.equal(parsed.context.tracked_paths_total, 2000);
            assert.equal(parsed.context.tracked_paths_omitted_for_auto, 1980);
            assert.equal(parsed.context.symbols.length, 8);
            assert.equal(parsed.context.symbols_total, 500);
            assert.equal(parsed.context.symbols_omitted_for_auto, 492);
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_auto_inspect_bundle_adapts_to_fast_and_legacy_composer_limits(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "auto-inspect-payload-bound-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");

            const script = fs.readFileSync(process.argv[2], "utf8");
            class FakeElement {}
            class FakeTextArea extends FakeElement {}
            class FakeInput extends FakeElement {}
            class FakeButton extends FakeElement {}
            class FakeMutationObserver { observe() {} }
            class FakeInputEvent {}
            const context = {
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
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(script, context, { filename: process.argv[2] });

            const searches = Array.from({ length: 8 }, (_, searchIndex) => ({
              query: `query-${searchIndex}`,
              total_matches: 20,
              truncated: true,
              matches: Array.from({ length: 20 }, (_, matchIndex) => ({
                kind: "content",
                path: `cursor-api/Komponenty/module-${searchIndex}/file-${matchIndex}.py`,
                line: matchIndex + 1,
                text: `match-${searchIndex}-${matchIndex} ${"x".repeat(900)}`
              }))
            }));
            const reads = Array.from({ length: 6 }, (_, index) => ({
              path: `cursor-api/Komponenty/module-${index}/file.py`,
              source: "search_match",
              start_line: 1,
              end_line: 400,
              total_lines: 900,
              content: `read-${index} ${"y".repeat(3600)}`,
              content_sha256: `sha256:${String(index).padStart(64, "0")}`,
              file_sha256: `sha256:${String(index + 1).padStart(64, "0")}`,
              truncated: true
            }));
            const payload = {
              status: "success",
              operation: "inspect_bundle",
              base_sha: "a".repeat(40),
              response_profile: "compact",
              result_bytes: 18000,
              performance: { parallel_searches: true, search_workers: 4 },
              mirror_sync: {
                status: "up_to_date",
                phase: "pre_inspect_bundle",
                local_head: "a".repeat(40),
                remote_head_after: "a".repeat(40)
              },
              context: {
                source_clean: false,
                source_changes: [],
                source_changes_outside_scope: 12,
                symbols: [],
                latest_promotion: {
                  status: "promoted",
                  source_commit: "a".repeat(40),
                  changed_files: ["sections/section.liquid"]
                }
              },
              tree: [],
              tree_summary: { allowed_files: 2469, focused_files: 1283 },
              searches,
              reads,
              reads_truncated: true,
              task_guidance: {
                trace_id: "gicleeapp-large-inspect:1",
                phase: "analysis",
                complexity: { score: 5, class: "medium", suggested_iterations: 5 },
                next_operation: "multi_file_patch_or_focused_read",
                cache: "miss"
              }
            };
            const marker = "BDB_AUTO_RESULT:gicleeapp-large-inspect:1";

            function build(limit) {
              return vm.runInContext(
                `autoResultText(${JSON.stringify(payload)}, ${JSON.stringify(marker)}, ${limit})`,
                context
              );
            }

            const fast = build(16 * 1024);
            const legacy = build(4 * 1024);
            assert.ok(Buffer.byteLength(fast, "utf8") <= 16 * 1024, Buffer.byteLength(fast, "utf8"));
            assert.ok(Buffer.byteLength(legacy, "utf8") <= 4 * 1024, Buffer.byteLength(legacy, "utf8"));
            const fastPayload = JSON.parse(fast.split("BDB_RESULT:\n", 2)[1]);
            const legacyPayload = JSON.parse(legacy.split("BDB_RESULT:\n", 2)[1]);
            assert.equal(fastPayload.operation, "inspect_bundle");
            assert.equal(fastPayload.base_sha, "a".repeat(40));
            assert.equal(fastPayload.searches.length, 8);
            assert.equal(fastPayload.searches[0].matches[0].path, "cursor-api/Komponenty/module-0/file-0.py");
            assert.ok(["rich", "compact", "tight", "minimal"].includes(fastPayload.auto_payload.profile));
            assert.equal(legacyPayload.operation, "inspect_bundle");
            assert.equal(legacyPayload.base_sha, "a".repeat(40));
            assert.equal(legacyPayload.searches.length, 8);
            assert.ok(["tight", "minimal"].includes(legacyPayload.auto_payload.profile));
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_auto_send_uses_bounded_payload_and_composer_guard() -> None:
    content = (EXTENSION / "content.js").read_text(encoding="utf-8")
    auto_send = (EXTENSION / "content_auto_send.js").read_text(encoding="utf-8")

    assert "const BDB_AUTO_CONTINUATION_TARGET_BYTES = 12 * 1024;" in content
    assert "const BDB_AUTO_CONTINUATION_MAX_BYTES = 16 * 1024;" in content
    assert "const BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES = 4 * 1024;" in content
    assert "const BDB_COMPOSER_INSERT_MAX_BYTES = 64 * 1024;" in content
    assert "function autoResultText(" in content
    assert "requestedMaxBytes = BDB_AUTO_CONTINUATION_MAX_BYTES" in content
    assert "snapshot_contents_omitted_for_auto" in content
    assert "snapshot_paths_omitted_for_auto" in content
    assert "tracked_paths_omitted_for_auto" in content
    assert "symbols_omitted_for_auto" in content
    assert "maxBytes = BDB_COMPOSER_INSERT_MAX_BYTES" in content
    assert "bdbUtf8ByteLength(text) > maxBytes" in content

    assert 'typeof autoResultText === "function"' in auto_send
    assert "autoResultText(response, marker, autoContinuationMaxBytes)" in auto_send
    assert 'typeof BDB_AUTO_CONTINUATION_MAX_BYTES === "number"' in auto_send
    assert 'typeof BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES === "number"' in auto_send
    assert "let prepared = bdbPrepareAutoContinuation(" in auto_send
    assert "autoContinuationMaxBytes" in auto_send
    assert "async function bdbPrepareManualContinuation(text)" in auto_send
    assert "composerText(current).includes(text)" in auto_send
