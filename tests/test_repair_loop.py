from __future__ import annotations

import pytest

from bdb_bridge.repair_loop import analyze_failed_pytest_result


EXPECTED = "tests/test_calculator.py::test_safe_divide_by_zero_returns_none"


def failed_result() -> dict[str, object]:
    return {
        "status": "failed",
        "exit_code": 1,
        "changed_files": [],
        "stdout_tail": f"FAILED {EXPECTED} - ZeroDivisionError\n1 failed, 2 passed",
        "stderr_tail": "",
        "data": {
            "checkpoint_state": "rolled_back",
            "rollback_performed": True,
        },
    }


def test_analyze_failed_pytest_result_requires_and_reports_rollback() -> None:
    analysis = analyze_failed_pytest_result(failed_result(), expected_test=EXPECTED)

    assert analysis.status == "failed"
    assert analysis.exit_code == 1
    assert analysis.rollback_confirmed is True
    assert analysis.failed_test == EXPECTED
    assert "ZeroDivisionError" in analysis.diagnostic


def test_analyze_failed_pytest_result_rejects_unrolled_changes() -> None:
    result = failed_result()
    result["changed_files"] = ["src/calculator.py"]

    with pytest.raises(ValueError, match="no durable changed_files"):
        analyze_failed_pytest_result(result, expected_test=EXPECTED)


def test_analyze_failed_pytest_result_rejects_missing_expected_test() -> None:
    result = failed_result()
    result["stdout_tail"] = "1 failed"

    with pytest.raises(ValueError, match="expected failed test"):
        analyze_failed_pytest_result(result, expected_test=EXPECTED)
