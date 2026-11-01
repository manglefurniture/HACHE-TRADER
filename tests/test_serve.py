"""Ensure the Telegram control service cannot start a trading market worker."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import unittest
from unittest.mock import patch


class StopAfterFirstPoll(Exception):
    pass


class TelegramServeTests(unittest.TestCase):
    def test_serve_requires_telegram_and_never_creates_market_worker(self):
        from hache_trader.__main__ import main
        with TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["hache_trader", "serve", "--state-dir", directory]):
                with self.assertRaisesRegex(Exception, "telegram_required"):
                    main()

        with TemporaryDirectory() as directory:
            env = {
                "HACHE_TRADER_BOT_ID": "9297293373",
                "HACHE_TRADER_CHAT_ID": "5074130395",
                "HACHE_TRADER_USER_ID": "5074130395",
                "CREDENTIALS_DIRECTORY": "/dev/null/hache-trader-test",
            }

            def telegram_result(method, data):
                if method == "getWebhookInfo":
                    return {"url": ""}
                if method == "getMe":
                    return {"id": 9297293373}
                raise AssertionError("Unexpected Telegram call: " + method)

            with patch.dict(os.environ, env), \
                    patch.object(sys, "argv", ["hache_trader", "serve", "--telegram", "--state-dir", directory]), \
                    patch("hache_trader.__main__.read_secret", return_value="000000:fake_only"), \
                    patch("hache_trader.__main__.TelegramAPI") as api, \
                    patch("hache_trader.__main__.Worker") as worker, \
                    patch("hache_trader.__main__.SafeTrade") as market, \
                    patch("hache_trader.__main__.Channel") as channel:
                api.return_value.call.side_effect = telegram_result
                channel.return_value.poll.side_effect = StopAfterFirstPoll
                with self.assertRaises(StopAfterFirstPoll):
                    main()
                channel.return_value.poll.assert_called_once()
                worker.assert_not_called()
                market.assert_not_called()


if __name__ == "__main__":
    unittest.main()
