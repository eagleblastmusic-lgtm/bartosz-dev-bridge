from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_auto_is_disabled_by_default_and_has_bounded_limits() -> None:
    background = read("background.js")
    assert "autoEnabled: false" in background
    assert "autoMaxIterations: 4" in background
    assert "autoMaxMinutes: 10" in background
    assert "iterations < 1 || iterations > 8" in background
    assert "minutes < 1 || minutes > 30" in background
    assert "chrome.storage.session" in background
    assert "non_sequential_iteration" in background


def test_auto_requires_action_metadata_and_hard_stops() -> None:
    background = read("background.js")
    assert 'metadata.mode !== "auto"' in background
    assert "loop_id" in background
    for terminal in (
        "done",
        "needs_user",
        "policy_denied",
        "manual_reconciliation_required",
        "failed",
        "cancelled",
        "aborted",
    ):
        assert terminal in background


def test_auto_submit_uses_exact_dom_guard_and_assisted_fallback() -> None:
    content = read("content.js")
    assert "BDB_AUTO_RESULT:" in content
    assert "requireEmpty: true" in content
    assert 'button[data-testid=\'send-button\']' in content
    assert "composer.closest(\"form\")" in content
    assert "button.click()" in content
    assert "AUTO → ASSISTED" in content
    assert "aria-label" not in content


def test_popup_exposes_explicit_auto_opt_in() -> None:
    popup = read("popup.html")
    script = read("popup.js")
    assert 'id="auto-enabled"' in popup
    assert 'id="auto-iterations"' in popup
    assert 'id="auto-minutes"' in popup
    assert "BDB_SET_AUTO_SETTINGS" in script
