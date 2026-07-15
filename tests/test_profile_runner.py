from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from bdb_bridge import BridgeConfig, ExecutionCoordinator, Journal, WorkspaceManager
from bdb_bridge.execution import sanitized_test_environment

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
BASE_SHA = "a" * 40


def init_fixture(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "bdb-poc-fixture"
    fixture = tmp_path / "fixture"
    shutil.copytree(source, fixture)
    return fixture


def make_config(tmp_path: Path, fixture: Path, timeout: int = 30) -> BridgeConfig:
    return BridgeConfig(
        control_repo_path=tmp_path / "control",
        fixture_repo_path=fixture,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("src/clamp.py", "tests/test_clamp.py"),
        poll_interval_seconds=0.01,
        max_poll_seconds=30,
        test_timeout_seconds=timeout,
        python_executable=sys.executable,
    )


def test_sanitized_test_environment() -> None:
    env = sanitized_test_environment()

    # Must only contain specific allowed keys
    assert "SYSTEMROOT" in env or "windir" in env or "PATH" in env or not env
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert env.get("PYTHONHASHSEED") == "0"

    # Must not contain arbitrary user-specific environment variables
    assert "USERPROFILE" not in env
    assert "APPDATA" not in env


def test_profile_runner_success(tmp_path: Path) -> None:
    fixture = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")

    # The execution contract requires an exact 40-character hexadecimal base SHA.
    now = "2026-07-15T12:00:00Z"
    journal._connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "repo1", BASE_SHA, "active", now, now),
    )
    journal._connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SESSION_ID, str(fixture), BASE_SHA, 0, "hash", now, now),
    )

    wm = WorkspaceManager(
        config,
        SESSION_ID,
        BASE_SHA,
        ["src/clamp.py", "tests/test_clamp.py"],
    )
    wm.path = fixture  # Use fixture directory directly as workspace path.

    fixture.joinpath("src/clamp.py").write_text(
        "def clamp_percent(value: int) -> int:\n    return max(0, min(100, value))\n",
        encoding="utf-8",
    )

    coord = ExecutionCoordinator(config, journal)
    profile_run = coord._run_profile(wm)

    assert profile_run.status == "success"
    assert profile_run.exit_code == 0
    assert "passed" in profile_run.stdout
    assert profile_run.duration_ms > 0

    journal.close()


def test_profile_runner_failure(tmp_path: Path) -> None:
    fixture = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")

    fixture.joinpath("tests/test_clamp.py").write_text(
        "def test_fail(): assert False",
        encoding="utf-8",
    )

    wm = WorkspaceManager(
        config,
        SESSION_ID,
        BASE_SHA,
        ["src/clamp.py", "tests/test_clamp.py"],
    )
    wm.path = fixture

    coord = ExecutionCoordinator(config, journal)
    profile_run = coord._run_profile(wm)

    assert profile_run.status == "failed"
    assert profile_run.exit_code != 0
    assert "failed" in profile_run.stdout

    journal.close()


def test_profile_runner_timeout(tmp_path: Path) -> None:
    fixture = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture, timeout=1)
    journal = Journal.open(tmp_path / "journal.db")

    fixture.joinpath("tests/test_clamp.py").write_text(
        "import time\ndef test_sleep(): time.sleep(5)",
        encoding="utf-8",
    )

    wm = WorkspaceManager(
        config,
        SESSION_ID,
        BASE_SHA,
        ["src/clamp.py", "tests/test_clamp.py"],
    )
    wm.path = fixture

    coord = ExecutionCoordinator(config, journal)
    profile_run = coord._run_profile(wm)

    assert profile_run.status == "timeout"
    assert profile_run.exit_code is None

    journal.close()
