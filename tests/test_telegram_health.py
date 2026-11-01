"""Regression for repeated Telegram timeouts pausing the paper-trading agent."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from hache_trader.domain import GuardError, Policy
from hache_trader.reports import render
from hache_trader.store import Store
from hache_trader.telegram_health import record_telegram_failure, record_telegram_recovery
from test_core import NOW


class TelegramHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.policy = Policy()
        self.store = Store(Path(self.tmp.name) / "ledger.sqlite", self.policy, NOW)
        self.store.reconcile(NOW)
        with self.store.transaction():
            s = self.store.read()
            s["last_cycle"] = NOW
            self.store.save(s, "healthy_market_fixture", {}, NOW)
        self.store.control("resume", NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def events(self, kind):
        return self.store.db.execute(
            "SELECT count(*) FROM events WHERE kind=?", (kind,)
        ).fetchone()[0]

    def test_outage_pauses_once_not_every_two_seconds(self):
        self.assertTrue(record_telegram_failure(self.store, NOW+1, "telegram_unavailable"))
        for n in range(2, 32, 2):
            self.assertFalse(record_telegram_failure(
                self.store, NOW+n, "telegram_cooldown"))
        self.assertEqual(self.events("telegram_connection_lost"), 1)
        self.assertEqual(self.events("pause"), 0)
        s = self.store.read()
        self.assertTrue(s["paused"])
        self.assertTrue(s["telegram_resume_required"])
        self.assertEqual(s["telegram_connection"], "unavailable")
        self.assertIsNone(s["last_error"])
        self.assertEqual(s["cash"], "50")
        self.assertEqual(s["orders"], {})
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE id LIKE 'telegram:pause:%'"
        ).fetchone()[0], 1)
        with self.assertRaisesRegex(GuardError, "resume_requires_telegram_connection"):
            self.store.control("resume", NOW+32)
        text = render(self.store, "estado", NOW+32, self.policy, runtime_mode="trial")
        self.assertIn("Telegram: conexión interrumpida", text)

    def test_recovery_is_not_automatic_resume(self):
        record_telegram_failure(self.store, NOW+1, "telegram_unavailable")
        self.assertTrue(record_telegram_recovery(self.store, NOW+33))
        self.assertFalse(record_telegram_recovery(self.store, NOW+35))
        self.assertEqual(self.events("telegram_connection_restored"), 1)
        s = self.store.read()
        self.assertTrue(s["paused"])
        self.assertTrue(s["telegram_resume_required"])
        self.assertEqual(s["telegram_connection"], "connected")
        text = render(self.store, "estado", NOW+35, self.policy, runtime_mode="trial")
        self.assertIn("recuperada; requiere /reanudar", text)
        rows = self.store.db.execute(
            "SELECT body FROM outbox WHERE id LIKE 'telegram:restored:%'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("PAUSADAS", json.loads(rows[0]["body"])["text"])
        self.store.control("resume", NOW+35)
        self.assertFalse(self.store.read()["paused"])
        self.assertFalse(self.store.read()["telegram_resume_required"])

    def test_existing_manual_pause_not_mistaken_for_auto_resume(self):
        self.store.control("pause", NOW)
        self.assertTrue(record_telegram_failure(self.store, NOW+1, "telegram_unavailable"))
        self.assertFalse(self.store.read()["telegram_resume_required"])
        record_telegram_recovery(self.store, NOW+2)
        self.assertTrue(self.store.read()["paused"])
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE id LIKE 'telegram:pause:%'"
        ).fetchone()[0], 0)
        self.assertEqual(self.events("telegram_connection_lost"), 1)


if __name__ == "__main__":
    unittest.main()
