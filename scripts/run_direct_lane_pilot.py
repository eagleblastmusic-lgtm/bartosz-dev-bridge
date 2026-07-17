from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bdb_bridge.native_messaging import encode_native_message, read_native_message


ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
ALIAS = "synthetic"


def canonical_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_value(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_encoding": "utf-8",
        "content": content.decode("utf-8"),
        "content_sha256": sha256_value(content),
    }


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        input=input_bytes,
        shell=False,
        capture_output=True,
        text=input_bytes is None,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace") if isinstance(completed.stderr, bytes) else completed.stderr
        stdout = completed.stdout.decode("utf-8", errors="replace") if isinstance(completed.stdout, bytes) else completed.stdout
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return completed


def git(repo: Path, *args: str) -> str:
    return str(run(["git", "-C", str(repo), *args]).stdout).strip()


def load_json_output(completed: subprocess.CompletedProcess[Any]) -> dict[str, Any]:
    stdout = completed.stdout.decode("utf-8", errors="strict") if isinstance(completed.stdout, bytes) else completed.stdout
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Expected JSON object output")
    return value


def wait_until(
    description: str,
    producer: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float,
    process: subprocess.Popen[Any] | None = None,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Bridge exited while waiting for {description}: {process.returncode}")
        try:
            last = producer()
            if predicate(last):
                return last
        except Exception as exc:
            last = exc
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {description}; last={last!r}")


def initialize_fixture(root: Path) -> tuple[Path, str, bytes, bytes]:
    fixture = root / "fixture"
    fixture.mkdir()
    git(fixture, "init")
    git(fixture, "config", "user.name", "BDB Direct Pilot")
    git(fixture, "config", "user.email", "direct-pilot@example.invalid")
    (fixture / "src").mkdir()
    (fixture / "tests").mkdir()
    before = (
        b"def clamp_percent(value: int) -> int:\n"
        b"    return value\n"
    )
    after = (
        b"def clamp_percent(value: int) -> int:\n"
        b"    return max(0, min(value, 100))\n"
    )
    (fixture / "src" / "clamp.py").write_bytes(before)
    (fixture / "tests" / "test_clamp.py").write_text(
        "from src.clamp import clamp_percent\n\n"
        "def test_clamp_percent() -> None:\n"
        "    assert clamp_percent(-1) == 0\n"
        "    assert clamp_percent(50) == 50\n"
        "    assert clamp_percent(120) == 100\n",
        encoding="utf-8",
    )
    git(fixture, "add", "--", "src/clamp.py", "tests/test_clamp.py")
    git(fixture, "commit", "-m", "initialize direct pilot fixture")
    return fixture, git(fixture, "rev-parse", "HEAD"), before, after


def initialize_control(root: Path) -> tuple[Path, Path]:
    remote = root / "control.git"
    seed = root / "control-seed"
    run(["git", "init", "--bare", str(remote)])
    run(["git", "clone", str(remote), str(seed)])
    git(seed, "config", "user.name", "BDB Direct Pilot")
    git(seed, "config", "user.email", "direct-pilot@example.invalid")
    (seed / "README.md").write_text("# Direct Lane pilot control\n", encoding="utf-8")
    git(seed, "add", "--", "README.md")
    git(seed, "commit", "-m", "initialize direct pilot control")
    git(seed, "branch", "-M", "main")
    git(seed, "push", "-u", "origin", "main")
    for branch in ("commands", "results"):
        git(seed, "switch", "-C", branch, "main")
        git(seed, "push", "-u", "origin", branch)
    git(seed, "switch", "main")
    control = root / "bridge-control"
    run(["git", "clone", "--branch", "main", str(remote), str(control)])
    git(control, "config", "user.name", "BDB Direct Pilot")
    git(control, "config", "user.email", "direct-pilot@example.invalid")
    return remote, control


def build_configs(
    root: Path,
    fixture: Path,
    control: Path,
    python_executable: str,
) -> tuple[Path, Path]:
    runtime = root / "runtime"
    runtime.mkdir()
    bridge_config_path = root / "bridge-config.json"
    bridge_config = {
        "schema_version": "1.1",
        "control_repo_path": str(control),
        "fixture_repo_path": str(fixture),
        "worktree_root": str(root / "worktrees"),
        "runtime_dir": str(runtime),
        "journal_path": str(runtime / "journal.db"),
        "repository_id": "bdb-direct-lane-pilot",
        "allowed_paths": ["src/clamp.py", "tests/test_clamp.py", "PILOT_RESULT.md"],
        "commands_ref": "origin/commands",
        "results_ref": "origin/results",
        "python_executable": python_executable,
        "test_timeout_seconds": 60,
        "heartbeat_interval_seconds": 0.2,
        "heartbeat_stale_seconds": 5,
        "idle_poll_seconds": 5.0,
        "direct_spool_enabled": True,
    }
    bridge_config_path.write_text(
        json.dumps(bridge_config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    native_config_path = root / "native-host.json"
    native_config = {
        "schema": "bdb-native-host-config-v1",
        "repositories": {
            ALIAS: {"bridge_config_path": str(bridge_config_path)},
        },
        "allowed_origins": [ORIGIN],
        "state_path": str(root / "native-host-arm.json"),
        "session_store_path": str(root / "native-host-sessions.json"),
        "max_wait_seconds": 60,
        "max_message_bytes": 1048576,
    }
    native_config_path.write_text(
        json.dumps(native_config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bridge_config_path, native_config_path


def service_json(
    python_executable: str,
    repo_root: Path,
    bridge_config: Path,
    *command: str,
) -> dict[str, Any]:
    completed = run(
        [python_executable, "-m", "bdb_bridge", "bridge", *command, "--config", str(bridge_config), "--json"],
        cwd=repo_root,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(str(completed.stderr))
    return load_json_output(completed)


def parse_native_response(output: bytes) -> dict[str, Any]:
    stream = io.BytesIO(output)
    response = read_native_message(stream)
    if response is None:
        raise RuntimeError("Native Host returned no framed response")
    if stream.read(1) != b"":
        raise RuntimeError("Native Host returned unexpected trailing bytes")
    return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    root = Path(args.root).expanduser().resolve(strict=False)
    python_executable = str(Path(args.python).expanduser().resolve(strict=True))
    if root.exists():
        raise RuntimeError(f"Pilot root already exists: {root}")
    try:
        root.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise RuntimeError("Pilot root must stay outside the implementation checkout")
    root.mkdir(parents=True)

    report_path = root / "direct-lane-report.json"
    stdout_path = root / "bridge.stdout.log"
    stderr_path = root / "bridge.stderr.log"
    report: dict[str, Any] = {
        "schema": "bdb-direct-lane-pilot-report-v1",
        "status": "failed",
        "root": str(root),
        "started_at": canonical_time(datetime.now(timezone.utc)),
    }
    service: subprocess.Popen[Any] | None = None
    stdout_handle = None
    stderr_handle = None
    remote: Path | None = None
    offline_remote: Path | None = None

    try:
        fixture, base_sha, before, after = initialize_fixture(root)
        remote, control = initialize_control(root)
        bridge_config, native_config = build_configs(root, fixture, control, python_executable)

        arm = run(
            [
                python_executable,
                "-m",
                "bdb_bridge",
                "bridge",
                "native-host",
                "arm",
                "--config",
                str(native_config),
                "--minutes",
                "10",
            ],
            cwd=repo_root,
        )
        report["arm"] = load_json_output(arm)

        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        service = subprocess.Popen(
            [
                python_executable,
                "-m",
                "bdb_bridge",
                "bridge",
                "start",
                "--config",
                str(bridge_config),
                "--foreground",
            ],
            cwd=repo_root,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        running = wait_until(
            "Bridge RUNNING",
            lambda: service_json(python_executable, repo_root, bridge_config, "status"),
            lambda value: value.get("status") == "RUNNING",
            timeout=args.timeout,
            process=service,
        )
        report["running"] = running

        offline_remote = remote.with_name("control.offline.git")
        remote.rename(offline_remote)
        report["git_transport_offline_at"] = canonical_time(datetime.now(timezone.utc))

        pilot_note = (
            "# Direct Lane pilot\n\n"
            "This file was created through Native Messaging and Direct Spool while the Git transport was offline.\n"
        ).encode("utf-8")
        action = {
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
                            **content_fields(pilot_note),
                        },
                    ],
                },
            },
        }
        request = {
            "schema": "bdb-native-request-v1",
            "request_id": "direct-pilot-1",
            "action": "submit_action",
            "wait_seconds": min(60.0, args.timeout),
            "bdb_action": action,
        }
        native_started = time.perf_counter()
        native = run(
            [
                python_executable,
                "-m",
                "bdb_bridge.native_host",
                ORIGIN,
                "--config",
                str(native_config),
            ],
            cwd=repo_root,
            input_bytes=encode_native_message(request),
        )
        native_elapsed_ms = (time.perf_counter() - native_started) * 1000.0
        response = parse_native_response(bytes(native.stdout))
        if response.get("status") != "completed":
            raise RuntimeError(f"Native action did not complete locally: {response}")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("status") != "success":
            raise RuntimeError(f"Direct Lane result was not successful: {response}")
        command_id = str(response["command_id"])
        session_id = command_id.split(":", 1)[0]
        report["native_response"] = response
        report["native_round_trip_ms"] = native_elapsed_ms
        report["local_result_before_git_restore"] = True
        report["local_result_at"] = canonical_time(datetime.now(timezone.utc))

        staged = service_json(
            python_executable,
            repo_root,
            bridge_config,
            "edit",
            "status",
            "--command-id",
            command_id,
        )
        if staged.get("profile_status") != "success" or staged.get("result_status") != "success":
            raise RuntimeError(f"Local staged status is not successful: {staged}")
        if staged.get("command_state") not in {"result_staged", "result_published"}:
            raise RuntimeError(f"Unexpected pre-restore command state: {staged}")
        report["before_git_restore"] = staged

        offline_remote.rename(remote)
        offline_remote = None
        report["git_transport_restored_at"] = canonical_time(datetime.now(timezone.utc))

        published = wait_until(
            "Git fallback publication",
            lambda: service_json(
                python_executable,
                repo_root,
                bridge_config,
                "edit",
                "status",
                "--command-id",
                command_id,
            ),
            lambda value: value.get("command_state") == "result_published",
            timeout=args.timeout,
            process=service,
        )
        report["after_git_restore"] = published

        stop = run(
            [
                python_executable,
                "-m",
                "bdb_bridge",
                "bridge",
                "stop",
                "--config",
                str(bridge_config),
            ],
            cwd=repo_root,
        )
        report["stop"] = load_json_output(stop)
        service.wait(timeout=args.timeout)
        if service.returncode != 0:
            raise RuntimeError(f"Bridge exited with code {service.returncode}")

        workspace = Path(str(published["workspace_path"]))
        if (workspace / "src" / "clamp.py").read_bytes() != after:
            raise RuntimeError("Workspace patch bytes do not match expected bytes")
        if not (workspace / "PILOT_RESULT.md").exists():
            raise RuntimeError("Workspace pilot result file is missing")
        if (fixture / "src" / "clamp.py").read_bytes() != before:
            raise RuntimeError("Source checkout was mutated")
        if git(fixture, "status", "--porcelain=v1"):
            raise RuntimeError("Source checkout is dirty after the pilot")

        report.update(
            {
                "status": "success",
                "base_sha": base_sha,
                "session_id": session_id,
                "command_id": command_id,
                "workspace": str(workspace),
                "source_checkout_clean": True,
                "git_fallback_published_without_reexecution": True,
                "completed_at": canonical_time(datetime.now(timezone.utc)),
                "artifacts": {
                    "bridge_config": str(bridge_config),
                    "native_config": str(native_config),
                    "journal": str(root / "runtime" / "journal.db"),
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                },
            }
        )
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["completed_at"] = canonical_time(datetime.now(timezone.utc))
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise
    finally:
        if offline_remote is not None and offline_remote.exists() and remote is not None and not remote.exists():
            offline_remote.rename(remote)
        if service is not None and service.poll() is None:
            service.terminate()
            try:
                service.wait(timeout=10)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=10)
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
