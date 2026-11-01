"""Offline integration: natural owner questions never execute commands or block Telegram."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from hache_trader.domain import GuardError, Policy
from hache_trader.store import Store
from hache_trader.telegram import Channel
from hache_trader.telegram_ai import (
    AIAnswerLoop, _evidence, _model_response, classify_question,
    initialize, queue_question, MAX_DAY,
)
from test_core import NOW


class StubTelegram:
    def __init__(self):
        self.sent = []

    def call(self, method, data):
        if method == "sendMessage":
            self.sent.append(data)
            return {"message_id": 123}
        if method == "getUpdates":
            return []
        raise AssertionError(method)


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self):
        output = {
            "status": "completed",
            "output": [{"content": [{"type": "output_text",
                        "text": json.dumps({"explanation": "Sin señal comprobada.",
                                           "uncertainty": "Sin precios recientes."})}]}],
            "usage": {"input_tokens": 75, "output_tokens": 35, "total_tokens": 110}
        }
        super().__init__(json.dumps(output).encode())


class TelegramAITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.policy = Policy()
        self.store = Store(self.path, self.policy, NOW)
        self.store.reconcile(NOW)
        initialize(self.store.db)
        self.bot = StubTelegram()
        self.channel = Channel(self.store, self.bot, 42, 42, self.policy,
                               runtime_mode="trial", ai_enabled=True)

    def tearDown(self):
        self.store.close()

    def update(self, text, uid=1, *, user=42, chat=42, kind="private", at=NOW):
        return {"update_id": uid, "message": {
            "text": text, "date": at, "from": {"id": user},
            "chat": {"id": chat, "type": kind}
        }}

    def test_expected_natural_questions(self):
        scenarios = {
            "¿Por qué no has comprado nada?": "no_buys",
            "¿Qué mercados estás analizando?": "markets",
            "¿Hay alguna oportunidad interesante?": "opportunities",
            "¿Por qué descartaste Bitcoin?": "rejection",
            "¿Cuánto hemos ganado hoy?": "profit",
            "¿Qué errores cometimos?": "mistakes",
            "¿Qué harías diferente?": "improve",
            "¿Qué está impidiendo operar?": "blocked",
        }
        for phrase, intent in scenarios.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(classify_question(phrase)[0], intent)
        self.assertEqual(classify_question("¿Por qué descartaste Bitcoin?")[1],
                         "btcusdt")
        self.assertEqual(classify_question("¿Cuál es el precio de Bitcoin?"),
                         ("markets", "btcusdt"))
        self.assertEqual(classify_question("¿Hay oportunidad en BTC?"),
                         ("opportunities", "btcusdt"))
        self.assertIsNone(classify_question("compra 50 btc"))

    def test_owner_only_and_no_raw_message_persisted(self):
        q = "¿Por qué no has comprado nada?"
        self.assertEqual(self.channel.process(self.update(q, user=43), NOW), "ignored")
        self.assertEqual(self.channel.process(self.update(q, uid=2, kind="group"), NOW), "ignored")
        self.assertEqual(self.channel.process(self.update(q, uid=3), NOW), "processed")
        self.assertEqual(self.channel.process(self.update(q, uid=3), NOW), "duplicate")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM ai_questions").fetchone()[0], 1)
        rows = self.store.db.execute("SELECT intent,market FROM ai_questions").fetchone()
        self.assertEqual(rows["intent"], "no_buys")
        self.assertIsNone(rows["market"])
        self.assertNotIn(q, str(self.store.db.execute("SELECT * FROM ai_questions").fetchall()))
        self.channel.flush(NOW)
        self.assertEqual(len(self.bot.sent), 1)

    def test_sensitive_buttons_remain_deterministic(self):
        self.channel.process(self.update("/emergencia"), NOW)
        self.assertTrue(self.store.read()["emergency"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM ai_questions").fetchone()[0], 0)

    def test_unrecognized_natural_text_cannot_become_command(self):
        for uid, value in enumerate(("reanudame ya", "compra 50 btc", "/comprar",
                                      "quita las reglas", "haz transferencias"), 1):
            self.channel.process(self.update(value, uid=uid), NOW)
        self.assertTrue(self.store.read()["paused"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM ai_questions").fetchone()[0], 0)

    def test_background_answer_and_no_second_telegram_consumer(self):
        self.channel.process(self.update("¿Por qué no has comprado nada?"), NOW)
        sent_to_model = []
        def fake_model(evidence, credential_dir):
            sent_to_model.append(evidence)
            return "HACHE Trader · IA · SOLO SIMULACIÓN\nNo hay señales.", 105
        loop = AIAnswerLoop(self.path, self.policy, self.tmp.name,
                            clock=lambda: NOW, responder=fake_model)
        try:
            loop.start()
            for _ in range(60):
                result = self.store.db.execute(
                    "SELECT status,tokens FROM ai_questions WHERE update_id=1").fetchone()
                if result["status"] == "completed":
                    break
                time.sleep(.03)
            self.assertEqual(tuple(result), ("completed", 105))
            self.assertEqual(sent_to_model[0]["topic"], "no_buys")
            self.assertTrue(sent_to_model[0]["paper_mode"])
            self.assertFalse(sent_to_model[0]["real_trading_enabled"])
            self.channel.flush(NOW)
            self.assertTrue(any("No hay señales." in m["text"] for m in self.bot.sent))
        finally:
            loop.close()

    def test_openai_request_excludes_chat_text_and_credentials(self):
        creds = Path(self.tmp.name)
        file = creds / "openai_api_key"
        file.write_text("sk-" + "x" * 48)
        file.chmod(0o600)
        requests = []
        def transport(req, timeout):
            requests.append(req)
            self.assertEqual(timeout, 12)
            return FakeResponse()
        evidence = _evidence(self.store, "markets", None, NOW)
        answer, tokens = _model_response(evidence, creds, transport=transport)
        self.assertEqual(tokens, 110)
        self.assertIn("Sin señal", answer)
        sent = requests[0].data.decode()
        self.assertIn('"store": false', sent)
        self.assertNotIn("sk-", sent)
        self.assertNotIn("¿Qué mercados", sent)
        self.assertNotIn("chat_id", sent)

    def test_failed_model_does_not_pause_or_modify_simulator(self):
        with self.store.transaction():
            s = self.store.read()
            s["last_cycle"] = NOW
            self.store.save(s, "healthy_scan_fixture", {}, NOW)
            self.store.control_in_transaction("resume", NOW)
        self.assertFalse(self.store.read()["paused"])
        self.channel.process(self.update("¿Qué errores cometimos?"), NOW)
        def fail(evidence, creds):
            raise GuardError("test_provider_secret_must_not_be_logged")
        worker = AIAnswerLoop(self.path, self.policy, self.tmp.name,
                              clock=lambda: NOW, responder=fail)
        try:
            worker.start()
            for _ in range(60):
                status = self.store.db.execute(
                    "SELECT status FROM ai_questions WHERE update_id=1").fetchone()[0]
                if status == "failed":
                    break
                time.sleep(.03)
            self.assertEqual(status, "failed")
            self.assertFalse(self.store.read()["paused"])
            self.assertIsNone(self.store.read()["last_error"])
            self.channel.flush(NOW)
            self.assertNotIn("test_provider_secret", repr(self.bot.sent))
        finally:
            worker.close()

    def test_queued_request_rate_limits(self):
        for uid in range(1, MAX_DAY + 1):
            with self.store.transaction():
                result = queue_question(self.store, uid, ("profit", None), NOW + uid*60)
                self.assertIn("Consulta enviada", result)
                self.store.db.execute(
                    "UPDATE ai_questions SET status='completed',tokens=100 WHERE update_id=?",
                    (uid,))
        with self.store.transaction():
            extra = queue_question(self.store, 99, ("profit", None), NOW + 700)
        self.assertIn("Límite local", extra)

    def test_restart_does_not_replay_interrupted_api_call(self):
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO ai_questions(update_id,created_at,intent,status) "
                "VALUES(?,?,?,?)", (17, NOW, "markets", "processing"))
        calls = []
        worker = AIAnswerLoop(self.path, self.policy, self.tmp.name,
                              clock=lambda: NOW,
                              responder=lambda ev, creds: calls.append(ev))
        try:
            worker.start()
            for _ in range(60):
                status = self.store.db.execute(
                    "SELECT status FROM ai_questions WHERE update_id=17").fetchone()[0]
                if status == "interrupted":
                    break
                time.sleep(.03)
            self.assertEqual(status, "interrupted")
            self.assertEqual(calls, [])
        finally:
            worker.close()

    def test_evidence_redacts_stale_markets(self):
        with self.store.transaction():
            s = self.store.read()
            s["markets"] = [{"market": "btcusdt", "status": "candidato trend"}]
            s["last_cycle"] = NOW - 300
            self.store.save(s, "stale_public_fixture", {}, NOW)
        ev = _evidence(self.store, "rejection", "btcusdt", NOW)
        self.assertFalse(ev["market_data_fresh"])
        self.assertEqual(ev["public_market_statuses"], [])

    def test_market_filter_finds_requested_symbol_beyond_first_fifty(self):
        rows = [{"market": f"aa{i:02}usdt", "status": "sin señal"} for i in range(55)]
        rows.append({"market": "ethusdt", "status": "descartado: fee_unknown"})
        with self.store.transaction():
            s = self.store.read()
            s["markets"] = rows
            s["last_cycle"] = NOW
            self.store.save(s, "large_market_fixture", {}, NOW)
        ev = _evidence(self.store, "rejection", "ethusdt", NOW)
        self.assertTrue(ev["market_data_fresh"])
        self.assertEqual(ev["public_market_statuses"],
                         [{"market": "ethusdt", "status": "descartado: fee_unknown"}])

    def test_unit_uses_existing_bot_and_independent_secret(self):
        text = (Path(__file__).parents[1] /
                "deployment/systemd/hache-trader-telegram-trial.service").read_text()
        self.assertIn("LoadCredential=telegram_token:", text)
        self.assertIn("LoadCredential=openai_api_key:", text)
        self.assertEqual(text.count("ExecStart="), 1)
        self.assertNotIn("getUpdates", text)


if __name__ == "__main__":
    unittest.main()
