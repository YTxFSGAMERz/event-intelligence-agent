import os
import unittest
from unittest.mock import patch

import main


class TelegramDeliveryTests(unittest.TestCase):
    def test_missing_secrets_raise_instead_of_silently_accepting_alert(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "DRY_RUN": "",
        }):
            with self.assertRaisesRegex(RuntimeError, "Telegram secrets not configured"):
                main.telegram_send("Test alert")

    def test_dry_run_still_allows_preview_without_secrets(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "DRY_RUN": "true",
        }):
            with patch.object(main, "telegram_api_call") as api_call:
                main.telegram_send("Test alert")
            api_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
