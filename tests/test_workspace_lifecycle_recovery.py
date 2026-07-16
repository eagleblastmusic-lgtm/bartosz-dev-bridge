from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import InstanceLock, Journal, WorkspaceLifecycleCoordinator, WorkspaceLifecycleState
from bdb_bridge.execution import SystemCrash
from tests.helpers.workspace_lifecycle_fixture import SESSION, git, make_fixture


def _locked(config, callback):
    lock = InstanceLock(Path(config.runtime_dir) / "bridge.instance.lock")
    assert lock.acquire() is True
    try:
        return callback()
    finally:
        lock.release()


def _crash_at(expected: str):
    def hook(point: str) -> None:
        if point == expected:
            raise SystemCrash(point)
    return hook


def test_cleanup_removes_only_exact_completed_worktree_and_is_idempotent(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, _ = make_fixture(tmp_path)
    unrelated = Path(cfg.worktree_root) / "unrelated"
    git(Path(cfg.fixture_repo_path), "worktree", "add", "--detach", str(unrelated), workspace.base_sha)
    coordinator = WorkspaceLifecycleCoordinator(cfg, journal)
    outcome = _locked(cfg, lambda: coordinator.cleanup(SESSION, confirm_session_id=SESSION, lock_held=True))
    assert outcome.removed and outcome.state is WorkspaceLifecycleState.REMOVED
    assert not wm.path.exists()
    assert unrelated.exists()
    listing = git(Path(cfg.fixture_repo_path), "worktree", "list", "--porcelain").replace("\\", "/")
    assert unrelated.resolve().as_posix() in listing
    assert git(Path(cfg.fixture_repo_path), "status", "--porcelain=v1") == ""
    replay = _locked(cfg, lambda: coordinator.cleanup(SESSION, confirm_session_id=SESSION, lock_held=True))
    assert replay.already_removed and not replay.removed
    assert journal._connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='workspace.cleanup_completed' AND session_id=?", (SESSION,)
    ).fetchone()[0] == 1
    assert journal.get_workspace(SESSION) is not None
    journal.close()


@pytest.mark.parametrize(
    "fault_point,expected_state,path_exists",
    [
        ("AFTER_CLEANUP_REQUEST_BEFORE_START", WorkspaceLifecycleState.CLEANUP_REQUESTED, True),
        ("AFTER_CLEANUP_STARTED_BEFORE_REMOVE", WorkspaceLifecycleState.REMOVING, True),
        ("AFTER_WORKTREE_REMOVE_BEFORE_JOURNAL_ACK", WorkspaceLifecycleState.REMOVING, False),
    ],
)
def test_cleanup_crash_recovery_reopens_and_completes(
    tmp_path: Path, fault_point: str, expected_state: WorkspaceLifecycleState, path_exists: bool
) -> None:
    cfg, journal, wm, workspace, _ = make_fixture(tmp_path / fault_point.lower())
    crashing = WorkspaceLifecycleCoordinator(cfg, journal, fault_hook=_crash_at(fault_point))
    with pytest.raises(SystemCrash):
        _locked(cfg, lambda: crashing.cleanup(SESSION, confirm_session_id=SESSION, lock_held=True))
    persisted = journal.get_workspace_lifecycle(SESSION)
    assert persisted is not None and persisted.state is expected_state
    assert wm.path.exists() is path_exists
    journal.close()

    reopened = Journal.open(cfg.journal_path)
    recovered = WorkspaceLifecycleCoordinator(cfg, reopened)
    outcome = _locked(cfg, lambda: recovered.cleanup(SESSION, confirm_session_id=SESSION, lock_held=True))
    assert outcome.state is WorkspaceLifecycleState.REMOVED
    assert not wm.path.exists()
    assert git(Path(cfg.fixture_repo_path), "status", "--porcelain=v1") == ""
    assert reopened._connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='workspace.cleanup_completed' AND session_id=?", (SESSION,)
    ).fetchone()[0] == 1
    reopened.close()
