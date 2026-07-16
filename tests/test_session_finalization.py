from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import BridgeError, CommandState, SessionFinalizer, SessionState, WorkspaceLifecycleState
from tests.helpers.workspace_lifecycle_fixture import NOW, SESSION, make_fixture


def test_finalize_is_atomic_preserves_workspace_and_keeps_result_published(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path, session_state=SessionState.ACTIVE)
    outcome = SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert outcome.finalized and not outcome.idempotent
    assert journal.get_session(SESSION).state is SessionState.COMPLETED
    assert journal.get_command(command_id).state is CommandState.RESULT_PUBLISHED
    lifecycle = journal.get_workspace_lifecycle(SESSION)
    assert lifecycle is not None and lifecycle.state is WorkspaceLifecycleState.PRESERVED
    assert wm.path.exists()
    replay = SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert replay.idempotent and not replay.finalized
    assert journal._connection.execute(
        "SELECT COUNT(*) FROM events WHERE session_id=? AND event_type='workspace.preserved'", (SESSION,)
    ).fetchone()[0] == 1
    journal.close()


def test_finalize_requires_shared_lock(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path, session_state=SessionState.ACTIVE)
    with pytest.raises(BridgeError) as exc:
        SessionFinalizer(journal).finalize(SESSION, lock_held=False)
    assert exc.value.code == "instance_lock_failed"
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert journal.get_workspace_lifecycle(SESSION) is None
    assert wm.path.exists()
    journal.close()


def test_finalize_rechecks_active_service_inside_transaction(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path, session_state=SessionState.ACTIVE)
    journal._connection.execute(
        "INSERT INTO service_instances VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("inst-finalize", 999999, "running", NOW, NOW, None, None, None, None, NOW, NOW),
    )
    with pytest.raises(BridgeError) as exc:
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert exc.value.code == "instance_already_running"
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert journal.get_workspace_lifecycle(SESSION) is None
    assert wm.path.exists()
    journal.close()


@pytest.mark.parametrize("state", [
    CommandState.DISCOVERED, CommandState.VALIDATED, CommandState.CLAIMED,
    CommandState.EXECUTING, CommandState.EFFECT_RECORDED, CommandState.RESULT_STAGED,
    CommandState.MANUAL_RECONCILIATION_REQUIRED,
])
def test_finalize_rejects_unresolved_or_manual_commands(tmp_path: Path, state: CommandState) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path / state.value, session_state=SessionState.ACTIVE, command_state=state
    )
    with pytest.raises(BridgeError):
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert wm.path.exists()
    journal.close()


def test_finalize_rejects_pending_outbox_and_blocking_issue(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path, session_state=SessionState.ACTIVE)
    result_hash = "sha256:" + "a" * 64
    journal._connection.execute(
        "INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?)",
        (command_id, SESSION, 1, "success", None, result_hash, "{}", "result.json", NOW),
    )
    journal._connection.execute(
        "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (command_id, SESSION, 1, result_hash, "result.json", "pending", 0, None, None, None, None, NOW, NOW),
    )
    with pytest.raises(BridgeError):
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    journal._connection.execute("DELETE FROM outbox")
    journal._connection.execute(
        "INSERT INTO ingestion_issues(source_id,source_path,snapshot_sha,document_commit_sha,raw_sha256,session_id,command_id,error_code,detail,blocking,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("commands", "x", "a" * 40, None, "a" * 64, SESSION, command_id, "collision", "hashes only", 1, NOW),
    )
    with pytest.raises(BridgeError):
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    journal.close()


def test_finalize_rejects_non_active_terminal_states(tmp_path: Path) -> None:
    for state in (SessionState.ABORTED, SessionState.MANUAL_RECONCILIATION_REQUIRED):
        cfg, journal, wm, workspace, _ = make_fixture(tmp_path / state.value, session_state=state)
        with pytest.raises(BridgeError):
            SessionFinalizer(journal).finalize(SESSION, lock_held=True)
        assert wm.path.exists()
        journal.close()
