"""Regression tests for Telegram delivery configuration."""
import io
import os
import unittest
from unittest.mock import patch

from main import main, telegram_configuration_error, telegram_send


class TelegramDeliveryConfigurationTests(unittest.TestCase):
    def test_live_scan_stops_before_state_load_when_credentials_are_missing(self):
        with (
            patch.dict(os.environ, {
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "",
                "DRY_RUN": "",
            }),
            patch("main.read_sources", return_value=["https://example.org/feed.xml"]) as read_sources,
            patch("main.read_state") as read_state,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(main(), 2)
            read_sources.assert_called_once_with()
            read_state.assert_not_called()
            self.assertIn("TELEGRAM_BOT_TOKEN", stderr.getvalue())
            self.assertIn("TELEGRAM_CHAT_ID", stderr.getvalue())

    def test_sender_raises_instead_of_claiming_log_output_was_delivered(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "DRY_RUN": "",
        }):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN"):
                telegram_send("Example opportunity alert")

    def test_dry_run_is_allowed_without_credentials(self):
        with (
            patch.dict(os.environ, {"DRY_RUN": "true"}),
            patch("builtins.print") as print_mock,
        ):
            self.assertIsNone(telegram_configuration_error())
            telegram_send("Example dry-run alert")
            print_mock.assert_any_call("Example dry-run alert")


if __name__ == "__main__":
    unittest.main()
