"""Real threaded regression: Telegram remains responsive during slow paper scans."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from hache_trader.domain import GuardError, Policy
from hache_trader.store import Store
from hache_trader.telegram import Channel
from hache_trader.trial_runner import TrialMarketLoop
from test_core import NOW


class PaperResponsivenessTests(unittest.TestCase):
    def test_telegram_pause_works_while_market_is_blocked(self):
        started = threading.Event()
        release = threading.Event()

        class SlowWorker:
            def __init__(self, store, policy, api, stop_file, clock):
                self.store = store

            def cycle(self):
                started.set()
                release.wait(5)
                return True

        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "ledger.sqlite"
            policy = Policy()
            controller = Store(ledger, policy, NOW)
            controller.reconcile(NOW)
            market = TrialMarketLoop(ledger, policy, object(),
                                     Path(d) / "STOP", clock=lambda: NOW,
                                     worker_factory=SlowWorker)
            try:
                market.start()
                self.assertTrue(started.wait(2))
                market.check()
                channel = Channel(controller, object(), 42, 42, policy,
                                  runtime_mode="trial")
                update = {"update_id": 1, "message": {
                    "text": "/pausar", "date": NOW, "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                }}
                t0 = time.monotonic()
                self.assertEqual(channel.process(update, NOW), "processed")
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 1)
                self.assertTrue(controller.read()["paused"])
                self.assertEqual(controller.read()["cash"], "50")
                self.assertEqual(controller.read()["orders"], {})
                self.assertTrue(market._thread.is_alive())
            finally:
                release.set()
                market.close()
                controller.close()

    def test_market_thread_failure_is_not_silently_ignored(self):
        crashed = threading.Event()

        class BrokenWorker:
            def __init__(self, *args, **kwargs):
                pass

            def cycle(self):
                crashed.set()
                raise RuntimeError("fake_private_provider_response")

        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "ledger.sqlite"
            policy = Policy()
            store = Store(ledger, policy, NOW)
            store.reconcile(NOW)
            store.close()
            market = TrialMarketLoop(ledger, policy, object(),
                                     Path(d) / "STOP", clock=lambda: NOW,
                                     worker_factory=BrokenWorker)
            try:
                try:
                    market.start()
                except GuardError as exc:
                    self.assertEqual(str(exc), "trial_market_loop_unavailable")
                self.assertTrue(crashed.wait(2))
                self.assertTrue(market._failed.wait(2))
                with self.assertRaisesRegex(GuardError, "trial_market_loop_unavailable"):
                    market.check()
                check = Store(ledger, policy, NOW)
                try:
                    self.assertTrue(check.read()["paused"])
                finally:
                    check.close()
            finally:
                market.close()


if __name__ == "__main__":
    unittest.main()
