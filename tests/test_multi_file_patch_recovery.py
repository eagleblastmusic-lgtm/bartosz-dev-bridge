from __future__ import annotations

import base64
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge import InstanceLock, Journal
from bdb_bridge.edit_operation_models import MAX_STRUCTURAL_CONTENT_BYTES
from bdb_bridge.edit_operation_parser import sha256_bytes
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.multi_file_patch_executor import MultiFilePatchExecutor
from bdb_bridge.multi_file_patch_journal import compute_multi_file_checkpoint_sha256
from bdb_bridge.multi_file_patch_migration import MIGRATION_V9
from bdb_bridge.multi_file_patch_models import MAX_BATCH_SNAPSHOT_BYTES
from bdb_bridge.multi_file_patch_parser import parse_multi_file_patch
from bdb_bridge.multi_file_patch_planner import MultiFilePatchPlanner
from bdb_bridge.multi_file_patch_recovery_models import (
    MultiFileCheckpointPath,
    MultiFileCheckpointState,
)
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_manager import WorkspaceManager


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b703"
COMMAND_ID = f"{SESSION_ID}:000001"
SECOND_SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b704"
SECOND_COMMAND_ID = f"{SECOND_SESSION_ID}:000001"


@dataclass
class TestEnvironment:
    config: SimpleNamespace
    journal_path: Path
    journal: Journal
    workspace: WorkspaceManager
    plan: object
    executor: MultiFilePatchExecutor
    instance_lock: InstanceLock

    def close(self) -> None:
        self.journal.close()
        self.instance_lock.release()


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


def register_command(journal: Journal, session_id: str, command_id: str) -> None:
    journal.record_command(
        session_id,
        command_id,
        1,
        {
            "session_id": session_id,
            "command_id": command_id,
            "sequence": 1,
            "expected_revision": 0,
            "operation": "multi_file_patch",
            "payload": {},
        },
    )


def preserve_workspace(journal: Journal, workspace: WorkspaceManager) -> None:
    record = journal.get_workspace(workspace.session_id)
    assert record is not None
    journal.record_workspace_preserved(
        session_id=workspace.session_id,
        workspace_path=str(workspace.path),
        base_sha=workspace.base_sha,
        expected_revision=record.revision,
        expected_state_hash=record.state_hash,
    )


def environment(
    tmp_path: Path,
    *,
    preserve: bool = True,
) -> TestEnvironment:
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
        runtime_dir=tmp_path / "runtime",
    )
    journal_path = tmp_path / "journal.db"
    journal = Journal.open(journal_path, now_fn=lambda: "2026-07-16T12:00:00Z")
    journal.create_session(SESSION_ID, "fixture", base_sha)
    register_command(journal, SESSION_ID, COMMAND_ID)
    workspace = WorkspaceManager(config, SESSION_ID, base_sha, ["*"])
    workspace.ensure_workspace(journal)
    if preserve:
        preserve_workspace(journal, workspace)
    planner = MultiFilePatchPlanner(workspace)
    plan = planner.plan(parse_multi_file_patch(patch_document()))
    instance_lock = InstanceLock(config.runtime_dir / "bridge.instance.lock")
    instance_lock.acquire()
    executor = MultiFilePatchExecutor(
        workspace,
        journal,
        instance_lock=instance_lock,
    )
    return TestEnvironment(
        config=config,
        journal_path=journal_path,
        journal=journal,
        workspace=workspace,
        plan=plan,
        executor=executor,
        instance_lock=instance_lock,
    )


def reopen(env: TestEnvironment, now: str) -> tuple[Journal, WorkspaceManager, InstanceLock, MultiFilePatchExecutor]:
    env.close()
    journal = Journal.open(env.journal_path, now_fn=lambda: now)
    workspace = WorkspaceManager(
        env.config,
        SESSION_ID,
        env.workspace.base_sha,
        ["*"],
    )
    instance_lock = InstanceLock(env.config.runtime_dir / "bridge.instance.lock")
    instance_lock.acquire()
    executor = MultiFilePatchExecutor(
        workspace,
        journal,
        instance_lock=instance_lock,
    )
    return journal, workspace, instance_lock, executor


def checkpoint_paths(env: TestEnvironment) -> tuple[MultiFileCheckpointPath, ...]:
    by_name = {item.path: item for item in env.plan.paths}
    return tuple(
        MultiFileCheckpointPath(
            command_id=COMMAND_ID,
            ordinal=ordinal,
            path=item.path,
            before_exists=item.before_exists,
            before=item.before,
            before_sha256=item.before_sha256,
            after_exists=item.after_exists,
            after=item.after,
            after_sha256=item.after_sha256,
            roles=item.roles,
            operation_indices=item.operation_indices,
        )
        for ordinal, item in enumerate(by_name[path] for path in env.plan.changed_paths)
    )


