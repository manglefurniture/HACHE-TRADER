"""Offline SafeTrade/Telegram integration contracts. No network or real funds."""
from email.utils import formatdate
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from hache_trader.domain import GuardError, Policy
from hache_trader.safetrade import SafeTrade
from hache_trader.store import Store
from hache_trader.telegram import Channel
from test_core import NOW


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(json.dumps(data).encode("utf-8"))
        self.headers = {"Date": formatdate(NOW, usegmt=True), "Age": "0"}


class AdapterContractTests(unittest.TestCase):
    def test_public_get_uses_canonical_host_without_private_headers(self):
        requests = []
        def transport(req, timeout):
            requests.append(req)
            return Response([])
        api = SafeTrade(transport=transport, clock=lambda: NOW,
                        sleep=lambda _: None)
        self.assertEqual(api.markets(), [])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertTrue(requests[0].full_url.startswith(
            "https://safe.trade/api/v2/trade/public/"))
        self.assertNotIn("X-auth-apikey", requests[0].headers)

    def test_live_writes_and_private_read_without_credentials_denied(self):
        api = SafeTrade(clock=lambda: NOW, sleep=lambda _: None)
        for path in ("/trade/market/orders/cancel",
                     "/trade/account/withdraws",
                     "/trade/account/balances/spot"):
            with self.assertRaisesRegex(GuardError, "endpoint_not_allowed|readonly_credentials_missing"):
                api.get(path)

    def test_rate_limit_pauses_requests_without_retrying_indefinitely(self):
        called = []
        def limited(req, timeout):
            called.append(req.full_url)
            raise urllib.error.HTTPError(
                req.full_url, 429, "rate limited",
                {"Retry-After": "60"}, None)
        api = SafeTrade(transport=limited, clock=lambda: NOW,
                        sleep=lambda _: None)
        with self.assertRaisesRegex(GuardError, "safetrade_http_429"):
            api.markets()
        with self.assertRaisesRegex(GuardError, "circuit_open"):
            api.markets()
        self.assertEqual(len(called), 1)


class TelegramContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.policy = Policy()
        self.store = Store(Path(self.temp.name) / "paper.sqlite", self.policy, NOW)
        self.store.reconcile(NOW)
        self.sent = []

        class FakeTelegram:
            def __init__(internal):
                internal.message_id = 123
            def call(internal, method, body):
                self.sent.append((method, body))
                return {"message_id": internal.message_id}

        self.channel = Channel(self.store, FakeTelegram(), 42, 42, self.policy)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def command(update_id, action, *, user=42, chat=42, kind="private"):
        return {"update_id": update_id,
                "message": {
                    "text": "/" + action, "date": NOW,
                    "from": {"id": user},
                    "chat": {"id": chat, "type": kind}
                }}

    def test_owner_only_and_replay_protected(self):
        self.assertEqual(
            self.channel.process(self.command(1, "emergencia", user=43), NOW),
            "ignored")
        self.assertEqual(
            self.channel.process(self.command(2, "emergencia", kind="group"), NOW),
            "ignored")
        self.assertFalse(self.store.read()["emergency"])
        self.assertEqual(
            self.channel.process(self.command(3, "emergencia"), NOW),
            "processed")
        self.assertTrue(self.store.read()["emergency"])
        self.assertEqual(
            self.channel.process(self.command(3, "emergencia"), NOW),
            "duplicate")
        self.channel.flush(NOW)
        self.channel.flush(NOW)
        self.assertEqual(len(self.sent), 1)

    def test_callback_acknowledged_and_response_sent_once(self):
        calls = []
        update = {
            "update_id": 9,
            "callback_query": {
                "id": "safe_cb_9",
                "data": "menu:estado",
                "from": {"id": 42},
                "message": {"message_id": 100,
                            "chat": {"id": 42, "type": "private"}}
            },
        }

        class Stub:
            def call(self, method, body):
                calls.append((method, body))
                if method == "getUpdates":
                    return [update]
                if method == "answerCallbackQuery":
                    return True
                if method == "sendMessage":
                    return {"message_id": 123}
                raise AssertionError(method)

        self.channel.api = Stub()
        self.channel.poll(NOW)
        self.assertEqual([name for name, _ in calls],
                         ["getUpdates", "answerCallbackQuery", "sendMessage"])
        self.assertEqual(calls[1][1], {"callback_query_id": "safe_cb_9"})
        self.assertEqual(len(self.store.db.execute("SELECT * FROM outbox").fetchall()), 1)
        self.channel.poll(NOW)
        self.assertEqual(sum(name == "sendMessage" for name, _ in calls), 1)

    def test_expired_callback_ack_error_does_not_block_owner_reply(self):
        update = {
            "update_id": 10,
            "callback_query": {
                "id": "expired_10",
                "data": "menu:estado",
                "from": {"id": 42},
                "message": {"message_id": 100,
                            "chat": {"id": 42, "type": "private"}}
            },
        }
        sent = []

        class Stub:
            def call(self, method, body):
                if method == "getUpdates":
                    return [update]
                if method == "answerCallbackQuery":
                    raise GuardError("telegram_rejected")
                if method == "sendMessage":
                    sent.append(body)
                    return {"message_id": 123}
                raise AssertionError(method)

        self.channel.api = Stub()
        self.channel.poll(NOW)
        self.assertEqual(len(sent), 1)
        self.assertIn("SIMULACIÓN", sent[0]["text"])

    def test_resume_requires_confirmed_message_and_healthy_state(self):
        with self.store.transaction():
            state = self.store.read()
            state["last_cycle"] = NOW
            self.store.save(state, "healthy_fixture", {}, NOW)
        self.channel.process(self.command(4, "reanudar"), NOW)
        self.channel.flush(NOW)
        nonce = self.store.db.execute(
            "SELECT nonce FROM confirmations LIMIT 1").fetchone()[0]
        callback = {
            "update_id": 5,
            "callback_query": {
                "data": "resume:" + nonce, "from": {"id": 42},
                "message": {"message_id": 999,
                            "chat": {"id": 42, "type": "private"}}
            }
        }
        self.channel.process(callback, NOW)
        self.assertTrue(self.store.read()["paused"])
        callback["update_id"] = 6
        callback["callback_query"]["message"]["message_id"] = 123
        self.channel.process(callback, NOW)
        self.assertFalse(self.store.read()["paused"])


if __name__ == "__main__":
    unittest.main()
