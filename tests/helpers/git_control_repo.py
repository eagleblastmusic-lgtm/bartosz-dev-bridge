from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
SESSION_ID_B = "018f3f66-6cb3-4f66-9f2e-3d7647d1b702"
BASE_SHA = "a" * 40
CREATED_AT = "2026-07-15T08:00:00Z"
EXPIRES_AT = "2026-07-16T08:00:00Z"
EXPIRED_AT = "2026-07-14T08:00:00Z"
FIXED_NOW = "2026-07-15T05:40:00Z"


def fixed_now() -> str:
    return FIXED_NOW


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@dataclass(frozen=True)
class ControlRepoFixture:
    remote: Path
    writer: Path
    clone: Path


def init_control_remote(tmp_path: Path) -> ControlRepoFixture:
    remote = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    run_git(writer, "config", "user.name", "GHB0 Test")
    run_git(writer, "config", "user.email", "ghb0@example.invalid")
    (writer / "README.md").write_text("# control\n", encoding="utf-8")
    run_git(writer, "add", "README.md")
    run_git(writer, "commit", "-m", "initial")
    run_git(writer, "branch", "-M", "main")
    run_git(writer, "push", "-u", "origin", "main")
    for branch in ("commands", "results"):
        run_git(writer, "checkout", "-B", branch, "main")
        run_git(writer, "push", "-u", "origin", branch)
    run_git(writer, "checkout", "commands")

    clone = tmp_path / "bridge-control"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(clone)],
        check=True,
        capture_output=True,
    )
    return ControlRepoFixture(remote=remote, writer=writer, clone=clone)


def manifest_payload(
    *,
    session_id: str = SESSION_ID,
    repository_id: str = "bdb-poc-fixture",
    base_sha: str = BASE_SHA,
    created_at: str = CREATED_AT,
    expires_at: str = EXPIRES_AT,
) -> dict:
    return {
        "schema_version": "1.1",
        "session_id": session_id,
        "repository_id": repository_id,
        "base_sha": base_sha,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def command_payload(
    *,
    session_id: str = SESSION_ID,
    sequence: int = 1,
    operation: str = "open_read",
    created_at: str = CREATED_AT,
    expires_at: str = EXPIRES_AT,
    expected_revision: int = 0,
    expected_state_hash: str | None = None,
    payload: dict | None = None,
) -> dict:
    return {
        "schema_version": "1.1",
        "session_id": session_id,
        "command_id": f"{session_id}:{sequence:06d}",
        "sequence": sequence,
        "operation": operation,
        "created_at": created_at,
        "expires_at": expires_at,
        "expected_revision": expected_revision,
        "expected_state_hash": expected_state_hash,
        "payload": payload or {"path": "src/clamp.py"},
    }


def write_manifest(writer: Path, session_id: str, manifest: dict) -> None:
    root = writer / "sessions" / session_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def write_command(writer: Path, session_id: str, sequence: int, command: dict) -> None:
    root = writer / "sessions" / session_id / "commands"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{sequence:06d}.json").write_text(json.dumps(command), encoding="utf-8")


def commit_and_push_commands(writer: Path, message: str = "update commands") -> str:
    run_git(writer, "add", "--", "sessions")
    run_git(writer, "commit", "-m", message)
    run_git(writer, "push", "origin", "commands")
    return run_git(writer, "rev-parse", "HEAD").stdout.strip()


def fetch_clone(clone: Path) -> None:
    run_git(
        clone,
        "fetch",
        "--prune",
        "origin",
        "+refs/heads/commands:refs/remotes/origin/commands",
        "+refs/heads/results:refs/remotes/origin/results",
    )
