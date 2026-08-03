from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_task_controller_is_last_background_wrapper() -> None:
    entry = read("background_full_entry.js")
    assert '"background_task_controller.js"' in entry
    assert entry.index('"background_task_controller.js"') > entry.index(
        '"background_conversation_binding.js"'
    )


def test_task_controller_has_bounded_local_contracts() -> None:
    controller = read("background_task_controller.js")
    assert 'const BDB_TASK_MAX_LEDGER = 64' in controller
    assert 'const BDB_TASK_MAX_DIAGNOSTICS = 200' in controller
    assert 'const BDB_TASK_MAX_CHECKPOINTS = 16' in controller
    assert 'const BDB_TASK_MAX_CACHE_ENTRIES = 32' in controller
    assert "bdbTaskNormalizeLoopId" in controller
    assert 'reason: "high_risk_requires_assisted"' in controller
    assert 'schema: "bdb-acceptance-result-v1"' in controller
    assert 'status: needsVisualConfirmation ? "needs_confirmation"' in controller
    assert '"await_user_visual_feedback"' in controller
    assert "bdbTaskResumeAfterVisualFeedback" in controller
    assert 'event: "visual_feedback_resumed"' in controller
    assert 'metadata.continue_after_user_feedback !== true' in controller
    assert "feedbackContinuation" in controller
    assert "bdbTaskCheckpointRestore" in controller
    assert "bdbTaskLatestPendingCheckpoint" in controller
    assert 'status: "recovering_result"' in controller
    assert 'event: "task_result_recovery_requested"' in controller
    assert 'source_code_included: false' in controller
    assert 'credentials_included: false' in controller


def test_popup_exposes_health_tasks_shadow_and_explicit_self_test() -> None:
    popup = read("popup.html")
    script = read("popup.js")
    assert 'id="auto-shadow"' in popup
    assert 'id="test-auto"' in popup
    assert 'id="task-state"' in popup
    assert 'id="export-diagnostics"' in popup
    assert "window.confirm" in script
    assert 'type: "BDB_CONTENT_SELF_TEST"' in script
    assert 'type: "BDB_AUTO_DIAGNOSTICS"' in script


def test_content_build_version_detects_stale_chatgpt_tab() -> None:
    manifest = json.loads(read("manifest.json"))
    health = read("content_health.js")
    assert f'const BDB_CONTENT_BUILD_VERSION = "{manifest["version"]}"' in health
    assert 'type: "BDB_HEALTH"' in health
    assert "content_version_match" in health
    assert "Przeładuj kartę" in health
    assert 'type: "BDB_MARK_AUTO_RESULT_DELIVERED"' in health


def test_diagnostics_zip_contains_one_sanitized_json_artifact() -> None:
    exporter = read("popup_diagnostics.js")
    assert "bdbZipSingleFile" in exporter
    assert 'bdbZipSingleFile("bdb-diagnostics.json"' in exporter
    assert 'type: "application/zip"' in exporter
