"""Offline safety regressions for explicitly enabled simulated paper trials."""
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import sys
import unittest

from hache_trader.__main__ import main
from hache_trader.domain import GuardError, Policy
from hache_trader.engine import PaperEngine
from hache_trader.reports import render
from hache_trader.store import Store
from hache_trader.telegram import Channel, menu
from test_core import NOW, market


class PaperTrialTests(unittest.TestCase):
    def test_hard_cap_three_paper_buys_per_cancun_day(self):
        with TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder)/"ledger.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                with store.transaction():
                    state = store.read()
                    state["last_cycle"] = NOW
                    store.save(state, "healthy_fixture", {}, NOW)
                store.control("resume", NOW)
                engine = PaperEngine(store, policy, Path(folder)/"STOP")
                for n in range(3):
                    ident = "entry_" + str(n)
                    engine.submit(ident, market(), "buy", D("20"), D("1"), NOW+n,
                                  stop=D("0.99"), target=D("1.02"), expected_loss=D("0.30"))
                    engine.cancel(ident, NOW+n)
                with self.assertRaisesRegex(GuardError, "paper_daily_entry_limit"):
                    engine.submit("fourth", market(), "buy", D("20"), D("1"), NOW+4,
                                  stop=D("0.99"), target=D("1.02"), expected_loss=D("0.30"))
                state = store.read()
                self.assertEqual(state["cash"], "50.000")
                self.assertEqual(D(state["reserved"]), D("0"))
                self.assertIsNone(state["position"])
                self.assertEqual(len(state["orders"]), 3)
                self.assertFalse(state["emergency"])
            finally:
                store.close()

    def test_trial_denied_without_opt_in_even_if_telegram_requested(self):
        with TemporaryDirectory() as folder:
            with patch.object(sys, "argv", ["hache_trader", "trial", "--telegram", "--state-dir", folder]), \
                 patch.dict(os.environ, {"HACHE_TRADER_ENABLE_PAPER_TRIAL": ""}):
                with self.assertRaisesRegex(GuardError, "paper_trial_not_enabled"):
                    main()
            store = Store(Path(folder)/"ledger.sqlite", Policy(), NOW)
            try:
                self.assertTrue(store.read()["paused"])
                self.assertEqual(store.read()["cash"], "50")
            finally:
                store.close()

    def test_trial_boot_remains_paused_until_owner_confirmation(self):
        class EndTest(Exception):
            pass

        with TemporaryDirectory() as folder:
            env = {
                "HACHE_TRADER_ENABLE_PAPER_TRIAL": "EXPLICIT_SIMULATION_ONLY",
                "HACHE_TRADER_BOT_ID": "1234567890",
                "HACHE_TRADER_CHAT_ID": "424242",
                "HACHE_TRADER_USER_ID": "424242",
                "CREDENTIALS_DIRECTORY": "/dev/null/synthetic",
            }

            def telegram_result(method, data):
                if method == "getWebhookInfo":
                    return {"url": ""}
                if method == "getMe":
                    return {"id": 1234567890}
                raise AssertionError(method)

            with patch.object(sys, "argv", ["hache_trader", "trial", "--telegram", "--state-dir", folder]), \
                    patch.dict(os.environ, env), \
                    patch("hache_trader.__main__.read_secret", return_value="000000:fake"), \
                    patch("hache_trader.__main__.TelegramAPI") as telegram_api, \
                    patch("hache_trader.__main__.Channel") as channel, \
                    patch("hache_trader.__main__.Worker") as worker:
                telegram_api.return_value.call.side_effect = telegram_result
                channel.return_value.poll.return_value = None
                worker.return_value.cycle.side_effect = EndTest
                with self.assertRaises(EndTest):
                    main()
                self.assertEqual(channel.call_args.kwargs["runtime_mode"], "trial")
                self.assertTrue(channel.call_args.kwargs["allow_resume"])
                worker.return_value.cycle.assert_called_once()
            store = Store(Path(folder)/"ledger.sqlite", Policy(), NOW)
            try:
                self.assertTrue(store.read()["paused"])
                self.assertEqual(store.read()["cash"], "50")
                status = render(store, "estado", NOW, Policy(), runtime_mode="trial")
                self.assertIn("operaciones FICTICIAS", status)
                self.assertIn("Pausado: True", status)
                self.assertIn("0/3", status)
            finally:
                store.close()

    def test_trial_menu_can_confirm_paper_only_while_observer_cannot(self):
        with TemporaryDirectory() as folder:
            policy = Policy()
            store = Store(Path(folder)/"ledger.sqlite", policy, NOW)
            try:
                store.reconcile(NOW)
                trial = Channel(store, object(), 42, 42, policy, runtime_mode="trial")
                observer = Channel(store, object(), 42, 42, policy, runtime_mode="observe")
                self.assertTrue(trial.allow_resume)
                self.assertFalse(observer.allow_resume)
                self.assertIn("reanudar", str(menu(allow_resume=trial.allow_resume)))
                self.assertNotIn("reanudar", str(menu(allow_resume=observer.allow_resume)))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
