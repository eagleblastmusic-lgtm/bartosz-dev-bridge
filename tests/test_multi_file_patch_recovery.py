from __future__ import annotations

import base64
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge import Journal
from bdb_bridge.edit_operation_parser import sha256_bytes
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.multi_file_patch_executor import MultiFilePatchExecutor
from bdb_bridge.multi_file_patch_migration import MIGRATION_V9
from bdb_bridge.multi_file_patch_parser import parse_multi_file_patch
from bdb_bridge.multi_file_patch_planner import MultiFilePatchPlanner
from bdb_bridge.multi_file_patch_recovery_models import MultiFileCheckpointState
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_manager import WorkspaceManager


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b703"
COMMAND_ID = f"{SESSION_ID}:000001"


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    return completed.stdout.strip()


def content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_bytes(content),
    }


def replacement(path: str, before: bytes, after: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-file-replacement-v1",
        "kind": "replace_file",
        "path": path,
        "expected_sha256": sha256_bytes(before),
        **content_fields(after),
    }


def create(path: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "create_file",
        "path": path,
        **content_fields(content),
    }


def delete(path: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "delete_file",
        "path": path,
        "expected_sha256": sha256_bytes(content),
    }


def patch_document() -> dict[str, object]:
    return {
        "schema": "bdb-multi-file-patch-v1",
        "operations": [
            replacement("a.txt", b"a", b"A"),
            create("new.txt", b"new"),
            delete("old.txt", b"old"),
        ],
    }


def environment(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    run_git(source, "init")
    run_git(source, "config", "user.email", "bridge-test@localhost.invalid")
    run_git(source, "config", "user.name", "Bridge Test")
    (source / "a.txt").write_bytes(b"a")
    (source / "old.txt").write_bytes(b"old")
    run_git(source, "add", "--", "a.txt", "old.txt")
    run_git(source, "commit", "-m", "fixture")
    base_sha = run_git(source, "rev-parse", "HEAD")

    config = SimpleNamespace(
        fixture_repo_path=source,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("*",),
    )
    journal_path = tmp_path / "journal.db"
    journal = Journal.open(journal_path, now_fn=lambda: "2026-07-16T12:00:00Z")
    journal.create_session(SESSION_ID, "fixture", base_sha)
    journal.record_command(
        SESSION_ID,
        COMMAND_ID,
        1,
        {
            "session_id": SESSION_ID,
            "command_id": COMMAND_ID,
            "sequence": 1,
            "expected_revision": 0,
            "operation": "multi_file_patch",
            "payload": {},
        },
    )
    workspace = WorkspaceManager(config, SESSION_ID, base_sha, ["*"])
    workspace.ensure_workspace(journal)
    planner = MultiFilePatchPlanner(workspace)
    plan = planner.plan(parse_multi_file_patch(patch_document()))
    executor = MultiFilePatchExecutor(workspace, journal)
    return config, journal_path, journal, workspace, plan, executor


def assert_before(workspace: WorkspaceManager) -> None:
    assert (workspace.path / "a.txt").read_bytes() == b"a"
    assert (workspace.path / "old.txt").read_bytes() == b"old"
    assert not (workspace.path / "new.txt").exists()


def assert_after(workspace: WorkspaceManager) -> None:
    assert (workspace.path / "a.txt").read_bytes() == b"A"
    assert not (workspace.path / "old.txt").exists()
    assert (workspace.path / "new.txt").read_bytes() == b"new"


def test_v9_migration_and_checkpoint_paths_are_immutable(tmp_path: Path) -> None:
    _, _, journal, _, plan, executor = environment(tmp_path)
    row = journal._connection.execute(
        "SELECT name, checksum FROM schema_migrations WHERE version = 9"
    ).fetchone()
    assert row == ("journal_v9_multi_file_patch_recovery", MIGRATION_V9.checksum())
    executor.checkpoint(command_id=COMMAND_ID, session_id=SESSION_ID, plan=plan)
    with pytest.raises(sqlite3.DatabaseError):
        journal._connection.execute(
            "UPDATE multi_file_patch_checkpoint_paths SET roles_json = '[]' WHERE command_id = ?",
            (COMMAND_ID,),
        )
    journal.close()


def test_apply_then_commit_advances_revision_once(tmp_path: Path) -> None:
    _, _, journal, workspace, plan, executor = environment(tmp_path)
    before = journal.get_workspace(SESSION_ID)
    assert before is not None and before.revision == 0
    executor.checkpoint(command_id=COMMAND_ID, session_id=SESSION_ID, plan=plan)
    applied = executor.apply(COMMAND_ID)
    assert applied.state is MultiFileCheckpointState.APPLIED
    assert journal.get_workspace(SESSION_ID) == before
    assert_after(workspace)
    committed = executor.commit(COMMAND_ID)
    assert committed.state is MultiFileCheckpointState.COMMITTED
    after = journal.get_workspace(SESSION_ID)
    assert after is not None and after.revision == 1
    assert after.state_hash != before.state_hash
    assert executor.commit(COMMAND_ID).state is MultiFileCheckpointState.COMMITTED
    assert journal.get_workspace(SESSION_ID).revision == 1
    journal.close()


def test_recovery_reuses_exact_temp_after_crash(tmp_path: Path) -> None:
    config, journal_path, journal, workspace, plan, executor = environment(tmp_path)
    executor.checkpoint(command_id=COMMAND_ID, session_id=SESSION_ID, plan=plan)

    def crash(stage: str) -> None:
        if stage == "AFTER_BATCH_TEMP_WRITTEN:0:apply":
            raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError):
        executor.apply(COMMAND_ID, fault_hook=crash)
    assert journal.get_multi_file_patch_checkpoint(COMMAND_ID).state is MultiFileCheckpointState.APPLYING
    journal.close()

    reopened = Journal.open(journal_path, now_fn=lambda: "2026-07-16T12:01:00Z")
    resumed_workspace = WorkspaceManager(config, SESSION_ID, workspace.base_sha, ["*"])
    recovered = MultiFilePatchExecutor(resumed_workspace, reopened).recover(COMMAND_ID)
    assert recovered.state is MultiFileCheckpointState.APPLIED
    assert_after(resumed_workspace)
    assert not list(resumed_workspace.path.rglob(".bdb_batch_*"))
    reopened.close()


