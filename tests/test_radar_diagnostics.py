"""Offline paper radar diagnostics: counts, decisions, safety and persistence."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from hache_trader.domain import Policy, GuardError
from hache_trader.reports import render
from hache_trader.runtime import Worker
from hache_trader.scan_diagnostics import append_scan, render_diagnostics
from hache_trader.store import Store
from hache_trader.telegram import menu
from test_core import NOW, candles
from test_worker import MarketStub


class RadarDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = Policy()
        self.store = Store(self.root / "ledger.sqlite", self.policy, NOW)
        self.addCleanup(self.store.close)
        self.store.reconcile(NOW)
        self.api = MarketStub()
        self.worker = Worker(self.store, self.policy, self.api, self.root / "STOP",
                             clock=lambda: self.api.now)

    def scan_rows(self):
        return self.store.db.execute(
            "SELECT universe,eligible,checked,signal_markets,qualified,reasons,decision "
            "FROM scan_diagnostics ORDER BY id").fetchall()

    def test_paused_cycle_and_filled_cycle_diagnostics(self):
        self.assertTrue(self.worker.cycle())
        row = self.scan_rows()[-1]
        self.assertEqual(tuple(row)[:5], (1, 1, 1, 1, 1))
        self.assertEqual(row["decision"], "paused")
        self.assertEqual(json.loads(row["reasons"]), {"qualified": 1})
        output = render(self.store, "diagnostico", self.api.now,
                        self.policy, runtime_mode="trial")
        self.assertIn("simulación pausada", output)
        self.assertIn("no mercados únicos", output)
        self.assertEqual(self.store.read()["orders"], {})
        self.store.control("resume", self.api.now)
        self.assertTrue(self.worker.cycle())
        self.assertEqual(self.scan_rows()[-1]["decision"], "entry_filled")
        self.assertIsNotNone(self.store.read()["position"])
        self.store.reconcile(self.api.now)

    def test_candle_failure_and_no_signal_are_distinct(self):
        class NoCandles(MarketStub):
            def candles(self, market):
                raise GuardError("insufficient_candles")
        self.api = NoCandles()
        self.worker = Worker(self.store, self.policy, self.api,
                             self.root / "STOP", clock=lambda: self.api.now)
        self.assertTrue(self.worker.cycle())
        row = self.scan_rows()[-1]
        self.assertEqual((row["signal_markets"], row["qualified"]), (0, 0))
        self.assertEqual(row["decision"], "no_qualified_opportunity")
        self.assertEqual(json.loads(row["reasons"]), {"insufficient_candles": 1})
        self.assertIn("velas insuficientes: 1", render_diagnostics(self.store, self.api.now))

        class FlatMarket(MarketStub):
            def candles(self, market):
                return candles(self.now, trend=False)
        self.api = FlatMarket()
        self.worker = Worker(self.store, self.policy, self.api,
                             self.root / "STOP", clock=lambda: self.api.now)
        self.assertTrue(self.worker.cycle())
        self.assertEqual(json.loads(self.scan_rows()[-1]["reasons"]), {"no_signal": 1})

    def test_pending_not_rejected_and_secret_error_sanitized(self):
        with self.store.transaction():
            append_scan(self.store, NOW, statuses=[
                {"market": "a", "status": "pendiente de análisis de profundidad y velas"},
                {"market": "b", "status": "sin señal explicable"},
                {"market": "c", "status": "volumen insuficiente o ausente"},
                {"market": "d", "status": "descartado: SECRET_PROVIDER_TOKEN"}
            ], eligible=3, checked=2, signals=0, candidates=0,
                decision="no_qualified_opportunity")
        row = self.scan_rows()[0]
        counts = json.loads(row["reasons"])
        self.assertEqual(counts["not_evaluated_this_cycle"], 1)
        self.assertEqual(counts["other_market_filter"], 1)
        self.assertNotIn("SECRET_PROVIDER_TOKEN", row["reasons"])
        out = render_diagnostics(self.store, NOW)
        self.assertNotIn("- pendiente de su turno:", out)
        self.assertIn("volumen insuficiente: 1", out)

    def test_restart_preserves_history_and_day_boundary(self):
        self.worker.cycle()
        self.store.close()
        restarted = Store(self.root / "ledger.sqlite", self.policy, NOW+1)
        try:
            restarted.reconcile(NOW+1)
            self.assertEqual(restarted.db.execute(
                "SELECT count(*) FROM scan_diagnostics").fetchone()[0], 1)
            self.assertIn("todavía no hay ciclos registrados",
                          render_diagnostics(restarted, NOW+86400))
        finally:
            restarted.close()

    def test_optional_diagnostic_error_never_pauses_worker(self):
        with patch("hache_trader.runtime.append_scan", side_effect=ValueError("fixture")):
            self.assertTrue(self.worker.cycle())
        self.assertIsNone(self.store.read()["last_error"])
        self.assertEqual(len(self.scan_rows()), 0)
        self.assertTrue(self.store.read()["paused"])

    def test_telegram_menu_read_only_and_no_network(self):
        self.assertIn("menu:diagnostico", json.dumps(menu(ai_enabled=True)))
        self.assertNotIn("menu:diagnostico", json.dumps(menu(ai_enabled=False)))
        self.assertIn("únicamente en simulación", render(
            self.store, "diagnostico", NOW, self.policy, runtime_mode="observe"))
        before = self.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertIn("todavía no hay ciclos registrados", render_diagnostics(self.store, NOW))
        self.assertEqual(before, self.store.db.execute(
            "SELECT count(*) FROM events").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
