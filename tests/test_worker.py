"""Offline regression tests for autonomous paper trading. No network or funds."""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
import urllib.error

from hache_trader.safetrade import SafeTrade

from hache_trader.domain import GuardError, Policy
from hache_trader.runtime import Worker
from hache_trader.store import Store
from test_core import NOW, book, candles


class MarketStub:
    def __init__(self):
        self.now = NOW
        self.exit = False
        self.fail = False
        self.missing_market = False

    def get(self, path, query=None):
        if self.fail:
            raise GuardError("provider_unavailable")
        if path.endswith("trading_fees"):
            return [{"market_id": "any", "taker": "0.001"}]
        if path.endswith("tickers"):
            result = {"abcusdt": {"volume": "20000"}}
            if self.missing_market:
                result["badusdt"] = {"volume": "20000"}
            return result
        raise AssertionError("Unexpected endpoint")

    def markets(self):
        rows = [{
            "id": "abcusdt", "base_unit": "abc", "quote_unit": "usdt",
            "state": "enabled", "min_amount": "0.01",
            "amount_precision": 4, "price_precision": 4,
            "min_price": "0.0001", "max_price": "100000"
        }]
        if self.missing_market:
            rows.append({**rows[0], "id": "badusdt", "base_unit": "bad"})
        return rows

    def book(self, market):
        self.now += 2
        assert market == "abcusdt"
        return book(self.now,
                    "1.03" if self.exit else "0.999",
                    "1.031" if self.exit else "1",
                    sequence=self.now)

    def candles(self, market):
        if market == "badusdt":
            raise GuardError("safetrade_http_404")
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
                notifications = store.db.execute(
                    "SELECT id FROM outbox WHERE id LIKE 'paper:closed:%'").fetchall()
                self.assertEqual(len(notifications), 1)
                self.assertEqual(notifications[0]["id"], "paper:closed:1")
                # A completed closure must not also produce the generic
                # "exit evaluated" message.
                self.assertEqual(store.db.execute(
                    "SELECT count(*) FROM outbox WHERE id LIKE 'exit:%'").fetchone()[0], 0)
                self.assertEqual(sum(1 for x in store.read()["orders"]
                                     if x.startswith("entry:")), 1)
                self.assertTrue(worker.cycle())
                self.assertIsNone(store.read()["position"])
                self.assertEqual(sum(1 for x in store.read()["orders"]
                                     if x.startswith("entry:")), 1)
            finally:
                store.close()

    def test_daily_entry_cap_skips_without_pausing_and_resets_next_day(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder) / "paper.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                api = MarketStub()
                worker = Worker(store, policy, api, Path(folder) / "STOP",
                                clock=lambda: api.now)
                self.assertTrue(worker.cycle())
                store.control("resume", api.now)
                with store.transaction():
                    state = store.read()
                    for n in range(3):
                        state["orders"]["old_" + str(n)] = {
                            "side": "buy", "created": NOW,
                            "market": "abcusdt", "status": "cancelled",
                            "reserve": "0"
                        }
                    store.save(state, "three_old_entries_fixture", {}, api.now)
                self.assertTrue(worker.cycle())
                state = store.read()
                self.assertFalse(state["paused"])
                self.assertIsNone(state["last_error"])
                self.assertIsNone(state["position"])
                self.assertEqual(len(state["orders"]), 3)
                api.now += 86400
                self.assertTrue(worker.cycle())
                state = store.read()
                self.assertFalse(state["paused"])
                self.assertIsNone(state["last_error"])
                new_buys = [o for o in state["orders"].values()
                            if o["side"] == "buy" and o["created"] >= NOW+86400]
                self.assertEqual(len(new_buys), 1)
            finally:
                store.close()

    def test_missing_pair_is_skipped_without_disabling_other_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder) / "paper.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                api = MarketStub()
                api.missing_market = True
                worker = Worker(store, policy, api, Path(folder) / "STOP",
                                clock=lambda: api.now)
                self.assertTrue(worker.cycle())
                statuses = {x["market"]: x["status"]
                            for x in store.read()["markets"]}
                self.assertIn("badusdt", statuses)
                self.assertIn("safetrade_http_404", statuses["badusdt"])
                self.assertIsNone(store.read()["last_error"])
                store.control("resume", api.now)
                self.assertTrue(worker.cycle())
                self.assertIsNotNone(store.read()["position"])
            finally:
                store.close()

    def test_public_404_does_not_open_global_circuit(self):
        calls = []
        def transport(request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404,
                                         "not found", {}, None)
        api = SafeTrade(transport=transport, clock=lambda: NOW,
                        sleep=lambda _: None)
        for _ in range(2):
            with self.assertRaisesRegex(GuardError, "safetrade_http_404"):
                api.get("/trade/public/markets/abcusdt/depth")
        self.assertEqual(len(calls), 2)
        self.assertEqual(api.blocked_until, 0)

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
