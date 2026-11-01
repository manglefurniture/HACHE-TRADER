"""User-facing observer reporting, while preserving read-only paper state."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hache_trader.domain import Policy
from hache_trader.observer import MarketObserver
from hache_trader.reports import _market_message, render
from hache_trader.store import Store
from test_core import NOW, book
from test_observer import MarketStub


class WideSpreadMarket(MarketStub):
    def book(self, market):
        return book(NOW, bid="0.98", ask="1.00",
                    name=market.removesuffix("usdt"))


class ReportClarityTests(unittest.TestCase):
    def test_actual_spread_and_maximum_are_reported(self):
        with TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder) / "paper.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                obs = MarketObserver(store, policy, WideSpreadMarket(),
                                     clock=lambda: NOW, batch_size=1)
                self.assertTrue(obs.cycle())
                status = store.read()["markets"][0]["status"]
                self.assertIn("spread 2.00%", status)
                self.assertIn("máximo 0.40%", status)
                markets = render(store, "mercados", NOW, policy, runtime_mode="observe")
                self.assertIn("ABC/USDT", markets)
                self.assertIn("Sin órdenes ni movimientos de fondos.", markets)
                self.assertEqual(store.read()["cash"], "50")
                self.assertEqual(store.read()["orders"], {})
            finally:
                store.close()

    def test_stale_observer_state_does_not_hide_paper_trading(self):
        with TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder) / "paper.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                with store.transaction():
                    state = store.read()
                    state["observer_state"] = "solo lectura"
                    state["paused"] = False
                    state["last_cycle"] = NOW
                    store.save(state, "fixture_paper_active", {}, NOW)
                status = render(store, "estado", NOW, policy, runtime_mode="paper")
                self.assertIn("Pausado: False", status)
                self.assertNotIn("Operaciones automáticas: deshabilitadas", status)
                help_msg = render(store, "ayuda", NOW, policy, runtime_mode="paper")
                self.assertIn("/reanudar", help_msg)
                markets = render(store, "mercados", NOW, policy, runtime_mode="paper")
                self.assertNotIn("Sin órdenes ni movimientos de fondos.", markets)
            finally:
                store.close()

    def test_explanations_and_local_time_in_read_only_mode(self):
        self.assertEqual(_market_message("descartado: insufficient_candles"),
                         "descartado: velas recientes insuficientes")
        with TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder) / "paper.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                with store.transaction():
                    state = store.read()
                    state["observer_state"] = "solo lectura"
                    state["observer_eligible"] = 14
                    state["last_cycle"] = NOW
                    state["markets"] = [
                        {"market": "btcusdt", "status": "descartado: insufficient_candles"}
                    ]
                    store.save(state, "test_reporting", {}, NOW)
                status = render(store, "estado", NOW, policy, runtime_mode="observe")
                self.assertIn("Radar: solo lectura", status)
                self.assertIn("Operaciones automáticas: deshabilitadas", status)
                self.assertIn("Último barrido:", status)
                self.assertIn("(Cancún)", status)
                markets = render(store, "mercados", NOW, policy)
                self.assertIn("BTC/USDT: descartado: velas recientes insuficientes", markets)
                self.assertIn("Mercados elegibles por volumen: 14", markets)
                help_text = render(store, "ayuda", NOW, policy, runtime_mode="observe")
                self.assertNotIn("/reanudar", help_text)
                self.assertTrue(store.read()["paused"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
