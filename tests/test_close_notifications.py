"""Closing a simulated position queues one durable owner-only Telegram alert."""
import json
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hache_trader.domain import Policy, money
from hache_trader.engine import PaperEngine
from hache_trader.store import Store
from hache_trader.telegram import Channel
from test_core import NOW, book, market


class CloseAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.policy = Policy()
        self.store = Store(Path(self.tmp.name) / "paper.sqlite", self.policy, NOW)
        self.store.reconcile(NOW)
        with self.store.transaction():
            s = self.store.read()
            s["last_cycle"] = NOW
            self.store.save(s, "fresh_fixture", {}, NOW)
        self.store.control("resume", NOW)
        self.engine = PaperEngine(self.store, self.policy, Path(self.tmp.name) / "STOP")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def buy(self):
        self.engine.submit("buy", market(), "buy", D(20), D(1), NOW,
                           stop=D("0.99"), target=D("1.02"), expected_loss=D("0.30"))
        self.engine.match("buy", market(), book(NOW, sequence=1), NOW)

    def sell(self, ident="sell", quantity="20", now=NOW+1,
             bid="1.02", size="1000", seq=2):
        self.engine.submit(ident, market(), "sell", D(quantity), D(bid), now)
        return self.engine.match(ident, market(), book(
            now, bid=bid, ask="1.021" if D(bid) == D("1.02") else "0.991",
            size=size, sequence=seq), now)

    def alerts(self):
        return [dict(r) for r in self.store.db.execute(
            "SELECT id,body,sent FROM outbox WHERE id LIKE 'paper:closed:%' ORDER BY rowid")]

    def test_profit_close_once_with_fees_and_actual_net(self):
        self.buy()
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.sell(), D(20))
        self.assertEqual(self.store.read()["closed_trades"], 1)
        self.assertIsNone(self.store.read()["position"])
        rows = self.alerts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "paper:closed:1")
        message = json.loads(rows[0]["body"])["text"]
        self.assertIn("ABC/USDT", message)
        self.assertIn("CERRADA", message)
        self.assertIn("Ganancia: +0.359600 USDT netos ficticios", message)
        self.assertIn("Comisiones estimadas (compra y ventas): 0.040400 USDT", message)
        self.assertIn("50.359600 USDT", message)
        self.assertEqual(money(self.store.read()["realized"]), D("0.35960"))
        self.assertFalse(self.store.read()["emergency"])
        # Repeated matching of a filled order cannot enqueue a duplicate alert.
        self.engine.match("sell", market(), book(NOW+2, "1.02", "1.021", sequence=3), NOW+2)
        self.assertEqual(len(self.alerts()), 1)

    def test_loss_close_is_labeled_as_loss(self):
        self.buy()
        self.sell(bid="0.99")
        rows = self.alerts()
        self.assertEqual(len(rows), 1)
        message = json.loads(rows[0]["body"])["text"]
        self.assertIn("Pérdida:", message)
        self.assertIn("USDT netos ficticios", message)
        self.assertLess(money(self.store.read()["realized"]), 0)

    def test_partial_exit_and_cancellation_do_not_send_close_alert(self):
        self.buy()
        self.assertEqual(self.sell(quantity="20", size="50"), D(5))
        self.assertEqual(self.alerts(), [])
        self.engine.cancel("sell", NOW+1)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(money(self.store.read()["position"]["quantity"]), D(15))
        self.assertEqual(self.sell("sell2", "15", NOW+2, "1.02", "1000", 3), D(15))
        self.assertEqual(len(self.alerts()), 1)
        message = json.loads(self.alerts()[0]["body"])["text"]
        self.assertIn("Ganancia: +0.359600", message)

    def test_order_cancelled_without_fill_never_notifies(self):
        self.engine.submit("buy", market(), "buy", D(20), D(1), NOW,
                           stop=D("0.99"), target=D("1.02"), expected_loss=D("0.30"))
        self.engine.cancel("buy", NOW)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.store.read()["closed_trades"], 0)

    def test_legacy_open_position_does_not_fabricate_individual_net(self):
        self.buy()
        with self.store.transaction():
            s = self.store.read()
            s["position"].pop("trade_net")
            s["position"].pop("trade_fees")
            self.store.save(s, "old_position_fixture", {}, NOW)
        self.sell()
        text = json.loads(self.alerts()[0]["body"])["text"]
        self.assertIn("Neto individual no disponible", text)
        self.assertNotIn("Ganancia:", text)

    def test_closing_alert_is_delivered_before_routine_queue(self):
        self.buy()
        with self.store.transaction():
            for n in range(12):
                self.store.enqueue("routine:" + str(n), {"text": "Reporte rutinario " + str(n)})
        self.sell()
        sent = []

        class Bot:
            def call(_, method, body):
                sent.append(body)
                return {"message_id": 100 + len(sent)}

        channel = Channel(self.store, Bot(), 424242, 424242, self.policy,
                          runtime_mode="trial")
        channel.flush(NOW+2)
        self.assertEqual(len(sent), 10)
        self.assertIn("Posición CERRADA: ABC/USDT", sent[0]["text"])
        self.assertEqual(self.alerts()[0]["sent"], 1)
        channel.flush(NOW+2)
        self.assertEqual(len(sent), 13)
        self.assertEqual(sum("Posición CERRADA" in body["text"] for body in sent), 1)

    def test_alert_survives_restart_and_sends_only_to_owner_chat(self):
        self.buy()
        self.sell()
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "paper.sqlite", self.policy, NOW+3)
        self.store.reconcile(NOW+3)
        sent = []

        class Bot:
            def call(_, method, body):
                self.assertEqual(method, "sendMessage")
                sent.append(body)
                return {"message_id": 77}

        channel = Channel(self.store, Bot(), 424242, 424242, self.policy,
                          runtime_mode="trial")
        channel.flush(NOW+3)
        channel.flush(NOW+3)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], 424242)
        self.assertIn("ABC/USDT", sent[0]["text"])
        self.assertEqual(self.alerts()[0]["sent"], 1)


if __name__ == "__main__":
    unittest.main()