def test_partial_apply_recovers_and_can_roll_back(tmp_path: Path) -> None:
    config, journal_path, journal, workspace, plan, executor = environment(tmp_path)
    executor.checkpoint(command_id=COMMAND_ID, session_id=SESSION_ID, plan=plan)

    def crash(stage: str) -> None:
        if stage == "AFTER_BATCH_PATH_APPLIED:0":
            raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError):
        executor.apply(COMMAND_ID, fault_hook=crash)
    journal.close()

    reopened = Journal.open(journal_path, now_fn=lambda: "2026-07-16T12:02:00Z")
    resumed_workspace = WorkspaceManager(config, SESSION_ID, workspace.base_sha, ["*"])
    resumed = MultiFilePatchExecutor(resumed_workspace, reopened)
    assert resumed.recover(COMMAND_ID).state is MultiFileCheckpointState.APPLIED
    assert_after(resumed_workspace)
    rolled_back = resumed.rollback(COMMAND_ID)
    assert rolled_back.state is MultiFileCheckpointState.ROLLED_BACK
    assert_before(resumed_workspace)
    record = reopened.get_workspace(SESSION_ID)
    assert record is not None and record.revision == 0
    reopened.close()


def test_rollback_itself_is_crash_recoverable(tmp_path: Path) -> None:
    config, journal_path, journal, workspace, plan, executor = environment(tmp_path)
    executor.checkpoint(command_id=COMMAND_ID, session_id=SESSION_ID, plan=plan)
    executor.apply(COMMAND_ID)

    def crash(stage: str) -> None:
        if stage == "AFTER_BATCH_PATH_ROLLED_BACK:0":
            raise RuntimeError("synthetic rollback crash")

    with pytest.raises(RuntimeError):
        executor.rollback(COMMAND_ID, fault_hook=crash)
    journal.close()

    reopened = Journal.open(journal_path, now_fn=lambda: "2026-07-16T12:03:00Z")
    resumed_workspace = WorkspaceManager(config, SESSION_ID, workspace.base_sha, ["*"])
    outcome = MultiFilePatchExecutor(resumed_workspace, reopened).recover(COMMAND_ID)
    assert outcome.state is MultiFileCheckpointState.ROLLED_BACK
    assert_before(resumed_workspace)
    reopened.close()


def test_unexpected_external_change_blocks_recovery(tmp_path: Path) -> None:
    _, _, journal, workspace, plan, executor = environment(tmp_path)
    executor.checkpoint(command_id=COMMAND_ID, session_id=SESSION_ID, plan=plan)

    def crash(stage: str) -> None:
        if stage == "AFTER_BATCH_PATH_APPLIED:0":
            raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError):
        executor.apply(COMMAND_ID, fault_hook=crash)
    (workspace.path / "old.txt").write_bytes(b"foreign")
    with pytest.raises(BridgeError) as blocked:
        executor.recover(COMMAND_ID)
    assert blocked.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED
    assert journal.get_multi_file_patch_checkpoint(COMMAND_ID).state is MultiFileCheckpointState.BLOCKED
    journal.close()
