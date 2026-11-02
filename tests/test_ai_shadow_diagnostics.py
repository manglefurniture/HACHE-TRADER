"""Provider failures are machine-readable, sanitised and never automatically retried."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error

from hache_trader.ai_shadow import API_URL, run_once, analyze
from hache_trader.domain import GuardError

from test_ai_shadow import MarketAPI, OpenAIFake, Response, ShadowTests


class ShadowDiagnosticTests(unittest.TestCase):
    NOW = ShadowTests.NOW

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.creds = self.folder / "creds"
        self.creds.mkdir()
        self.secret = self.creds / "openai_api_key"
        self.secret.write_text("sk-" + "A" * 50)
        self.secret.chmod(0o600)
        self.db = self.folder / "shadow.sqlite"

    def execute(self, transport):
        return run_once(
            self.db, self.creds, api=MarketAPI(),
            clock=lambda: self.NOW, transport=transport,
        )

    def failure(self, transport, code, *, expected_tokens=0):
        with self.assertRaisesRegex(GuardError, "^" + code + "$"):
            self.execute(transport)
        with sqlite3.connect(self.db) as db:
            rows = db.execute(
                "SELECT status,error_code,total_tokens,report,snapshot FROM runs"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        status, stored_code, tokens, report, snapshot = rows[0]
        self.assertEqual((status, stored_code, tokens), ("failed", code, expected_tokens))
        self.assertIsNone(report)
        self.assertGreater(len(snapshot), 2)
        # Saved records never include the provider's raw body or credentials.
        self.assertNotIn("sk-", repr(rows))
        self.assertNotIn("PRIVATE_PAYLOAD", repr(rows))

    def test_http_error_codes_without_secrets(self):
        for status, code in ((400, "openai_http_400"), (401, "openai_http_401"),
                             (403, "openai_http_403"), (429, "openai_http_429"),
                             (500, "openai_http_5xx"), (503, "openai_http_5xx"),
                             (451, "openai_http_other")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                self.db = Path(temporary) / "failed.sqlite"
                def provider(request, timeout):
                    raise urllib.error.HTTPError(
                        API_URL, status, "PRIVATE_PAYLOAD sk-PRIVATE_PAYLOAD", {},
                        None
                    )
                self.failure(provider, code)

    def test_timeout_and_network_unavailable_do_not_log_exception(self):
        cases = (
            (TimeoutError("PRIVATE_PAYLOAD sk-PRIVATE_PAYLOAD"), "openai_timeout"),
            (OSError("PRIVATE_PAYLOAD sk-PRIVATE_PAYLOAD"), "openai_network_unavailable"),
            (urllib.error.URLError(TimeoutError("PRIVATE_PAYLOAD")), "openai_timeout"),
        )
        for exc, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                self.db = Path(temporary) / "failed.sqlite"
                def provider(request, timeout):
                    raise exc
                self.failure(provider, code)

    def test_malformed_response_is_not_labeled_network_error(self):
        self.failure(
            lambda request, timeout: Response(b"PRIVATE_PAYLOAD not JSON"),
            "openai_json_invalid",
        )

    def test_output_limit_persists_verified_tokens_even_on_failure(self):
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 210, "output_tokens": 450, "total_tokens": 660},
            "output": [],
        }
        self.failure(
            lambda request, timeout: Response(json.dumps(payload).encode()),
            "openai_output_limit", expected_tokens=660,
        )
        with sqlite3.connect(self.db) as db:
            self.assertEqual(
                tuple(db.execute("SELECT input_tokens,output_tokens FROM runs").fetchone()),
                (210, 450),
            )
        attempts = []
        self.assertEqual(self.execute(lambda req, timeout: attempts.append(req))["new_api_calls"], 0)
        self.assertEqual(attempts, [])

    def test_other_incomplete_response_is_separate_code(self):
        payload = {"status": "incomplete", "output": [], "usage": {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        }}
        self.failure(
            lambda request, timeout: Response(json.dumps(payload).encode()),
            "openai_response_incomplete", expected_tokens=15,
        )

    def test_failed_credential_is_distinct_and_never_calls_openai(self):
        self.secret.unlink()
        requests = []
        with self.assertRaisesRegex(GuardError, "^credential_unavailable$"):
            self.execute(lambda req, timeout: requests.append(req))
        self.assertEqual(requests, [])
        with sqlite3.connect(self.db) as db:
            state = db.execute("SELECT status,error_code FROM runs").fetchone()
        self.assertEqual(state, ("failed", "credential_unavailable"))

    def test_response_with_invalid_usage_is_not_marked_successful(self):
        payload = {
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": json.dumps({
                "summary": "Prueba", "markets": [{
                    "market": "btcusdt", "verdict": "caution", "reason": "Prueba",
                }],
            })}]}],
            "usage": {"input_tokens": "10", "output_tokens": 20, "total_tokens": 30}
        }
        self.failure(
            lambda request, timeout: Response(json.dumps(payload).encode()),
            "openai_usage_invalid",
        )

    def test_successful_provider_still_records_exact_usage(self):
        provider = OpenAIFake()
        result = self.execute(provider)
        self.assertEqual(result["usage"]["total_tokens"], 135)
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT status,error_code,total_tokens FROM runs").fetchone()
        self.assertEqual(row, ("completed", None, 135))


if __name__ == "__main__":
    unittest.main()
