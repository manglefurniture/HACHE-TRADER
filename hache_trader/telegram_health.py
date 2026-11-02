"""Persist one safe fail-closed Telegram outage transition per incident.

The market scanner and the private Telegram controller use separate SQLite
connections. Never log provider exceptions or token-bearing request URLs.
"""
from .domain import GuardError

_TELEGRAM_FAILURES = frozenset((
    "telegram_unavailable", "telegram_cooldown", "telegram_rejected",
    "invalid_telegram_batch", "invalid_telegram_update",
    "invalid_telegram_receipt", "telegram_response_too_large",
))


def record_telegram_failure(store, now, reason):
    """Pause on sustained transient failures or immediately on critical errors."""
    code = reason if reason in _TELEGRAM_FAILURES else "telegram_control_error"
    with store.transaction():
        state = store.read()
        if state.get("telegram_connection") == "unavailable" and state["paused"]:
            return False
        was_running = not state["paused"]
        state["paused"] = True
        state["telegram_connection"] = "unavailable"
        state["telegram_resume_required"] = (
            bool(state.get("telegram_resume_required")) or was_running
        )
        store.save(state, "telegram_connection_lost", {"category": code}, now)
        if was_running:
            store.enqueue("telegram:pause:" + str(now), {
                "text": "SIMULACIÓN · Se pausaron nuevas entradas por un fallo de "
                        "comunicación con Telegram. No se cerraron posiciones. "
                        "Cuando vuelva la conexión, comprueba /estado y usa "
                        "/reanudar con confirmación si quieres continuar."
            })
    return True


def record_telegram_recovery(store, now):
    """Recovery never automatically resumes simulated orders."""
    with store.transaction():
        state = store.read()
        if state.get("telegram_connection") != "unavailable":
            return False
        state["telegram_connection"] = "connected"
        store.save(state, "telegram_connection_restored", {}, now)
        store.enqueue("telegram:restored:" + str(now), {
            "text": "SIMULACIÓN · Telegram recuperó la conexión. "
                    "Las entradas permanecen "
                    + ("PAUSADAS hasta confirmar /reanudar."
                       if state["paused"] else "según su estado previo.")
                    + " Consulta /estado."
        })
    return True
