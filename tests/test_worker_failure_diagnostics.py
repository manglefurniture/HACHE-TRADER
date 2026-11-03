"""Regression tests for transient public-data failure reporting (paper only)."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hache_trader.domain import GuardError, Policy
from hache_trader.reports import render
from hache_trader.runtime import Worker
from hache_trader.store import Store
from test_core import NOW
from test_worker import MarketStub


class WorkerFailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite"
        self.policy = Policy()
        self.store = Store(self.path, self.policy, NOW)
        self.addCleanup(self.store.close)
        self.store.reconcile(NOW)
        self.api = MarketStub()
        self.worker = Worker(self.store, self.policy, self.api,
                             Path(self.temp.name) / "STOP",
                             clock=lambda: self.api.now)

    def error_notifications(self):
        return self.store.db.execute(
            "SELECT COUNT(*) FROM outbox WHERE id LIKE 'error:%'"
        ).fetchone()[0]

    def test_repeated_failure_alerts_are_throttled_even_after_success(self):
        self.api.fail = True
        self.assertFalse(self.worker.cycle())
        self.assertEqual(self.error_notifications(), 1)
        self.assertTrue(self.store.read()["paused"])
        failure = self.store.read()["worker_last_failure"]
        self.assertEqual(failure["phase"], "public_fees")
        self.assertEqual(failure["reason"], "market_guard_rejected")
        self.api.fail = False
        self.assertTrue(self.worker.cycle())
        self.assertIsNone(self.store.read()["last_error"])
        self.assertTrue(self.store.read()["paused"])
        self.api.fail = True
        self.api.now += 60
        self.assertFalse(self.worker.cycle())
        self.assertEqual(self.error_notifications(), 1)
        self.assertEqual(self.store.read()["worker_last_failure"]["at"], NOW + 60)
        self.assertIn("Último fallo del motor", render(
            self.store, "estado", self.api.now, self.policy, runtime_mode="trial"
        ))
        self.api.now += 1800
        self.assertFalse(self.worker.cycle())
        self.assertEqual(self.error_notifications(), 2)
        self.assertTrue(self.store.read()["paused"])

    def test_last_failure_and_throttle_survive_reopening_ledger(self):
        self.api.fail = True
        self.worker.cycle()
        self.store.close()
        reopened = Store(self.path, self.policy, NOW + 60)
        try:
            other_api = MarketStub()
            other_api.now = NOW + 60
            other_api.fail = True
            worker = Worker(reopened, self.policy, other_api,
                            Path(self.temp.name) / "STOP",
                            clock=lambda: other_api.now)
            self.assertFalse(worker.cycle())
            self.assertEqual(reopened.db.execute(
                "SELECT COUNT(*) FROM outbox WHERE id LIKE 'error:%'"
            ).fetchone()[0], 1)
        finally:
            reopened.close()

    def test_market_catalog_failure_report_has_safe_phase_and_code(self):
        class BrokenCatalog(MarketStub):
            def markets(self):
                raise GuardError("markets_schema_unverified")
        self.api = BrokenCatalog()
        self.worker = Worker(self.store, self.policy, self.api,
                             Path(self.temp.name) / "STOP",
                             clock=lambda: self.api.now)
        self.assertFalse(self.worker.cycle())
        self.assertEqual(
            self.store.read()["worker_last_failure"]["reason"],
            "markets_schema_unverified",
        )
        self.assertEqual(
            self.store.read()["worker_last_failure"]["phase"], "market_catalog"
        )
        self.assertTrue(self.store.read()["paused"])

    def test_no_arbitrary_error_message_or_provider_secret_persisted(self):
        class HostileFailure(MarketStub):
            def get(self, path, query=None):
                raise GuardError("https://private.example/sk-PROVIDER_SECRET")
        self.api = HostileFailure()
        self.worker = Worker(self.store, self.policy, self.api,
                             Path(self.temp.name) / "STOP",
                             clock=lambda: self.api.now)
        self.assertFalse(self.worker.cycle())
        state = self.store.read()
        self.assertEqual(
            state["worker_last_failure"]["reason"], "market_guard_rejected"
        )
        rows = self.store.db.execute("SELECT payload FROM events").fetchall()
        self.assertNotIn("sk-PROVIDER_SECRET", str(rows))
        self.assertNotIn("private.example", str(rows))
        self.assertNotIn("sk-PROVIDER_SECRET", render(
            self.store, "estado", NOW, self.policy, runtime_mode="trial"
        ))

    def test_market_rate_limit_is_a_safe_diagnostic_not_an_auto_resume(self):
        class RateLimited(MarketStub):
            def get(self, path, query=None):
                raise GuardError("safetrade_http_429")
        self.api = RateLimited()
        self.worker = Worker(self.store, self.policy, self.api,
                             Path(self.temp.name) / "STOP",
                             clock=lambda: self.api.now)
        self.assertFalse(self.worker.cycle())
        self.assertEqual(
            self.store.read()["worker_last_failure"]["reason"],
            "safetrade_http_429"
        )
        self.assertTrue(self.store.read()["paused"])
        self.assertEqual(self.store.read()["orders"], {})
        self.assertIsNone(self.store.read()["position"])


if __name__ == "__main__":
    unittest.main()
