"""Native SafeTrade timeframes and deliberately bounded paper learning samples."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hache_trader.domain import GuardError, Policy, money
from hache_trader.runtime import Worker
from hache_trader.safetrade import SafeTrade
from hache_trader.store import Store
from hache_trader.strategy import signals, study_signals, entry_plan
from test_core import NOW
from test_worker import MarketStub


def bars(now, period, *, study=False, zero=False):
    prices = [Decimal("1") for _ in range(21)]
    if study:
        prices[-2:] = [Decimal("0.994"), Decimal("0.996")]
    else:
        prices = [Decimal("1") + Decimal(i) / 1000 for i in range(21)]
    start = (now // period) * period - 21 * period
    result = []
    for i, close in enumerate(prices):
        opening = prices[i - 1] if i else close
        result.append([
            start + i * period, str(opening),
            str(max(opening, close) * Decimal("1.001")),
            str(min(opening, close) * Decimal("0.999")),
            str(close), "0" if zero else "100",
        ])
    return result


class ProbeMarket(MarketStub):
    def __init__(self, *, valid_period=5):
        super().__init__()
        self.valid_period = valid_period
        self.candle_calls = []

    def candles(self, market, *, period=1):
        self.candle_calls.append(period)
        if period != self.valid_period:
            raise GuardError("insufficient_candles")
        return bars(self.now, period*60, study=True)


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = Policy()
        self.store = Store(self.root/"ledger.sqlite", self.policy, NOW)
        self.addCleanup(self.store.close)
        self.store.reconcile(NOW)

    def test_native_public_request_intervals_and_reject_invalid(self):
        requests = []
        class StubPublic(SafeTrade):
            def get(self, path, query=None):
                requests.append((path, query))
                return []
        api = StubPublic(clock=lambda: NOW)
        for period, limit in ((1,30),(5,40),(15,40)):
            self.assertEqual(api.candles("abcusdt",period=period), [])
            path, query = requests[-1]
            self.assertTrue(path.endswith("/abcusdt/k-line"))
            self.assertEqual(query["period"], period)
            self.assertEqual(query["limit"], limit)
            self.assertEqual(query["time_to"]-query["time_from"], period*60*limit)
        for invalid in (0,2,30,True,"5"):
            with self.subTest(period=invalid), self.assertRaisesRegex(GuardError, "unsupported_candle_interval"):
                api.candles("abcusdt", period=invalid)
        self.assertEqual(len(requests), 3)

    def test_no_synthetic_bars_and_strict_closed_validation(self):
        for p in (300,900):
            samples=bars(NOW,p,study=True)
            self.assertEqual(signals(samples,NOW,period=p), [])
            choice=study_signals(samples,NOW,p)
            self.assertEqual(len(choice),1)
            self.assertEqual(choice[0].strategy,"study_probe")
            with self.assertRaisesRegex(GuardError,"irregular_volume"):
                study_signals(bars(NOW,p,study=True,zero=True),NOW,p)
            with self.assertRaisesRegex(GuardError,"candle_gap_or_duplicate"):
                gap=bars(NOW,p,study=True)
                gap[11][0]+=p
                study_signals(gap,NOW,p)
            with self.assertRaisesRegex(GuardError,"insufficient_candles"):
                study_signals(samples[:10],NOW,p)
        self.assertEqual(study_signals(bars(NOW,60,study=True),NOW,60),[])

    def test_worker_falls_back_to_five_min_and_opens_small_paper_position(self):
        api=ProbeMarket(valid_period=5)
        worker=Worker(self.store,self.policy,api,self.root/"STOP",clock=lambda:api.now)
        self.assertTrue(worker.cycle())
        self.assertEqual(api.candle_calls,[1,5])
        self.assertEqual(self.store.read()["orders"],{})
        self.store.control("resume",api.now)
        self.assertTrue(worker.cycle())
        buys=[(k,v) for k,v in self.store.read()["orders"].items() if v["side"]=="buy"]
        self.assertEqual(len(buys),1)
        ident,order=buys[0]
        self.assertTrue(ident.endswith(":study_probe"))
        self.assertLessEqual(money(order["quantity"])*money(order["price"]),Decimal("10"))
        self.assertIsNotNone(self.store.read()["position"])
        self.assertEqual(self.store.read()["closed_trades"],0)
        self.assertEqual(self.store.db.execute(
            "SELECT decision FROM scan_diagnostics ORDER BY id DESC LIMIT 1").fetchone()[0],
                         "entry_filled")
        self.assertTrue(any("5m" in x["status"] for x in self.store.read()["markets"]))

    def test_study_probe_strict_one_per_local_day(self):
        api=ProbeMarket()
        worker=Worker(self.store,self.policy,api,self.root/"STOP",clock=lambda:api.now)
        worker.cycle()
        self.store.control("resume",api.now)
        with self.store.transaction():
            s=self.store.read()
            s["orders"]["entry:abcusdt:123:study_probe"]={
                "side":"buy","created":NOW,"market":"abcusdt","status":"cancelled","reserve":"0"
            }
            self.store.save(s,"existing_probe_fixture",{},api.now)
        self.assertTrue(worker.cycle())
        self.assertEqual(len(self.store.read()["orders"]),1)
        self.assertIsNone(self.store.read()["position"])
        self.assertEqual(self.store.db.execute(
            "SELECT decision FROM scan_diagnostics ORDER BY id DESC LIMIT 1").fetchone()[0],
                         "study_daily_limit")

    def test_fifteen_min_fallback_and_nonrecoverable_errors(self):
        api=ProbeMarket(valid_period=15)
        worker=Worker(self.store,self.policy,api,self.root/"STOP",clock=lambda:api.now)
        self.assertTrue(worker.cycle())
        self.assertEqual(api.candle_calls,[1,5,15])
        self.assertIn("15m", self.store.read()["markets"][0]["status"])
        class Fatal(ProbeMarket):
            def candles(self,market,*,period=1):
                raise GuardError("future_candle")
        api=Fatal()
        worker=Worker(self.store,self.policy,api,self.root/"STOP",clock=lambda:api.now)
        self.assertTrue(worker.cycle())
        self.assertIn("future_candle", self.store.read()["markets"][0]["status"])
        self.assertEqual(self.store.read()["orders"], {})
        self.assertIsNone(self.store.read()["last_error"])


if __name__ == "__main__":
    unittest.main()
