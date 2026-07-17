from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from bdb_bridge.native_host import NATIVE_CONFIG_SCHEMA, default_native_config_path


STATE_SCHEMA = "bdb-workspace-loop-state-v1"


def run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def is_subpath(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    implementation = Path(__file__).resolve().parents[1]
    root = Path(args.root).expanduser().resolve(strict=False)
    source = Path(args.repo).expanduser().resolve(strict=True)
    native_config = Path(args.native_config).expanduser().resolve(strict=True)
    python = Path(args.python).expanduser().resolve(strict=True)

    if root.exists():
        raise RuntimeError(f"Workspace loop root already exists: {root}")
    if not source.joinpath(".git").exists():
        raise RuntimeError(f"Source is not a non-bare Git checkout: {source}")
    if git(source, "status", "--porcelain=v1"):
        raise RuntimeError("Source checkout must be clean during preparation")
    branch = git(source, "symbolic-ref", "-q", "--short", "HEAD")
    if not branch:
        raise RuntimeError("Source checkout must be attached to a local branch")
    if is_subpath(root, source) or is_subpath(root, implementation):
        raise RuntimeError("Workspace loop root must stay outside source and implementation checkouts")
    if not args.allowed_path:
        raise RuntimeError("At least one --allowed-path is required")

    root.mkdir(parents=True)
    control_remote = root / "control.git"
    control_seed = root / "control-seed"
    control = root / "bridge-control"
    runtime = root / "runtime"
    worktrees = root / "worktrees"
    bridge_config = root / "bridge-config.json"
    state_path = root / "workspace-loop-state.json"

    run(["git", "init", "--bare", str(control_remote)])
    run(["git", "clone", str(control_remote), str(control_seed)])
    git(control_seed, "config", "core.autocrlf", "false")
    git(control_seed, "config", "user.name", "Bartosz Dev Bridge")
    git(control_seed, "config", "user.email", "bdb@localhost.invalid")
    control_seed.joinpath("README.md").write_text(
        "# Bartosz Dev Bridge local workspace control\n",
        encoding="utf-8",
        newline="\n",
    )
    git(control_seed, "add", "--", "README.md")
    git(control_seed, "commit", "-m", "initialize local workspace control")
    git(control_seed, "branch", "-M", "main")
    git(control_seed, "push", "-u", "origin", "main")
    for name in ("commands", "results"):
        git(control_seed, "switch", "-C", name, "main")
        git(control_seed, "push", "-u", "origin", name)
    git(control_seed, "switch", "main")
    run(["git", "clone", "--branch", "main", str(control_remote), str(control)])
    runtime.mkdir()

    config_document = {
        "schema_version": "1.1",
        "control_repo_path": str(control),
        "fixture_repo_path": str(source),
        "worktree_root": str(worktrees),
        "runtime_dir": str(runtime),
        "journal_path": str(runtime / "journal.db"),
        "repository_id": f"bdb-workspace-{args.alias}",
        "allowed_paths": list(args.allowed_path),
        "commands_ref": "origin/commands",
        "results_ref": "origin/results",
        "python_executable": str(python),
        "test_timeout_seconds": args.test_timeout,
        "heartbeat_interval_seconds": 0.5,
        "heartbeat_stale_seconds": 10.0,
        "idle_poll_seconds": 5.0,
        "direct_spool_enabled": True,
    }
    atomic_json(bridge_config, config_document)

    native_document = json.loads(native_config.read_text(encoding="utf-8-sig"))
    if not isinstance(native_document, dict) or native_document.get("schema") != NATIVE_CONFIG_SCHEMA:
        raise RuntimeError("Native Host config schema is unsupported")
    repositories = native_document.get("repositories")
    if not isinstance(repositories, dict):
        raise RuntimeError("Native Host config has no repositories object")
    candidate = {"bridge_config_path": str(bridge_config)}
    existing = repositories.get(args.alias)
    if existing is not None and existing != candidate:
        raise RuntimeError(f"Native Host alias already points somewhere else: {args.alias}")
    backup = native_config.with_name(
        native_config.name + ".pre-workspace-loop." + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".bak"
    )
    shutil.copy2(native_config, backup)
    repositories[args.alias] = candidate
    atomic_json(native_config, native_document)

    state = {
        "schema": STATE_SCHEMA,
        "status": "prepared",
        "alias": args.alias,
        "source_repo": str(source),
        "source_branch": branch,
        "source_head": git(source, "rev-parse", "HEAD"),
        "root": str(root),
        "bridge_config": str(bridge_config),
        "native_config": str(native_config),
        "native_config_backup": str(backup),
        "python_executable": str(python),
        "promoter_script": str(implementation / "scripts" / "run_workspace_promoter.py"),
        "promoter_pid_file": str(runtime / "workspace-promoter.pid"),
        "promoter_stop_file": str(runtime / "workspace-promoter.stop"),
        "promoter_stdout": str(runtime / "workspace-promoter.stdout.log"),
        "promoter_stderr": str(runtime / "workspace-promoter.stderr.log"),
        "allowed_paths": list(args.allowed_path),
        "prepared_at": time.time(),
    }
    atomic_json(state_path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a Bartosz Dev Bridge local workspace loop")
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--allowed-path", action="append", default=[])
    parser.add_argument("--native-config", default=str(default_native_config_path()))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--test-timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 1.0 <= args.test_timeout <= 3_600.0:
        parser.error("--test-timeout must be between 1 and 3600 seconds")
    result = prepare(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
