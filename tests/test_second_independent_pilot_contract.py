from __future__ import annotations

import base64
import json
from pathlib import Path

from bdb_bridge.ingestion_validate import parse_command_envelope
from bdb_bridge.recovery_journal import sha256_bytes


ROOT = Path(__file__).resolve().parents[1]
SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b711"


def _unittest_command() -> dict[str, object]:
    content = b"value = 1\n"
    return {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": f"{SESSION_ID}:000001",
        "sequence": 1,
        "created_at": "2026-07-19T18:00:00Z",
        "expires_at": "2026-07-19T19:00:00Z",
        "operation": "multi_file_patch",
        "expected_revision": 0,
        "expected_state_hash": "sha256:" + "1" * 64,
        "payload": {
            "profile_id": "poc_unittest",
            "patch": {
                "schema": "bdb-multi-file-patch-v1",
                "operations": [
                    {
                        "schema": "bdb-edit-operation-v1",
                        "kind": "create_file",
                        "path": "inventory/parser.py",
                        "content_base64": base64.b64encode(content).decode("ascii"),
                        "content_sha256": sha256_bytes(content),
                    }
                ],
            },
        },
    }


def test_multi_file_gate_accepts_only_the_fixed_unittest_profile() -> None:
    parsed = parse_command_envelope(
        json.dumps(_unittest_command()),
        source_path=f"sessions/{SESSION_ID}/commands/000001.json",
    )
    assert parsed["payload"]["profile_id"] == "poc_unittest"


def test_second_pilot_is_independent_and_bounded() -> None:
    pilot = (ROOT / "bdb_bridge" / "second_independent_pilot.py").read_text(
        encoding="utf-8"
    )
    fixture = (ROOT / "bdb_bridge" / "second_pilot_fixture.py").read_text(
        encoding="utf-8"
    )
    analyzer = (ROOT / "bdb_bridge" / "unittest_repair_loop.py").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((pilot, fixture, analyzer))

    assert 'ALIAS = "inventory2"' in fixture
    assert 'PROFILE_ID = "poc_unittest"' in fixture
    assert "analyze_failed_unittest_result" in pilot
    assert "WorkspacePromoter(config).promote_file" in pilot
    assert '"user_interventions_between_attempts": 0' in pilot
    assert "inventory/parser.py" in fixture
    assert "inventory/report.py" in fixture
    assert "tests/test_inventory_report.py" in fixture
    assert "SECOND_PILOT_RESULT.md" in fixture
    assert "shell=True" not in combined
    assert "merge_pull_request" not in combined


def test_second_pilot_has_a_windows_entrypoint() -> None:
    wrapper = (ROOT / "scripts" / "Invoke-BDBSecondIndependentPilot.ps1").read_text(
        encoding="utf-8"
    )
    assert "bdb_bridge.second_independent_pilot" in wrapper
    assert "TimeoutSeconds" in wrapper
