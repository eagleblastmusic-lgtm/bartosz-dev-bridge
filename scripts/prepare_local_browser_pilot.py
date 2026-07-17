from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALIAS = "pilot"
REPOSITORY_ID = "bdb-local-browser-pilot"
ALLOWED_PATHS = ["src/clamp.py", "tests/test_clamp.py", "PILOT_RESULT.md"]


def canonical_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def sha256_value(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_value(content),
    }


def ensure_root_is_safe(root: Path, implementation_root: Path) -> None:
    if root.exists():
        raise RuntimeError(f"Pilot root already exists: {root}")
    try:
        root.relative_to(implementation_root)
    except ValueError:
        return
    raise RuntimeError("Pilot root must stay outside the Bridge implementation checkout")


def initialize_fixture(root: Path) -> tuple[Path, str, bytes, bytes]:
    fixture = root / "fixture"
    fixture.mkdir()
    git(fixture, "init")
    git(fixture, "config", "core.autocrlf", "false")
    git(fixture, "config", "user.name", "BDB Local Browser Pilot")
    git(fixture, "config", "user.email", "browser-pilot@example.invalid")

    before = b"def clamp_percent(value: int) -> int:\n    return value\n"
    after = b"def clamp_percent(value: int) -> int:\n    return max(0, min(value, 100))\n"
    (fixture / "src").mkdir()
    (fixture / "tests").mkdir()
    (fixture / "src" / "clamp.py").write_bytes(before)
    (fixture / "tests" / "test_clamp.py").write_text(
        "from src.clamp import clamp_percent\n\n"
        "def test_clamp_percent() -> None:\n"
        "    assert clamp_percent(-1) == 0\n"
        "    assert clamp_percent(50) == 50\n"
        "    assert clamp_percent(120) == 100\n",
        encoding="utf-8",
        newline="\n",
    )
    git(fixture, "add", "--", "src/clamp.py", "tests/test_clamp.py")
    git(fixture, "commit", "-m", "initialize local browser pilot fixture")
    status = git(fixture, "status", "--porcelain=v1")
    if status:
        raise RuntimeError(f"Synthetic fixture is not clean after initialization: {status}")
    return fixture, git(fixture, "rev-parse", "HEAD"), before, after


def initialize_control(root: Path) -> tuple[Path, Path]:
    remote = root / "control.git"
    seed = root / "control-seed"
    run(["git", "init", "--bare", str(remote)])
    run(["git", "clone", str(remote), str(seed)])
    git(seed, "config", "user.name", "BDB Local Browser Pilot")
    git(seed, "config", "user.email", "browser-pilot@example.invalid")
    (seed / "README.md").write_text("# Local browser pilot control\n", encoding="utf-8", newline="\n")
    git(seed, "add", "--", "README.md")
    git(seed, "commit", "-m", "initialize local browser pilot control")
    git(seed, "branch", "-M", "main")
    git(seed, "push", "-u", "origin", "main")
    for branch in ("commands", "results"):
        git(seed, "switch", "-C", branch, "main")
        git(seed, "push", "-u", "origin", branch)
    git(seed, "switch", "main")

    control = root / "bridge-control"
    run(["git", "clone", "--branch", "main", str(remote), str(control)])
    git(control, "config", "user.name", "BDB Local Browser Pilot")
    git(control, "config", "user.email", "browser-pilot@example.invalid")
    return remote, control


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def build_bridge_config(
    root: Path,
    fixture: Path,
    control: Path,
    python_executable: str,
) -> Path:
    runtime = root / "runtime"
    runtime.mkdir()
    config_path = root / "bridge-config.json"
    write_json(
        config_path,
        {
            "schema_version": "1.1",
            "control_repo_path": str(control),
            "fixture_repo_path": str(fixture),
            "worktree_root": str(root / "worktrees"),
            "runtime_dir": str(runtime),
            "journal_path": str(runtime / "journal.db"),
            "repository_id": REPOSITORY_ID,
            "allowed_paths": ALLOWED_PATHS,
            "commands_ref": "origin/commands",
            "results_ref": "origin/results",
            "python_executable": python_executable,
            "test_timeout_seconds": 60,
            "heartbeat_interval_seconds": 0.5,
            "heartbeat_stale_seconds": 10,
            "idle_poll_seconds": 5.0,
            "direct_spool_enabled": True,
        },
    )
    return config_path


def write_actions(root: Path, before: bytes, after: bytes) -> tuple[Path, Path]:
    actions = root / "actions"
    actions.mkdir()
    read_path = actions / "01-open-read.json"
    patch_path = actions / "02-multi-file-patch.json"
    write_json(
        read_path,
        {
            "schema": "bdb-action-v1",
            "repo_alias": ALIAS,
            "operation": "open_read",
            "expected_revision": 0,
            "payload": {"path": "src/clamp.py"},
        },
    )
    note = b"# Browser pilot\n\nCreated through the ChatGPT browser extension and Direct Lane.\n"
    write_json(
        patch_path,
        {
            "schema": "bdb-action-v1",
            "repo_alias": ALIAS,
            "operation": "multi_file_patch",
            "expected_revision": 0,
            "payload": {
                "profile_id": "poc_pytest",
                "patch": {
                    "schema": "bdb-multi-file-patch-v1",
                    "operations": [
                        {
                            "schema": "bdb-file-replacement-v1",
                            "kind": "replace_file",
                            "path": "src/clamp.py",
                            "expected_sha256": sha256_value(before),
                            **content_fields(after),
                        },
                        {
                            "schema": "bdb-edit-operation-v1",
                            "kind": "create_file",
                            "path": "PILOT_RESULT.md",
                            **content_fields(note),
                        },
                    ],
                },
            },
        },
    )
    return read_path, patch_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    implementation_root = Path(__file__).resolve().parents[1]
    root = Path(args.root).expanduser().resolve(strict=False)
    python_executable = str(Path(args.python).expanduser().resolve(strict=True))
    ensure_root_is_safe(root, implementation_root)
    root.mkdir(parents=True)

    report_path = root / "browser-pilot-setup.json"
    report: dict[str, Any] = {
        "schema": "bdb-local-browser-pilot-setup-v1",
        "status": "failed",
        "root": str(root),
        "created_at": canonical_time(),
    }
    try:
        fixture, base_sha, before, after = initialize_fixture(root)
        remote, control = initialize_control(root)
        bridge_config = build_bridge_config(root, fixture, control, python_executable)
        read_action, patch_action = write_actions(root, before, after)
        report.update(
            {
                "status": "prepared",
                "repo_alias": ALIAS,
                "repository_id": REPOSITORY_ID,
                "base_sha": base_sha,
                "source_checkout_clean": True,
                "allowed_paths": ALLOWED_PATHS,
                "fixture_repo": str(fixture),
                "control_remote": str(remote),
                "control_checkout": str(control),
                "bridge_config": str(bridge_config),
                "runtime_dir": str(root / "runtime"),
                "extension_directory": str(implementation_root / "browser_extension"),
                "read_action": str(read_action),
                "patch_action": str(patch_action),
                "python_executable": python_executable,
            }
        )
        write_json(report_path, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        write_json(report_path, report)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
