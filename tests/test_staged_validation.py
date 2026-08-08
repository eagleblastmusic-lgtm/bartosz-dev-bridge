from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.models import ProfileRunOutcome
from bdb_bridge.multi_file_patch_runtime_journal import (
    count_consecutive_adaptive_full_skips,
    validation_timing_summary,
)
from bdb_bridge.staged_validation import (
    VALIDATION_PLAN_ID,
    _browser_regression_test_paths,
    _migration_schema_literal_issue,
    _requires_full_pytest,
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


def test_browser_fast_mapping_is_narrow_and_multi_file_scope_escalates_to_regression(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in (
        "test_browser_auto_decision_retry_runtime.py",
        "test_browser_conversation_tab_binding_runtime.py",
        "test_browser_auto_contract.py",
        "test_browser_task_controller_contract.py",
        "test_browser_unrelated_runtime.py",
    ):
        (tests / name).write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

    fast = _targeted_test_paths(
        tmp_path,
        ("browser_extension/content_auto_retry.js",),
    )
    assert fast == (
        "tests/test_browser_auto_decision_retry_runtime.py",
        "tests/test_browser_conversation_tab_binding_runtime.py",
    )
    assert _browser_regression_test_paths(
        tmp_path,
        ("browser_extension/content_auto_retry.js",),
    ) == ()

    regression = _browser_regression_test_paths(
        tmp_path,
        (
            "browser_extension/content_auto_retry.js",
            "browser_extension/popup.js",
        ),
    )
    assert regression == (
        "tests/test_browser_auto_contract.py",
        "tests/test_browser_auto_decision_retry_runtime.py",
        "tests/test_browser_conversation_tab_binding_runtime.py",
        "tests/test_browser_task_controller_contract.py",
        "tests/test_browser_unrelated_runtime.py",
    )


def test_migration_changes_preflight_all_migration_contract_tests(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    migration_contract_tests = (
        "test_code_relationship_migrations.py",
        "test_direct_checkout_workspace_migration.py",
        "test_durable_ingestion_additional.py",
        "test_journal_migrations.py",
        "test_multi_file_patch_v10_contracts.py",
        "test_repository_index_migrations.py",
        "test_result_outbox_migrations.py",
        "test_service_lifecycle_migrations.py",
        "test_workspace_recovery_migrations.py",
    )
    for name in migration_contract_tests:
        (tests / name).write_text("def test_contract():\n    assert True\n", encoding="utf-8")

    expected = tuple(f"tests/{name}" for name in sorted(migration_contract_tests))
    selected_for_migration_module = _targeted_test_paths(
        tmp_path,
        ("bdb_bridge/direct_checkout_workspace_migration.py",),
    )
    selected_for_registry = _targeted_test_paths(
        tmp_path,
        ("bdb_bridge/migrations.py",),
    )

    assert selected_for_migration_module == expected
    assert selected_for_registry == expected


def test_schema_literal_preflight_rejects_hardcoded_full_registry_range(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example_migrations.py").write_text(
        "from bdb_bridge.migrations import MIGRATIONS\n"
        "def test_registry():\n"
        "    assert tuple(m.version for m in MIGRATIONS) == tuple(range(1, 13))\n",
        encoding="utf-8",
    )

    issue = _migration_schema_literal_issue(tmp_path, ("bdb_bridge/migrations.py",))

    assert issue is not None
    assert "hardcoded full migration range" in issue


def test_schema_literal_preflight_rejects_hardcoded_future_version(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example_migrations.py").write_text(
        "def test_future(conn):\n"
        "    conn.execute(\"INSERT INTO schema_migrations(version,name) VALUES(13,'future')\")\n",
        encoding="utf-8",
    )

    issue = _migration_schema_literal_issue(tmp_path, ("bdb_bridge/migrations.py",))

    assert issue is not None
    assert "hardcoded future schema version" in issue


def test_schema_literal_preflight_rejects_current_version_after_journal_open(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example_migrations.py").write_text(
        "class Journal:\n"
        "    @classmethod\n"
        "    def open(cls, path):\n"
        "        return cls()\n"
        "def test_current():\n"
        "    journal = Journal.open('journal.db')\n"
        "    assert journal.execute(\"SELECT MAX(version) FROM schema_migrations\").fetchone()[0] == 12\n",
        encoding="utf-8",
    )

    issue = _migration_schema_literal_issue(tmp_path, ("bdb_bridge/migrations.py",))

    assert issue is not None
    assert "hardcoded current schema version after Journal.open" in issue


def test_schema_literal_preflight_allows_historical_subset_contracts(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example_migrations.py").write_text(
        "from bdb_bridge.migrations import MIGRATIONS\n"
        "def test_v9_history(conn):\n"
        "    assert tuple(m.version for m in MIGRATIONS[:9]) == tuple(range(1, 10))\n"
        "    assert conn.execute(\"SELECT MAX(version) FROM schema_migrations\").fetchone()[0] == 7\n",
        encoding="utf-8",
    )

    issue = _migration_schema_literal_issue(tmp_path, ("bdb_bridge/migrations.py",))

    assert issue is None


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
    assert "--durations=20" not in calls[0]
    assert calls[1] == [sys.executable, "-m", "pytest", "-q", "--durations=20"]
    assert "[targeted] status=success" in outcome.stdout
    assert "[full] status=success" in outcome.stdout


def test_adaptive_full_policy_is_conservative_except_browser_scope() -> None:
    assert _requires_full_pytest(()) is True
    assert _requires_full_pytest(("bdb_bridge/execution.py",)) is True
    assert _requires_full_pytest(("bdb_bridge/migrations.py",)) is True
    assert _requires_full_pytest(("browser_extension/content.js", "README.md")) is True
    assert _requires_full_pytest(("browser_extension/content.js",)) is False
    assert _requires_full_pytest(
        ("browser_extension/content.js", "tests/test_browser_auto_loop_runtime.py")
    ) is False


def test_staged_profile_skips_full_for_low_risk_browser_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    extension = tmp_path / "browser_extension"
    tests = tmp_path / "tests"
    extension.mkdir()
    tests.mkdir()
    (extension / "content.js").write_text("const VALUE = 1;\n", encoding="utf-8")
    (tests / "test_browser_runtime.py").write_text(
        "def test_browser_runtime():\n    assert True\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="browser ok\n", stderr="")

    monkeypatch.setattr("bdb_bridge.staged_validation.subprocess.run", fake_run)
    outcome = run_staged_pytest_profile(
        workspace_path=tmp_path,
        python_executable=sys.executable,
        timeout_seconds=30,
        environment={},
        changed_paths=("browser_extension/content.js",),
    )

    assert outcome.status == "success"
    assert calls == [[sys.executable, "-m", "pytest", "-q", "tests/test_browser_runtime.py"]]
    assert "[targeted] status=success" in outcome.stdout
    assert "[full] skipped=adaptive_low_risk_browser_scope" in outcome.stdout


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
        self.adaptive_skip_debt = 0

    def _now_fn(self) -> str:
        self.clock += 1
        return f"2026-08-07T16:00:{self.clock:02d}Z"

    def get_validation_run(self, command_id: str, plan_id: str, stage_index: int):
        return self.records.get((command_id, plan_id, stage_index))

    def count_consecutive_adaptive_full_skips(
        self,
        command_id: str,
        plan_id: str,
        *,
        limit: int = 4,
    ) -> int:
        assert command_id
        assert plan_id == VALIDATION_PLAN_ID
        return min(self.adaptive_skip_debt, limit)

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
        assert stage_name in {"structural", "targeted", "regression", "full"}
        record = _FakeValidationRecord(outcome)
        self.records[(command_id, plan_id, stage_index)] = record
        return record


def test_validation_timing_summary_exposes_precise_full_duration_and_skip_state() -> None:
    journal = _FakeValidationJournal()
    command_id = "command:timing"
    journal.records[(command_id, VALIDATION_PLAN_ID, 1)] = _FakeValidationRecord(
        ProfileRunOutcome("success", 0, "[structural] status=success duration_ms=1\n", "", 1)
    )
    journal.records[(command_id, VALIDATION_PLAN_ID, 2)] = _FakeValidationRecord(
        ProfileRunOutcome("success", 0, "[targeted] status=success duration_ms=2\n", "", 2)
    )
    journal.records[(command_id, VALIDATION_PLAN_ID, 3)] = _FakeValidationRecord(
        ProfileRunOutcome("success", 0, "[regression] skipped=not_required\n", "", 0)
    )
    journal.records[(command_id, VALIDATION_PLAN_ID, 4)] = _FakeValidationRecord(
        ProfileRunOutcome("success", 0, "[full] status=success duration_ms=37\n", "", 37)
    )

    summary = validation_timing_summary(journal, command_id, VALIDATION_PLAN_ID)

    assert summary["structural_ms"] == 1
    assert summary["targeted_ms"] == 2
    assert summary["regression_ms"] == 0
    assert summary["full_ms"] == 37
    assert summary["total_ms"] == 40
    assert summary["full_executed"] is True
    assert summary["stages"]["regression"]["executed"] is False

    skipped_command = "command:timing-skipped"
    journal.records[(skipped_command, VALIDATION_PLAN_ID, 4)] = _FakeValidationRecord(
        ProfileRunOutcome(
            "success",
            0,
            "[full] skipped=adaptive_low_risk_browser_scope debt=1/4\n",
            "",
            0,
        )
    )
    skipped = validation_timing_summary(journal, skipped_command, VALIDATION_PLAN_ID)
    assert skipped["full_ms"] == 0
    assert skipped["full_executed"] is False


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
    assert calls == [[sys.executable, "-m", "pytest", "-q", "--durations=20"]]
    assert "[structural] persisted" in outcome.stdout
    assert "[targeted] persisted" in outcome.stdout
    assert "[regression] skipped=not_required" in outcome.stdout
    assert "[full] status=success" in outcome.stdout
    assert (command_id, VALIDATION_PLAN_ID, 3) in journal.records
    assert (command_id, VALIDATION_PLAN_ID, 4) in journal.records


def test_adaptive_debt_counter_stops_at_last_real_full() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, repository_id TEXT)"
    )
    connection.execute(
        "CREATE TABLE commands (command_id TEXT PRIMARY KEY, session_id TEXT, sequence INTEGER)"
    )
    connection.execute(
        "CREATE TABLE validation_runs (command_id TEXT, plan_id TEXT, stage_name TEXT, status TEXT, stdout_tail TEXT, created_at TEXT)"
    )
    for sequence in range(1, 6):
        connection.execute(
            "INSERT INTO sessions (session_id, repository_id) VALUES (?, 'repo:1')",
            (f"session:{sequence}",),
        )
        connection.execute(
            "INSERT INTO commands (command_id, session_id, sequence) VALUES (?, ?, 1)",
            (f"command:{sequence}", f"session:{sequence}"),
        )
    connection.execute(
        "INSERT INTO validation_runs VALUES ('command:1', ?, 'full', 'success', '[full] status=success duration_ms=1', '2026-08-08T00:00:01Z')",
        (VALIDATION_PLAN_ID,),
    )
    for sequence in (2, 3, 4):
        connection.execute(
            "INSERT INTO validation_runs VALUES (?, ?, 'full', 'success', '[full] skipped=adaptive_low_risk_browser_scope debt=1/4', ?)",
            (f"command:{sequence}", VALIDATION_PLAN_ID, f"2026-08-08T00:00:0{sequence}Z"),
        )
    connection.execute("INSERT INTO sessions VALUES ('session:other', 'repo:other')")
    connection.execute("INSERT INTO commands VALUES ('command:other', 'session:other', 1)")
    connection.execute(
        "INSERT INTO validation_runs VALUES ('command:other', ?, 'full', 'success', '[full] skipped=adaptive_low_risk_browser_scope debt=1/4', '2026-08-08T00:00:09Z')",
        (VALIDATION_PLAN_ID,),
    )

    class DebtJournal:
        _connection = connection

        def _ensure_open(self) -> None:
            return None

        def get_command(self, command_id: str):
            assert command_id == "command:5"
            return SimpleNamespace(session_id="session:5", sequence=1)

        def get_session(self, session_id: str):
            assert session_id == "session:5"
            return SimpleNamespace(repository_id="repo:1")

    journal = DebtJournal()
    assert count_consecutive_adaptive_full_skips(
        journal,
        "command:5",
        VALIDATION_PLAN_ID,
        limit=4,
    ) == 3

    connection.execute(
        "UPDATE validation_runs SET status='failed', stdout_tail='[full] status=failed duration_ms=1' WHERE command_id='command:4'"
    )
    assert count_consecutive_adaptive_full_skips(
        journal,
        "command:5",
        VALIDATION_PLAN_ID,
        limit=4,
    ) == 4
    connection.execute(
        "UPDATE validation_runs SET status='success', stdout_tail='[full] skipped=adaptive_low_risk_browser_scope debt=1/4' WHERE command_id='command:4'"
    )

    connection.execute(
        "UPDATE validation_runs SET stdout_tail='[full] skipped=adaptive_low_risk_browser_scope debt=1/4' WHERE command_id='command:1'"
    )
    assert count_consecutive_adaptive_full_skips(
        journal,
        "command:5",
        VALIDATION_PLAN_ID,
        limit=4,
    ) == 4
    connection.close()


def test_durable_browser_scope_forces_full_when_adaptive_debt_reaches_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    extension = tmp_path / "browser_extension"
    tests = tmp_path / "tests"
    extension.mkdir()
    tests.mkdir()
    (extension / "content_auto_retry.js").write_text("const VALUE = 1;\n", encoding="utf-8")
    for name in (
        "test_browser_auto_decision_retry_runtime.py",
        "test_browser_conversation_tab_binding_runtime.py",
    ):
        (tests / name).write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

    journal = _FakeValidationJournal()
    journal.adaptive_skip_debt = 4
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("bdb_bridge.staged_validation.subprocess.run", fake_run)
    outcome = run_durable_staged_pytest_profile(
        journal=journal,
        command_id="command:debt",
        workspace_path=tmp_path,
        python_executable=sys.executable,
        timeout_seconds=30,
        environment={},
        changed_paths=("browser_extension/content_auto_retry.js",),
    )

    assert outcome.status == "success"
    assert len(calls) == 2
    assert "--durations=20" not in calls[0]
    assert calls[1] == [sys.executable, "-m", "pytest", "-q", "--durations=20"]
    assert "[full] status=success" in outcome.stdout


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
