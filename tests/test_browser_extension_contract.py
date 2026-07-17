from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_manifest_has_minimal_mv3_permissions() -> None:
    manifest = json.loads(read("manifest.json"))
    assert manifest["manifest_version"] == 3
    assert manifest["permissions"] == ["nativeMessaging", "storage"]
    assert manifest["host_permissions"] == ["https://chatgpt.com/*"]
    assert manifest["background"] == {"service_worker": "background.js"}
    assert manifest["content_scripts"][0]["world"] == "ISOLATED"
    serialized = json.dumps(manifest)
    for forbidden in ("<all_urls>", "tabs", "debugger", "webRequest", "downloads"):
        assert forbidden not in serialized


def test_manual_content_handler_remains_assisted() -> None:
    content = read("content.js")
    assert "bdb-action-v1" in content
    assert "BDB: Wykonaj" in content
    assert "Przygotuj kontynuację" in content
    start = content.index('button.addEventListener("click", async () => {')
    end = content.index("panel.append(button, output);", start)
    manual_handler = content[start:end]
    assert "button.click()" not in manual_handler
    assert "send-button" not in manual_handler
    assert "autoSend(" not in manual_handler
    assert "MutationObserver" in content
    assert "navigator.clipboard.writeText" in content


def test_manual_click_reparses_current_code_block_before_submission() -> None:
    content = read("content.js")
    start = content.index('button.addEventListener("click", async () => {')
    end = content.index("panel.append(button, output);", start)
    manual_handler = content[start:end]
    assert "const currentAction = parseAction(codeBlock);" in manual_handler
    assert 'type: "BDB_SUBMIT_ACTION", action: currentAction' in manual_handler
    assert 'type: "BDB_SUBMIT_ACTION", action }' not in manual_handler
    assert "Blok BDB zmienił się" in manual_handler


def test_background_accepts_only_versioned_actions_and_native_host() -> None:
    background = read("background.js")
    assert 'const HOST_NAME = "com.bartosz.dev_bridge"' in background
    assert 'const ACTION_SCHEMA = "bdb-action-v1"' in background
    assert 'action: "submit_action"' in background
    assert "chrome.runtime.sendNativeMessage" in background
    assert "eval(" not in background
    assert "new Function" not in background


def test_extension_contains_no_remote_scripts_or_inline_script() -> None:
    manifest = json.loads(read("manifest.json"))
    files = [path for path in EXTENSION.rglob("*") if path.is_file()]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text or path.name in {"manifest.json", "README.md"}
    popup = read("popup.html")
    assert '<script src="popup.js"></script>' in popup
    assert "<script>" not in popup
