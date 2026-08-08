from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_bridge import CommandState, Journal
from bdb_bridge.execution import (
    ExecutionCoordinator,
    _apply_exact_replacements,
    _replacement_pairs,
)
from bdb_bridge.protocol import BridgeError


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b702"
COMMAND_ID = f"{SESSION_ID}:000001"


def test_multiline_lf_request_matches_crlf_file_and_preserves_crlf() -> None:
    before = "first\r\nold heading\r\nold setting\r\nlast\r\n"
    after = _apply_exact_replacements(
        before,
        [("old heading\nold setting\n", "new heading\nnew setting\n")],
    )

    assert after == "first\r\nnew heading\r\nnew setting\r\nlast\r\n"
    assert "\n" not in after.replace("\r\n", "")


def test_bounded_batch_changes_related_values_in_one_file() -> None:
    payload = {
        "replacements": [
            {"old": '"text": "old"', "new": '"text": "new"'},
            {"old": '"fm_quote_text": "old"', "new": '"fm_quote_text": "new"'},
        ]
    }

    pairs = _replacement_pairs(payload)
    after = _apply_exact_replacements(
        '{\r\n  "text": "old",\r\n  "fm_quote_text": "old"\r\n}\r\n',
        pairs,
    )

    assert after.count('"new"') == 2
    assert after.count('"old"') == 0


def test_batch_rejects_non_unique_replacement() -> None:
    with pytest.raises(BridgeError, match="Replacement 1.*found 2"):
        _apply_exact_replacements("old\nold\n", [("old", "new")])


def test_exact_terminal_outcome_persists_specific_error_code(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "journal.db")
    try:
        journal.create_session(SESSION_ID, "exact-fixture", "a" * 40)
        journal.record_command(
            SESSION_ID,
            COMMAND_ID,
            1,
            {
                "schema_version": "1.1",
                "session_id": SESSION_ID,
                "command_id": COMMAND_ID,
                "sequence": 1,
                "operation": "replace_exact_and_test",
                "expected_revision": 0,
                "expected_state_hash": "sha256:" + "b" * 64,
                "payload": {"profile_id": "poc_pytest", "path": "src/app.py"},
            },
        )
        journal.transition_command(COMMAND_ID, CommandState.DISCOVERED, CommandState.VALIDATED)
        journal.transition_command(COMMAND_ID, CommandState.VALIDATED, CommandState.CLAIMED)

        coordinator = ExecutionCoordinator(object(), journal)
        outcome = coordinator._terminal_claimed_outcome(
            COMMAND_ID,
            CommandState.POLICY_DENIED,
            "policy_denied",
            "replace_mismatch",
            "Replacement 1: expected exactly one match, found 0",
            0,
            "sha256:" + "b" * 64,
        )

        assert str(outcome.error_code) == "replace_mismatch"
        event = [
            item
            for item in journal.list_events(session_id=SESSION_ID, command_id=COMMAND_ID)
            if item.event_type == "command.terminal_diagnostic"
        ]
        assert len(event) == 1
        assert json.loads(event[0].payload_json or "{}") == {
            "detail": "Replacement 1: expected exactly one match, found 0",
            "error_code": "replace_mismatch",
        }
    finally:
        journal.close()


def test_multi_file_runtime_classifies_replace_mismatch_as_pre_mutation_terminal() -> None:
    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "bdb_bridge"
        / "multi_file_patch_runtime.py"
    )
    source = runtime_path.read_text(encoding="utf-8")
    anchor = "BridgeErrorCode.UNSUPPORTED_OPERATION.value,"
    anchor_index = source.index(anchor)
    classification_block = source[max(0, anchor_index - 600):anchor_index + len(anchor)]

    assert "BridgeErrorCode.REPLACE_MISMATCH.value" in classification_block
