from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from bdb_bridge import BridgeConfig, CommandState, Journal, SessionState, WorkspaceManager

NOW = "2026-07-15T21:00:00Z"
SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b708"
COMMAND = f"{SESSION}:000001"


def git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def make_fixture(root: Path, *, session_id: str = SESSION, session_state: SessionState = SessionState.COMPLETED, command_state: CommandState = CommandState.RESULT_PUBLISHED):
    fixture = root / "fixture"
    shutil.copytree(
        Path(__file__).parents[2] / "bdb-poc-fixture",
        fixture,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", "*.pyc"),
    )
    git(fixture, "init", "-b", "main")
    git(fixture, "config", "core.autocrlf", "false")
    git(fixture, "config", "user.name", "Test")
    git(fixture, "config", "user.email", "test@example.invalid")
    git(fixture, "add", "--", ".gitattributes", ".gitignore", "pyproject.toml", "src", "tests")
    git(fixture, "commit", "-m", "baseline")
    base = git(fixture, "rev-parse", "HEAD")
    control = root / "control"
    control.mkdir(parents=True)
    worktrees = root / "worktrees"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    cfg = BridgeConfig(
        control, fixture, worktrees,
        runtime_dir=runtime,
        journal_path=runtime / "journal.db",
        allowed_paths=("src/clamp.py", "tests/test_clamp.py"),
        python_executable=sys.executable,
        test_timeout_seconds=20,
    )
    journal = Journal.open(cfg.journal_path, now_fn=lambda: NOW)
    manifest = {
        "schema_version": "1.1", "session_id": session_id,
        "repository_id": "fixture", "base_sha": base,
        "allowed_paths": ["src/clamp.py", "tests/test_clamp.py"],
    }
    journal._connection.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
        (session_id, "fixture", base, session_state.value, NOW, NOW),
    )
    journal._connection.execute(
        "INSERT INTO session_ingestion VALUES(?,?,?,?,?,?,?,?,?,?)",
        (session_id, "manifest.json", "b" * 40, "raw", "manifest", json.dumps(manifest), NOW, "2099-01-01T00:00:00Z", NOW, NOW),
    )
    command_id = f"{session_id}:000001"
    doc = {
        "schema_version": "1.1", "session_id": session_id,
        "command_id": command_id, "sequence": 1,
        "operation": "replace_exact_and_test", "expected_revision": 0,
        "payload": {"path": "src/clamp.py", "old": "return value", "new": "return value", "profile_id": "poc_pytest"},
    }
    journal._connection.execute(
        "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (command_id, session_id, 1, "c" * 64, json.dumps(doc), "c" * 40, command_state.value, 0, None, NOW, NOW),
    )
    wm = WorkspaceManager(cfg, session_id, base, manifest["allowed_paths"])
    workspace = wm.ensure_workspace(journal)
    return cfg, journal, wm, workspace, command_id
