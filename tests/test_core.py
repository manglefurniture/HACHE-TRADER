import concurrent.futures
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hache_trader.domain import Book, GuardError, LiveExecutor, Market, Policy, money
from hache_trader.engine import PaperEngine
from hache_trader.reports import report
from hache_trader.runtime import ProcessLock
from hache_trader.store import Store, local_day
from hache_trader.strategy import Signal, signals, entry_plan

NOW = 1793506800


def market(name="abc"):
    return Market(name+"usdt", name, "usdt", D("0.01"), D("1"), 4, 4, D("0.001"), "fixture", True)


def book(now=NOW, bid="0.999", ask="1", size="1000", name="abc", sequence=1):
    return Book(name+"usdt", now, ((D(bid), D(size)),), ((D(ask), D(size)),), sequence)


def candles(now=NOW, trend=True):
    result = []
    for i in range(30):
        price = D("0.97") + D(i)/1000 if trend else D(1)
        result.append([now-1800+i*60, str(price), str(price+D("0.001")), str(price-D("0.001")), str(price), "100"])
    return result


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.policy = Policy()
        self.store = Store(self.path/"ledger.sqlite", self.policy, NOW)
        self.store.reconcile(NOW)
        self.engine = PaperEngine(self.store, self.policy, self.path/"STOP")
        with self.store.transaction():
            s = self.store.read()
            s["last_cycle"] = NOW
            self.store.save(s, "healthy", {}, NOW)
        self.store.control("resume", NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def buy(self, ident="buy", quantity="20", now=NOW):
        return self.engine.submit(ident, market(), "buy", D(quantity), D("1"), now,
                                  stop=D("0.99"), target=D("1.02"), expected_loss=D("0.3"))

    def test_round_trip_conserves_money_and_fees(self):
        self.buy()
        self.assertEqual(self.store.read()["reserved"], "20.020")
        self.engine.match("buy", market(), book(), NOW)
        self.assertEqual(self.store.read()["realized"], "0")
        self.engine.submit("sell", market(), "sell", D(20), D("1.02"), NOW+1)
        self.engine.match("sell", market(), book(NOW+1, "1.02", "1.021", sequence=2), NOW+1)
        s = self.store.read()
        self.assertEqual(money(s["cash"]), D("50.35960"))
        self.assertEqual(money(s["fees"]), D("0.04040"))
        self.assertEqual(s["closed_trades"], 1)
        self.assertIsNone(s["position"])
        self.store.reconcile(NOW+2)

    def test_partial_fill_cancel_releases_only_remainder(self):
        self.buy()
        self.assertEqual(self.engine.match("buy", market(), book(size="50"), NOW), D(5))
        s = self.store.read()
        self.assertEqual(s["orders"]["buy"]["status"], "partial")
        self.assertEqual(money(s["reserved"]), D("15.015"))
        self.engine.cancel("buy", NOW+1)
        s = self.store.read()
        self.assertEqual(money(s["cash"]), D("44.995"))
        self.assertEqual(money(s["position"]["quantity"]), D(5))
        self.store.reconcile(NOW+2)

    def test_limit_touch_without_ask_liquidity_does_not_fill(self):
        self.buy()
        self.assertEqual(self.engine.match("buy", market(), book(bid="1", ask="1.001"), NOW), D(0))
        self.assertIsNone(self.store.read()["position"])

    def test_idempotency_and_conflicting_reuse(self):
        self.buy()
        self.buy()
        self.assertEqual(len(self.store.read()["orders"]), 1)
        with self.assertRaisesRegex(GuardError, "idempotency"):
            self.buy(quantity="19")

    def test_replayed_book_and_older_sequence_rejected(self):
        self.buy()
        self.engine.match("buy", market(), book(size="50"), NOW)
        with self.assertRaisesRegex(GuardError, "consumed"):
            self.engine.match("buy", market(), book(NOW+1, size="50"), NOW+1)

    def test_old_or_future_data_rejected(self):
        self.buy()
        for timestamp in (NOW-31, NOW+1):
            with self.assertRaises(GuardError):
                self.engine.match("buy", market(), book(timestamp), NOW)

    def test_paused_and_independent_stop_block_buy(self):
        self.store.control("pause", NOW)
        with self.assertRaisesRegex(GuardError, "blocked"):
            self.buy()
        self.store.control("resume", NOW)
        (self.path/"STOP").touch()
        with self.assertRaisesRegex(GuardError, "blocked"):
            self.buy()

    def test_emergency_does_not_claim_position_closed(self):
        self.buy()
        self.engine.match("buy", market(), book(), NOW)
        self.store.control("emergency", NOW)
        with self.assertRaises(GuardError):
            self.store.control("resume", NOW)
        self.assertIsNotNone(self.store.read()["position"])
        self.engine.manage_exit(market(), book(NOW+1, size="50", sequence=2), NOW+1)
        self.assertEqual(money(self.store.read()["position"]["quantity"]), D(15))

    def test_caps_daily_budget_and_no_averaging(self):
        with self.assertRaises(GuardError):
            self.buy(quantity="26")
        with self.store.transaction():
            s = self.store.read()
            s["day_loss"] = "1.4"
            self.store.save(s, "loss_budget_fixture", {}, NOW)
        with self.assertRaisesRegex(GuardError, "daily_loss"):
            self.buy()
        with self.store.transaction():
            s = self.store.read()
            s["day_loss"] = "0"
            self.store.save(s, "reset_fixture", {}, NOW)
        self.buy()
        self.engine.match("buy", market(), book(), NOW)
        with self.assertRaisesRegex(GuardError, "averaging"):
            self.buy("second")

    def test_restart_releases_reservation_and_requires_resume(self):
        self.buy()
        self.store.close()
        self.store = Store(self.path/"ledger.sqlite", self.policy, NOW+1)
        self.store.reconcile(NOW+1)
        s = self.store.read()
        self.assertEqual(money(s["reserved"]), 0)
        self.assertTrue(s["paused"])
        self.assertEqual(money(s["cash"]), 50)

    def test_atomic_rollback(self):
        before = self.store.read()
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                s = self.store.read()
                s["paused"] = True
                self.store.save(s, "not_committed", {}, NOW)
                raise RuntimeError("crash")
        self.assertEqual(before, self.store.read())
        self.store.reconcile(NOW)

    def test_audit_tamper_and_immutable_events(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM events")
        s = self.store.read()
        s["paused"] = True
        self.store.db.execute("UPDATE state SET body=?", (json.dumps(s),))
        with self.assertRaisesRegex(GuardError, "mismatch"):
            self.store.reconcile(NOW)

    def test_backup_restore(self):
        self.buy()
        backup = self.path/"backup.sqlite"
        self.store.backup(backup)
        restored = Store(backup, self.policy, NOW+1)
        restored.reconcile(NOW+1)
        self.assertEqual(money(restored.read()["cash"]), 50)
        restored.close()

    def test_concurrent_entry_reserves_once(self):
        def attempt(i):
            store = Store(self.path/"ledger.sqlite", self.policy, NOW)
            try:
                PaperEngine(store, self.policy, self.path/"STOP").submit(str(i), market(), "buy", D(20), D(1), NOW, stop=D("0.99"), target=D("1.02"), expected_loss=D("0.3"))
                return True
            except GuardError:
                return False
            finally:
                store.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.store.read()["orders"]), 1)

    def test_unknown_mark_is_not_zero_or_profit(self):
        self.buy()
        self.engine.match("buy", market(), book(), NOW)
        r = report(self.store, NOW)
        self.assertIsNone(r["unrealized_estimate"])
        self.assertEqual(r["realized"], "0")
        self.assertFalse(r["live_authorized"])

    def test_process_lock(self):
        with ProcessLock(self.path/"worker.lock"):
            with self.assertRaises(GuardError):
                with ProcessLock(self.path/"worker.lock"):
                    pass


