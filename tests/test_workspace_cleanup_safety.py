from __future__ import annotations

import os
from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    CommandState,
    SessionState,
    WorkspaceLifecycleCoordinator,
    WorkspaceLifecycleState,
)
from tests.helpers.workspace_lifecycle_fixture import NOW, SESSION, git, make_fixture


def _add_result_outbox(journal, command_id: str, state: str) -> None:
    result_hash = "sha256:" + "d" * 64
    journal._connection.execute(
        "INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?)",
        (command_id, SESSION, 1, "success", None, result_hash, "{}", "sessions/x/result.json", NOW),
    )
    journal._connection.execute(
        "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (command_id, SESSION, 1, result_hash, "sessions/x/result.json", state, 0, None, None, None, None, NOW, NOW),
    )


def _add_service(journal, state: str) -> None:
    journal._connection.execute(
        "INSERT INTO service_instances VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"inst-{state}", 999999, state, NOW, NOW, NOW if state == "stopping" else None, None, None, None, NOW, NOW),
    )


@pytest.mark.parametrize(
    "case",
    [
        "active", "completing", "manual", "service_running", "service_stopping",
        "pending_outbox", "collision_outbox", "recoverable", "blocking_issue",
        "outside_root", "other_session_path", "attached_branch", "wrong_base",
        "unauthorized_file", "temp_artifact", "physical_hash", "source_dirty",
        "lifecycle_identity", "missing_registration", "duplicate_registration",
    ],
)
def test_unsafe_cleanup_matrix_blocks_and_preserves(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_state = {
        "active": SessionState.ACTIVE,
        "completing": SessionState.COMPLETING,
        "manual": SessionState.MANUAL_RECONCILIATION_REQUIRED,
    }.get(case, SessionState.COMPLETED)
    command_state = CommandState.CLAIMED if case == "recoverable" else CommandState.RESULT_PUBLISHED
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path / case, session_state=session_state, command_state=command_state
    )
    coordinator = WorkspaceLifecycleCoordinator(cfg, journal)

    if case == "service_running":
        _add_service(journal, "running")
    elif case == "service_stopping":
        _add_service(journal, "stopping")
    elif case == "pending_outbox":
        _add_result_outbox(journal, command_id, "pending")
    elif case == "collision_outbox":
        _add_result_outbox(journal, command_id, "collision")
    elif case == "blocking_issue":
        journal._connection.execute(
            "INSERT INTO ingestion_issues(source_id,source_path,snapshot_sha,document_commit_sha,raw_sha256,session_id,command_id,error_code,detail,blocking,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("commands", "x", "a" * 40, None, "b" * 64, SESSION, command_id, "collision", "hashes only", 1, NOW),
        )
    elif case == "outside_root":
        journal._connection.execute(
            "UPDATE workspaces SET workspace_path=? WHERE session_id=?",
            (str(tmp_path / "foreign" / SESSION), SESSION),
        )
    elif case == "other_session_path":
        journal._connection.execute(
            "UPDATE workspaces SET workspace_path=? WHERE session_id=?",
            (str(Path(cfg.worktree_root) / "018f3f66-6cb3-4f66-9f2e-3d7647d1b799"), SESSION),
        )
    elif case == "attached_branch":
        git(wm.path, "checkout", "-b", "cleanup-attached")
    elif case == "wrong_base":
        source = Path(cfg.fixture_repo_path)
        (source / ".gitignore").write_text("__pycache__/\n*.pyc\n.extra\n", encoding="utf-8")
        git(source, "add", "--", ".gitignore")
        git(source, "commit", "-m", "second")
        git(wm.path, "checkout", "--detach", git(source, "rev-parse", "HEAD"))
    elif case == "unauthorized_file":
        (wm.path / "foreign.txt").write_text("keep", encoding="utf-8")
    elif case == "temp_artifact":
        (wm.path / ".bdb_temp_deadbeef").write_text("keep", encoding="utf-8")
    elif case == "physical_hash":
        (wm.path / "src" / "clamp.py").write_text("def clamp(value):\n    return 7\n", encoding="utf-8")
    elif case == "source_dirty":
        (Path(cfg.fixture_repo_path) / "src" / "clamp.py").write_text("dirty\n", encoding="utf-8")
    elif case == "lifecycle_identity":
        coordinator.preserve(SESSION)
        journal._connection.execute(
            "UPDATE workspace_lifecycle SET expected_revision=expected_revision+1 WHERE session_id=?", (SESSION,)
        )
    elif case == "missing_registration":
        monkeypatch.setattr(coordinator, "_registration_count", lambda _manager: 0)
    elif case == "duplicate_registration":
        monkeypatch.setattr(coordinator, "_registration_count", lambda _manager: 2)

    outcome = coordinator.cleanup(SESSION, confirm_session_id=SESSION, lock_held=True)
    assert outcome.state is WorkspaceLifecycleState.BLOCKED
    assert not outcome.removed
    assert wm.path.exists()
    assert journal.get_workspace(SESSION) is not None
    assert journal.path.exists()
    if case != "source_dirty":
        assert git(Path(cfg.fixture_repo_path), "status", "--porcelain=v1") == ""
    journal.close()


def test_confirmation_mismatch_refuses_before_lifecycle_change(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path)
    coordinator = WorkspaceLifecycleCoordinator(cfg, journal)
    with pytest.raises(BridgeError) as exc:
        coordinator.cleanup(
            SESSION,
            confirm_session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b799",
            lock_held=True,
        )
    assert exc.value.code == "policy_denied"
    assert journal.get_workspace_lifecycle(SESSION) is None
    assert wm.path.exists()
    journal.close()


def test_symlink_or_reparse_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path)
    backing = wm.path.with_name(wm.path.name + "-backing")
    real_symlink = False
    try:
        wm.path.rename(backing)
        os.symlink(backing, wm.path, target_is_directory=True)
        real_symlink = True
    except OSError:
        if backing.exists() and not wm.path.exists():
            backing.rename(wm.path)
        original = type(wm)._is_reparse
        monkeypatch.setattr(
            type(wm), "_is_reparse", staticmethod(lambda path: path == wm.path or original(path))
        )
    coordinator = WorkspaceLifecycleCoordinator(cfg, journal)
    outcome = coordinator.cleanup(SESSION, confirm_session_id=SESSION, lock_held=True)
    assert outcome.state is WorkspaceLifecycleState.BLOCKED
    assert wm.path.exists()
    if real_symlink:
        assert wm.path.is_symlink()
    journal.close()
