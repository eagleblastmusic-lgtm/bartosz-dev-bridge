from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_message_repair_pilot_keeps_the_bounded_contract() -> None:
    entrypoint = (ROOT / "scripts" / "one_message_repair_pilot.py").read_text(encoding="utf-8")
    coordinator = (ROOT / "bdb_bridge" / "one_message_pilot.py").read_text(encoding="utf-8")
    fixture = (ROOT / "bdb_bridge" / "one_message_pilot_fixture.py").read_text(encoding="utf-8")
    support = (ROOT / "bdb_bridge" / "one_message_pilot_support.py").read_text(encoding="utf-8")
    combined = "\n".join((entrypoint, coordinator, fixture, support))

    assert 'ALIAS = "calculator2"' in combined
    assert '"operation": "multi_file_patch"' in combined
    assert "analyze_failed_pytest_result" in coordinator
    assert "WorkspacePromoter(config).promote_file" in coordinator
    assert '"user_interventions_between_attempts": 0' in coordinator
    assert "shell=True" not in combined
    assert "merge_pull_request" not in combined


def test_one_message_repair_pilot_has_a_powershell_entrypoint() -> None:
    wrapper = (ROOT / "scripts" / "Invoke-BDBOneMessageRepairPilot.ps1").read_text(
        encoding="utf-8"
    )

    assert "one_message_repair_pilot.py" in wrapper
    assert "TimeoutSeconds" in wrapper
