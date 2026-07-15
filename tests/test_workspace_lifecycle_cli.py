from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bdb_bridge import InstanceLock, SessionState
from tests.helpers.workspace_lifecycle_fixture import SESSION, make_fixture


def _write_config(path: Path, cfg) -> Path:
    target = path / "config.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(cfg.control_repo_path),
                "fixture_repo_path": str(cfg.fixture_repo_path),
                "worktree_root": str(cfg.worktree_root),
                "runtime_dir": str(cfg.runtime_dir),
                "journal_path": str(cfg.journal_path),
                "repository_id": cfg.repository_id,
                "allowed_paths": list(cfg.allowed_paths),
                "commands_ref": cfg.commands_ref,
                "results_ref": cfg.results_ref,
                "python_executable": sys.executable,
                "heartbeat_interval_seconds": 0.05,
                "heartbeat_stale_seconds": 2.0,
                "idle_poll_seconds": 0.05,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return target


def _cli(config: Path, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "bdb_bridge", "bridge", *args, "--config", str(config)],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )


def test_workspace_status_json_is_canonical_and_stable(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path)
    config = _write_config(tmp_path, cfg)
    journal.close()
    result = _cli(config, "workspace", "status", "--session-id", SESSION, "--json")
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.endswith("\n")
    body = result.stdout[:-1]
    parsed = json.loads(body)
    assert body == json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    assert parsed["session_id"] == SESSION
    assert parsed["disposition"] == "preserve"
    assert parsed["lifecycle_state"] == "preserved"
    assert parsed["registered"] is True and parsed["present"] is True
    assert "content" not in body.lower() and "token" not in body.lower()


def test_workspace_preserve_real_cli_is_idempotent(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path)
    config = _write_config(tmp_path, cfg)
    journal.close()
    first = _cli(config, "workspace", "preserve", "--session-id", SESSION)
    second = _cli(config, "workspace", "preserve", "--session-id", SESSION)
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout) == json.loads(second.stdout) == {
        "disposition": "preserve", "session_id": SESSION, "state": "preserved"
    }
    assert wm.path.exists()


def test_session_finalize_real_cli_keeps_result_published_and_workspace(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(
        tmp_path, session_state=SessionState.ACTIVE
    )
    config = _write_config(tmp_path, cfg)
    journal.close()
    first = _cli(config, "session", "finalize", "--session-id", SESSION)
    second = _cli(config, "session", "finalize", "--session-id", SESSION)
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout)["finalized"] is True
    assert json.loads(second.stdout)["idempotent"] is True
    assert wm.path.exists()


def test_workspace_cleanup_real_cli_requires_exact_confirmation_and_lock(tmp_path: Path) -> None:
    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path)
    config = _write_config(tmp_path, cfg)
    journal.close()
    mismatch = _cli(
        config, "workspace", "cleanup", "--session-id", SESSION,
        "--confirm-session-id", "018f3f66-6cb3-4f66-9f2e-3d7647d1b799",
    )
    assert mismatch.returncode == 1
    assert "exactly match" in mismatch.stderr and "Traceback" not in mismatch.stderr
    assert wm.path.exists()

    lock = InstanceLock(Path(cfg.runtime_dir) / "bridge.instance.lock")
    lock.acquire()
    try:
        online = _cli(
            config, "workspace", "cleanup", "--session-id", SESSION,
            "--confirm-session-id", SESSION,
        )
        assert online.returncode == 1
        assert "Traceback" not in online.stderr
        assert wm.path.exists()
    finally:
        lock.release()

    removed = _cli(
        config, "workspace", "cleanup", "--session-id", SESSION,
        "--confirm-session-id", SESSION,
    )
    assert removed.returncode == 0 and "Traceback" not in removed.stderr
    assert json.loads(removed.stdout)["state"] == "removed"
    assert not wm.path.exists()
    assert Path(cfg.journal_path).exists()


def test_operator_cli_invalid_config_and_missing_session_are_controlled(tmp_path: Path) -> None:
    missing_config = tmp_path / "missing.json"
    invalid = _cli(missing_config, "workspace", "status", "--session-id", SESSION, "--json")
    assert invalid.returncode == 1
    assert "Traceback" not in invalid.stderr

    cfg, journal, wm, workspace, command_id = make_fixture(tmp_path / "valid")
    config = _write_config(tmp_path / "valid", cfg)
    journal.close()
    unknown = "018f3f66-6cb3-4f66-9f2e-3d7647d1b799"
    preserve = _cli(config, "workspace", "preserve", "--session-id", unknown)
    finalize = _cli(config, "session", "finalize", "--session-id", unknown)
    cleanup = _cli(
        config, "workspace", "cleanup", "--session-id", unknown,
        "--confirm-session-id", unknown,
    )
    assert {preserve.returncode, finalize.returncode, cleanup.returncode} == {1}
    assert all("Traceback" not in item.stderr for item in (preserve, finalize, cleanup))
    assert wm.path.exists()
