from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "browser_extension" / "background_entry.js"


def test_legacy_auto_state_migration_requires_exact_tab_scoped_key() -> None:
    entry = ENTRY.read_text(encoding="utf-8")
    assert "function isLegacyAutoStateKey" in entry
    assert 'const tabIdPart = remainder.slice(0, separator);' in entry
    assert 'const storedLoopId = remainder.slice(separator + 1);' in entry
    assert '/^\\d+$/.test(tabIdPart)' in entry
    assert "storedLoopId === loopId" in entry
    assert "chrome.storage.session.remove(obsoleteKeys)" in entry
    assert "newestSafeAutoState" in entry
