from __future__ import annotations

from pathlib import Path

from bdb_bridge import CommandState, InstanceLock, Journal
from bdb_bridge.multi_file_patch_runtime import MultiFilePatchRuntimeCoordinator
from bdb_bridge.runtime_hardening import _terminal_result_from_journal


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
COMMAND_ID = f"{SESSION_ID}:000001"
BASE_SHA = "a" * 40


def test_terminal_result_preserves_exact_pre_mutation_error(tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.db"
    journal = Journal.open(journal_path)
    try:
        journal.create_session(SESSION_ID, "diagnostic-fixture", BASE_SHA)
        journal.record_command(
            SESSION_ID,
            COMMAND_ID,
            1,
            {
                "schema_version": "1.1",
                "session_id": SESSION_ID,
                "command_id": COMMAND_ID,
                "sequence": 1,
                "operation": "multi_file_patch",
                "expected_revision": 0,
                "expected_state_hash": "sha256:" + "b" * 64,
                "payload": {"profile_id": "poc_pytest", "patch": {}},
            },
        )
        journal.transition_command(
            COMMAND_ID,
            CommandState.DISCOVERED,
            CommandState.VALIDATED,
        )
        journal.transition_command(
            COMMAND_ID,
            CommandState.VALIDATED,
            CommandState.CLAIMED,
        )

        coordinator = MultiFilePatchRuntimeCoordinator(
            object(),
            journal,
            InstanceLock(tmp_path / "instance.lock"),
        )
        coordinator._bdb_terminal_diagnostic = {
            "error_code": "unsafe_path",
            "detail": "Path is not allowed by local policy: START-MP4-PLAYER.cmd",
        }
        coordinator._terminal_claimed(COMMAND_ID, CommandState.POLICY_DENIED)

        result = _terminal_result_from_journal(journal_path, SESSION_ID, 1)
        assert result is not None
        assert result["status"] == "policy_denied"
        assert result["error_code"] == "unsafe_path"
        assert "START-MP4-PLAYER.cmd" in result["summary"]
        assert result["data"]["terminal_error_code"] == "unsafe_path"
        assert result["data"]["terminal_detail"] == (
            "Path is not allowed by local policy: START-MP4-PLAYER.cmd"
        )

        events = journal.list_events(session_id=SESSION_ID, command_id=COMMAND_ID)
        diagnostic = [event for event in events if event.event_type == "command.terminal_diagnostic"]
        assert len(diagnostic) == 1
        assert diagnostic[0].payload["error_code"] == "unsafe_path"
    finally:
        journal.close()
