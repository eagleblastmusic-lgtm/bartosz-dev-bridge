from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "vnext_chatgpt_dom_delivery_recovery.cjs"
ADAPTER = ROOT / "browser_extension_vnext" / "content_adapter.js"


def _run(mode: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser DOM recovery contract")
    completed = subprocess.run(
        [node, str(FIXTURE), mode, str(ADAPTER)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "mode",
    ["proof-normal", "proof-attribute", "proof-show-more", "proof-hidden-body"],
    ids=["legacy-user-message", "collapsed-canonical-content", "show-more-control-filtered", "hidden-canonical-content"],
)
def test_exact_send_effect_accepts_complete_current_conversation_message(mode: str) -> None:
    _run(mode)


@pytest.mark.parametrize(
    "mode",
    ["proof-altered", "proof-prefix", "proof-wrong-conversation", "proof-nonempty", "proof-old-identical"],
    ids=["altered-content", "prefix-only", "wrong-conversation", "composer-not-empty", "preexisting-message-not-a-new-send"],
)
def test_exact_send_effect_fails_closed_without_full_exact_proof(mode: str) -> None:
    _run(mode)


def test_send_attempted_collapsed_prompt_recovers_without_a_second_send() -> None:
    _run("recovery-collapsed")


def test_uncertain_send_attempt_remains_duplicate_blocked() -> None:
    _run("recovery-uncertain")


@pytest.mark.parametrize(
    "mode",
    ["legacy", "interactive", "initial-scan", "sweep"],
    ids=["legacy-pre-code", "interactive-json-container", "initial-page-scan", "periodic-sweep"],
)
def test_complete_result_is_found_in_bounded_assistant_content(mode: str) -> None:
    _run(mode)


@pytest.mark.parametrize(
    "mode",
    ["partial", "prose-json", "malformed", "wrong-schema", "oversized", "assistant-prose", "unrelated-page-text"],
    ids=["streaming-partial", "prose-around-json", "malformed-json", "wrong-schema", "oversized-result", "schema-in-prose", "schema-outside-assistant"],
)
def test_noncanonical_or_unbounded_result_text_is_ignored(mode: str) -> None:
    _run(mode)


@pytest.mark.parametrize(
    "mode",
    ["rerender", "wrong-binding", "wrong-conversation", "active"],
    ids=["rerender-at-most-once", "wrong-binding-rejected", "wrong-conversation-rejected", "exact-active-binding-submitted"],
)
def test_auto_result_path_preserves_identity_and_dedupe(mode: str) -> None:
    _run(mode)


def test_failed_live_combination_recovers_send_and_submits_existing_result() -> None:
    _run("combined")
