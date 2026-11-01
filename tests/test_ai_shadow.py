"""Offline safety, idempotency, provider and quota tests. No live API usage."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hache_trader import ai_shadow
from hache_trader.domain import GuardError, Book as RealBook


class Book:
    market = "btcusdt"
    bids = ((ai_shadow.money("99"), ai_shadow.money("2")),)
    asks = ((ai_shadow.money("100"), ai_shadow.money("3")),)

    def fresh(self, now, max_age):
        assert max_age == 30


class MarketAPI:
    def markets(self):
        return [{"id": "btcusdt", "quote_unit": "usdt", "state": "enabled"}]

    def get(self, path):
        assert path == "/trade/public/tickers"
        return {"btcusdt": {"volume": "50000", "open": "99", "last": "100"}}

    def book(self, market):
        assert market == "btcusdt"
        return Book()

    def candles(self, market):
        raise GuardError("insufficient_candles")


class Response:
    status = 200

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.data


class OpenAIFake:
    def __init__(self, name="btcusdt", status="completed"):
        self.requests = []
        self.name, self.status = name, status

    def __call__(self, request, timeout):
        assert timeout == 15
        self.requests.append(request)
        answer = {"summary": "Riesgo elevado", "markets": [
            {"market": self.name, "verdict": "caution", "reason": "Spread elevado"}]}
        response = {"status": self.status,
                    "output": [{"content": [{"type": "output_text", "text": json.dumps(answer)}]}],
                    "usage": {"input_tokens": 90, "output_tokens": 45, "total_tokens": 135}}
        return Response(json.dumps(response).encode())


class ShadowTests(unittest.TestCase):
    NOW = 1793530800

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.creds = self.root / "creds"
        self.creds.mkdir()
        self.key = self.creds / "openai_api_key"
        self.key.write_text("sk-" + "x" * 50)
        self.key.chmod(0o600)
        self.db = self.root / "shadow.sqlite"

    def run_shadow(self, provider, api=None):
        return ai_shadow.run_once(self.db, self.creds, api=api or MarketAPI(),
                                  clock=lambda: self.NOW, transport=provider)

    def test_readonly_public_snapshot(self):
        source = ai_shadow.collect_public_snapshot(MarketAPI(), now=self.NOW)
        market = source["markets"][0]
        self.assertEqual(market["market"], "btcusdt")
        self.assertEqual(market["spread_pct"], "1.0000")
        self.assertIsNone(market["volatility_20m_pct"])
        self.assertEqual(market["signal"], "unverified")

    def test_request_persistence_usage_and_no_sensitive_data(self):
        provider = OpenAIFake()
        outcome = self.run_shadow(provider)
        self.assertEqual(outcome["usage"]["total_tokens"], 135)
        request = provider.requests[0]
        self.assertEqual(request.full_url, ai_shadow.API_URL)
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gpt-5.4-nano")
        self.assertIs(body["store"], False)
        self.assertNotIn("sk-", json.dumps(body))
        with sqlite3.connect(self.db) as db:
            status, snapshot, report, tokens = db.execute(
                "SELECT status,snapshot,report,total_tokens FROM runs").fetchone()
        self.assertEqual((status, tokens), ("completed", 135))
        self.assertIn("btcusdt", snapshot + report)
        self.assertNotIn("sk-", snapshot + report)

    def test_book_freshness_uses_receipt_time(self):
        class ReceiptAPI(MarketAPI):
            received_at = ShadowTests.NOW + 3
            def book(self, market):
                # Actual deterministic Book rejects a timestamp in the future
                # relative to the clock captured before several SafeTrade GETs.
                return RealBook(
                    market, self.received_at,
                    ((ai_shadow.money("99"), ai_shadow.money("2")),),
                    ((ai_shadow.money("100"), ai_shadow.money("3")),), 1)
        self.assertEqual(
            ai_shadow.collect_public_snapshot(ReceiptAPI(), now=self.NOW)["markets"][0]["market"],
            "btcusdt")

    def test_restart_is_idempotent(self):
        provider = OpenAIFake()
        self.run_shadow(provider)
        with self.assertRaisesRegex(GuardError, "shadow_slot_already_attempted"):
            self.run_shadow(provider)
        self.assertEqual(len(provider.requests), 1)

    def test_failed_provider_attempt_is_recorded_once_without_leak(self):
        def unavailable(req, timeout):
            raise OSError("sensitive sk-DO_NOT_PRINT")
        with self.assertRaisesRegex(GuardError, "shadow_analysis_unavailable") as error:
            self.run_shadow(unavailable)
        self.assertNotIn("DO_NOT_PRINT", str(error.exception))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "failed")
        with self.assertRaisesRegex(GuardError, "shadow_slot_already_attempted"):
            self.run_shadow(OpenAIFake())

    def test_ai_must_not_invent_market(self):
        snapshot = ai_shadow.collect_public_snapshot(MarketAPI(), now=self.NOW)
        with self.assertRaisesRegex(GuardError, "openai_report_invalid"):
            ai_shadow.analyze(snapshot, "sk-" + "x" * 50, transport=OpenAIFake("fakeusdt"))

    def test_openai_credentials_must_not_be_world_readable(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(GuardError, "shadow_analysis_unavailable"):
            self.run_shadow(OpenAIFake())

    def test_no_data_no_openai_request(self):
        class Empty(MarketAPI):
            def markets(self):
                return []
        provider = OpenAIFake()
        with self.assertRaisesRegex(GuardError, "shadow_analysis_unavailable"):
            self.run_shadow(provider, api=Empty())
        self.assertEqual(len(provider.requests), 0)

    def test_daily_budget_blocks_attempt(self):
        with sqlite3.connect(self.db) as db:
            db.execute("""CREATE TABLE runs(slot INTEGER PRIMARY KEY, observed_at INTEGER NOT NULL,
                        snapshot TEXT NOT NULL, status TEXT NOT NULL, report TEXT, error_code TEXT,
                        input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0)""")
            for i in range(4):
                db.execute("INSERT INTO runs(slot,observed_at,snapshot,status) VALUES(?,?,?,?)",
                           (i, self.NOW - 100, "{}", "reserved"))
        with self.assertRaisesRegex(GuardError, "openai_local_budget_exhausted"):
            self.run_shadow(OpenAIFake())

    def test_systemd_isolated_and_credentials_only(self):
        unit = (Path(__file__).parents[1] / "deployment/systemd/hache-trader-ai-shadow.service").read_text()
        timer = (Path(__file__).parents[1] / "deployment/systemd/hache-trader-ai-shadow.timer").read_text()
        self.assertIn("LoadCredential=openai_api_key:/etc/hache-trader/openai_api_key", unit)
        self.assertIn("StateDirectory=hache-trader-ai-shadow", unit)
        self.assertNotIn("getUpdates", unit + timer)
        self.assertNotIn("telegram_token", unit)
        self.assertEqual(timer.count("OnCalendar="), 4)


if __name__ == "__main__":
    unittest.main()
