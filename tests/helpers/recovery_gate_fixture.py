from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CREATED_AT = "2026-07-15T21:00:00Z"
EXPIRES_AT = "2099-01-01T00:00:00Z"
FAULT_CASES = (
    ("A", "AFTER_DISCOVERED_BEFORE_VALIDATION", "discovered"),
    ("B", "AFTER_EXECUTE_CLAIM", "claimed"),
    ("C", "AFTER_TEMP_WRITE_BEFORE_REPLACE", "executing"),
    ("D", "AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT", "executing"),
    ("E", "AFTER_EFFECT_COMMIT_BEFORE_PROFILE", "effect_recorded"),
    ("F", "AFTER_STAGE_COMMIT_BEFORE_PUBLISH", "result_staged"),
    ("G", "AFTER_REMOTE_PUSH_BEFORE_LOCAL_ACK", "result_staged"),
)


@dataclass(frozen=True)
class GateEnvironment:
    root: Path
    session_id: str
    command_id: str
    fixture: Path
    worktrees: Path
    control_remote: Path
    writer: Path
    control: Path
    runtime: Path
    journal: Path
    config: Path
    base_sha: str


def _run(
    argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
    timeout: float = 60.0, check: bool = True, text: bool = True,
):
    completed = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=text,
        check=False, timeout=timeout, shell=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"command failed {argv!r}: {completed.stderr!r}")
    return completed


def git(repo: Path, *args: str, text: bool = True):
    return _run(["git", "-C", str(repo), *args], text=text)


_TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".json", ".gitignore"}


