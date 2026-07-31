from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_assisted_async_polling_keeps_same_command_long_enough() -> None:
    source = (
        EXTENSION / "background_async_result.js"
    ).read_text(encoding="utf-8")

    match = re.search(
        r"const BDB_ASYNC_RESULT_ATTEMPTS = ([0-9]+);",
        source,
    )

    assert match is not None
    assert int(match.group(1)) >= 8

    assert 'action: "result"' in source
    assert "session_id: parsed.sessionId" in source
    assert "sequence: parsed.sequence" in source
    assert "return waitForRequiredPromotion(action, latest);" in source

    loop_position = source.index(
        "for (let attempt = 0; "
        "attempt < BDB_ASYNC_RESULT_ATTEMPTS;"
    )
    exhausted_position = source.index(
        "async_poll_exhausted: true"
    )

    assert exhausted_position > loop_position


def test_required_promotion_accepts_exact_replacement_results() -> None:
    source = (
        EXTENSION / "background.js"
    ).read_text(encoding="utf-8")

    assert 'dataOperation === "multi_file_patch"' in source
    assert (
        'dataOperation === "replace_exact_and_test"'
        in source
    )
    assert "const legacyExactResult = Boolean(" in source
    assert '"Command effect recorded"' in source
    assert "receipt.command_id === commandId" in source
    assert "Array.isArray(context.source_changes)" in source
    assert "context.source_changes.length === 0" in source



def test_required_promotion_wait_budget_is_at_least_30_seconds() -> None:
    source = (
        EXTENSION / "background.js"
    ).read_text(encoding="utf-8")

    attempts_match = re.search(
        r"const PROMOTION_WAIT_ATTEMPTS = ([0-9]+);",
        source,
    )

    milliseconds_match = re.search(
        r"const PROMOTION_WAIT_MILLISECONDS = ([0-9]+);",
        source,
    )

    assert attempts_match is not None
    assert milliseconds_match is not None

    attempts = int(attempts_match.group(1))
    milliseconds = int(milliseconds_match.group(1))

    assert attempts * milliseconds >= 30_000

    assert (
        "attempt < PROMOTION_WAIT_ATTEMPTS"
        in source
    )

    assert (
        "await sleep(PROMOTION_WAIT_MILLISECONDS);"
        in source
    )
