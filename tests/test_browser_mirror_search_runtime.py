from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_background_routes_workspace_context_sync_and_local_search() -> None:
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert 'const SEARCH_TEXT_OPERATION = "search_text";' in source
    assert 'sync_mirror: syncMirror === true' in source
    assert 'nativeContext(repoAlias, { syncMirror: true })' in source
    assert 'action: "search_text"' in source
    assert 'if (action.operation === SEARCH_TEXT_OPERATION)' in source


def test_auto_preserves_bounded_open_read_search_and_mirror_receipt(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "mirror-search-auto-runtime.cjs"
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

            function render(payload, marker) {
              const text = vm.runInContext(
                `autoResultText(${JSON.stringify(payload)}, ${JSON.stringify(marker)})`,
                context
              );
              assert.ok(Buffer.byteLength(text, "utf8") <= 16 * 1024, Buffer.byteLength(text, "utf8"));
              return JSON.parse(text.split("BDB_RESULT:\n", 2)[1]);
            }

            const mirror = {
              schema: "bdb-mirror-sync-v1",
              status: "up_to_date",
              local_head: "a".repeat(40),
              remote_head_after: "a".repeat(40),
              pushed: false
            };
            const openRead = render({
              status: "success",
              command_id: "read:1",
              mirror_sync: mirror,
              data: {
                operation: "open_read",
                path: "snippets/large.liquid",
                start_line: 100,
                end_line: 180,
                total_lines: 900,
                content: "x".repeat(9000),
                content_sha256: `sha256:${"1".repeat(64)}`,
                file_sha256: `sha256:${"2".repeat(64)}`,
                returned_bytes: 9000,
                file_bytes: 20000
              }
            }, "BDB_AUTO_RESULT:read:1");
            assert.equal(openRead.operation, "open_read");
            assert.equal(openRead.path, "snippets/large.liquid");
            assert.equal(openRead.auto_payload.reason, "open_read_compacted");
            assert.equal(openRead.content.length, 2200);
            assert.equal(openRead.mirror_sync.status, "up_to_date");

            const search = render({
              status: "success",
              operation: "search_text",
              query: "mask-image",
              matches: Array.from({ length: 20 }, (_, index) => ({
                kind: "content",
                path: `assets/file-${index}.css`,
                line: index + 1,
                text: `.item-${index} { mask-image: linear-gradient(black, transparent); }`
              })),
              total_matches: 20,
              scanned_files: 100,
              skipped_files: 0,
              base_sha: "b".repeat(40),
              mirror_sync: mirror
            }, "BDB_AUTO_RESULT:search:1");
            assert.equal(search.operation, "search_text");
            assert.equal(search.matches.length, 12);
            assert.equal(search.auto_payload.reason, "search_text_compacted");

            const write = render({
              status: "success",
              operation: "replace_exact_and_test",
              changed_files: ["assets/theme.css"],
              stdout_tail: "ok",
              promotion: {
                status: "promoted",
                command_id: "write:1",
                source_commit: "c".repeat(40),
                changed_files: ["assets/theme.css"],
                mirror_sync: mirror
              },
              verification: {
                schema: "bdb-post-action-verification-v1",
                status: "verified",
                command_id: "write:1",
                source_commit: "c".repeat(40),
                mirror_sync: mirror
              },
              padding: "z".repeat(9000)
            }, "BDB_AUTO_RESULT:write:1");
            assert.equal(write.promotion.status, "promoted");
            assert.equal(write.promotion.mirror_sync.status, "up_to_date");
            assert.equal(write.verification.status, "verified");
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


def test_auto_contract_uses_adaptive_eight_sixteen_and_four_kib_caps() -> None:
    source = (EXTENSION / "content.js").read_text(encoding="utf-8")
    assert "function bdbAutoOpenReadPayload(payload)" in source
    assert "function bdbAutoSearchTextPayload(payload)" in source
    assert 'reason: "open_read_compacted"' in source
    assert 'reason: "search_text_compacted"' in source
    assert "mirror_sync: promotion.mirror_sync" in source
    assert "const BDB_AUTO_CONTINUATION_TARGET_BYTES = 12 * 1024;" in source
    assert "const BDB_AUTO_CONTINUATION_MAX_BYTES = 16 * 1024;" in source
    assert "const BDB_AUTO_LEGACY_CONTINUATION_MAX_BYTES = 4 * 1024;" in source
    assert 'function bdbAutoInspectBundlePayload(payload, profile = "rich")' in source
    assert 'reason: "inspect_bundle_compacted"' in source
    assert 'for (const profile of ["compact", "tight", "minimal"])' in source
