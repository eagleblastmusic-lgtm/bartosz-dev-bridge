from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120.0,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=text,
        check=False,
        timeout=timeout,
        shell=False,
    )
    if check and completed.returncode != 0:
        stdout = completed.stdout if text else completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {argv!r}\n"
            f"stdout: {stdout[-4000:]}\n"
            f"stderr: {stderr[-4000:]}"
        )
    return completed


def git(repo: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo), *args], timeout=timeout)


def canonical_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_value(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_value(content),
    }


def clean_workspace_state_hash(base_sha: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"bdb-poc-state-v1\0")
    digest.update(base_sha.encode("ascii"))
    digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def load_json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Expected JSON output but received:\nstdout: {completed.stdout[-4000:]}\n"
            f"stderr: {completed.stderr[-4000:]}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("Expected a JSON object")
    return value


def wait_until(
    description: str,
    probe,
    predicate,
    *,
    timeout: float,
    interval: float = 0.25,
    process: subprocess.Popen[str] | None = None,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Bridge exited before {description}; code={process.returncode}")
        try:
            last = probe()
            if predicate(last):
                return last
        except (OSError, RuntimeError, json.JSONDecodeError):
            pass
        time.sleep(interval)
    raise RuntimeError(f"Timed out waiting for {description}; last={last!r}")


def ensure_outside_repo(root: Path, repo_root: Path) -> None:
    root = root.resolve(strict=False)
    repo_root = repo_root.resolve(strict=False)
    try:
        root.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise RuntimeError("Pilot root must be outside the bartosz-dev-bridge checkout")
    try:
        repo_root.relative_to(root)
    except ValueError:
        return
    raise RuntimeError("Pilot root cannot contain the bartosz-dev-bridge checkout")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a persistent Bartosz Dev Bridge operator pilot."
    )
    parser.add_argument("--root", required=True, help="New, non-existing pilot directory")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used to run the Bridge and poc_pytest profile",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    root = Path(args.root).expanduser().resolve(strict=False)
    python_executable = str(Path(args.python).expanduser().resolve(strict=True))
    ensure_outside_repo(root, repo_root)
    if root.exists():
        raise RuntimeError(f"Pilot root already exists; refusing to overwrite: {root}")

    root.mkdir(parents=True)
    report_path = root / "pilot-report.json"
    bridge_stdout_path = root / "bridge.stdout.log"
    bridge_stderr_path = root / "bridge.stderr.log"
    service: subprocess.Popen[str] | None = None
    stdout_handle = None
    stderr_handle = None
    report: dict[str, Any] = {
        "schema": "bdb-persistent-pilot-report-v1",
        "status": "running",
        "root": str(root),
        "python_executable": python_executable,
        "started_at": canonical_time(datetime.now(timezone.utc)),
    }

    try:
        fixture = root / "fixture"
        shutil.copytree(
            repo_root / "bdb-poc-fixture",
            fixture,
            ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", "*.pyc"),
        )
        git(fixture, "init", "-b", "main")
        git(fixture, "config", "core.autocrlf", "false")
        git(fixture, "config", "user.name", "BDB Pilot")
        git(fixture, "config", "user.email", "pilot@example.invalid")
        git(fixture, "add", "--", ".")
        git(fixture, "commit", "-m", "pilot baseline")
        base_sha = git(fixture, "rev-parse", "HEAD").stdout.strip()

        control_remote = root / "control.git"
        run(["git", "init", "--bare", str(control_remote)])
        writer = root / "writer"
        run(["git", "clone", str(control_remote), str(writer)])
        git(writer, "config", "user.name", "BDB Pilot")
        git(writer, "config", "user.email", "pilot@example.invalid")
        (writer / "README.md").write_text(
            "# Bartosz Dev Bridge persistent pilot control\n", encoding="utf-8"
        )
        git(writer, "add", "--", "README.md")
        git(writer, "commit", "-m", "initialize pilot control")
        git(writer, "branch", "-M", "main")
        git(writer, "push", "-u", "origin", "main")
        for branch in ("commands", "results"):
            git(writer, "switch", "-C", branch, "main")
            git(writer, "push", "-u", "origin", branch)
        git(writer, "switch", "commands")

        control = root / "bridge-control"
        run(["git", "clone", "--branch", "main", str(control_remote), str(control)])
        git(control, "config", "user.name", "BDB Pilot")
        git(control, "config", "user.email", "pilot@example.invalid")

        worktrees = root / "worktrees"
        runtime = root / "runtime"
        runtime.mkdir()
        journal = runtime / "journal.db"
        config_path = root / "config.json"
        allowed_paths = ["src/clamp.py", "tests/test_clamp.py", "PILOT_RESULT.md"]
        config = {
            "schema_version": "1.1",
            "control_repo_path": str(control),
            "fixture_repo_path": str(fixture),
            "worktree_root": str(worktrees),
            "runtime_dir": str(runtime),
            "journal_path": str(journal),
            "repository_id": "bdb-persistent-pilot",
            "allowed_paths": allowed_paths,
            "commands_ref": "origin/commands",
            "results_ref": "origin/results",
            "python_executable": python_executable,
            "test_timeout_seconds": 60,
            "heartbeat_interval_seconds": 0.2,
            "heartbeat_stale_seconds": 5,
            "idle_poll_seconds": 0.2,
        }
        config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        stdout_handle = bridge_stdout_path.open("w", encoding="utf-8")
        stderr_handle = bridge_stderr_path.open("w", encoding="utf-8")
        service = subprocess.Popen(
            [
                python_executable,
                "-m",
                "bdb_bridge",
                "bridge",
                "start",
                "--config",
                str(config_path),
                "--foreground",
            ],
            cwd=repo_root,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        def service_status() -> dict[str, Any]:
            completed = run(
                [
                    python_executable,
                    "-m",
                    "bdb_bridge",
                    "bridge",
                    "status",
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=repo_root,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr)
            return load_json_output(completed)

        running_status = wait_until(
            "service RUNNING",
            service_status,
            lambda value: value.get("status") == "RUNNING",
            timeout=args.timeout,
            process=service,
        )

        session_id = str(uuid.uuid4())
        command_id = f"{session_id}:000001"
        now = datetime.now(timezone.utc)
        created_at = canonical_time(now)
        expires_at = canonical_time(now + timedelta(days=1))
        source_path = fixture / "src" / "clamp.py"
        before = source_path.read_bytes()
        after = (
            b"def clamp_percent(value: int) -> int:\n"
            b"    return max(0, min(value, 100))\n"
        )
        pilot_note = (
            f"# Persistent pilot\n\n"
            f"Session: `{session_id}`\n\n"
            "This file was created by the final `multi_file_patch` gate.\n"
        ).encode("utf-8")

        manifest = {
            "schema_version": "1.1",
            "session_id": session_id,
            "repository_id": "bdb-persistent-pilot",
            "base_sha": base_sha,
            "allowed_paths": allowed_paths,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        command = {
            "schema_version": "1.1",
            "session_id": session_id,
            "command_id": command_id,
            "sequence": 1,
            "created_at": created_at,
            "expires_at": expires_at,
            "operation": "multi_file_patch",
            "expected_revision": 0,
            "expected_state_hash": clean_workspace_state_hash(base_sha),
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
                            **content_fields(pilot_note),
                        },
                    ],
                },
            },
        }
        session_root = writer / "sessions" / session_id
        (session_root / "commands").mkdir(parents=True)
        (session_root / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        (session_root / "commands" / "000001.json").write_text(
            json.dumps(command, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        git(writer, "add", "--", f"sessions/{session_id}")
        git(writer, "commit", "-m", f"pilot command {session_id}")
        git(writer, "push", "origin", "commands")

        def edit_status() -> dict[str, Any]:
            completed = run(
                [
                    python_executable,
                    "-m",
                    "bdb_bridge",
                    "bridge",
                    "edit",
                    "status",
                    "--config",
                    str(config_path),
                    "--command-id",
                    command_id,
                    "--json",
                ],
                cwd=repo_root,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr)
            return load_json_output(completed)

        final_status = wait_until(
            "published successful command",
            edit_status,
            lambda value: (
                value.get("command_state") == "result_published"
                and value.get("checkpoint_state") == "committed"
                and value.get("profile_status") == "success"
                and value.get("result_status") == "success"
            ),
            timeout=args.timeout,
            process=service,
        )

        stopped = run(
            [
                python_executable,
                "-m",
                "bdb_bridge",
                "bridge",
                "stop",
                "--config",
                str(config_path),
            ],
            cwd=repo_root,
            check=False,
        )
        if stopped.returncode != 0:
            raise RuntimeError(f"Graceful stop failed: {stopped.stderr}")
        service.wait(timeout=30)
        if service.returncode != 0:
            raise RuntimeError(f"Bridge exited with code {service.returncode}")
        service = None
        stdout_handle.close()
        stderr_handle.close()
        stdout_handle = None
        stderr_handle = None

        workspace = worktrees / session_id
        if (workspace / "src" / "clamp.py").read_bytes() != after:
            raise RuntimeError("Workspace clamp.py does not match expected AFTER bytes")
        if (workspace / "PILOT_RESULT.md").read_bytes() != pilot_note:
            raise RuntimeError("Workspace PILOT_RESULT.md does not match expected bytes")

        git(writer, "fetch", "origin", "results")
        result_path = f"sessions/{session_id}/results/000001.json"
        remote_result = run(
            [
                "git",
                "--git-dir",
                str(control_remote),
                "show",
                f"results:{result_path}",
            ]
        ).stdout
        remote_document = json.loads(remote_result)
        if remote_document.get("status") != "success":
            raise RuntimeError("Published result is not successful")

        report.update(
            {
                "status": "pass",
                "finished_at": canonical_time(datetime.now(timezone.utc)),
                "session_id": session_id,
                "command_id": command_id,
                "base_sha": base_sha,
                "config_path": str(config_path),
                "journal_path": str(journal),
                "workspace_path": str(workspace),
                "control_remote_path": str(control_remote),
                "writer_path": str(writer),
                "bridge_control_path": str(control),
                "bridge_stdout_path": str(bridge_stdout_path),
                "bridge_stderr_path": str(bridge_stderr_path),
                "service_status": running_status,
                "edit_status": final_status,
                "published_result_path": result_path,
                "published_result": remote_document,
            }
        )
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print("PERSISTENT PILOT: PASS")
        print(f"Root: {root}")
        print(f"Session: {session_id}")
        print(f"Command: {command_id}")
        print(f"Workspace: {workspace}")
        print(f"Report: {report_path}")
        print("Artifacts were preserved. No cleanup was performed.")
        return 0
    except Exception as exc:
        report.update(
            {
                "status": "failed",
                "finished_at": canonical_time(datetime.now(timezone.utc)),
                "error_type": type(exc).__name__,
                "error": str(exc)[:8000],
            }
        )
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"PERSISTENT PILOT: FAILED: {exc}", file=sys.stderr)
        print(f"Artifacts preserved at: {root}", file=sys.stderr)
        return 1
    finally:
        if service is not None and service.poll() is None:
            try:
                run(
                    [
                        python_executable,
                        "-m",
                        "bdb_bridge",
                        "bridge",
                        "stop",
                        "--config",
                        str(root / "config.json"),
                    ],
                    cwd=repo_root,
                    check=False,
                    timeout=15,
                )
                service.wait(timeout=20)
            except Exception:
                pass
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
