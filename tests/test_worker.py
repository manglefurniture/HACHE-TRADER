"""Offline regression tests for autonomous paper trading. No network or funds."""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from hache_trader.domain import GuardError, Policy
from hache_trader.runtime import Worker
from hache_trader.store import Store
from test_core import NOW, book, candles


class MarketStub:
    def __init__(self):
        self.now = NOW
        self.exit = False
        self.fail = False

    def get(self, path, query=None):
        if self.fail:
            raise GuardError("provider_unavailable")
        if path.endswith("trading_fees"):
            return [{"market_id": "any", "taker": "0.001"}]
        if path.endswith("tickers"):
            return {"abcusdt": {"volume": "20000"}}
        raise AssertionError("Unexpected endpoint")

    def markets(self):
        return [{
            "id": "abcusdt", "base_unit": "abc", "quote_unit": "usdt",
            "state": "enabled", "min_amount": "0.01",
            "amount_precision": 4, "price_precision": 4,
            "min_price": "0.0001", "max_price": "100000"
        }]

    def book(self, market):
        self.now += 2
        assert market == "abcusdt"
        return book(self.now,
                    "1.03" if self.exit else "0.999",
                    "1.031" if self.exit else "1",
                    sequence=self.now)

    def candles(self, market):
        return candles(self.now)

    def ticker(self, market):
        return self.now, Decimal("20000")


class WorkerTests(unittest.TestCase):
    def test_exit_does_not_reenter_or_reuse_entry_in_same_minute(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder) / "paper.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                api = MarketStub()
                worker = Worker(store, policy, api, Path(folder) / "STOP",
                                clock=lambda: api.now)
                self.assertTrue(worker.cycle())
                self.assertIsNone(store.read()["position"])
                store.control("resume", api.now)
                self.assertTrue(worker.cycle())
                self.assertIsNotNone(store.read()["position"])
                opened = sum(1 for x in store.read()["orders"]
                             if x.startswith("entry:"))
                self.assertEqual(opened, 1)
                api.exit = True
                self.assertTrue(worker.cycle())
                self.assertIsNone(store.read()["position"])
                self.assertEqual(store.read()["closed_trades"], 1)
                self.assertEqual(sum(1 for x in store.read()["orders"]
                                     if x.startswith("entry:")), 1)
                self.assertTrue(worker.cycle())
                self.assertIsNone(store.read()["position"])
                self.assertEqual(sum(1 for x in store.read()["orders"]
                                     if x.startswith("entry:")), 1)
            finally:
                store.close()

    def test_exchange_failure_blocks_new_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder) / "paper.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                api = MarketStub()
                worker = Worker(store, policy, api, Path(folder) / "STOP",
                                clock=lambda: api.now)
                api.fail = True
                self.assertFalse(worker.cycle())
                self.assertTrue(store.read()["paused"])
                self.assertIsNotNone(store.read()["last_error"])
                self.assertIsNone(store.read()["position"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