class DomainTests(unittest.TestCase):
    def test_nonfinite_binary_float_and_bool_money(self):
        for invalid in (float("nan"), 0.1, True, "NaN", "Infinity", "1e1000"):
            with self.assertRaises(GuardError):
                money(invalid)

    def test_policy_protected_caps_and_live_interlock(self):
        for args in ({"mode":"real"}, {"max_order":"26"}, {"max_positions":2}, {"daily_loss":"2"}, {"participation":"1"}):
            with self.assertRaises(GuardError):
                Policy(**args)
        for method in ("submit", "cancel", "withdraw"):
            with self.assertRaises(GuardError):
                getattr(LiveExecutor(), method)()

    def test_all_usdt_pairs_supported(self):
        signal = Signal("trend", D("0.01"), D("0.002"), "fixture")
        for name in ("btc", "eth", "pearl", "arrr", "xyz"):
            plan = entry_plan(market(name), book(name=name), signal, D(20000), Policy(), NOW)
            self.assertLessEqual(plan["quantity"]*plan["price"]*D("1.001"), D(25))
            self.assertLessEqual(plan["expected_loss"], D("0.50"))

    def test_spread_volume_depth_volatility_filters(self):
        signal = Signal("trend", D("0.01"), D("0.002"), "fixture")
        for b, volume, sig in ((book(bid="0.9"), D(20000), signal), (book(), D(10), signal), (book(size="1"), D(20000), signal), (book(), D(20000), replace(signal, volatility=D("0.1")))):
            with self.assertRaises(GuardError):
                entry_plan(market(), b, sig, volume, Policy(), NOW)

    def test_candles_no_lookahead_gap_flat_and_irregular_volume(self):
        self.assertEqual(signals(candles(trend=False), NOW), [])
        self.assertEqual(signals(candles(), NOW)[0].strategy, "trend")
        bad = candles()
        bad[-1][0] = NOW+60
        with self.assertRaises(GuardError):
            signals(bad, NOW)
        bad = candles()
        del bad[-2]
        with self.assertRaises(GuardError):
            signals(bad, NOW)
        bad = candles()
        bad[-1][5] = "0"
        with self.assertRaises(GuardError):
            signals(bad, NOW)

    def test_cancun_day_boundary(self):
        self.assertEqual(local_day(1793509200-1), "2026-10-31")
        self.assertEqual(local_day(1793509200), "2026-11-01")


if __name__ == "__main__":
    unittest.main()
