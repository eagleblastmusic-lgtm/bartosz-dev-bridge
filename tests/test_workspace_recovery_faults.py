from __future__ import annotations

import os
import shutil
import json
import sys
from pathlib import Path
import pytest

from bdb_bridge import (
    Journal,
    BridgeConfig,
    BridgeError,
    BridgeErrorCode,
    WorkspaceManager,
    ExecutionCoordinator,
    CommandState,
    SessionState,
)
from bdb_bridge.execution import SystemCrash
from bdb_poc.git_ops import Git

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
COMMAND_ID = f"{SESSION_ID}:000001"
FIXED_NOW = "2026-07-15T12:00:00Z"
def fixed_now() -> str:
    return FIXED_NOW

def run_git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()

import subprocess

def init_fixture(tmp_path: Path) -> tuple[Path, str]:
    source = Path(__file__).parents[1] / "bdb-poc-fixture"
    fixture = tmp_path / "fixture"
    shutil.copytree(source, fixture)
    run_git(fixture, "init", "-b", "main")
    run_git(fixture, "config", "user.name", "POC Test")
    run_git(fixture, "config", "user.email", "poc@example.invalid")
    run_git(fixture, "add", "--", ".gitignore", "pyproject.toml", "src", "tests")
    run_git(fixture, "commit", "-m", "fixture baseline")
    return fixture, run_git(fixture, "rev-parse", "HEAD")

def make_config(tmp_path: Path, fixture: Path) -> BridgeConfig:
    return BridgeConfig(
        control_repo_path=tmp_path / "control",
        fixture_repo_path=fixture,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("src/clamp.py", "tests/test_clamp.py"),
        poll_interval_seconds=0.01,
        max_poll_seconds=30,
        test_timeout_seconds=30,
        python_executable=sys.executable,
    )