def prospective_temp(env: TestEnvironment, *, ordinal: int = 0, mode: str = "apply") -> Path:
    paths = checkpoint_paths(env)
    workspace_record = env.journal.get_workspace(SESSION_ID)
    assert workspace_record is not None
    after_hash = env.executor._predicted_state_hash(paths, after=True)
    checkpoint_sha = compute_multi_file_checkpoint_sha256(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        patch_sha256=env.plan.patch.patch_sha256,
        plan_sha256=env.plan.plan_sha256,
        workspace_revision_before=workspace_record.revision,
        workspace_state_hash_before=workspace_record.state_hash,
        workspace_state_hash_after=after_hash,
        paths=paths,
    )
    pseudo = SimpleNamespace(
        record=SimpleNamespace(checkpoint_sha256=checkpoint_sha),
        paths=paths,
    )
    return env.executor._temp_path(pseudo, paths[ordinal], mode)


def assert_before(workspace: WorkspaceManager) -> None:
    assert (workspace.path / "a.txt").read_bytes() == b"a"
    assert (workspace.path / "old.txt").read_bytes() == b"old"
    assert not (workspace.path / "new.txt").exists()


def assert_after(workspace: WorkspaceManager) -> None:
    assert (workspace.path / "a.txt").read_bytes() == b"A"
    assert not (workspace.path / "old.txt").exists()
    assert (workspace.path / "new.txt").read_bytes() == b"new"


