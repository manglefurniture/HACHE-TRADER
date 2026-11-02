"""Bounded, auditable scan diagnostics; no changes to trading decisions.

Counts represent evaluations, not distinct markets or guaranteed opportunities.
Every write is called after a completed paper cycle and uses the existing
Store transaction. Only fixed reason codes are persisted.
"""
from collections import Counter
import json
import re

from .store import local_day

MAX_REASON_CODES = 25
RETAIN_SECONDS = 7 * 86400

_REASON_LABELS = {
    "volume_below_minimum": "volumen insuficiente",
    "not_evaluated_this_cycle": "pendiente de su turno",
    "no_signal": "sin señal",
    "qualified": "superó los filtros",
    "insufficient_candles": "velas insuficientes",
    "stale_candles": "velas desactualizadas",
    "candle_gap_or_duplicate": "velas incompletas",
    "irregular_volume": "volumen irregular",
    "fee_unknown": "comisiones no verificadas",
    "safetrade_http_404": "datos de mercado no disponibles",
    "stale_or_future_book": "profundidad desactualizada",
    "insufficient_liquidity_or_spread": "liquidez o spread insuficiente",
    "excessive_volatility": "volatilidad excesiva",
    "insufficient_depth": "profundidad insuficiente",
    "predicted_loss_exceeds_limit": "riesgo estimado superior al límite",
    "other_market_filter": "otro filtro del mercado",
}
_DECISIONS = {
    "no_qualified_opportunity": "ningún mercado superó todos los filtros",
    "paused": "simulación pausada",
    "emergency": "emergencia activa",
    "position_present": "posición existente o cerrada durante el ciclo",
    "daily_limit": "límite de entradas diarias alcanzado",
    "stop_file": "parada de seguridad activa",
    "duplicate_entry_id": "entrada del mismo mercado y minuto ya evaluada",
    "entry_filled": "entrada ficticia ejecutada al menos parcialmente",
    "entry_unfilled": "orden ficticia enviada sin ejecución",
}


def _reason(status):
    if status == "volumen insuficiente o ausente":
        return "volume_below_minimum"
    if status == "pendiente de análisis de profundidad y velas":
        return "not_evaluated_this_cycle"
    if status == "sin señal explicable":
        return "no_signal"
    if status.startswith("candidato "):
        return "qualified"
    if status.startswith("descartado: "):
        code = status.removeprefix("descartado: ")
        return code if code in _REASON_LABELS else "other_market_filter"
    return "other_market_filter"


def append_scan(store, now, *, statuses, eligible, checked, signals, candidates, decision):
    """Existing Store transaction required. Never persists provider exception text."""
    if not store.db.in_transaction:
        raise RuntimeError("scan_diagnostics_requires_transaction")
    if decision not in _DECISIONS:
        raise ValueError("invalid_diagnostic_decision")
    counts = Counter(_reason(str(item["status"])) for item in statuses)
    # Keys are fixed allowlisted values and counts are nonnegative integers.
    counts = {k: int(v) for k, v in counts.items() if k in _REASON_LABELS and v > 0}
    if (len(counts) > MAX_REASON_CODES or min(eligible, checked, signals, candidates) < 0
            or checked > eligible or candidates > signals or signals > checked):
        raise ValueError("invalid_diagnostic_counts")
    store.db.execute(
        """INSERT INTO scan_diagnostics
           (at,day,universe,eligible,checked,signal_markets,qualified,reasons,decision)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (int(now), local_day(int(now)), len(statuses), eligible, checked,
         signals, candidates, json.dumps(counts, sort_keys=True), decision),
    )
    store.db.execute("DELETE FROM scan_diagnostics WHERE at<?",
                     (int(now) - RETAIN_SECONDS,))


def render_diagnostics(store, now):
    """Read-only daily summary for the owner; no API calls."""
    today = local_day(int(now))
    rows = store.db.execute(
        "SELECT at,universe,eligible,checked,signal_markets,qualified,"
        "reasons,decision FROM scan_diagnostics WHERE day=? ORDER BY id",
        (today,),
    ).fetchall()
    if not rows:
        return ("HACHE-TRADER · SIMULACIÓN\n"
                "Diagnóstico: todavía no hay ciclos registrados desde la actualización. "
                "La estrategia no ha sido modificada.")
    reasons = Counter()
    decisions = Counter()
    for row in rows:
        reasons.update(json.loads(row["reasons"]))
        decisions[row["decision"]] += 1
    examined = sum(row["checked"] for row in rows)
    signals = sum(row["signal_markets"] for row in rows)
    qualified = sum(row["qualified"] for row in rows)
    last = rows[-1]
    age = int(now) - last["at"]
    freshness = ("Último registro: hace " + str(age) + " s."
                 if 0 <= age <= 180 else "Último registro: desactualizado.")
    # Exclude pending markets from rejections, and do not confuse repeated
    # evaluations with unique instruments.
    excluded = {"not_evaluated_this_cycle", "qualified"}
    top = sorted(
        ((n, c) for n, c in reasons.items() if n not in excluded),
        key=lambda x: (-x[1], x[0]),
    )[:5]
    parts = [
        "HACHE-TRADER · SIMULACIÓN",
        "Diagnóstico del día (Cancún)",
        "Ciclos completados: " + str(len(rows)),
        "Mercados examinados: " + str(examined) + " evaluaciones, no mercados únicos",
        "Evaluaciones con señal: " + str(signals),
        "Evaluaciones que superaron filtros: " + str(qualified),
        "Último ciclo: " + str(last["checked"]) + " examinados de "
        + str(last["eligible"]) + " elegibles; " + str(last["universe"]) + " mercados listados.",
        freshness,
        "Última decisión: " + _DECISIONS[last["decision"]] + ".",
    ]
    if top:
        parts.append("Motivos más frecuentes (conteo de evaluaciones):")
        parts.extend("- " + _REASON_LABELS[k] + ": " + str(v) for k, v in top)
    parts.append("No se cuentan como descartes los mercados pendientes de su turno.")
    parts.append("Sin órdenes reales; no se alteraron los controles de riesgo.")
    return "\n".join(parts)
