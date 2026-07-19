from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UnittestFailureAnalysis:
    status: str
    failed_test: str
    exit_code: int
    rollback_confirmed: bool
    diagnostic: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failed_test": self.failed_test,
            "exit_code": self.exit_code,
            "rollback_confirmed": self.rollback_confirmed,
            "diagnostic": self.diagnostic,
        }


def analyze_failed_unittest_result(
    result: dict[str, Any],
    *,
    expected_test: str,
) -> UnittestFailureAnalysis:
    data = result.get("data")
    if result.get("status") != "failed" or result.get("exit_code") != 1:
        raise RuntimeError("Initial unittest result is not an expected test failure")
    if not isinstance(data, dict):
        raise RuntimeError("Initial unittest result has no durable data")
    if data.get("checkpoint_state") != "rolled_back":
        raise RuntimeError("Failed unittest candidate did not roll back")
    if data.get("rollback_performed") is not True:
        raise RuntimeError("Failed unittest result did not confirm rollback")
    if result.get("changed_files") != []:
        raise RuntimeError("Failed unittest result retained changed files")

    output = "\n".join(
        str(result.get(field) or "")
        for field in ("stdout_tail", "stderr_tail", "summary")
    )
    if expected_test not in output:
        raise RuntimeError("Expected unittest failure was not present in bounded output")
    if "FAIL" not in output and "FAILED" not in output:
        raise RuntimeError("Bounded unittest output has no failure marker")

    diagnostic_lines = [line.strip() for line in output.splitlines() if line.strip()]
    diagnostic = " | ".join(diagnostic_lines[-12:])[:600]
    return UnittestFailureAnalysis(
        status="failed",
        failed_test=expected_test,
        exit_code=1,
        rollback_confirmed=True,
        diagnostic=diagnostic,
    )
