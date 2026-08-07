from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.models import ProfileRunOutcome
from bdb_bridge.staged_validation import (
    VALIDATION_PLAN_ID,
    _targeted_test_paths,
    run_durable_staged_pytest_profile,
    run_staged_pytest_profile,
)


def test_targeted_tests_include_changed_test_and_matching_bridge_module(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
    (tests / "test_other.py").write_text("def test_other():\n    assert True\n", encoding="utf-8")
    selected = _targeted_test_paths(
        tmp_path,
        ("bdb_bridge/alpha.py", "tests/test_other.py"),
    )
    assert selected == ("tests/test_alpha.py", "tests/test_other.py")


def test_staged_profile_runs_targeted_before_one_full_pytest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "bdb_bridge"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_alpha.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("bdb_bridge.staged_validation.subprocess.run", fake_run)
    outcome = run_staged_pytest_profile(
        workspace_path=tmp_path,
        python_executable=sys.executable,
        timeout_seconds=30,
        environment={},
        changed_paths=("bdb_bridge/alpha.py",),
    )

    assert outcome.status == "success"
    assert len(calls) == 2
    assert calls[0][-1] == "tests/test_alpha.py"
    assert calls[1] == [sys.executable, "-m", "pytest", "-q"]
    assert "[targeted] status=success" in outcome.stdout
    assert "[full] status=success" in outcome.stdout


def test_staged_profile_stops_before_full_pytest_when_targeted_tests_fail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    changed_test = tests / "test_failure.py"
    changed_test.write_text("def test_failure():\n    assert False\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=1, stdout="1 failed\n", stderr="boom\n")

    monkeypatch.setattr("bdb_bridge.staged_validation.subprocess.run", fake_run)
    outcome = run_staged_pytest_profile(
        workspace_path=tmp_path,
        python_executable=sys.executable,
        timeout_seconds=30,
        environment={},
        changed_paths=("tests/test_failure.py",),
    )

    assert outcome.status == "failed"
    assert len(calls) == 1
    assert "[full]" not in outcome.stdout


def test_staged_profile_stops_on_python_compile_failure_before_pytest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "bdb_bridge"
    package.mkdir()
    broken = package / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("pytest must not run after structural failure")

    monkeypatch.setattr("bdb_bridge.staged_validation.subprocess.run", fail_if_called)
    outcome = run_staged_pytest_profile(
        workspace_path=tmp_path,
        python_executable=sys.executable,
        timeout_seconds=30,
        environment={},
        changed_paths=("bdb_bridge/broken.py",),
    )

    assert outcome.status == "failed"
    assert "[structural] python_compile failed" in outcome.stdout


def test_targeted_tests_map_top_level_package_without_core_layer_dependency(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_gui_current_operation_view.py").write_text(
        "def test_gui_view():\n    assert True\n",
        encoding="utf-8",
    )
    selected = _targeted_test_paths(
        tmp_path,
        ("bdb_gui/current_operation_view.py",),
    )
    assert selected == ("tests/test_gui_current_operation_view.py",)


class _FakeValidationRecord:
    def __init__(self, outcome: ProfileRunOutcome) -> None:
        self._outcome = outcome

    def to_outcome(self) -> ProfileRunOutcome:
        return self._outcome


class _FakeValidationJournal:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, int], _FakeValidationRecord] = {}
        self.clock = 0

    def _now_fn(self) -> str:
        self.clock += 1
        return f"2026-08-07T16:00:{self.clock:02d}Z"

    def get_validation_run(self, command_id: str, plan_id: str, stage_index: int):
        return self.records.get((command_id, plan_id, stage_index))

    def record_validation_run(
        self,
        *,
        command_id: str,
        plan_id: str,
        stage_index: int,
        stage_name: str,
        outcome: ProfileRunOutcome,
        started_at: str,
        finished_at: str,
    ):
        assert stage_name in {"structural", "targeted", "full"}
        record = _FakeValidationRecord(outcome)
        self.records[(command_id, plan_id, stage_index)] = record
        return record


def test_durable_stages_reuse_completed_structural_and_targeted_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "bdb_bridge"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_alpha.py").write_text(
        "def test_alpha():\n    assert True\n",
        encoding="utf-8",
    )
    journal = _FakeValidationJournal()
    command_id = "command:1"
    journal.records[(command_id, VALIDATION_PLAN_ID, 1)] = _FakeValidationRecord(
        ProfileRunOutcome("success", 0, "[structural] persisted\n", "", 1)
    )
    journal.records[(command_id, VALIDATION_PLAN_ID, 2)] = _FakeValidationRecord(
        ProfileRunOutcome("success", 0, "[targeted] persisted\n", "", 2)
    )
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="full ok\n", stderr="")

    monkeypatch.setattr("bdb_bridge.staged_validation.subprocess.run", fake_run)
    outcome = run_durable_staged_pytest_profile(
        journal=journal,
        command_id=command_id,
        workspace_path=tmp_path,
        python_executable=sys.executable,
        timeout_seconds=30,
        environment={},
        changed_paths=("bdb_bridge/alpha.py",),
    )

    assert outcome.status == "success"
    assert calls == [[sys.executable, "-m", "pytest", "-q"]]
    assert "[structural] persisted" in outcome.stdout
    assert "[targeted] persisted" in outcome.stdout
    assert "[full] status=success" in outcome.stdout
    assert (command_id, VALIDATION_PLAN_ID, 3) in journal.records


def test_durable_targeted_failure_is_reused_without_running_full_pytest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = _FakeValidationJournal()
    command_id = "command:2"
    journal.records[(command_id, VALIDATION_PLAN_ID, 1)] = _FakeValidationRecord(
        ProfileRunOutcome("success", 0, "[structural] persisted\n", "", 1)
    )
    journal.records[(command_id, VALIDATION_PLAN_ID, 2)] = _FakeValidationRecord(
        ProfileRunOutcome("failed", 1, "[targeted] persisted failure\n", "boom\n", 3)
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("pytest must not run after a persisted targeted failure")

    monkeypatch.setattr("bdb_bridge.staged_validation.subprocess.run", fail_if_called)
    outcome = run_durable_staged_pytest_profile(
        journal=journal,
        command_id=command_id,
        workspace_path=tmp_path,
        python_executable=sys.executable,
        timeout_seconds=30,
        environment={},
        changed_paths=(),
    )

    assert outcome.status == "failed"
    assert "[targeted] persisted failure" in outcome.stdout
    assert (command_id, VALIDATION_PLAN_ID, 3) not in journal.records
