"""Telegram transport failures vs malformed provider responses: real API adapter."""
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hache_trader.domain import GuardError, Policy
from hache_trader.store import Store
from hache_trader.telegram import TelegramAPI
from hache_trader.telegram_health import record_telegram_failure
from test_core import NOW

TOKEN = "123456789:NOT_A_REAL_TOKEN_FOR_LOCAL_TEST_ONLY"


class APIReply(io.BytesIO):
    def __init__(self, raw):
        super().__init__(raw)


class TelegramProtocolGraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "paper.sqlite", Policy(), NOW)
        self.addCleanup(self.store.close)
        self.store.reconcile(NOW)
        with self.store.transaction():
            state = self.store.read()
            state["last_cycle"] = NOW
            self.store.save(state, "healthy", {}, NOW)
        self.store.control("resume", NOW)

    def test_malformed_responses_fail_closed_without_grace(self):
        for raw in (b"not-json", b"[]", b'{"ok":true}',
                    b'{"ok":true,"result":', b'{"ok":false,"result":{}}'):
            with self.subTest(raw=raw):
                api = TelegramAPI(
                    TOKEN, transport=lambda req, timeout: APIReply(raw),
                    clock=lambda: NOW
                )
                with self.assertRaises(GuardError) as caught:
                    api.call("getUpdates", {"offset": 0, "timeout": 0})
                error = str(caught.exception)
                self.assertIn(error, ("telegram_response_invalid", "telegram_rejected"))
                self.assertTrue(record_telegram_failure(self.store, NOW + 1, error))
                self.assertTrue(self.store.read()["paused"])
                # Each provider response is tested with a freshly authorized
                # simulator state; no real calls or credentials are used.
                self.store.control("pause", NOW + 1)
                with self.store.transaction():
                    s = self.store.read()
                    s["telegram_connection"] = "connected"
                    s["last_cycle"] = NOW
                    s["telegram_resume_required"] = False
                    self.store.save(s, "reset_after_protocol_fixture", {}, NOW)
                self.store.control("resume", NOW)

    def test_transport_error_is_debounced(self):
        def fail(req, timeout):
            raise OSError("private endpoint details should not leak")
        api = TelegramAPI(TOKEN, transport=fail, clock=lambda: NOW)
        with self.assertRaisesRegex(GuardError, "telegram_unavailable"):
            api.call("getUpdates", {"offset": 0, "timeout": 0})
        self.assertFalse(record_telegram_failure(
            self.store, NOW + 1, "telegram_unavailable"
        ))
        self.assertFalse(self.store.read()["paused"])


if __name__ == "__main__":
    unittest.main()
