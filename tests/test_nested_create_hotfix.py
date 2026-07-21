from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge import InstanceLock, Journal
from bdb_bridge.edit_operation_parser import sha256_bytes
from bdb_bridge.multi_file_patch_executor import MultiFilePatchExecutor
from bdb_bridge.multi_file_patch_parser import parse_multi_file_patch
from bdb_bridge.multi_file_patch_planner import MultiFilePatchPlanner
from bdb_bridge.workspace_manager import WorkspaceManager


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b799"
COMMAND_ID = f"{SESSION_ID}:000001"


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    return completed.stdout.strip()


def create(path: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "create_file",
        "path": path,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_bytes(content),
    }


def test_nested_create_is_planned_applied_and_rolled_back_safely(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.email", "bridge-test@localhost.invalid")
    git(source, "config", "user.name", "Bridge Test")
    (source / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(source, "add", "--", "README.md")
    git(source, "commit", "-m", "fixture")
    base_sha = git(source, "rev-parse", "HEAD")

    config = SimpleNamespace(
        fixture_repo_path=source,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("src/**", "tests/**", "README.md"),
        runtime_dir=tmp_path / "runtime",
    )
    journal = Journal.open(tmp_path / "journal.db", now_fn=lambda: "2026-07-21T09:00:00Z")
    lock = InstanceLock(config.runtime_dir / "bridge.instance.lock")
    lock.acquire()
    try:
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
        workspace = WorkspaceManager(
            config,
            SESSION_ID,
            base_sha,
            ["src/**", "tests/**", "README.md"],
        )
        before = workspace.ensure_workspace(journal)
        journal.record_workspace_preserved(
            session_id=SESSION_ID,
            workspace_path=str(workspace.path),
            base_sha=base_sha,
            expected_revision=before.revision,
            expected_state_hash=before.state_hash,
        )

        patch = parse_multi_file_patch(
            {
                "schema": "bdb-multi-file-patch-v1",
                "operations": [
                    create("src/calculator.py", b"def add(a, b):\n    return a + b\n"),
                    create("tests/test_calculator.py", b"def test_smoke():\n    assert True\n"),
                ],
            }
        )
        planner = MultiFilePatchPlanner(workspace)
        plan = planner.plan(patch)
        assert plan.changed_paths == ("src/calculator.py", "tests/test_calculator.py")
        assert not (workspace.path / "src").exists()
        assert not (workspace.path / "tests").exists()

        executor = MultiFilePatchExecutor(workspace, journal, instance_lock=lock)
        executor.checkpoint(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            plan=plan,
        )
        executor.apply(COMMAND_ID)
        assert (workspace.path / "src" / "calculator.py").is_file()
        assert (workspace.path / "tests" / "test_calculator.py").is_file()

        executor.rollback(COMMAND_ID)
        assert not (workspace.path / "src" / "calculator.py").exists()
        assert not (workspace.path / "tests" / "test_calculator.py").exists()
        assert workspace.compute_state_hash() == before.state_hash
    finally:
        journal.close()
        lock.release()
