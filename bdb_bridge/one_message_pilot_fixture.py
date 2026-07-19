from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .one_message_pilot_support import ALIAS, ORIGIN, content_fields, git, sha256_value

EXPECTED_FAILED_TEST = "test_safe_divide_by_zero_returns_none"


def initialize_calculator2(root: Path) -> dict[str, Any]:
    fixture = root / "calculator2"
    fixture.mkdir()
    git(fixture, "init")
    git(fixture, "config", "user.name", "BDB One Message Pilot")
    git(fixture, "config", "user.email", "one-message-pilot@example.invalid")
    (fixture / "src").mkdir()
    (fixture / "tests").mkdir()
    (fixture / "src" / "__init__.py").write_text("", encoding="utf-8")

    source_before = b"def add(left: int, right: int) -> int:\n    return left + right\n"
    tests_before = (
        b"from src.calculator import add\n\n"
        b"def test_add() -> None:\n"
        b"    assert add(2, 3) == 5\n"
    )
    source_failed = (
        source_before
        + b"\n"
        + b"def safe_divide(left: float, right: float) -> float | None:\n"
        + b"    return left / right\n"
    )
    source_repaired = (
        source_before
        + b"\n"
        + b"def safe_divide(left: float, right: float) -> float | None:\n"
        + b"    if right == 0:\n"
        + b"        return None\n"
        + b"    return left / right\n"
    )
    tests_after = (
        b"from src.calculator import add, safe_divide\n\n"
        b"def test_add() -> None:\n"
        b"    assert add(2, 3) == 5\n\n"
        b"def test_safe_divide() -> None:\n"
        b"    assert safe_divide(9, 3) == 3\n\n"
        b"def test_safe_divide_by_zero_returns_none() -> None:\n"
        b"    assert safe_divide(9, 0) is None\n"
    )

    (fixture / "src" / "calculator.py").write_bytes(source_before)
    (fixture / "tests" / "test_calculator.py").write_bytes(tests_before)
    git(fixture, "add", "--", ".")
    git(fixture, "commit", "-m", "initialize calculator2 pilot")
    return {
        "fixture": fixture,
        "base_sha": git(fixture, "rev-parse", "HEAD"),
        "source_before": source_before,
        "tests_before": tests_before,
        "source_failed": source_failed,
        "source_repaired": source_repaired,
        "tests_after": tests_after,
    }


def build_configs(root: Path, fixture: Path, control: Path, python_executable: str) -> tuple[Path, Path]:
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
        "repository_id": "bdb-one-message-calculator2-pilot",
        "allowed_paths": ["src/calculator.py", "tests/test_calculator.py", "PILOT_RESULT.md"],
        "commands_ref": "origin/commands",
        "results_ref": "origin/results",
        "python_executable": python_executable,
        "test_timeout_seconds": 60,
        "heartbeat_interval_seconds": 0.2,
        "heartbeat_stale_seconds": 5,
        "idle_poll_seconds": 0.2,
        "direct_spool_enabled": True,
    }
    bridge_config_path.write_text(json.dumps(bridge_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    native_config_path = root / "native-host.json"
    native_config = {
        "schema": "bdb-native-host-config-v1",
        "repositories": {ALIAS: {"bridge_config_path": str(bridge_config_path)}},
        "allowed_origins": [ORIGIN],
        "state_path": str(root / "native-host-arm.json"),
        "session_store_path": str(root / "native-host-sessions.json"),
        "max_wait_seconds": 120,
        "max_message_bytes": 1048576,
    }
    native_config_path.write_text(json.dumps(native_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return bridge_config_path, native_config_path


def replacement(path: str, before: bytes, after: bytes) -> dict[str, Any]:
    return {
        "schema": "bdb-file-replacement-v1",
        "kind": "replace_file",
        "path": path,
        "expected_sha256": sha256_value(before),
        **content_fields(after),
    }


def create_file(path: str, content: bytes) -> dict[str, Any]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "create_file",
        "path": path,
        **content_fields(content),
    }
