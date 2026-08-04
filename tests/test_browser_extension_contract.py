from __future__ import annotations

import runpy
import unittest
from pathlib import Path


_LEGACY = runpy.run_path(
    str(Path(__file__).with_name("_browser_extension_contract_legacy.py"))
)
for _name, _value in _LEGACY.items():
    if _name.startswith("test_") and callable(_value):
        globals()[_name] = _value

read = _LEGACY["read"]


def test_auto_continues_only_after_verified_rollback_profile_failure() -> None:
    background = read("background.js")
    assert "continue_on_failure" in background
    assert "isRecoverableProfileFailure" in background
    assert 'result.status === "failed" || result.status === "timeout"' in background
    assert 'data.operation === "multi_file_patch"' in background
    assert "data.rollback_performed === true" in background
    assert 'data.checkpoint_state === "rolled_back"' in background
    assert (
        "recoverableFailure || recoverableReadFailure || recoverableNativeError"
        in background
    )
    assert 'response.error.code === "internal_error"' in background
    assert "recoverableFailure," in background


def test_auto_send_requires_confirmed_multi_strategy_submission() -> None:
    companion = read("content_auto_send.js")
    retry = read("content_auto_retry.js")
    for expected in (
        "BDB_AUTO_SEND_STRATEGIES",
        '"button_click"',
        '"request_submit"',
        '"enter_key"',
        "bdbWaitForSendConfirmation",
        "bdbUserMessageContains",
        "form.requestSubmit",
        'new KeyboardEvent("keydown"',
        "send_not_confirmed",
        "markerStillPresent",
        "confirmedVia",
    ):
        assert expected in companion
    assert (
        'sent.sent && sent.confirmed === true && sent.confirmedVia === "user_message"'
        in retry
    )


class BrowserExtensionUpdatedContractTests(unittest.TestCase):
    def test_updated_auto_contracts(self) -> None:
        test_auto_continues_only_after_verified_rollback_profile_failure()
        test_auto_send_requires_confirmed_multi_strategy_submission()


if __name__ == "__main__":
    unittest.main()
