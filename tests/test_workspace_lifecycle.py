from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    WorkspaceDisposition,
    WorkspaceLifecycleCoordinator,
    WorkspaceLifecycleState,
)
from tests.helpers.workspace_lifecycle_fixture import SESSION, make_fixture


def test_preserve_is_default_idempotent_and_audited(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, _ = make_fixture(tmp_path)
    coordinator = WorkspaceLifecycleCoordinator(cfg, journal)
    first = coordinator.preserve(SESSION)
    second = coordinator.preserve(SESSION)
    assert first == second
    assert first.disposition is WorkspaceDisposition.PRESERVE
    assert first.state is WorkspaceLifecycleState.PRESERVED
    assert journal._connection.execute(
        "SELECT COUNT(*) FROM events WHERE session_id=? AND event_type='workspace.preserved'", (SESSION,)
    ).fetchone()[0] == 1
    assert wm.path.exists()
    journal.close()


def test_identity_conflict_is_controlled(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, _ = make_fixture(tmp_path)
    journal.record_workspace_preserved(
        session_id=SESSION, workspace_path=workspace.workspace_path, base_sha=workspace.base_sha,
        expected_revision=workspace.revision, expected_state_hash=workspace.state_hash,
    )
    with pytest.raises(BridgeError) as exc:
        journal.record_workspace_preserved(
            session_id=SESSION, workspace_path=workspace.workspace_path, base_sha=workspace.base_sha,
            expected_revision=workspace.revision + 1, expected_state_hash=workspace.state_hash,
        )
    assert exc.value.code == "journal_conflict"
    assert wm.path.exists()
    journal.close()


def test_lifecycle_transitions_and_replay_are_idempotent(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, _ = make_fixture(tmp_path)
    journal.record_workspace_preserved(
        session_id=SESSION, workspace_path=workspace.workspace_path, base_sha=workspace.base_sha,
        expected_revision=workspace.revision, expected_state_hash=workspace.state_hash,
    )
    requested = journal.request_workspace_cleanup(session_id=SESSION)
    assert journal.request_workspace_cleanup(session_id=SESSION) == requested
    started = journal.mark_workspace_cleanup_started(session_id=SESSION)
    assert journal.mark_workspace_cleanup_started(session_id=SESSION) == started
    blocked = journal.mark_workspace_cleanup_blocked(session_id=SESSION, diagnostic="  unsafe\x00  state  ")
    assert blocked.state is WorkspaceLifecycleState.BLOCKED
    assert blocked.disposition is WorkspaceDisposition.PRESERVE
    assert blocked.last_error == "unsafe state"
    retried = journal.request_workspace_cleanup(session_id=SESSION)
    assert retried.state is WorkspaceLifecycleState.CLEANUP_REQUESTED
    journal.mark_workspace_cleanup_started(session_id=SESSION)
    removed = journal.mark_workspace_cleanup_completed(session_id=SESSION)
    assert removed.state is WorkspaceLifecycleState.REMOVED
    before = journal._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert journal.mark_workspace_cleanup_completed(session_id=SESSION) == removed
    assert journal._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    journal.close()


def test_event_fault_rolls_back_state_and_event(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, _ = make_fixture(tmp_path)
    journal.record_workspace_preserved(
        session_id=SESSION, workspace_path=workspace.workspace_path, base_sha=workspace.base_sha,
        expected_revision=workspace.revision, expected_state_hash=workspace.state_hash,
    )

    def fault(point: str) -> None:
        if point == "AFTER_LIFECYCLE_STATE_WRITE_BEFORE_EVENT":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError):
        journal.request_workspace_cleanup(session_id=SESSION, fault_hook=fault)
    record = journal.get_workspace_lifecycle(SESSION)
    assert record is not None and record.state is WorkspaceLifecycleState.PRESERVED
    assert journal._connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='workspace.cleanup_requested'"
    ).fetchone()[0] == 0
    journal.close()
