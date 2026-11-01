"""Safe, offline validation of the read-only diagnostic path."""
import unittest
from hache_trader.diagnostics import diagnose
from hache_trader.domain import GuardError
from test_core import NOW, book


class PublicMarket:
    def __init__(self):
        self.fail_at = None
        self.calls = []

    def _stage(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            raise GuardError("untrusted_provider_failure")

    def markets(self):
        self._stage("markets")
        return [{
            "id": "abcusdt", "base_unit": "abc", "quote_unit": "usdt",
            "state": "enabled", "min_amount": "0.01",
            "amount_precision": 4, "price_precision": 4,
            "min_price": "0.0001", "max_price": "100000"
        }]

    def get(self, route, query=None):
        self._stage("fees")
        return [{"market_id": "any", "taker": "0.001"}]

    def ticker(self, market):
        self._stage("ticker")
        return NOW, "20000"

    def book(self, market):
        self._stage("depth")
        return book(NOW)

    def candles(self, market):
        self._stage("candles")
        return []


class DiagnosticTests(unittest.TestCase):
    def test_public_diagnosis_reports_only_checked_data(self):
        client = PublicMarket()
        result = diagnose(client, "abcusdt", clock=lambda: NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "SIMULACIÓN")
        self.assertFalse(result["live_authorized"])
        self.assertEqual(result["checks"], [
            "markets", "fees", "ticker", "depth", "candles"])
        self.assertEqual(result["usdt_markets"], 1)
        self.assertEqual(result["public_fee_estimate"], "0.001")

    def test_provider_error_never_leaks_private_details(self):
        for stage in ("markets", "fees", "ticker", "depth", "candles"):
            client = PublicMarket()
            client.fail_at = stage
            result = diagnose(client, "abcusdt", clock=lambda: NOW)
            self.assertFalse(result["ok"])
            self.assertEqual(result["failure_stage"], stage)
            self.assertEqual(result["error"], "public_api_unavailable_or_invalid")
            self.assertNotIn("untrusted_provider_failure", str(result))

    def test_unknown_or_invalid_market_does_not_request_private_data(self):
        client = PublicMarket()
        result = diagnose(client, "missingusdt", clock=lambda: NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(client.calls, ["markets"])
        client = PublicMarket()
        result = diagnose(client, "../account/private", clock=lambda: NOW)
        self.assertEqual(result["error"], "invalid_market")
        self.assertFalse(client.calls)

    def test_stale_ticker_rejected(self):
        client = PublicMarket()
        client.ticker = lambda market: (NOW - 31, "20000")
        result = diagnose(client, "abcusdt", clock=lambda: NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_stage"], "ticker")


if __name__ == "__main__":
    unittest.main()
