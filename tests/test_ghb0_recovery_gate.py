from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.recovery_gate_fixture import FAULT_CASES, run_recovery_gate


def test_ghb0_recovery_gate_runs_seven_fresh_process_restart_sessions(tmp_path: Path) -> None:
    report_path = tmp_path / "artifacts" / "ghb0-gate" / "recovery-gate.json"
    report = run_recovery_gate(tmp_path / "sessions", report_path=report_path)
    assert report["schema_version"] == "1.0"
    assert report["gate"] == "GHB0"
    assert report["sessions"] == len(FAULT_CASES) == 7
    assert report["passed"] == 7
    assert report["failed"] == 0
    assert len({case["session_id"] for case in report["cases"]}) == 7
    assert [case["case"] for case in report["cases"]] == list("ABCDEFG")
    assert all(case["manual_repair"] == "no" for case in report["cases"])
    exact = json.dumps(report, sort_keys=True, separators=(",", ":"))
    assert report_path.read_text(encoding="utf-8") == exact
