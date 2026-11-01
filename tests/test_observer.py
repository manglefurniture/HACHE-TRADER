"""Offline observer regression tests: no execution or funds."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import os
import sys

from hache_trader.domain import GuardError, Policy
from hache_trader.observer import MarketObserver
from hache_trader.store import Store
from test_core import NOW, book, candles


class MarketStub:
    def __init__(self, missing=False, failure=False):
        self.missing, self.failure = missing, failure
        self.calls = []

    def get(self, path, query=None):
        self.calls.append(path)
        if self.failure and path.endswith("trading_fees"):
            raise GuardError("safetrade_http_429")
        if path.endswith("trading_fees"):
            return [{"market_id": "any", "taker": "0.001"}]
        if path.endswith("tickers"):
            return {x: {"volume": "25000"} for x in ("abcusdt", "defusdt")}
        raise AssertionError(path)

    def markets(self):
        self.calls.append("markets")
        return [{
            "id": x+"usdt", "base_unit": x, "quote_unit": "usdt",
            "state": "enabled", "min_amount": "0.01",
            "amount_precision": 4, "price_precision": 4,
            "min_price": "0.0001", "max_price": "100000"
        } for x in ("abc", "def")]

    def candles(self, market):
        self.calls.append("candles:" + market)
        if self.missing and market == "abcusdt":
            raise GuardError("safetrade_http_404")
        return candles(NOW)

    def book(self, market):
        self.calls.append("depth:" + market)
        return book(NOW, name=market.removesuffix("usdt"))


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.policy = Policy()
        self.store = Store(Path(self.temp.name)/"ledger.sqlite", self.policy, NOW)
        self.store.reconcile(NOW)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_only_market_data_and_rotating_read_only_state(self):
        api = MarketStub()
        obs = MarketObserver(self.store, self.policy, api,
                             clock=lambda: NOW, batch_size=1)
        before = self.store.read()
        self.assertTrue(obs.cycle())
        first = self.store.read()
        self.assertEqual([x["market"] for x in first["markets"]], ["abcusdt"])
        self.assertTrue(first["paused"])
        self.assertEqual(first["cash"], before["cash"])
        self.assertEqual(first["orders"], {})
        self.assertIsNone(first["position"])
        self.assertEqual(first["realized"], before["realized"])
        self.assertTrue(obs.cycle())
        self.assertEqual([x["market"] for x in self.store.read()["markets"]], ["defusdt"])
        self.assertFalse(any("/trade/market/orders" in call for call in api.calls))

    def test_missing_individual_market_does_not_pause_other_scans(self):
        api = MarketStub(missing=True)
        obs = MarketObserver(self.store, self.policy, api,
                             clock=lambda: NOW, batch_size=2)
        self.assertTrue(obs.cycle())
        data = self.store.read()
        self.assertEqual(len(data["markets"]), 2)
        self.assertIn("safetrade_http_404", data["markets"][0]["status"])
        self.assertIsNone(data["last_error"])
        self.assertEqual(data["observer_state"], "solo lectura")

    def test_exchange_outage_preserves_capital_and_blocks_entries(self):
        api = MarketStub(failure=True)
        obs = MarketObserver(self.store, self.policy, api, clock=lambda: NOW)
        self.assertFalse(obs.cycle())
        data = self.store.read()
        self.assertTrue(data["paused"])
        self.assertEqual(data["last_error"], "observer_market_data_unavailable")
        self.assertEqual(data["cash"], "50")
        self.assertEqual(data["orders"], {})

    def test_observe_cli_never_creates_worker(self):
        from hache_trader.__main__ import main
        class StopAfterFirstPoll(Exception):
            pass

        env = {
            "HACHE_TRADER_BOT_ID": "1234567890",
            "HACHE_TRADER_CHAT_ID": "424242",
            "HACHE_TRADER_USER_ID": "424242",
            "CREDENTIALS_DIRECTORY": "/dev/null/hache-trader-test",
        }

        def telegram_result(method, data):
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getMe":
                return {"id": 1234567890}
            raise AssertionError(method)

        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, env), \
                    patch.object(sys, "argv", ["hache_trader", "observe", "--telegram", "--state-dir", directory]), \
                    patch("hache_trader.__main__.read_secret", return_value="000000:fake_only"), \
                    patch("hache_trader.__main__.TelegramAPI") as api, \
                    patch("hache_trader.__main__.Worker") as worker, \
                    patch("hache_trader.__main__.MarketObserver") as observer, \
                    patch("hache_trader.__main__.Channel") as channel:
                api.return_value.call.side_effect = telegram_result
                channel.return_value.poll.side_effect = StopAfterFirstPoll
                with self.assertRaises(StopAfterFirstPoll):
                    main()
                worker.assert_not_called()
                observer.assert_called_once()
                observer.return_value.cycle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
