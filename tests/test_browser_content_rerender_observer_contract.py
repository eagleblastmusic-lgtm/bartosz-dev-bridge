from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RERENDER = ROOT / "browser_extension" / "content_rerender.js"


def test_removed_panel_observer_rescans_only_live_element_targets() -> None:
    script = RERENDER.read_text(encoding="utf-8")
    assert "const bdbRemovedPanelObserver = new MutationObserver" in script
    assert 'record.type === "childList"' in script
    assert "record.removedNodes.length > 0" in script
    assert "record.target instanceof HTMLElement" in script
    assert "scan(record.target)" in script
    assert "bdbRemovedPanelObserver.observe(document.documentElement" in script
    assert "childList: true" in script
    assert "subtree: true" in script
    assert "chrome.runtime.sendMessage" not in script
