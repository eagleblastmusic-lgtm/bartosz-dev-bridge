from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from bdb_bridge import BridgeConfig, InstanceLock


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_service_config(
    tmp_path: Path,
    *,
    heartbeat_interval_seconds: float = 0.2,
    heartbeat_stale_seconds: float = 5.0,
    idle_poll_seconds: float = 2.0,
) -> tuple[BridgeConfig, Path, Path]:
    remote = tmp_path / "control-remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        text=True,
        capture_output=True,
        check=True,
    )

    control = tmp_path / "control"
    subprocess.run(
        ["git", "init", "-b", "main", str(control)],
        text=True,
        capture_output=True,
        check=True,
    )
    run_git(control, "config", "user.name", "Test")
    run_git(control, "config", "user.email", "test@example.invalid")
    (control / "README.txt").write_bytes(b"control\n")
    run_git(control, "add", "--", "README.txt")
    run_git(control, "commit", "-m", "initial")
    run_git(control, "branch", "commands")
    run_git(control, "branch", "results")
    run_git(control, "remote", "add", "origin", str(remote))
    run_git(control, "push", "origin", "main", "commands", "results")

    fixture = tmp_path / "fixture"
    subprocess.run(
        ["git", "init", "-b", "main", str(fixture)],
        text=True,
        capture_output=True,
        check=True,
    )
    run_git(fixture, "config", "user.name", "Test")
    run_git(fixture, "config", "user.email", "test@example.invalid")
    (fixture / "fixture.txt").write_bytes(b"fixture\n")
    run_git(fixture, "add", "--", "fixture.txt")
    run_git(fixture, "commit", "-m", "fixture")

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir(parents=True)
    durable_marker = worktree_root / "durable.marker"
    durable_marker.write_bytes(b"keep-me")

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    journal_path = runtime_dir / "journal.db"

    config = BridgeConfig(
        control_repo_path=control,
        fixture_repo_path=fixture,
        worktree_root=worktree_root,
        runtime_dir=runtime_dir,
        journal_path=journal_path,
        commands_ref="origin/commands",
        results_ref="origin/results",
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_stale_seconds=heartbeat_stale_seconds,
        idle_poll_seconds=idle_poll_seconds,
    )
    payload = {
        "schema_version": "1.1",
        "control_repo_path": str(control),
        "fixture_repo_path": str(fixture),
        "worktree_root": str(worktree_root),
        "runtime_dir": str(runtime_dir),
        "journal_path": str(journal_path),
        "commands_ref": "origin/commands",
        "results_ref": "origin/results",
        "heartbeat_interval_seconds": heartbeat_interval_seconds,
        "heartbeat_stale_seconds": heartbeat_stale_seconds,
        "idle_poll_seconds": idle_poll_seconds,
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return config, config_path, durable_marker


def cli_command(config_path: Path, *args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "bdb_bridge",
        "bridge",
        *args,
        "--config",
        str(config_path),
    ]


def read_status(config_path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        cli_command(config_path, "status", "--json"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip())


def wait_for_status(
    config_path: Path,
    expected: str,
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        latest = read_status(config_path)
        if latest["status"] == expected:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"status did not become {expected}; latest={latest!r}")


def wait_for_heartbeat_change(
    config_path: Path,
    initial: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        latest = read_status(config_path)
        if latest["status"] == "RUNNING" and latest["heartbeat_at"] != initial:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"heartbeat did not advance from {initial!r}; latest={latest!r}")


def assert_lock_is_free(config: BridgeConfig) -> None:
    lock = InstanceLock(Path(config.runtime_dir) / "bridge.instance.lock")
    assert lock.acquire() is True
    lock.release()
