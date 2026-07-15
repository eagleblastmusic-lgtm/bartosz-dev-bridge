from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, ResultBuildInput, ResultStager, sha256_bytes
from tests.helpers.result_outbox_fixture import (
    COMMAND_ID,
    FINISHED,
    NOW,
    SESSION_ID,
    build_staged,
    make_journal,
    make_outcome,
)

GOLDEN_RESULT = '{\n  "artifacts": [],\n  "changed_files": [\n    "src/clamp.py"\n  ],\n  "command_commit_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",\n  "command_id": "018f3f66-6cb3-4f66-9f2e-3d7647d1b701:000001",\n  "diff": "diff --git a/src/clamp.py b/src/clamp.py\\n-value = 1\\n+value = 2\\n",\n  "diff_sha256": "sha256:e868cdbe47af89e12df776aa7a4ecfbcecab3f7d8a2e6e4d03bec1259183ce0d",\n  "duration_ms": 2000,\n  "end_marker": "BDB-END:sha256:482255bb63aba6f03ca1c086d4c28c096fddd93a899c65a7735f7d36eca9a3cd",\n  "error_code": null,\n  "executor_version": "0.5.0-ghb0",\n  "exit_code": 0,\n  "finished_at": "2026-07-15T12:00:02Z",\n  "schema_version": "1.1",\n  "sequence": 1,\n  "session_id": "018f3f66-6cb3-4f66-9f2e-3d7647d1b701",\n  "started_at": "2026-07-15T12:00:00Z",\n  "state_hash_after": "sha256:2222222222222222222222222222222222222222222222222222222222222222",\n  "state_hash_before": "sha256:1111111111111111111111111111111111111111111111111111111111111111",\n  "status": "success",\n  "stderr_sha256": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",\n  "stderr_tail": "",\n  "stdout_sha256": "sha256:7b01e2c3e1d342955503e71c68d062e608aa4d0421a5bccb58f722637cadc75b",\n  "stdout_tail": "3 passed\\n",\n  "summary": "Command effect recorded",\n  "truncated": false,\n  "workspace_revision_after": 1,\n  "workspace_revision_before": 0\n}'


def test_result_builder_is_byte_deterministic_and_valid(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    first = build_staged(journal)
    second = build_staged(journal)
    assert first == second
    assert first.result_json == GOLDEN_RESULT
    assert first.result_bytes == first.result_json.encode("utf-8", errors="strict")
    assert first.result_sha256 == sha256_bytes(first.result_bytes)
    assert not first.result_bytes.endswith(b"\n")
    parsed = json.loads(first.result_json)
    assert parsed["schema_version"] == "1.1"
    assert parsed["session_id"] == SESSION_ID
    assert parsed["command_id"] == COMMAND_ID
    assert parsed["duration_ms"] == 2000
    assert parsed["changed_files"] == ["src/clamp.py"]
    assert parsed["artifacts"] == []
    assert parsed["end_marker"].startswith("BDB-END:sha256:")
    journal.close()


def test_builder_rejects_surrogate_and_effect_mismatch(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    session = journal.get_session(SESSION_ID)
    command = journal.get_command(COMMAND_ID)
    plan = journal.get_operation_plan(COMMAND_ID)
    effect = journal.get_operation_effect(COMMAND_ID)
    assert session and command and plan and effect
    with pytest.raises(BridgeError):
        ResultStager(journal).build(ResultBuildInput(session, command, plan, effect, make_outcome(stdout="\ud800"), NOW, FINISHED))
    bad = replace(make_outcome(), workspace_revision_after=2)
    with pytest.raises(BridgeError):
        ResultStager(journal).build(ResultBuildInput(session, command, plan, effect, bad, NOW, FINISHED))
    journal.close()


def test_builder_truncates_with_hashes_of_full_controlled_text(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    staged = build_staged(journal, stdout="x" * 50_000)
    parsed = json.loads(staged.result_json)
    assert parsed["truncated"] is True
    assert len(staged.result_bytes) <= 16 * 1024
    assert parsed["stdout_sha256"] == sha256_bytes(("x" * 50_000).encode())
    assert len(parsed["stdout_tail"]) <= 5_000
    journal.close()
