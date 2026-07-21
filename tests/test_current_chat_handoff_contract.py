from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"
GUI = ROOT / "bdb_gui"
OPERATOR = ROOT / "bdb_operator"


def test_project_launcher_requires_visible_focused_conversation() -> None:
    content = (EXTENSION / "content_project_launcher.js").read_text(encoding="utf-8")

    assert "function bdbProjectConversationIsActive()" in content
    assert 'document.visibilityState === "visible"' in content
    assert "document.hasFocus()" in content
    assert "bdbProjectConversationId()" in content
    assert "if (!bdbProjectConversationIsActive())" in content


def test_project_launcher_persists_conversation_repo_and_launch_binding() -> None:
    content = (EXTENSION / "content_project_launcher.js").read_text(encoding="utf-8")
    background = (EXTENSION / "background_conversation_binding.js").read_text(encoding="utf-8")
    entry = (EXTENSION / "background_full_entry.js").read_text(encoding="utf-8")

    assert "bdbConversationBindingsV1" in content
    assert "bdbBindProjectConversation" in content
    assert "conversation_id: conversationId" in content
    assert "repo_alias: launch.repo_alias" in content
    assert "launch_id: launch.launch_id" in content
    assert "bdbConversationBindingsV1" in background
    assert "session_id: bdbBindingSessionId(commandId)" in background
    assert "command_id: commandId" in background
    assert '"background_conversation_binding.js"' in entry


def test_control_center_and_creator_service_do_not_open_new_chatgpt_tab() -> None:
    window = (GUI / "project_window.py").read_text(encoding="utf-8")
    hardening = (OPERATOR / "project_creator_hardening.py").read_text(encoding="utf-8")

    assert "ProjectCreatorService(" in window
    assert "browser_opener=lambda _url: True" in window
    assert "Prompt oczekuje w aktywnej rozmowie ChatGPT" in window
    assert 'kwargs["browser_opener"] = lambda _url: True' in hardening
    assert "chatgpt_active_conversation_waiting" in hardening
