from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ALLOWED_PATHS = ["src/clamp.py", "tests/test_clamp.py", "PILOT_RESULT.md"]


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        shell=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {argv!r}\n"
            f"stdout: {completed.stdout[-4000:]}\n"
            f"stderr: {completed.stderr[-4000:]}"
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


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def resolve_executable(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate.resolve(strict=True))
    discovered = shutil.which(value)
    if not discovered:
        raise RuntimeError(f"Python executable was not found: {value}")
    return str(Path(discovered).resolve(strict=True))


def ensure_outside_repo(root: Path, repo_root: Path) -> None:
    resolved_root = root.resolve(strict=False)
    resolved_repo = repo_root.resolve(strict=False)
    try:
        resolved_root.relative_to(resolved_repo)
    except ValueError:
        pass
    else:
        raise RuntimeError("Pilot root must be outside the bartosz-dev-bridge checkout")
    try:
        resolved_repo.relative_to(resolved_root)
    except ValueError:
        return
    raise RuntimeError("Pilot root cannot contain the bartosz-dev-bridge checkout")


def validate_control_location(value: str, *, prepare_only: bool) -> str:
    local = Path(value).expanduser()
    if local.exists():
        if not prepare_only:
            raise RuntimeError("A local control repository is allowed only with --prepare-only")
        return str(local.resolve(strict=True))

    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise RuntimeError("Control URL must be an HTTPS github.com repository URL")
    if parsed.username or parsed.password:
        raise RuntimeError("Control URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError("Control URL must not contain a query string or fragment")
    if not parsed.path.endswith(".git") or len(parsed.path.strip("/").split("/")) != 2:
        raise RuntimeError("Control URL must have the form https://github.com/<owner>/<repo>.git")
    return value


def remote_heads(control_url: str) -> dict[str, str]:
    completed = run(["git", "ls-remote", "--heads", control_url], timeout=60.0)
    heads: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        sha, separator, ref = line.partition("\t")
        if not separator or not ref.startswith("refs/heads/"):
            continue
        heads[ref.removeprefix("refs/heads/")] = sha
    return heads


def load_status(
    python_executable: str,
    repo_root: Path,
    config_path: Path,
) -> dict[str, Any]:
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
        raise RuntimeError(completed.stderr or completed.stdout)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Bridge status did not return a JSON object")
    return value


def wait_for_running(
    python_executable: str,
    repo_root: Path,
    config_path: Path,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            last = load_status(python_executable, repo_root, config_path)
            if last.get("status") == "RUNNING":
                return last
        except (OSError, RuntimeError, json.JSONDecodeError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for Bridge RUNNING status; last={last!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a persistent Bridge pilot using a private GitHub commands/results repository."
    )
    parser.add_argument("--root", required=True, help="New, non-existing pilot directory")
    parser.add_argument("--control-url", required=True, help="Private GitHub control repository clone URL")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare artifacts without starting Bridge; intended for local CI smoke validation",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    root = Path(args.root).expanduser().resolve(strict=False)
    python_executable = resolve_executable(args.python)
    control_url = validate_control_location(args.control_url, prepare_only=args.prepare_only)

    ensure_outside_repo(root, repo_root)
    if root.exists():
        raise RuntimeError(f"Pilot root already exists; refusing to overwrite: {root}")

    heads = remote_heads(control_url)
    missing = [name for name in ("main", "commands", "results") if name not in heads]
    if missing:
        raise RuntimeError(f"Control repository is missing required branches: {missing}")

    root.mkdir(parents=True)
    report_path = root / "github-pilot-report.json"
    request_path = root / "github-pilot-request.json"
    service_started = False
    report: dict[str, Any] = {
        "schema": "bdb-github-pilot-report-v1",
        "status": "preparing",
        "root": str(root),
        "control_url": control_url,
        "python_executable": python_executable,
        "started_at": canonical_time(datetime.now(timezone.utc)),
        "remote_heads_before": heads,
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
        git(fixture, "config", "user.name", "BDB GitHub Pilot")
        git(fixture, "config", "user.email", "pilot@example.invalid")
        git(fixture, "add", "--", ".")
        git(fixture, "commit", "-m", "github pilot baseline")
        base_sha = git(fixture, "rev-parse", "HEAD").stdout.strip()

        control = root / "bridge-control"
        run(["git", "clone", "--branch", "main", control_url, str(control)], timeout=120.0)
        git(control, "config", "user.name", "BDB GitHub Pilot")
        git(control, "config", "user.email", "pilot@example.invalid")
        git(
            control,
            "fetch",
            "origin",
            "+refs/heads/commands:refs/remotes/origin/commands",
            "+refs/heads/results:refs/remotes/origin/results",
        )
        for ref in ("origin/commands", "origin/results"):
            verified = git(control, "rev-parse", "--verify", ref, timeout=30.0).stdout.strip()
            if not verified:
                raise RuntimeError(f"Remote ref is unavailable after fetch: {ref}")

        for ref in ("origin/commands", "origin/results"):
            paths = git(control, "ls-tree", "-r", "--name-only", ref).stdout.splitlines()
            if any(path == "sessions" or path.startswith("sessions/") for path in paths):
                raise RuntimeError(
                    f"{ref} already contains session data; use a fresh single-use pilot repository"
                )

        worktrees = root / "worktrees"
        runtime = root / "runtime"
        runtime.mkdir()
        journal = runtime / "journal.db"
        config_path = root / "config.json"
        config = {
            "schema_version": "1.1",
            "control_repo_path": str(control),
            "fixture_repo_path": str(fixture),
            "worktree_root": str(worktrees),
            "runtime_dir": str(runtime),
            "journal_path": str(journal),
            "repository_id": "bdb-github-pilot-fixture",
            "allowed_paths": ALLOWED_PATHS,
            "commands_ref": "origin/commands",
            "results_ref": "origin/results",
            "python_executable": python_executable,
            "test_timeout_seconds": 60,
            "heartbeat_interval_seconds": 0.5,
            "heartbeat_stale_seconds": 10,
            "idle_poll_seconds": 1.0,
        }
        config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
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
            f"# GitHub remote pilot\n\n"
            f"Session: `{session_id}`\n\n"
            "This file was created from a command delivered through the private GitHub commands branch.\n"
        ).encode("utf-8")

        manifest = {
            "schema_version": "1.1",
            "session_id": session_id,
            "repository_id": "bdb-github-pilot-fixture",
            "base_sha": base_sha,
            "allowed_paths": ALLOWED_PATHS,
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
        manifest_path = f"sessions/{session_id}/manifest.json"
        command_path = f"sessions/{session_id}/commands/000001.json"
        result_path = f"sessions/{session_id}/results/000001.json"
        request = {
            "schema": "bdb-github-pilot-request-v1",
            "session_id": session_id,
            "command_id": command_id,
            "base_sha": base_sha,
            "created_at": created_at,
            "expires_at": expires_at,
            "expected_state_hash": command["expected_state_hash"],
            "before_sha256": sha256_value(before),
            "manifest_path": manifest_path,
            "command_path": command_path,
            "result_path": result_path,
            "manifest": manifest,
            "command": command,
        }
        request_bytes = canonical_json_bytes(request)
        request_path.write_text(
            json.dumps(request, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if args.prepare_only:
            service_status: dict[str, Any] = {"status": "NOT_STARTED"}
            final_status = "prepared"
        else:
            started = run(
                [
                    python_executable,
                    "-m",
                    "bdb_bridge",
                    "bridge",
                    "start",
                    "--config",
                    str(config_path),
                    "--background",
                ],
                cwd=repo_root,
                check=False,
            )
            if started.returncode != 0:
                raise RuntimeError(
                    "Bridge background start failed:\n"
                    f"stdout: {started.stdout[-4000:]}\n"
                    f"stderr: {started.stderr[-4000:]}"
                )
            service_started = True
            service_status = wait_for_running(
                python_executable,
                repo_root,
                config_path,
                timeout=args.timeout,
            )
            final_status = "ready"

        report.update(
            {
                "status": final_status,
                "finished_at": canonical_time(datetime.now(timezone.utc)),
                "session_id": session_id,
                "command_id": command_id,
                "base_sha": base_sha,
                "config_path": str(config_path),
                "journal_path": str(journal),
                "request_path": str(request_path),
                "request_sha256": sha256_value(request_bytes),
                "manifest_path": manifest_path,
                "command_path": command_path,
                "result_path": result_path,
                "workspace_path": str(worktrees / session_id),
                "bridge_control_path": str(control),
                "fixture_path": str(fixture),
                "service_status": service_status,
            }
        )
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        label = "READY" if not args.prepare_only else "PREPARED"
        print(f"GITHUB REMOTE PILOT: {label}")
        print(f"Root: {root}")
        print(f"Session: {session_id}")
        print(f"Command: {command_id}")
        print(f"Base SHA: {base_sha}")
        print(f"Created: {created_at}")
        print(f"Expires: {expires_at}")
        print(f"Expected state hash: {command['expected_state_hash']}")
        print(f"Before SHA256: {sha256_value(before)}")
        print(f"Manifest path: {manifest_path}")
        print(f"Command path: {command_path}")
        print(f"Result path: {result_path}")
        print(f"Request: {request_path}")
        print(f"Report: {report_path}")
        print(f"Config: {config_path}")
        print(f"Service: {service_status.get('status')}")
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
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if service_started:
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
                timeout=15.0,
            )
        print(f"GITHUB REMOTE PILOT: FAILED: {exc}", file=sys.stderr)
        print(f"Artifacts preserved at: {root}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"GITHUB REMOTE PILOT: FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
