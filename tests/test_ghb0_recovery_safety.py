from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from tests.helpers.recovery_gate_fixture import (
    git,
    remote_bytes,
    remote_commit_count,
    run_cli,
    setup_gate_environment,
    start_service,
    stop_and_wait,
    wait_for,
)

CLAMP_LF = b"def clamp_percent(value: int) -> int:\n    return value\n"


def test_command_collision_is_persisted_hash_only_and_blocks_execution(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "command-collision")
    discovered = run_cli(
        env, "start", "--foreground", fault="AFTER_DISCOVERED_BEFORE_VALIDATION"
    )
    assert discovered.returncode == 2 and "Traceback" not in discovered.stderr
    conn = sqlite3.connect(env.journal)
    original_json, original_hash, original_state = conn.execute(
        "SELECT command_json,command_sha256,state FROM commands WHERE command_id=?",
        (env.command_id,),
    ).fetchone()
    conn.close()
    assert original_state == "discovered"

    path = env.writer / "sessions" / env.session_id / "commands" / "000001.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["payload"]["new"] = "    return 42\n"
    path.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    git(env.writer, "add", "--", str(path.relative_to(env.writer)))
    git(env.writer, "commit", "-m", "collision")
    git(env.writer, "push", "origin", "commands")

    process = start_service(env)
    wait_for(
        env.journal,
        "SELECT COUNT(*) FROM ingestion_issues WHERE command_id=? AND blocking=1",
        1, (env.command_id,), process=process,
    )
    time.sleep(0.2)
    stop_and_wait(env, process)
    conn = sqlite3.connect(env.journal)
    current_json, current_hash, current_state = conn.execute(
        "SELECT command_json,command_sha256,state FROM commands WHERE command_id=?",
        (env.command_id,),
    ).fetchone()
    detail = conn.execute(
        "SELECT detail FROM ingestion_issues WHERE command_id=? ORDER BY issue_id DESC LIMIT 1",
        (env.command_id,),
    ).fetchone()[0]
    counts = {
        "plan": conn.execute("SELECT COUNT(*) FROM operation_plans WHERE command_id=?", (env.command_id,)).fetchone()[0],
        "effect": conn.execute("SELECT COUNT(*) FROM operation_effects WHERE command_id=?", (env.command_id,)).fetchone()[0],
        "result": conn.execute("SELECT COUNT(*) FROM results WHERE command_id=?", (env.command_id,)).fetchone()[0],
    }
    conn.close()
    assert (current_json, current_hash) == (original_json, original_hash)
    assert current_state in {"discovered", "validated"}
    assert counts == {"plan": 0, "effect": 0, "result": 0}
    assert "existing_sha256=" in detail and "incoming_sha256=" in detail
    assert "return 42" not in detail and len(detail) <= 500
    assert env.worktrees.exists() is False or not any(env.worktrees.iterdir())

    replay = start_service(env)
    time.sleep(0.3)
    stop_and_wait(env, replay)
    conn = sqlite3.connect(env.journal)
    assert conn.execute(
        "SELECT COUNT(*) FROM ingestion_issues WHERE command_id=? AND blocking=1",
        (env.command_id,),
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM operation_plans WHERE command_id=?", (env.command_id,)).fetchone()[0] == 0
    conn.close()


def test_result_collision_preserves_remote_and_moves_to_manual(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "result-collision")
    fault = run_cli(env, "start", "--foreground", fault="AFTER_STAGE_COMMIT_BEFORE_PUBLISH")
    assert fault.returncode == 2 and "Traceback" not in fault.stderr
    conn = sqlite3.connect(env.journal)
    _result_json, remote_path = conn.execute(
        "SELECT result_json,remote_path FROM results WHERE command_id=?", (env.command_id,)
    ).fetchone()
    conn.close()

    git(env.writer, "checkout", "results")
    collision_path = env.writer / remote_path
    collision_path.parent.mkdir(parents=True, exist_ok=True)
    collision_path.write_bytes(b'{"foreign":true}')
    git(env.writer, "add", "--", remote_path)
    git(env.writer, "commit", "-m", "foreign result")
    git(env.writer, "push", "origin", "results")
    git(env.writer, "checkout", "commands")
    foreign_head_count = remote_commit_count(env)

    recovery = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "manual_reconciliation_required", (env.command_id,), process=recovery,
    )
    stop_and_wait(env, recovery)
    assert remote_bytes(env, remote_path) == b'{"foreign":true}'
    assert remote_commit_count(env) == foreign_head_count
    conn = sqlite3.connect(env.journal)
    command_state = conn.execute(
        "SELECT state FROM commands WHERE command_id=?", (env.command_id,)
    ).fetchone()[0]
    session_state = conn.execute(
        "SELECT state FROM sessions WHERE session_id=?", (env.session_id,)
    ).fetchone()[0]
    outbox_state, diagnostic = conn.execute(
        "SELECT state,last_error FROM outbox WHERE command_id=?", (env.command_id,)
    ).fetchone()
    lifecycle = conn.execute(
        "SELECT disposition,state FROM workspace_lifecycle WHERE session_id=?", (env.session_id,)
    ).fetchone()
    conn.close()
    assert command_state == session_state == "manual_reconciliation_required"
    assert outbox_state == "collision" and len(diagnostic) <= 500
    assert lifecycle[0] == "preserve"
    assert (env.worktrees / env.session_id).exists()

    replay = start_service(env)
    time.sleep(0.3)
    stop_and_wait(env, replay)
    assert remote_commit_count(env) == foreign_head_count


