from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bdb_bridge import MAX_RESULT_BYTES, ResultCoordinator
from bdb_bridge import open_read_result
from bdb_bridge.models import BridgeErrorCode, CommandState
from bdb_bridge.protocol import BridgeError
from bdb_bridge.recovery_journal import sha256_bytes


SESSION_ID = "11111111-1111-4111-8111-111111111111"
COMMAND_ID = f"{SESSION_ID}:000001"
STARTED_AT = "2026-07-17T18:00:00.000000Z"
FINISHED_AT = "2026-07-17T18:00:00.001000Z"


def build_value(content: str) -> dict[str, object]:
    encoded = content.encode("utf-8")
    return {
        "path": "src/clamp.py",
        "start_line": 1,
        "end_line": 500,
        "total_lines": 500,
        "content": content,
        "content_sha256": sha256_bytes(encoded),
        "file_sha256": sha256_bytes(encoded),
        "returned_bytes": len(encoded),
        "file_bytes": len(encoded),
        "truncated": False,
        "workspace_revision": 0,
        "workspace_state_hash": "sha256:" + ("0" * 64),
    }


def build_staged(content: str):
    return open_read_result.build_open_read_result(
        build_value(content),
        session=SimpleNamespace(session_id=SESSION_ID),
        command=SimpleNamespace(
            command_id=COMMAND_ID,
            sequence=1,
            command_commit_sha="a" * 40,
        ),
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )


def assert_consistent_prefix(original: str, staged) -> None:
    parsed = json.loads(staged.result_json)
    returned = parsed["data"]["content"]
    returned_bytes = returned.encode("utf-8")

    assert original.startswith(returned)
    assert parsed["data"]["returned_bytes"] == len(returned_bytes)
    assert parsed["data"]["content_sha256"] == sha256_bytes(returned_bytes)
    assert staged.result_sha256 == sha256_bytes(staged.result_bytes)
    assert staged.result_bytes == staged.result_json.encode("utf-8")
    assert len(staged.result_bytes) <= MAX_RESULT_BYTES


def test_safe_builder_preserves_prefix_for_content_over_eight_thousand_characters() -> None:
    content = "BEGIN|" + ("0123456789" * 809) + "|END"

    staged = build_staged(content)
    parsed = json.loads(staged.result_json)

    assert_consistent_prefix(content, staged)
    assert parsed["data"]["content"].startswith("BEGIN|")
    assert not parsed["data"]["content"].endswith("|END")
    assert parsed["truncated"] is True


def test_safe_builder_fits_heavily_escaped_content_without_hash_drift() -> None:
    content = "BEGIN|" + ('"\\' * 3_990) + "|END"

    staged = build_staged(content)
    parsed = json.loads(staged.result_json)

    assert_consistent_prefix(content, staged)
    assert parsed["data"]["content"].startswith("BEGIN|")
    assert len(parsed["data"]["content"]) < len(content)
    assert parsed["truncated"] is True


def test_open_read_build_failure_does_not_leave_command_executing(monkeypatch: pytest.MonkeyPatch) -> None:
    command = SimpleNamespace(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        state=CommandState.EXECUTING,
        command_json=json.dumps({"operation": "open_read", "payload": {"path": "src/clamp.py"}}),
    )

    class FakeJournal:
        def __init__(self) -> None:
            self.command = command
            self.blocked = None

        def get_command(self, command_id: str):
            assert command_id == COMMAND_ID
            return self.command

        def mark_workspace_recovery_blocked(self, **kwargs) -> None:
            self.blocked = kwargs
            self.command.state = CommandState.MANUAL_RECONCILIATION_REQUIRED

    def fail_execute(*args, **kwargs):
        raise BridgeError(BridgeErrorCode.RESULT_TOO_LARGE, "forced result build failure")

    monkeypatch.setattr(open_read_result, "_execute_open_read", fail_execute)
    coordinator = object.__new__(ResultCoordinator)
    coordinator.journal = FakeJournal()
    coordinator.config = object()
    coordinator.now_fn = lambda: STARTED_AT

    outcome = ResultCoordinator.process(coordinator, COMMAND_ID)

    assert outcome.command_state is CommandState.MANUAL_RECONCILIATION_REQUIRED
    assert outcome.staged is False
    assert coordinator.journal.blocked["reason_code"] == BridgeErrorCode.RESULT_TOO_LARGE.value
