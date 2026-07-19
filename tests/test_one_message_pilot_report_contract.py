from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_message_pilot_report_contains_completion_evidence() -> None:
    coordinator = (ROOT / "bdb_bridge" / "one_message_pilot.py").read_text(encoding="utf-8")

    for required in (
        '"schema": "bdb-one-message-repair-pilot-report-v1"',
        '"attempt_count": 2',
        '"initial_attempt"',
        '"rollback_performed"',
        '"repair_attempt"',
        '"promotion"',
        '"final_tests"',
        '"source_checkout_clean": True',
        '"receipt_path"',
        '"journal_path"',
    ):
        assert required in coordinator