def test_divergent_workspace_is_blocked_and_preserved(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "divergent")
    fault = run_cli(env, "start", "--foreground", fault="AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT")
    assert fault.returncode == 2
    target = env.worktrees / env.session_id / "src" / "clamp.py"
    target.write_text("def clamp_percent(value: int) -> int:\n    return 13\n", encoding="utf-8")

    recovery = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "manual_reconciliation_required", (env.command_id,), process=recovery,
    )
    stop_and_wait(env, recovery)
    conn = sqlite3.connect(env.journal)
    session_state = conn.execute(
        "SELECT state FROM sessions WHERE session_id=?", (env.session_id,)
    ).fetchone()[0]
    lifecycle = conn.execute(
        "SELECT disposition,state FROM workspace_lifecycle WHERE session_id=?", (env.session_id,)
    ).fetchone()
    conn.close()
    assert session_state == "manual_reconciliation_required"
    assert lifecycle[0] == "preserve"
    assert target.read_text(encoding="utf-8").endswith("return 13\n")
    assert (env.worktrees / env.session_id).exists()
    assert git(env.fixture, "status", "--porcelain=v1").stdout.strip() == ""


def test_persisted_transport_retry_restarts_and_publishes(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "retry")
    staged = run_cli(env, "start", "--foreground", fault="AFTER_STAGE_COMMIT_BEFORE_PUBLISH")
    assert staged.returncode == 2
    offline = env.control_remote.with_name("control.offline")
    env.control_remote.rename(offline)
    first = start_service(env)
    wait_for(
        env.journal, "SELECT attempt_count FROM outbox WHERE command_id=?",
        1, (env.command_id,), process=first,
    )
    stop_and_wait(env, first)
    offline.rename(env.control_remote)
    time.sleep(1.2)
    second = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "result_published", (env.command_id,), process=second,
    )
    stop_and_wait(env, second)
    assert remote_commit_count(env) == 1


def test_second_real_process_is_rejected_by_os_lock(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "second-process")
    first = start_service(env)
    wait_for(
        env.journal,
        "SELECT state FROM service_instances WHERE state='running' ORDER BY rowid DESC LIMIT 1",
        "running", process=first,
    )
    second = run_cli(env, "start", "--foreground")
    assert second.returncode == 1
    assert "already running" in second.stderr.lower()
    assert "Traceback" not in second.stderr
    stop_and_wait(env, first)


def test_gate_fixture_keeps_lf_bytes_through_detached_worktree(tmp_path: Path) -> None:
    """Regression: Windows autocrlf must not turn replace_exact old=...\\n into 0 matches."""
    env = setup_gate_environment(tmp_path / "lf-worktree")
    assert (env.fixture / "src" / "clamp.py").read_bytes() == CLAMP_LF
    worktree = env.worktrees / "detached-check"
    env.worktrees.mkdir(parents=True, exist_ok=True)
    git(env.fixture, "worktree", "add", "--detach", str(worktree), env.base_sha)
    assert (worktree / "src" / "clamp.py").read_bytes() == CLAMP_LF
    assert (worktree / "src" / "clamp.py").read_text(encoding="utf-8").count("    return value\n") == 1