def setup_db_for_command(journal: Journal, base_sha: str, command_payload: dict) -> None:
    now = FIXED_NOW
    # Record session
    journal._connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "repo1", base_sha, "active", now, now)
    )
    # Record manifest in session_ingestion
    manifest = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "repository_id": "repo1",
        "base_sha": base_sha,
        "allowed_paths": ["src/clamp.py", "tests/test_clamp.py"],
    }
    journal._connection.execute(
        """
        INSERT INTO session_ingestion (
            session_id, source_path, manifest_commit_sha, raw_sha256,
            manifest_sha256, manifest_json, created_remote_at, expires_at,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (SESSION_ID, "manifest.json", "csha", "rawsha", "msha", json.dumps(manifest), now, now, now, now)
    )
    # Record command in CLAIMED
    journal._connection.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            COMMAND_ID,
            SESSION_ID,
            1,
            "cmdsha",
            json.dumps(command_payload),
            "docsha",
            CommandState.CLAIMED.value,
            0,
            None,
            now,
            now,
        )
    )

def test_recovery_fault_matrix(tmp_path: Path) -> None:
    # We will test various fault points by running coordinator under a crash hook,
    # catching SystemCrash, resetting coordinator/journal, and running recovery.

    # 1. Crash AFTER_PLAN_COMMIT_BEFORE_WRITE (before file is modified)
    fixture, base_sha = init_fixture(tmp_path / "run1")
    config = make_config(tmp_path / "run1", fixture)

    db_path = tmp_path / "run1" / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    cmd_payload = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": COMMAND_ID,
        "sequence": 1,
        "operation": "replace_exact_and_test",
        "expected_revision": 0,
        "payload": {
            "path": "src/clamp.py",
            "old": "def clamp_percent(value: int) -> int:",
            "new": "def clamp_percent(value: int) -> int: # added comment",
            "profile_id": "poc_pytest"
        }
    }
    setup_db_for_command(journal, base_sha, cmd_payload)

    # Run with fault hook
    def hook_plan(point):
        if point == "AFTER_PLAN_COMMIT_BEFORE_WRITE":
            raise SystemCrash("crash after plan")

    coord = ExecutionCoordinator(config, journal, fault_hook=hook_plan)
    with pytest.raises(SystemCrash):
        coord.execute_or_recover(COMMAND_ID)

    journal.close()

    # Simulate restart: open a new journal and run recovery (which executes decision: EXECUTE)
    journal2 = Journal.open(db_path, now_fn=fixed_now)
    coord2 = ExecutionCoordinator(config, journal2)

    outcome = coord2.execute_or_recover(COMMAND_ID)
    assert outcome.status == "success"
    assert outcome.workspace_revision_after == 1

    cmd = journal2.get_command(COMMAND_ID)
    assert cmd.state == CommandState.EFFECT_RECORDED

    plan = journal2.get_operation_plan(COMMAND_ID)
    assert plan is not None

    effect = journal2.get_operation_effect(COMMAND_ID)
    assert effect is not None

    # Verify no duplicate events
    events = [e for e in journal2.list_events() if e.event_type == "operation.plan_recorded"]
    assert len(events) == 1

    journal2.close()

def test_recovery_crash_after_temp_write(tmp_path: Path) -> None:
    # 2. Crash AFTER_TEMP_WRITE_BEFORE_REPLACE (temp is written, target file is still before)
    fixture, base_sha = init_fixture(tmp_path / "run2")
    config = make_config(tmp_path / "run2", fixture)
    db_path = tmp_path / "run2" / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    cmd_payload = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": COMMAND_ID,
        "sequence": 1,
        "operation": "replace_exact_and_test",
        "expected_revision": 0,
        "payload": {
            "path": "src/clamp.py",
            "old": "def clamp_percent(value: int) -> int:",
            "new": "def clamp_percent(value: int) -> int: # second comment",
            "profile_id": "poc_pytest"
        }
    }
    setup_db_for_command(journal, base_sha, cmd_payload)

    def hook_temp(point):
        if point == "AFTER_TEMP_WRITE_BEFORE_REPLACE":
            raise SystemCrash("crash after temp write")

    coord = ExecutionCoordinator(config, journal, fault_hook=hook_temp)
    with pytest.raises(SystemCrash):
        coord.execute_or_recover(COMMAND_ID)

    journal.close()

    # Recovery should replace, commit effect, run profile, revision = 1
    journal2 = Journal.open(db_path, now_fn=fixed_now)
    coord2 = ExecutionCoordinator(config, journal2)

    outcome = coord2.execute_or_recover(COMMAND_ID)
    assert outcome.status == "success"
    assert outcome.workspace_revision_after == 1

    # Verify file was modified
    wm = WorkspaceManager(config, SESSION_ID, base_sha, [])
    assert b"second comment" in wm.read_exact_bytes("src/clamp.py")
    journal2.close()

def test_recovery_crash_after_replace_before_effect(tmp_path: Path) -> None:
    # 3. Crash AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT
    # Target file is planned-after, but no effect row in DB and command is EXECUTING
    fixture, base_sha = init_fixture(tmp_path / "run3")
    config = make_config(tmp_path / "run3", fixture)
    db_path = tmp_path / "run3" / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    cmd_payload = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": COMMAND_ID,
        "sequence": 1,
        "operation": "replace_exact_and_test",
        "expected_revision": 0,
        "payload": {
            "path": "src/clamp.py",
            "old": "def clamp_percent(value: int) -> int:",
            "new": "def clamp_percent(value: int) -> int: # third comment",
            "profile_id": "poc_pytest"
        }
    }
    setup_db_for_command(journal, base_sha, cmd_payload)

    def hook_replace(point):
        if point == "AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT":
            raise SystemCrash("crash after file replace")

    coord = ExecutionCoordinator(config, journal, fault_hook=hook_replace)
    with pytest.raises(SystemCrash):
        coord.execute_or_recover(COMMAND_ID)

    journal.close()

    # Recovery should detect PLANNED-AFTER, commit effect, revision = 1 without rewrite
    journal2 = Journal.open(db_path, now_fn=fixed_now)
    coord2 = ExecutionCoordinator(config, journal2)

    outcome = coord2.execute_or_recover(COMMAND_ID)
    assert outcome.status == "success"
    assert outcome.workspace_revision_after == 1

    # Verify exact one effect is committed
    effect = journal2.get_operation_effect(COMMAND_ID)
    assert effect is not None
    journal2.close()

def test_recovery_diverged_manual_reconciliation(tmp_path: Path) -> None:
    # 6. Manual file change before restart (making target content different from before or planned-after)
    fixture, base_sha = init_fixture(tmp_path / "run6")
    config = make_config(tmp_path / "run6", fixture)
    db_path = tmp_path / "run6" / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    cmd_payload = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": COMMAND_ID,
        "sequence": 1,
        "operation": "replace_exact_and_test",
        "expected_revision": 0,
        "payload": {
            "path": "src/clamp.py",
            "old": "def clamp_percent(value: int) -> int:",
            "new": "def clamp_percent(value: int) -> int: # comment",
            "profile_id": "poc_pytest"
        }
    }
    setup_db_for_command(journal, base_sha, cmd_payload)

    def hook_plan(point):
        if point == "AFTER_PLAN_COMMIT_BEFORE_WRITE":
            raise SystemCrash("crash after plan")

    coord = ExecutionCoordinator(config, journal, fault_hook=hook_plan)
    with pytest.raises(SystemCrash):
        coord.execute_or_recover(COMMAND_ID)
    journal.close()

    # Reread and modify the file manually to some other text
    wm = WorkspaceManager(config, SESSION_ID, base_sha, [])
    wm.resolve_allowed_path("src/clamp.py").write_text("def clamp_percent(value: int) -> int: # totally foreign text", encoding="utf-8")

    journal2 = Journal.open(db_path, now_fn=fixed_now)
    coord2 = ExecutionCoordinator(config, journal2)

    outcome = coord2.execute_or_recover(COMMAND_ID)
    assert outcome.status == "manual_reconciliation_required"
    assert outcome.error_code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED

    # Verify session and command transitioned to MANUAL_RECONCILIATION_REQUIRED
    cmd = journal2.get_command(COMMAND_ID)
    assert cmd.state == CommandState.MANUAL_RECONCILIATION_REQUIRED

    sess = journal2.get_session(SESSION_ID)
    assert sess.state == SessionState.MANUAL_RECONCILIATION_REQUIRED

    events = [e for e in journal2.list_events() if e.event_type == "workspace.recovery_blocked"]
    assert len(events) == 1

    journal2.close()

def test_recovery_diverged_extra_file(tmp_path: Path) -> None:
    # 7. Additional untracked foreign file
    fixture, base_sha = init_fixture(tmp_path / "run7")
    config = make_config(tmp_path / "run7", fixture)
    db_path = tmp_path / "run7" / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    cmd_payload = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": COMMAND_ID,
        "sequence": 1,
        "operation": "replace_exact_and_test",
        "expected_revision": 0,
        "payload": {
            "path": "src/clamp.py",
            "old": "def clamp_percent(value: int) -> int:",
            "new": "def clamp_percent(value: int) -> int: # comment",
            "profile_id": "poc_pytest"
        }
    }
    setup_db_for_command(journal, base_sha, cmd_payload)

    def hook_plan(point):
        if point == "AFTER_PLAN_COMMIT_BEFORE_WRITE":
            raise SystemCrash("crash")
    coord = ExecutionCoordinator(config, journal, fault_hook=hook_plan)
    with pytest.raises(SystemCrash):
        coord.execute_or_recover(COMMAND_ID)
    journal.close()

    # Write untracked file to workspace
    wm = WorkspaceManager(config, SESSION_ID, base_sha, [])
    wm.resolve_allowed_path("src/untracked.py").write_text("print(123)", encoding="utf-8")

    journal2 = Journal.open(db_path, now_fn=fixed_now)
    coord2 = ExecutionCoordinator(config, journal2)

    outcome = coord2.execute_or_recover(COMMAND_ID)
    assert outcome.status == "manual_reconciliation_required"
    journal2.close()

def test_recovery_diverged_missing_directory(tmp_path: Path) -> None:
    # 9. Workspace directory missing
    fixture, base_sha = init_fixture(tmp_path / "run9")
    config = make_config(tmp_path / "run9", fixture)
    db_path = tmp_path / "run9" / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    cmd_payload = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": COMMAND_ID,
        "sequence": 1,
        "operation": "replace_exact_and_test",
        "expected_revision": 0,
        "payload": {
            "path": "src/clamp.py",
            "old": "def clamp_percent(value: int) -> int:",
            "new": "def clamp_percent(value: int) -> int: # comment",
            "profile_id": "poc_pytest"
        }
    }
    setup_db_for_command(journal, base_sha, cmd_payload)

    def hook_plan(point):
        if point == "AFTER_PLAN_COMMIT_BEFORE_WRITE":
            raise SystemCrash("crash")
    coord = ExecutionCoordinator(config, journal, fault_hook=hook_plan)
    with pytest.raises(SystemCrash):
        coord.execute_or_recover(COMMAND_ID)
    journal.close()

    # Delete the workspace directory physically
    wm = WorkspaceManager(config, SESSION_ID, base_sha, [])
    shutil.rmtree(wm.path)

    journal2 = Journal.open(db_path, now_fn=fixed_now)
    coord2 = ExecutionCoordinator(config, journal2)

    outcome = coord2.execute_or_recover(COMMAND_ID)
    assert outcome.status == "manual_reconciliation_required"
    journal2.close()
