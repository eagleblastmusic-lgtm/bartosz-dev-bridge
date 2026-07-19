from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_message_repair_pilot_keeps_the_bounded_contract() -> None:
    script = (ROOT / "scripts" / "run_one_message_repair_pilot.py").read_text(encoding="utf-8")

    assert 'ALIAS = "calculator2"' in script
    assert '"operation": "multi_file_patch"' in script
    assert "analyze_failed_pytest_result" in script
    assert "WorkspacePromoter(config).promote_file" in script
    assert '"user_interventions_between_attempts": 0' in script
    assert "shell=True" not in script
    assert "merge_pull_request" not in script


def test_one_message_repair_pilot_has_a_powershell_entrypoint() -> None:
    wrapper = (ROOT / "scripts" / "Invoke-BDBOneMessageRepairPilot.ps1").read_text(
        encoding="utf-8"
    )

    assert "run_one_message_repair_pilot.py" in wrapper
    assert "TimeoutSeconds" in wrapper
