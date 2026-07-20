from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .one_message_pilot_fixture import replacement
from .one_message_pilot_support import ORIGIN, git, run


ALIAS = "kalkulator"
REMOTE_URL = "https://github.com/eagleblastmusic-lgtm/kalkulator.git"
PINNED_SHA = "4bd377f0fb33194da586a2aa58b67efcb86bc2e4"
EXPECTED_FAILED_TEST = "test_square_current_value"


def _replace_once(text: str, before: str, after: str, *, label: str) -> str:
    if text.count(before) != 1:
        raise RuntimeError(f"Pinned kalkulator source does not contain one expected anchor: {label}")
    return text.replace(before, after, 1)


def remote_main_sha() -> str:
    completed = run(["git", "ls-remote", "--exit-code", REMOTE_URL, "refs/heads/main"])
    line = str(completed.stdout).strip()
    sha, separator, ref = line.partition("\t")
    if separator != "\t" or ref != "refs/heads/main" or len(sha) != 40:
        raise RuntimeError(f"Unexpected kalkulator ls-remote response: {line!r}")
    return sha.lower()


def initialize_kalkulator(root: Path) -> dict[str, Any]:
    remote_before = remote_main_sha()
    if remote_before != PINNED_SHA:
        raise RuntimeError(f"kalkulator main moved: expected {PINNED_SHA}, received {remote_before}")

    fixture = root / "kalkulator-source"
    run(["git", "clone", "--no-checkout", REMOTE_URL, str(fixture)])
    git(fixture, "config", "core.autocrlf", "false")
    git(fixture, "config", "user.name", "BDB Real Repository Pilot")
    git(fixture, "config", "user.email", "real-repo-pilot@example.invalid")
    git(fixture, "checkout", "-B", "bdb-real-pilot", PINNED_SHA)
    if git(fixture, "rev-parse", "HEAD") != PINNED_SHA:
        raise RuntimeError("Pinned kalkulator commit was not checked out")
    if git(fixture, "status", "--porcelain=v1"):
        raise RuntimeError("Pinned kalkulator checkout is dirty before the pilot")

    git(fixture, "remote", "remove", "origin")
    if git(fixture, "remote"):
        raise RuntimeError("Real repository pilot retained a Git remote")

    calculator_before = (fixture / "calculator.py").read_bytes()
    tests_before = (fixture / "tests" / "test_calculator.py").read_bytes()
    readme_before = (fixture / "README.md").read_bytes()

    calculator_text = calculator_before.decode("utf-8")
    tests_text = tests_before.decode("utf-8")
    readme_text = readme_before.decode("utf-8")

    percent_anchor = (
        '    def percent(self) -> str:\n'
        '        if not self.error:\n'
        '            self.display = format_number(self._number() / Decimal("100"))\n'
        '            self.replace = True\n'
        '        return self.display\n\n'
    )
    failed_square = (
        percent_anchor
        + '    def square(self) -> str:\n'
        + '        if not self.error:\n'
        + '            self.display = format_number(self._number() * Decimal("2"))\n'
        + '            self.replace = True\n'
        + '        return self.display\n\n'
    )
    repaired_square = (
        percent_anchor
        + '    def square(self) -> str:\n'
        + '        if not self.error:\n'
        + '            value = self._number()\n'
        + '            self.display = format_number(value * value)\n'
        + '            self.replace = True\n'
        + '        return self.display\n\n'
    )

    calculator_failed = _replace_once(calculator_text, percent_anchor, failed_square, label="percent method")
    calculator_repaired = _replace_once(calculator_text, percent_anchor, repaired_square, label="percent method")
    key_anchor = (
        '        elif char == "%":\n'
        '            run(engine.percent)\n'
        '        return "break"\n'
    )
    key_with_square = (
        '        elif char == "%":\n'
        '            run(engine.percent)\n'
        '        elif char.lower() == "s":\n'
        '            run(engine.square)\n'
        '        return "break"\n'
    )
    calculator_failed = _replace_once(calculator_failed, key_anchor, key_with_square, label="keyboard handler")
    calculator_repaired = _replace_once(calculator_repaired, key_anchor, key_with_square, label="keyboard handler")

    square_tests = (
        '\n\ndef test_square_current_value():\n'
        '    engine = CalculatorEngine(display="12.5")\n'
        '    assert engine.square() == "156.25"\n'
        '    assert engine.replace is True\n\n'
        '    engine.digit("3")\n'
        '    assert engine.display == "3"\n'
    )
    tests_after = tests_text.rstrip() + square_tests.rstrip() + "\n"
    if tests_after.endswith("\n\n"):
        raise RuntimeError("Generated real-repository test file has a blank line at EOF")

    readme_after = _replace_once(
        readme_text,
        "- liczby dziesiętne, procenty i zmiana znaku;\n",
        "- liczby dziesiętne, procenty, podnoszenie do kwadratu i zmiana znaku;\n",
        label="feature list",
    )
    readme_after = _replace_once(
        readme_after,
        "- `.` lub `,` — separator dziesiętny.\n",
        "- `.` lub `,` — separator dziesiętny;\n- `s` — podnieś bieżącą liczbę do kwadratu.\n",
        label="keyboard shortcuts",
    )

    return {
        "fixture": fixture,
        "base_sha": PINNED_SHA,
        "remote_main_before": remote_before,
        "calculator_before": calculator_before,
        "tests_before": tests_before,
        "readme_before": readme_before,
        "calculator_failed": calculator_failed.encode("utf-8"),
        "calculator_repaired": calculator_repaired.encode("utf-8"),
        "tests_after": tests_after.encode("utf-8"),
        "readme_after": readme_after.encode("utf-8"),
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
        "repository_id": "bdb-real-repository-kalkulator-pilot",
        "allowed_paths": ["calculator.py", "tests/test_calculator.py", "README.md"],
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


def failed_operations(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        replacement("calculator.py", data["calculator_before"], data["calculator_failed"]),
        replacement("tests/test_calculator.py", data["tests_before"], data["tests_after"]),
        replacement("README.md", data["readme_before"], data["readme_after"]),
    ]


def repaired_operations(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        replacement("calculator.py", data["calculator_before"], data["calculator_repaired"]),
        replacement("tests/test_calculator.py", data["tests_before"], data["tests_after"]),
        replacement("README.md", data["readme_before"], data["readme_after"]),
    ]
