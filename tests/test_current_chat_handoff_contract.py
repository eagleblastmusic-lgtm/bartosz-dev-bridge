from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"
GUI = ROOT / "bdb_gui"


def test_project_launcher_requires_visible_focused_conversation() -> None:
    content = (EXTENSION / "content_project_launcher.js").read_text(encoding="utf-8")

    assert "function bdbProjectConversationIsActive()" in content
    assert 'document.visibilityState === "visible"' in content
    assert "document.hasFocus()" in content
    assert "bdbProjectConversationId()" in content
    assert "if (!bdbProjectConversationIsActive())" in content


def test_control_center_does_not_open_a_new_chatgpt_tab() -> None:
    window = (GUI / "project_window.py").read_text(encoding="utf-8")

    assert "ProjectCreatorService(browser_opener=lambda _url: True)" in window
    assert "Prompt oczekuje w aktywnej rozmowie ChatGPT" in window
