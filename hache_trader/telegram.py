"""Independent owner-only Telegram channel with atomic inbox/outbox."""
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import urllib.error
import urllib.request
from .domain import GuardError, canonical
from .reports import render
from .safetrade import NoRedirect
from .telegram_ai import classify_question, queue_question

READ = frozenset(("start", "estado", "saldo", "mercados", "posiciones", "historial", "ganancias", "diario", "semanal", "riesgo", "ayuda"))


def read_secret(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise GuardError("unsafe_secret_path")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="ascii") as f:
        meta = os.fstat(f.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 256:
            raise GuardError("unsafe_secret_file")
        if os.name == "posix":
            if meta.st_uid not in (0, os.geteuid()):
                raise GuardError("unsafe_secret_permissions")
            mode = stat.S_IMODE(meta.st_mode)
            # systemd LoadCredential may grant access to a DynamicUser via a
            # read-only group ACL (mode 0440), even when the source was 0600.
            # Trust that exception ONLY in the service's credential directory.
            credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
            systemd_credential = bool(credentials_dir) and (
                path.parent == Path(credentials_dir)
                and path.name == "telegram_token"
            )
            if systemd_credential:
                parent = os.stat(path.parent, follow_symlinks=False)
                if (not stat.S_ISDIR(parent.st_mode)
                        or parent.st_uid not in (0, os.geteuid())
                        or stat.S_IMODE(parent.st_mode) & 0o022):
                    raise GuardError("unsafe_secret_permissions")
            if mode & 0o007 or mode & 0o030 or (
                mode & 0o040 and not systemd_credential
            ):
                raise GuardError("unsafe_secret_permissions")
        return f.read(257).strip()


class TelegramAPI:
    def __init__(self, token, transport=None, clock=time.time):
        if not re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,90}", token):
            raise GuardError("invalid_telegram_token")
        self._endpoint = "https://api.telegram.org/bot" + token + "/"
        self.transport = transport or urllib.request.build_opener(NoRedirect()).open
        self.clock, self.retry_at = clock, 0

    def call(self, method, data):
        if method not in ("getUpdates", "sendMessage", "answerCallbackQuery", "getMe", "getWebhookInfo"):
            raise GuardError("telegram_method_not_allowed")
        if self.clock() < self.retry_at:
            raise GuardError("telegram_cooldown")
        req = urllib.request.Request(self._endpoint + method, data=canonical(data).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            # Polls are non-long-polling (timeout=0). Bound a hung HTTPS
            # connection so a confirmation cannot sit behind a 35-second call.
            with self.transport(req, timeout=10 if method == "getUpdates" else 15) as response:
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise GuardError("telegram_response_too_large")
                result = json.loads(raw)
            if not isinstance(result, dict) or "result" not in result:
                raise GuardError("telegram_response_invalid")
            if result.get("ok") is not True:
                raise GuardError("telegram_rejected")
            return result["result"]
        except GuardError:
            self.retry_at = self.clock() + 60
            raise
        except (ValueError, KeyError, TypeError):
            self.retry_at = self.clock() + 60
            raise GuardError("telegram_response_invalid") from None
        except OSError:
            self.retry_at = self.clock() + 60
            # Never surface the URL (contains bot token) or provider body.
            raise GuardError("telegram_unavailable") from None


def menu(*, allow_resume=True, ai_enabled=False):
    actions = ["estado", "saldo", "mercados", "posiciones", "historial", "ganancias", "riesgo", "pausar", "emergencia", "diario", "semanal"]
    if ai_enabled:
        actions.append("ia")
    if allow_resume:
        actions.insert(actions.index("emergencia"), "reanudar")
    return {"inline_keyboard": [[{"text": a.capitalize(), "callback_data": "menu:" + a} for a in actions[i:i+2]] for i in range(0, len(actions), 2)]}


class Channel:
    def __init__(self, store, api, chat_id, user_id, policy, *, allow_resume=True, runtime_mode="paper", ai_enabled=False):
        if any(type(x) is not int or x <= 0 for x in (chat_id, user_id)):
            raise GuardError("owner_ids_required")
        self.store, self.api, self.chat, self.user, self.policy = store, api, chat_id, user_id, policy
        if runtime_mode not in ("paper", "observe", "serve", "trial"):
            raise GuardError("invalid_runtime_mode")
        self.allow_resume = allow_resume and runtime_mode in ("paper", "trial")
        self.runtime_mode = runtime_mode
        self.ai_enabled = bool(ai_enabled) and runtime_mode == "trial"

    def process(self, update, now):
        if type(update) is not dict or type(update.get("update_id")) is not int or not 0 <= update["update_id"] < 2**63:
            raise GuardError("invalid_telegram_update")
        ident = update["update_id"]
        callback = update.get("callback_query")
        message = callback.get("message", {}) if isinstance(callback, dict) else update.get("message", {})
        if not isinstance(message, dict):
            return "ignored"
        origin = callback.get("from", {}) if isinstance(callback, dict) else message.get("from", {})
        chat = message.get("chat", {})
        if not isinstance(origin, dict) or not isinstance(chat, dict):
            return "ignored"
        authorized = (type(origin.get("id")) is int and origin["id"] == self.user and origin.get("is_bot") is not True
                      and type(chat.get("id")) is int and chat["id"] == self.chat and chat.get("type") == "private")
        with self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM updates WHERE id=?", (ident,)).fetchone():
                return "duplicate"
            self.store.db.execute("INSERT INTO updates VALUES(?)", (ident,))
            if not authorized:
                return "ignored"
            action = callback.get("data", "") if callback else message.get("text", "")
            if not isinstance(action, str) or len(action) > 200:
                return "ignored"
            raw_action = action
            # Ignore forwarded messages and stale text commands.
            if not callback and (message.get("forward_origin") or type(message.get("date")) is not int or not 0 <= now-message["date"] <= 300):
                return "ignored"
            action = action.removeprefix("menu:") if callback else action.removeprefix("/")
            confirmation = None
            if action in ("pausar", "emergencia"):
                self.store.control_in_transaction("pause" if action == "pausar" else "emergency", now)
                text = "SIMULACIÓN · Entradas bloqueadas. Las posiciones siguen abiertas hasta confirmar sus ejecuciones simuladas."
            elif action == "reanudar" and not self.allow_resume:
                text = "SIMULACIÓN · Radar de solo lectura: no hay compras ni ventas que reanudar."
            elif action == "reanudar":
                confirmation = secrets.token_hex(16)
                self.store.db.execute("INSERT INTO confirmations(nonce,expires) VALUES(?,?)", (confirmation, now+120))
                text = "SIMULACIÓN · Confirmar reanudación del agente simulado (vigencia 120 s). No habilita dinero real."
            elif action.startswith("resume:") and callback and not self.allow_resume:
                text = "SIMULACIÓN · Reanudación bloqueada en modo radar de solo lectura."
            elif action.startswith("resume:") and callback:
                nonce = action.removeprefix("resume:")
                row = self.store.db.execute("SELECT * FROM confirmations WHERE nonce=?", (nonce,)).fetchone()
                if not row or row["used"] or row["expires"] <= now or row["message_id"] != message.get("message_id") or row["message_id"] is None:
                    text = "SIMULACIÓN · Confirmación vencida, repetida o no vinculada."
                else:
                    try:
                        self.store.control_in_transaction("resume", now)
                        text = "SIMULACIÓN · Agente reanudado dentro de la política."
                    except GuardError:
                        text = "SIMULACIÓN · No se puede reanudar: revisar conciliación, emergencia y datos recientes."
                    self.store.db.execute("UPDATE confirmations SET used=1 WHERE nonce=?", (nonce,))
            elif action in READ:
                text = render(self.store, action, now, self.policy, runtime_mode=self.runtime_mode)
            elif action == "ia" and self.ai_enabled:
                text = ("SIMULACIÓN · Soy HACHE Trader con análisis OpenAI. "
                        "Pregunta por mercados, oportunidades, descartes, ganancias, "
                        "errores o por qué no hubo compras. "
                        "Solo consultas informativas; las acciones requieren botones.")
            elif self.ai_enabled and not callback and not raw_action.startswith("/"):
                classified = classify_question(raw_action)
                text = (queue_question(self.store, ident, classified, now)
                        if classified else
                        "SIMULACIÓN · Puedo explicar mercados, ganancias y decisiones ficticias. "
                        "Escribe /ia para ejemplos. No ejecuto instrucciones en lenguaje natural.")
            else:
                text = render(self.store, "ayuda", now, self.policy, runtime_mode=self.runtime_mode)
            markup = {"inline_keyboard": [[{"text": "Confirmar simulación", "callback_data": "resume:"+confirmation}]]} if confirmation else menu(allow_resume=self.allow_resume, ai_enabled=self.ai_enabled)
            self.store.enqueue("update:" + str(ident), {"text": text, "reply_markup": markup, "confirmation": confirmation})
        return "processed"

    def flush(self, now):
        rows = self.store.db.execute(
            "SELECT * FROM outbox WHERE sent=0 "
            "ORDER BY CASE WHEN id LIKE 'paper:closed:%' THEN 0 ELSE 1 END, rowid LIMIT 10"
        ).fetchall()
        for row in rows:
            body = json.loads(row["body"])
            nonce = body.pop("confirmation", None)
            if nonce:
                confirmation = self.store.db.execute("SELECT expires FROM confirmations WHERE nonce=?", (nonce,)).fetchone()
                if not confirmation or confirmation[0] <= now:
                    body = {"text": "SIMULACIÓN · Confirmación vencida. Solicita /reanudar otra vez."}
                    nonce = None
            receipt = self.api.call("sendMessage", {"chat_id": self.chat, **body})
            if not isinstance(receipt, dict) or type(receipt.get("message_id")) is not int:
                raise GuardError("invalid_telegram_receipt")
            with self.store.transaction():
                if nonce:
                    self.store.db.execute("UPDATE confirmations SET message_id=? WHERE nonce=?", (receipt["message_id"], nonce))
                self.store.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))

    def poll(self, now):
        last = self.store.db.execute("SELECT MAX(id) FROM updates").fetchone()[0]
        updates = self.api.call("getUpdates", {"offset": (last+1) if last is not None else 0, "limit": 100, "timeout": 0, "allowed_updates": ["message", "callback_query"]})
        if not isinstance(updates, list) or len(updates) > 100:
            raise GuardError("invalid_telegram_batch")
        for update in sorted(updates, key=lambda u: u["update_id"]):
            self.process(update, now)
            callback = update.get("callback_query")
            if isinstance(callback, dict):
                callback_id = callback.get("id")
                if isinstance(callback_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", callback_id):
                    # Telegram buttons otherwise stay spinning. Expired callback IDs
                    # are not financial failures and must not block outbox delivery.
                    try:
                        self.api.call("answerCallbackQuery", {"callback_query_id": callback_id})
                    except GuardError:
                        pass
        self.flush(now)
