from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    CommandState,
    SessionFinalizer,
    SessionState,
    SingleQueueScheduler,
    WorkspaceLifecycleState,
)
from tests.helpers.workspace_lifecycle_fixture import NOW, SESSION, make_fixture


WAITING_SESSION = "028f3f66-6cb3-4f66-9f2e-3d7647d1b709"
WAITING_COMMAND = f"{WAITING_SESSION}:000001"


def add_running_service(journal: object, state: str = "running") -> None:
    journal._connection.execute(
        "INSERT INTO service_instances VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("inst-handoff", 999999, state, NOW, NOW, None, None, None, None, NOW, NOW),
    )


def add_waiting_session(journal: object) -> None:
    active = journal.get_session(SESSION)
    assert active is not None
    manifest = {
        "schema_version": "1.1",
        "session_id": WAITING_SESSION,
        "repository_id": active.repository_id,
        "base_sha": active.base_sha,
        "allowed_paths": ["src/clamp.py", "tests/test_clamp.py"],
    }
    command = {
        "schema_version": "1.1",
        "session_id": WAITING_SESSION,
        "command_id": WAITING_COMMAND,
        "sequence": 1,
        "operation": "replace_exact_and_test",
        "expected_revision": 0,
        "payload": {
            "path": "src/clamp.py",
            "old": "return value",
            "new": "return value",
            "profile_id": "poc_pytest",
        },
    }
    journal._connection.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
        (
            WAITING_SESSION,
            active.repository_id,
            active.base_sha,
            SessionState.CREATED.value,
            NOW,
            NOW,
        ),
    )
    journal._connection.execute(
        "INSERT INTO session_ingestion VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            WAITING_SESSION,
            "waiting-manifest.json",
            "d" * 40,
            "raw-waiting",
            "manifest-waiting",
            json.dumps(manifest),
            NOW,
            "2099-01-01T00:00:00Z",
            NOW,
            NOW,
        ),
    )
    journal._connection.execute(
        "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            WAITING_COMMAND,
            WAITING_SESSION,
            1,
            "e" * 64,
            json.dumps(command),
            "e" * 40,
            CommandState.VALIDATED.value,
            0,
            None,
            NOW,
            NOW,
        ),
    )


def test_finalize_is_atomic_preserves_workspace_and_keeps_result_published(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
    )
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
        "SELECT COUNT(*) FROM events WHERE session_id=? AND event_type='workspace.preserved'",
        (SESSION,),
    ).fetchone()[0] == 1
    journal.close()


def test_finalize_requires_shared_lock(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
    )
    with pytest.raises(BridgeError) as exc:
        SessionFinalizer(journal).finalize(SESSION, lock_held=False)
    assert exc.value.code == "instance_lock_failed"
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert journal.get_workspace_lifecycle(SESSION) is None
    assert wm.path.exists()
    journal.close()


def test_finalize_rechecks_active_service_inside_transaction(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
    )
    add_running_service(journal)
    with pytest.raises(BridgeError) as exc:
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert exc.value.code == "instance_already_running"
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert journal.get_workspace_lifecycle(SESSION) is None
    assert wm.path.exists()
    journal.close()


@pytest.mark.parametrize(
    "state",
    [
        CommandState.DISCOVERED,
        CommandState.VALIDATED,
        CommandState.CLAIMED,
        CommandState.EXECUTING,
        CommandState.EFFECT_RECORDED,
        CommandState.RESULT_STAGED,
        CommandState.MANUAL_RECONCILIATION_REQUIRED,
    ],
)
def test_finalize_rejects_unresolved_or_manual_commands(
    tmp_path: Path,
    state: CommandState,
) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path / state.value,
        session_state=SessionState.ACTIVE,
        command_state=state,
    )
    with pytest.raises(BridgeError):
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert wm.path.exists()
    journal.close()


def test_finalize_rejects_pending_outbox_and_blocking_issue(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
    )
    result_hash = "sha256:" + "a" * 64
    journal._connection.execute(
        "INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?)",
        (command_id, SESSION, 1, "success", None, result_hash, "{}", "result.json", NOW),
    )
    journal._connection.execute(
        "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            command_id,
            SESSION,
            1,
            result_hash,
            "result.json",
            "pending",
            0,
            None,
            None,
            None,
            None,
            NOW,
            NOW,
        ),
    )
    with pytest.raises(BridgeError):
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    journal._connection.execute("DELETE FROM outbox")
    journal._connection.execute(
        "INSERT INTO ingestion_issues(source_id,source_path,snapshot_sha,document_commit_sha,raw_sha256,session_id,command_id,error_code,detail,blocking,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "commands",
            "x",
            "a" * 40,
            None,
            "a" * 64,
            SESSION,
            command_id,
            "collision",
            "hashes only",
            1,
            NOW,
        ),
    )
    with pytest.raises(BridgeError):
        SessionFinalizer(journal).finalize(SESSION, lock_held=True)
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    journal.close()


def test_finalize_rejects_non_active_terminal_states(tmp_path: Path) -> None:
    for state in (SessionState.ABORTED, SessionState.MANUAL_RECONCILIATION_REQUIRED):
        cfg, journal, wm, workspace, _ = make_fixture(
            tmp_path / state.value,
            session_state=state,
        )
        with pytest.raises(BridgeError):
            SessionFinalizer(journal).finalize(SESSION, lock_held=True)
        assert wm.path.exists()
        journal.close()


def test_scheduler_does_not_close_idle_session_without_waiting_work(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
    )
    add_running_service(journal)

    assert SingleQueueScheduler(journal).claim_next() is None
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert journal.get_workspace_lifecycle(SESSION) is None
    journal.close()


def test_scheduler_does_not_handoff_while_service_is_offline(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
    )
    add_waiting_session(journal)

    assert SingleQueueScheduler(journal).claim_next() is None
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert journal.get_session(WAITING_SESSION).state is SessionState.CREATED
    journal.close()


def test_scheduler_does_not_handoff_unresolved_active_session(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
        command_state=CommandState.RESULT_STAGED,
    )
    add_waiting_session(journal)
    add_running_service(journal)

    assert SingleQueueScheduler(journal).claim_next() is None
    assert journal.get_session(SESSION).state is SessionState.ACTIVE
    assert journal.get_session(WAITING_SESSION).state is SessionState.CREATED
    assert journal.get_workspace_lifecycle(SESSION) is None
    journal.close()


def test_scheduler_handoffs_completed_active_session_to_waiting_session(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path,
        session_state=SessionState.ACTIVE,
    )
    add_waiting_session(journal)
    add_running_service(journal)

    claimed = SingleQueueScheduler(journal).claim_next()

    assert claimed is not None
    assert claimed.command_id == WAITING_COMMAND
    assert claimed.session_id == WAITING_SESSION
    assert claimed.state is CommandState.CLAIMED
    assert journal.get_session(SESSION).state is SessionState.COMPLETED
    assert journal.get_session(WAITING_SESSION).state is SessionState.ACTIVE
    lifecycle = journal.get_workspace_lifecycle(SESSION)
    assert lifecycle is not None
    assert lifecycle.state is WorkspaceLifecycleState.PRESERVED
    assert wm.path.exists()
    assert journal._connection.execute(
        """SELECT COUNT(*) FROM events
        WHERE session_id=? AND event_type='session.auto_completed_for_handoff'""",
        (SESSION,),
    ).fetchone()[0] == 1
    journal.close()
