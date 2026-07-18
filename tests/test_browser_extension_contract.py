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
    assert manifest["background"] == {"service_worker": "background_full_entry.js"}
    assert manifest["content_scripts"][0]["world"] == "ISOLATED"
    assert manifest["content_scripts"][0]["js"] == [
        "content.js",
        "content_rerender.js",
        "content_auto_send.js",
    ]
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


def test_workspace_context_uses_native_context_without_new_permissions() -> None:
    background = read("background.js")
    content = read("content.js")
    css = read("content.css")
    assert 'const WORKSPACE_CONTEXT_OPERATION = "workspace_context"' in background
    assert 'action: "context"' in background
    assert "return await workspaceContext(action);" in background
    assert 'presentation.mode === "compact"' in content
    assert 'codeBlock.classList.add("bdb-action-source-hidden")' in content
    assert ".bdb-action-source-hidden" in css
    assert 'status: "completed"' in background
    assert 'operation: WORKSPACE_CONTEXT_OPERATION' in background


def test_required_promotion_blocks_auto_until_receipt_matches_command() -> None:
    background = read("background.js")
    assert 'promotion.mode === "required"' in background
    assert "waitForRequiredPromotion(action, response)" in background
    assert "context.latest_promotion" in background
    assert "receipt.command_id === commandId" in background
    assert "context.source_clean === true" in background
    assert 'reason: "promotion_not_observed"' in background
    assert 'status: "needs_user"' in background
    assert "PROMOTION_WAIT_ATTEMPTS" in background


def test_auto_continues_only_after_verified_rollback_profile_failure() -> None:
    background = read("background.js")
    assert "continue_on_failure" in background
    assert "isRecoverableProfileFailure" in background
    assert 'result.status === "failed" || result.status === "timeout"' in background
    assert 'data.operation === "multi_file_patch"' in background
    assert "data.rollback_performed === true" in background
    assert 'data.checkpoint_state === "rolled_back"' in background
    assert "const terminal = recoverableFailure ? null" in background
    assert "recoverableFailure," in background


def test_auto_remains_bounded_and_explicitly_opt_in() -> None:
    background = read("background.js")
    content = read("content.js")
    assert "autoEnabled: false" in background
    assert "autoMaxIterations: 4" in background
    assert "autoMaxMinutes: 10" in background
    assert "metadata.iteration > settings.autoMaxIterations" in background
    assert "now - state.startedAt > settings.autoMaxMinutes" in background
    assert 'automation.mode !== "auto"' in content
    assert "BDB_CONSIDER_AUTO" in content
    assert "BDB_AUTO_RESULT" in content


def test_auto_entry_synchronizes_loop_state_without_weakening_replay_guard() -> None:
    entry = read("background_entry.js")
    full_entry = read("background_full_entry.js")
    background = read("background.js")
    polling = read("background_async_result.js")
    popup = read("popup.js")
    assert 'importScripts("background.js")' in entry
    assert 'importScripts("background_entry.js", "background_async_result.js")' in full_entry
    assert "canonicalAutoStateKey" in entry
    assert "chrome.storage.session.get(null)" in entry
    assert "legacyAutoStateEntries" in entry
    assert 'reason = "iteration_already_processed"' in entry
    assert "expectedIteration" in entry
    assert "claimAutoReplay" in background
    assert "AUTO_REPLAY_GUARD_KEY" in background
    assert "claimAutoReplay =" not in entry
    assert 'action: "result"' in polling
    assert "waitForRequiredPromotion(action, latest)" in polling
    assert "BDB_ASYNC_RESULT_ATTEMPTS" in polling
    assert "Oczekiwana iteracja" in popup


def test_auto_send_requires_observed_composer_consumption() -> None:
    companion = read("content_auto_send.js")
    assert "BDB_AUTO_SEND_MAX_CLICKS" in companion
    assert "bdbWaitForComposerConsumption" in companion
    assert "send_not_confirmed" in companion
    assert "current.button.click()" in companion
    assert "markerStillPresent" in companion
    assert "return { sent: true" not in companion.split("current.button.click();", 1)[1].split("if (await", 1)[0]


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
