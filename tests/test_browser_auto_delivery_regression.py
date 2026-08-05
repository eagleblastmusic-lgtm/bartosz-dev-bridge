from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


class BrowserAutoDeliveryRegressionTests(unittest.TestCase):
    def test_delivery_requires_confirmed_user_message(self) -> None:
        sender = (EXTENSION / "content_auto_send.js").read_text(encoding="utf-8")
        retry = (EXTENSION / "content_auto_retry.js").read_text(encoding="utf-8")

        self.assertNotIn("composer_consumed", sender)
        self.assertIn(
            'return { confirmed: true, via: "user_message" };',
            sender,
        )
        self.assertIn(
            'sent.sent && sent.confirmed === true && sent.confirmedVia === "user_message"',
            retry,
        )


if __name__ == "__main__":
    unittest.main()
