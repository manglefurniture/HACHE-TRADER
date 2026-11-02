"""Transient Telegram outage safety regressions (offline; no API calls)."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hache_trader.domain import Policy
from hache_trader.engine import PaperEngine
from hache_trader.reports import render
from hache_trader.store import Store
from hache_trader.telegram_health import (
    record_telegram_failure, record_telegram_recovery,
    GRACE_SECONDS,
)
from test_core import NOW, book, market


class TelegramOutageGraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = Policy()
        self.store = Store(self.root / "paper.sqlite", self.policy, NOW)
        self.addCleanup(self.store.close)
        self.store.reconcile(NOW)
        with self.store.transaction():
            s = self.store.read()
            s["last_cycle"] = NOW
            self.store.save(s, "healthy_fixture", {}, NOW)
        self.store.control("resume", NOW)

    def events(self, name):
        return self.store.db.execute(
            "SELECT count(*) FROM events WHERE kind=?", (name,)
        ).fetchone()[0]

    def test_transient_outage_recovers_without_pausing_or_resume(self):
        self.assertFalse(record_telegram_failure(self.store, NOW + 1, "telegram_unavailable"))
        self.assertFalse(record_telegram_failure(self.store, NOW + 30, "telegram_cooldown"))
        status = render(self.store, "estado", NOW + 31, self.policy, runtime_mode="trial")
        self.assertIn("conexión intermitente", status)
        self.assertFalse(self.store.read()["paused"])
        self.assertTrue(record_telegram_recovery(self.store, NOW + 35))
        s = self.store.read()
        self.assertFalse(s["paused"])
        self.assertEqual(s["telegram_connection"], "connected")
        self.assertIsNone(s["telegram_failure_since"])
        self.assertFalse(s.get("telegram_resume_required", False))
        self.assertEqual(self.events("telegram_connection_lost"), 0)
        self.assertEqual(self.events("telegram_transient_recovered"), 1)
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE id LIKE 'telegram:%'"
        ).fetchone()[0], 0)

    def test_boundary_and_idempotency_confirmed_outage(self):
        self.assertEqual(GRACE_SECONDS, 90)
        self.assertFalse(record_telegram_failure(self.store, NOW + 1, "telegram_unavailable"))
        self.assertFalse(record_telegram_failure(self.store, NOW + 90, "telegram_cooldown"))
        self.assertTrue(record_telegram_failure(self.store, NOW + 91, "telegram_cooldown"))
        self.assertFalse(record_telegram_failure(self.store, NOW + 92, "telegram_cooldown"))
        self.assertTrue(self.store.read()["paused"])
        self.assertEqual(self.events("telegram_connection_lost"), 1)
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE id LIKE 'telegram:pause:%'"
        ).fetchone()[0], 1)
        self.assertTrue(record_telegram_recovery(self.store, NOW + 94))
        self.assertTrue(self.store.read()["paused"])
        self.assertTrue(self.store.read()["telegram_resume_required"])

    def test_unsafe_failures_pause_without_grace(self):
        for i, reason in enumerate((
            "invalid_telegram_batch", "invalid_telegram_update",
            "invalid_telegram_receipt", "telegram_rejected",
            "telegram_response_too_large", "unknown_code",
        )):
            with self.subTest(reason=reason), TemporaryDirectory() as subdir:
                path = Path(subdir) / "paper.sqlite"
                test_store = Store(path, self.policy, NOW)
                try:
                    test_store.reconcile(NOW)
                    with test_store.transaction():
                        s = test_store.read()
                        s["last_cycle"] = NOW
                        test_store.save(s, "fixture", {}, NOW)
                    test_store.control("resume", NOW)
                    self.assertTrue(record_telegram_failure(test_store, NOW + 1, reason))
                    self.assertTrue(test_store.read()["paused"])
                    self.assertEqual(test_store.read()["telegram_connection"], "unavailable")
                finally:
                    test_store.close()

    def test_success_after_grace_deadline_still_requires_manual_resume(self):
        self.assertFalse(record_telegram_failure(
            self.store, NOW + 1, "telegram_unavailable"
        ))
        self.assertTrue(record_telegram_recovery(self.store, NOW + 92))
        self.assertTrue(self.store.read()["paused"])
        self.assertTrue(self.store.read()["telegram_resume_required"])
        self.assertEqual(self.store.read()["telegram_connection"], "connected")
        self.assertEqual(self.events("telegram_connection_lost"), 1)
        self.assertEqual(self.events("telegram_connection_restored"), 1)
        self.assertEqual(self.events("telegram_transient_recovered"), 0)
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE id LIKE 'telegram:pause:%'"
        ).fetchone()[0], 1)

    def test_success_at_exact_grace_deadline_fails_closed(self):
        record_telegram_failure(self.store, NOW + 1, "telegram_unavailable")
        record_telegram_recovery(self.store, NOW + 91)
        self.assertTrue(self.store.read()["paused"])

    def test_invalid_clock_fails_closed(self):
        record_telegram_failure(self.store, NOW + 10, "telegram_unavailable")
        self.assertTrue(record_telegram_failure(self.store, NOW + 9, "telegram_cooldown"))
        self.assertTrue(self.store.read()["paused"])

    def test_position_exits_still_run_while_telegram_is_unavailable(self):
        engine = PaperEngine(self.store, self.policy, self.root / "STOP")
        engine.submit("buy1", market(), "buy", Decimal("20"), Decimal("1"), NOW,
                      stop=Decimal("0.99"), target=Decimal("1.02"),
                      expected_loss=Decimal("0.30"))
        engine.match("buy1", market(), book(), NOW)
        self.assertIsNotNone(self.store.read()["position"])
        record_telegram_failure(self.store, NOW + 1, "telegram_unavailable")
        record_telegram_failure(self.store, NOW + 91, "telegram_cooldown")
        self.assertTrue(self.store.read()["paused"])
        result = engine.manage_exit(
            market(), book(NOW + 92, bid="1.02", ask="1.021", sequence=2), NOW + 92
        )
        self.assertIsNotNone(result)
        self.assertIsNone(self.store.read()["position"])
        self.assertEqual(self.store.read()["closed_trades"], 1)
        self.assertTrue(self.store.read()["paused"])


if __name__ == "__main__":
    unittest.main()