def _normalize_copied_fixture_to_lf(root: Path) -> None:
    """Keep exact replace_exact payloads stable under Windows core.autocrlf=true."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        data = path.read_bytes()
        if b"\r\n" in data or b"\r" in data:
            path.write_bytes(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def setup_gate_environment(root: Path, *, session_id: str | None = None) -> GateEnvironment:
    root.mkdir(parents=True, exist_ok=True)
    session_id = session_id or str(uuid.uuid4())
    command_id = f"{session_id}:000001"

    fixture = root / "fixture"
    shutil.copytree(
        Path(__file__).parents[2] / "bdb-poc-fixture",
        fixture,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", "*.pyc"),
    )
    _normalize_copied_fixture_to_lf(fixture)
    git(fixture, "init", "-b", "main")
    # Worktree checkouts inherit this local config; keep bytes LF for exact replace.
    git(fixture, "config", "core.autocrlf", "false")
    git(fixture, "config", "user.name", "GHB0 Gate")
    git(fixture, "config", "user.email", "gate@example.invalid")
    git(fixture, "add", "--", ".gitattributes", ".gitignore", "pyproject.toml", "src", "tests")
    git(fixture, "commit", "-m", "baseline")
    base_sha = git(fixture, "rev-parse", "HEAD").stdout.strip()

    remote = root / "control.git"
    _run(["git", "init", "--bare", str(remote)])
    writer = root / "writer"
    _run(["git", "clone", str(remote), str(writer)])
    git(writer, "config", "user.name", "GHB0 Gate")
    git(writer, "config", "user.email", "gate@example.invalid")
    (writer / "README.md").write_text("# synthetic control\n", encoding="utf-8")
    git(writer, "add", "README.md")
    git(writer, "commit", "-m", "initial")
    git(writer, "branch", "-M", "main")
    git(writer, "push", "-u", "origin", "main")
    for branch in ("commands", "results"):
        git(writer, "checkout", "-B", branch, "main")
        git(writer, "push", "-u", "origin", branch)
    git(writer, "checkout", "commands")

    control = root / "bridge-control"
    _run(["git", "clone", "--branch", "main", str(remote), str(control)])
    git(control, "config", "user.name", "GHB0 Gate")
    git(control, "config", "user.email", "gate@example.invalid")

    manifest = {
        "schema_version": "1.1", "session_id": session_id,
        "repository_id": "bdb-poc-fixture", "base_sha": base_sha,
        "allowed_paths": ["src/clamp.py", "tests/test_clamp.py"],
        "created_at": CREATED_AT, "expires_at": EXPIRES_AT,
    }
    command = {
        "schema_version": "1.1", "session_id": session_id,
        "command_id": command_id, "sequence": 1,
        "operation": "replace_exact_and_test", "created_at": CREATED_AT,
        "expires_at": EXPIRES_AT, "expected_revision": 0,
        "expected_state_hash": None,
        "payload": {
            "path": "src/clamp.py", "old": "    return value\n",
            "new": "    return max(0, min(value, 100))\n",
            "profile_id": "poc_pytest",
        },
    }
    session_root = writer / "sessions" / session_id
    (session_root / "commands").mkdir(parents=True)
    (session_root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    (session_root / "commands" / "000001.json").write_text(
        json.dumps(command, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    git(writer, "add", "--", "sessions")
    git(writer, "commit", "-m", f"command {session_id}")
    git(writer, "push", "origin", "commands")

    worktrees = root / "worktrees"
    runtime = root / "runtime"
    runtime.mkdir()
    journal = runtime / "journal.db"
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "1.1", "control_repo_path": str(control),
                "fixture_repo_path": str(fixture), "worktree_root": str(worktrees),
                "runtime_dir": str(runtime), "journal_path": str(journal),
                "repository_id": "bdb-poc-fixture",
                "allowed_paths": ["src/clamp.py", "tests/test_clamp.py"],
                "commands_ref": "origin/commands", "results_ref": "origin/results",
                "python_executable": sys.executable, "test_timeout_seconds": 30,
                "heartbeat_interval_seconds": 0.05, "heartbeat_stale_seconds": 2,
                "idle_poll_seconds": 0.05,
            },
            sort_keys=True, separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return GateEnvironment(
        root, session_id, command_id, fixture, worktrees, remote, writer,
        control, runtime, journal, config, base_sha,
    )


def run_cli(env: GateEnvironment, *args: str, fault: str | None = None, timeout: float = 90.0):
    process_env = os.environ.copy()
    process_env["PYTHONUNBUFFERED"] = "1"
    if fault:
        process_env["BDB_FAULT_POINT"] = fault
    else:
        process_env.pop("BDB_FAULT_POINT", None)
    return _run(
        [sys.executable, "-m", "bdb_bridge", "bridge", *args, "--config", str(env.config)],
        cwd=Path(__file__).parents[2], env=process_env, timeout=timeout, check=False,
    )


def start_service(env: GateEnvironment, *, fault: str | None = None) -> subprocess.Popen[str]:
    process_env = os.environ.copy()
    process_env["PYTHONUNBUFFERED"] = "1"
    if fault:
        process_env["BDB_FAULT_POINT"] = fault
    else:
        process_env.pop("BDB_FAULT_POINT", None)
    return subprocess.Popen(
        [sys.executable, "-m", "bdb_bridge", "bridge", "start", "--config", str(env.config), "--foreground"],
        cwd=Path(__file__).parents[2], env=process_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def query(path: Path, sql: str, params: tuple[Any, ...] = ()):
    conn = sqlite3.connect(path, timeout=1.0)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def wait_for(
    path: Path, sql: str, expected: Any, params: tuple[Any, ...] = (), *,
    timeout: float = 60.0, process: subprocess.Popen[str] | None = None,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        if path.exists():
            try:
                rows = query(path, sql, params)
                last = rows[0][0] if rows else None
                if last == expected:
                    return last
            except sqlite3.Error:
                pass
        if process is not None and process.poll() is not None:
            out, err = process.communicate(timeout=5)
            raise AssertionError(
                f"service exited before {expected!r}; code={process.returncode}; stdout={out!r}; stderr={err!r}"
            )
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {expected!r}; last={last!r}")


def stop_and_wait(env: GateEnvironment, process: subprocess.Popen[str]) -> tuple[str, str]:
    stopped = run_cli(env, "stop")
    if stopped.returncode != 0:
        raise AssertionError(f"graceful stop failed: {stopped.stderr!r}")
    out, err = process.communicate(timeout=30)
    if process.returncode != 0:
        raise AssertionError(f"foreground stop failed: {process.returncode}, {out!r}, {err!r}")
    return out, err


def remote_bytes(env: GateEnvironment, remote_path: str) -> bytes:
    git(env.writer, "fetch", "origin", "results")
    return _run(
        ["git", "-C", str(env.writer), "show", f"origin/results:{remote_path}"],
        text=False,
    ).stdout


def remote_commit_count(env: GateEnvironment) -> int:
    git(env.writer, "fetch", "origin", "main", "results")
    return int(git(env.writer, "rev-list", "--count", "origin/main..origin/results").stdout.strip())


def snapshot_counts(env: GateEnvironment) -> dict[str, int]:
    conn = sqlite3.connect(env.journal, timeout=2.0)
    try:
        return {
            "plan": conn.execute("SELECT COUNT(*) FROM operation_plans WHERE command_id=?", (env.command_id,)).fetchone()[0],
            "effect": conn.execute("SELECT COUNT(*) FROM operation_effects WHERE command_id=?", (env.command_id,)).fetchone()[0],
            "result": conn.execute("SELECT COUNT(*) FROM results WHERE command_id=?", (env.command_id,)).fetchone()[0],
            "outbox": conn.execute("SELECT COUNT(*) FROM outbox WHERE command_id=?", (env.command_id,)).fetchone()[0],
            "claim": conn.execute("SELECT COUNT(*) FROM events WHERE command_id=? AND event_type='command.claimed'", (env.command_id,)).fetchone()[0],
            "attempt": conn.execute("SELECT attempt_count FROM outbox WHERE command_id=?", (env.command_id,)).fetchone()[0],
        }
    finally:
        conn.close()


def run_recovery_case(root: Path, case: str, fault: str, durable_state: str) -> dict[str, Any]:
    env = setup_gate_environment(root)
    crashed = run_cli(env, "start", "--foreground", fault=fault)
    if crashed.returncode != 2:
        raise AssertionError(
            f"fault process did not exit 2: {case} {crashed.returncode} {crashed.stdout!r} {crashed.stderr!r}"
        )
    if "Traceback" in crashed.stderr:
        raise AssertionError(f"raw traceback escaped controlled fault: {crashed.stderr}")
    state = wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        durable_state, (env.command_id,), timeout=10,
    )
    if case == "G" and remote_commit_count(env) != 1:
        raise AssertionError("remote push was not durable before local ACK fault")

    recovery = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "result_published", (env.command_id,), process=recovery,
    )
    stop_and_wait(env, recovery)

    conn = sqlite3.connect(env.journal, timeout=2.0)
    try:
        workspace = conn.execute(
            "SELECT revision,state_hash FROM workspaces WHERE session_id=?", (env.session_id,)
        ).fetchone()
        effect = conn.execute(
            "SELECT workspace_state_hash_after FROM operation_effects WHERE command_id=?", (env.command_id,)
        ).fetchone()
        result = conn.execute(
            "SELECT result_json,result_sha256,remote_path FROM results WHERE command_id=?", (env.command_id,)
        ).fetchone()
        outbox_state = conn.execute(
            "SELECT state FROM outbox WHERE command_id=?", (env.command_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    counts = snapshot_counts(env)
    assert workspace == (1, effect[0])
    assert counts["plan"] == counts["effect"] == counts["result"] == counts["outbox"] == 1
    assert counts["claim"] == 1
    assert outbox_state == "published"
    worktree_file = env.worktrees / env.session_id / "src" / "clamp.py"
    assert worktree_file.read_text(encoding="utf-8").count("return max(0, min(value, 100))") == 1
    remote = remote_bytes(env, result[2])
    expected = result[0].encode("utf-8")
    assert remote == expected
    assert not result[0].endswith("\n")
    assert "sha256:" + hashlib.sha256(remote).hexdigest() == result[1]
    assert remote_commit_count(env) == 1
    git(env.writer, "fetch", "origin", "results")
    changed = git(
        env.writer, "diff-tree", "--no-commit-id", "--name-only", "-r", "origin/results"
    ).stdout.splitlines()
    assert changed == [result[2]]
    assert git(env.fixture, "status", "--porcelain=v1").stdout.strip() == ""

    before = snapshot_counts(env)
    before_head = git(env.writer, "rev-parse", "origin/results").stdout.strip()
    noop = start_service(env)
    wait_for(
        env.journal,
        "SELECT state FROM service_instances WHERE state='running' ORDER BY rowid DESC LIMIT 1",
        "running", process=noop,
    )
    time.sleep(0.2)
    stop_and_wait(env, noop)
    assert snapshot_counts(env) == before
    git(env.writer, "fetch", "origin", "results")
    assert git(env.writer, "rev-parse", "origin/results").stdout.strip() == before_head

    return {
        "case": case, "session_id": env.session_id, "fault_point": fault,
        "durable_state_before_restart": state, "final_state": "result_published",
        "revision": workspace[0], "plan_count": counts["plan"],
        "effect_count": counts["effect"], "result_count": counts["result"],
        "outbox_count": counts["outbox"], "patch_count": 1, "publish_count": 1,
        "manual_repair": "no",
    }


def run_recovery_gate(root: Path, *, report_path: Path | None = None) -> dict[str, Any]:
    cases = [
        run_recovery_case(root / case.lower(), case, fault, durable)
        for case, fault, durable in FAULT_CASES
    ]
    report = {
        "schema_version": "1.0", "gate": "GHB0", "sessions": len(cases),
        "passed": len(cases), "failed": 0, "cases": cases,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
    return report