def test_v9_migration_and_checkpoint_rows_are_immutable(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        row = env.journal._connection.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 9"
        ).fetchone()
        assert row == ("journal_v9_multi_file_patch_recovery", MIGRATION_V9.checksum())
        env.executor.checkpoint(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            plan=env.plan,
        )
        with pytest.raises(sqlite3.DatabaseError):
            env.journal._connection.execute(
                "UPDATE multi_file_patch_checkpoint_paths SET roles_json = '[]' WHERE command_id = ?",
                (COMMAND_ID,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            env.journal._connection.execute(
                "UPDATE multi_file_patch_checkpoints SET path_count = 1 WHERE command_id = ?",
                (COMMAND_ID,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            env.journal._connection.execute(
                "DELETE FROM multi_file_patch_checkpoints WHERE command_id = ?",
                (COMMAND_ID,),
            )
    finally:
        env.close()


def test_apply_then_commit_advances_revision_and_lifecycle_once(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        before = env.journal.get_workspace(SESSION_ID)
        assert before is not None and before.revision == 0
        env.executor.checkpoint(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            plan=env.plan,
        )
        applied = env.executor.apply(COMMAND_ID)
        assert applied.state is MultiFileCheckpointState.APPLIED
        assert env.journal.get_workspace(SESSION_ID) == before
        assert_after(env.workspace)
        committed = env.executor.commit(COMMAND_ID)
        assert committed.state is MultiFileCheckpointState.COMMITTED
        after = env.journal.get_workspace(SESSION_ID)
        assert after is not None and after.revision == 1
        assert after.state_hash != before.state_hash
        lifecycle = env.journal.get_workspace_lifecycle(SESSION_ID)
        assert lifecycle is not None
        assert lifecycle.expected_revision == after.revision
        assert lifecycle.expected_state_hash == after.state_hash
        assert env.executor.commit(COMMAND_ID).state is MultiFileCheckpointState.COMMITTED
        assert env.journal.get_workspace(SESSION_ID).revision == 1
    finally:
        env.close()


def test_recovery_reuses_owned_exact_temp_after_crash(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.executor.checkpoint(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        plan=env.plan,
    )

    def crash(stage: str) -> None:
        if stage == "AFTER_BATCH_TEMP_WRITTEN:0:apply":
            raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError):
        env.executor.apply(COMMAND_ID, fault_hook=crash)
    assert (
        env.journal.get_multi_file_patch_checkpoint(COMMAND_ID).state
        is MultiFileCheckpointState.APPLYING
    )

    journal, workspace, lock, executor = reopen(
        env,
        "2026-07-16T12:01:00Z",
    )
    try:
        recovered = executor.recover(COMMAND_ID)
        assert recovered.state is MultiFileCheckpointState.APPLIED
        assert_after(workspace)
        assert not list(workspace.path.rglob(".bdb_batch_*"))
    finally:
        journal.close()
        lock.release()


def test_partial_apply_recovers_and_can_roll_back(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.executor.checkpoint(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        plan=env.plan,
    )

    def crash(stage: str) -> None:
        if stage == "AFTER_BATCH_PATH_APPLIED:0":
            raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError):
        env.executor.apply(COMMAND_ID, fault_hook=crash)

    journal, workspace, lock, executor = reopen(
        env,
        "2026-07-16T12:02:00Z",
    )
    try:
        assert executor.recover(COMMAND_ID).state is MultiFileCheckpointState.APPLIED
        assert_after(workspace)
        rolled_back = executor.rollback(COMMAND_ID)
        assert rolled_back.state is MultiFileCheckpointState.ROLLED_BACK
        assert_before(workspace)
        record = journal.get_workspace(SESSION_ID)
        assert record is not None and record.revision == 0
    finally:
        journal.close()
        lock.release()


def test_rollback_itself_is_crash_recoverable(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.executor.checkpoint(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        plan=env.plan,
    )
    env.executor.apply(COMMAND_ID)

    def crash(stage: str) -> None:
        if stage == "AFTER_BATCH_PATH_ROLLED_BACK:0":
            raise RuntimeError("synthetic rollback crash")

    with pytest.raises(RuntimeError):
        env.executor.rollback(COMMAND_ID, fault_hook=crash)

    journal, workspace, lock, executor = reopen(
        env,
        "2026-07-16T12:03:00Z",
    )
    try:
        outcome = executor.recover(COMMAND_ID)
        assert outcome.state is MultiFileCheckpointState.ROLLED_BACK
        assert_before(workspace)
    finally:
        journal.close()
        lock.release()


def test_unexpected_external_change_blocks_recovery(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        env.executor.checkpoint(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            plan=env.plan,
        )

        def crash(stage: str) -> None:
            if stage == "AFTER_BATCH_PATH_APPLIED:0":
                raise RuntimeError("synthetic crash")

        with pytest.raises(RuntimeError):
            env.executor.apply(COMMAND_ID, fault_hook=crash)
        (env.workspace.path / "old.txt").write_bytes(b"foreign")
        with pytest.raises(BridgeError) as blocked:
            env.executor.recover(COMMAND_ID)
        assert blocked.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED
        assert (
            env.journal.get_multi_file_patch_checkpoint(COMMAND_ID).state
            is MultiFileCheckpointState.BLOCKED
        )
    finally:
        env.close()


def test_executor_requires_canonical_acquired_instance_lock(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        executor = MultiFilePatchExecutor(env.workspace, env.journal)
        with pytest.raises(BridgeError) as error:
            executor.checkpoint(
                command_id=COMMAND_ID,
                session_id=SESSION_ID,
                plan=env.plan,
            )
        assert error.value.code == BridgeErrorCode.INSTANCE_LOCK_FAILED
    finally:
        env.close()


def test_executor_requires_preserved_workspace_lifecycle(tmp_path: Path) -> None:
    env = environment(tmp_path, preserve=False)
    try:
        with pytest.raises(BridgeError) as error:
            env.executor.checkpoint(
                command_id=COMMAND_ID,
                session_id=SESSION_ID,
                plan=env.plan,
            )
        assert error.value.code == BridgeErrorCode.JOURNAL_CONFLICT
    finally:
        env.close()


def test_cleanup_requested_lifecycle_blocks_checkpoint(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        env.journal.request_workspace_cleanup(session_id=SESSION_ID)
        with pytest.raises(BridgeError) as error:
            env.executor.checkpoint(
                command_id=COMMAND_ID,
                session_id=SESSION_ID,
                plan=env.plan,
            )
        assert error.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED
    finally:
        env.close()


def test_preexisting_internal_temp_is_never_adopted(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        temp = prospective_temp(env)
        temp.write_bytes(b"A")
        with pytest.raises(BridgeError) as error:
            env.executor.checkpoint(
                command_id=COMMAND_ID,
                session_id=SESSION_ID,
                plan=env.plan,
            )
        assert error.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED
        assert temp.read_bytes() == b"A"
        assert env.journal.get_multi_file_patch_checkpoint(COMMAND_ID) is None
    finally:
        env.close()


def test_temp_name_is_bounded_and_does_not_embed_target_name(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        temp = prospective_temp(env)
        assert len(temp.name) < 80
        assert "a.txt" not in temp.name
    finally:
        env.close()


def test_recover_all_is_scoped_to_executor_session(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        env.executor.checkpoint(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            plan=env.plan,
        )

        env.journal.create_session(
            SECOND_SESSION_ID,
            "fixture",
            env.workspace.base_sha,
        )
        register_command(env.journal, SECOND_SESSION_ID, SECOND_COMMAND_ID)
        second_workspace = WorkspaceManager(
            env.config,
            SECOND_SESSION_ID,
            env.workspace.base_sha,
            ["*"],
        )
        second_workspace.ensure_workspace(env.journal)
        preserve_workspace(env.journal, second_workspace)
        second_plan = MultiFilePatchPlanner(second_workspace).plan(
            parse_multi_file_patch(patch_document())
        )
        second_executor = MultiFilePatchExecutor(
            second_workspace,
            env.journal,
            instance_lock=env.instance_lock,
        )
        second_executor.checkpoint(
            command_id=SECOND_COMMAND_ID,
            session_id=SECOND_SESSION_ID,
            plan=second_plan,
        )

        outcomes = env.executor.recover_all()
        assert [outcome.command_id for outcome in outcomes] == [COMMAND_ID]
        assert (
            env.journal.get_multi_file_patch_checkpoint(SECOND_COMMAND_ID).state
            is MultiFileCheckpointState.PLANNED
        )
    finally:
        env.close()


def raw_checkpoint_path(
    *,
    ordinal: int,
    path: str,
    content: bytes,
) -> MultiFileCheckpointPath:
    return MultiFileCheckpointPath(
        command_id=COMMAND_ID,
        ordinal=ordinal,
        path=path,
        before_exists=False,
        before=None,
        before_sha256=None,
        after_exists=True,
        after=content,
        after_sha256=sha256_bytes(content),
        roles=("create-destination",),
        operation_indices=(0,),
    )


def record_raw_checkpoint(
    env: TestEnvironment,
    paths: tuple[MultiFileCheckpointPath, ...],
) -> None:
    workspace = env.journal.get_workspace(SESSION_ID)
    assert workspace is not None
    after_hash = "sha256:" + "0" * 64
    checkpoint_sha = compute_multi_file_checkpoint_sha256(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        patch_sha256=env.plan.patch.patch_sha256,
        plan_sha256=env.plan.plan_sha256,
        workspace_revision_before=workspace.revision,
        workspace_state_hash_before=workspace.state_hash,
        workspace_state_hash_after=after_hash,
        paths=paths,
    )
    env.journal.record_multi_file_patch_checkpoint(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        patch_sha256=env.plan.patch.patch_sha256,
        plan_sha256=env.plan.plan_sha256,
        checkpoint_sha256=checkpoint_sha,
        workspace_revision_before=workspace.revision,
        workspace_state_hash_before=workspace.state_hash,
        workspace_state_hash_after=after_hash,
        paths=paths,
    )


def test_journal_rejects_oversized_per_file_snapshot(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        paths = (
            raw_checkpoint_path(
                ordinal=0,
                path="big.bin",
                content=b"x" * (MAX_STRUCTURAL_CONTENT_BYTES + 1),
            ),
        )
        with pytest.raises(BridgeError) as error:
            record_raw_checkpoint(env, paths)
        assert error.value.code == BridgeErrorCode.INVALID_PAYLOAD
    finally:
        env.close()


def test_journal_rejects_oversized_total_snapshot(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        unit = b"x" * MAX_STRUCTURAL_CONTENT_BYTES
        count = MAX_BATCH_SNAPSHOT_BYTES // MAX_STRUCTURAL_CONTENT_BYTES + 1
        paths = tuple(
            raw_checkpoint_path(
                ordinal=index,
                path=f"big-{index:02d}.bin",
                content=unit,
            )
            for index in range(count)
        )
        with pytest.raises(BridgeError) as error:
            record_raw_checkpoint(env, paths)
        assert error.value.code == BridgeErrorCode.INVALID_PAYLOAD
    finally:
        env.close()


def test_corrupt_persisted_roles_map_to_journal_corrupt(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        env.executor.checkpoint(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            plan=env.plan,
        )
        env.journal._connection.execute(
            "DROP TRIGGER multi_file_patch_paths_no_update"
        )
        env.journal._connection.execute(
            "UPDATE multi_file_patch_checkpoint_paths SET roles_json = '{' "
            "WHERE command_id = ? AND ordinal = 0",
            (COMMAND_ID,),
        )
        with pytest.raises(BridgeError) as error:
            env.journal.get_multi_file_patch_bundle(COMMAND_ID)
        assert error.value.code == BridgeErrorCode.JOURNAL_CORRUPT
    finally:
        env.close()


def test_corrupt_persisted_totals_map_to_journal_corrupt(tmp_path: Path) -> None:
    env = environment(tmp_path)
    try:
        env.executor.checkpoint(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            plan=env.plan,
        )
        env.journal._connection.execute(
            "DROP TRIGGER multi_file_patch_checkpoint_immutable"
        )
        env.journal._connection.execute(
            "UPDATE multi_file_patch_checkpoints "
            "SET total_before_bytes = total_before_bytes + 1 WHERE command_id = ?",
            (COMMAND_ID,),
        )
        with pytest.raises(BridgeError) as error:
            env.journal.get_multi_file_patch_bundle(COMMAND_ID)
        assert error.value.code == BridgeErrorCode.JOURNAL_CORRUPT
    finally:
        env.close()
