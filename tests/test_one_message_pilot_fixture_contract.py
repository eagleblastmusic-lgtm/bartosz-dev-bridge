from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_calculator2_fixture_preserves_clean_checkout_contract() -> None:
    fixture = (ROOT / "bdb_bridge" / "one_message_pilot_fixture.py").read_text(
        encoding="utf-8"
    )

    assert 'git(fixture, "config", "core.autocrlf", "false")' in fixture
    assert '".pytest_cache/\\n__pycache__/\\n*.pyc\\n"' in fixture
    assert '"allowed_paths": ["src/calculator.py", "tests/test_calculator.py", "PILOT_RESULT.md"]' in fixture
