from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MAX_DIAGNOSTIC_CHARS = 1_200
_MAX_RESULT_TAIL_CHARS = 8_000


@dataclass(frozen=True)
class FailureAnalysis:
    """Bounded, evidence-based diagnosis of one rolled-back pytest attempt."""

    status: str
    exit_code: int
    rollback_confirmed: bool
    failed_test: str
    diagnostic: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "rollback_confirmed": self.rollback_confirmed,
            "failed_test": self.failed_test,
            "diagnostic": self.diagnostic,
        }


def analyze_failed_pytest_result(
    result: dict[str, Any],
    *,
    expected_test: str,
) -> FailureAnalysis:
    """Validate a failed, rolled-back result and extract bounded pytest evidence.

    This helper never guesses success. It accepts only the durable result shape emitted
    by the multi-file runtime after a non-zero pytest run and confirmed rollback.
    """

    if not isinstance(result, dict):
        raise ValueError("result must be an object")
    if result.get("status") != "failed":
        raise ValueError("repair analysis requires status=failed")

    exit_code = result.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code == 0:
        raise ValueError("repair analysis requires a non-zero integer exit_code")

    changed_files = result.get("changed_files")
    if changed_files != []:
        raise ValueError("failed attempt must expose no durable changed_files after rollback")

    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError("failed attempt is missing durable checkpoint data")
    if data.get("checkpoint_state") != "rolled_back" or data.get("rollback_performed") is not True:
        raise ValueError("failed attempt did not prove rollback")

    stdout = result.get("stdout_tail")
    stderr = result.get("stderr_tail")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ValueError("failed attempt is missing bounded test output")
    combined = (stdout + "\n" + stderr)[-_MAX_RESULT_TAIL_CHARS:]
    if expected_test not in combined:
        raise ValueError(f"expected failed test was not observed: {expected_test}")

    evidence: list[str] = []
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if expected_test in line or line.startswith("E ") or "FAILED" in line:
            evidence.append(line)
        if len(evidence) >= 6:
            break
    diagnostic = " | ".join(evidence)[:_MAX_DIAGNOSTIC_CHARS]
    if not diagnostic:
        diagnostic = f"pytest reported a non-zero result for {expected_test}"

    return FailureAnalysis(
        status="failed",
        exit_code=exit_code,
        rollback_confirmed=True,
        failed_test=expected_test,
        diagnostic=diagnostic,
    )
