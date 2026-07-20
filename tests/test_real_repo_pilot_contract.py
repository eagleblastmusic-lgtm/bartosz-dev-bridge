from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from bdb_bridge import real_repo_pilot, real_repo_pilot_fixture


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PATHS = ["calculator.py", "tests/test_calculator.py", "README.md"]


def test_real_repository_pilot_is_pinned_and_bounded() -> None:
    assert real_repo_pilot_fixture.REMOTE_URL == (
        "https://github.com/eagleblastmusic-lgtm/kalkulator.git"
    )
    assert real_repo_pilot_fixture.PINNED_SHA == (
        "4bd377f0fb33194da586a2aa58b67efcb86bc2e4"
    )
    assert re.fullmatch(r"[0-9a-f]{40}", real_repo_pilot_fixture.PINNED_SHA)
    assert real_repo_pilot_fixture.EXPECTED_FAILED_TEST == "test_square_current_value"
    assert real_repo_pilot.REPORT_SCHEMA == "bdb-real-repository-pilot-report-v1"


def test_real_repository_operations_touch_only_three_allowlisted_files() -> None:
    data = {
        "calculator_before": b"calculator-before",
        "calculator_failed": b"calculator-failed",
        "calculator_repaired": b"calculator-repaired",
        "tests_before": b"tests-before",
        "tests_after": b"tests-after",
        "readme_before": b"readme-before",
        "readme_after": b"readme-after",
    }

    failed = real_repo_pilot_fixture.failed_operations(data)
    repaired = real_repo_pilot_fixture.repaired_operations(data)

    assert [item["path"] for item in failed] == EXPECTED_PATHS
    assert [item["path"] for item in repaired] == EXPECTED_PATHS
    assert all(item["kind"] == "replace_file" for item in (*failed, *repaired))
    assert failed[0]["content_base64"] != repaired[0]["content_base64"]
    assert failed[1]["content_base64"] == repaired[1]["content_base64"]
    assert failed[2]["content_base64"] == repaired[2]["content_base64"]


def test_real_repository_fixture_enforces_one_newline_at_test_eof() -> None:
    source = inspect.getsource(real_repo_pilot_fixture)

    assert 'tests_text.rstrip() + square_tests.rstrip() + "\\n"' in source
    assert 'tests_after.endswith("\\n\\n")' in source
    assert "Generated real-repository test file has a blank line at EOF" in source


def test_real_repository_fixture_removes_remote_and_never_pushes() -> None:
    source = inspect.getsource(real_repo_pilot_fixture)
    lowered = source.lower()

    assert '"clone", "--no-checkout"' in source
    assert '"checkout", "-B", "bdb-real-pilot"' in source
    assert '"remote", "remove", "origin"' in source
    assert '"ls-remote"' in source
    assert '"push"' not in lowered
    assert '"remote", "add"' not in source
    assert "gh " not in lowered
    assert "api.github.com" not in lowered


def test_real_repository_runner_proves_two_attempts_and_remote_immutability() -> None:
    source = inspect.getsource(real_repo_pilot)

    assert '"attempt_limit": 2' in source
    assert '"user_interventions_between_attempts": 0' in source
    assert 'request_id="kalkulator-real-initial-attempt"' in source
    assert 'request_id="kalkulator-real-repair-attempt"' in source
    assert '"remote_mutation_performed": False' in source
    assert 'git(fixture, "remote")' in source
    assert 'remote_after = remote_main_sha()' in source
    assert 'receipt.get("parent_commit") != PINNED_SHA' in source
    assert 'WorkspacePromoter(config).promote_file(result_path)' in source


def test_real_repository_runner_has_no_direct_network_mutation_library() -> None:
    tree = ast.parse(inspect.getsource(real_repo_pilot))
    forbidden_roots = {
        "requests",
        "urllib",
        "http",
        "aiohttp",
        "websockets",
        "socket",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".", 1)[0]}
        else:
            continue
        assert forbidden_roots.isdisjoint(roots)


def test_real_repository_entrypoints_exist_and_are_non_installing() -> None:
    python_entry = ROOT / "scripts" / "real_repository_pilot.py"
    powershell_entry = ROOT / "scripts" / "Invoke-BDBRealRepositoryPilot.ps1"
    assert python_entry.is_file()
    assert powershell_entry.is_file()

    powershell = powershell_entry.read_text(encoding="utf-8").lower()
    assert "real_repository_pilot.py" in powershell
    for forbidden in (
        "invoke-webrequest",
        "git push",
        "gh pr",
        "gh release",
        "msiexec",
        "deploy",
    ):
        assert forbidden not in powershell
