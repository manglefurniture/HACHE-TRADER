"""Read-only reports: realized P&L never includes open positions."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from .domain import money
from .store import local_day


def report(store, now, period="all"):
    s = store.read()
    position, unrealized, equity = s["position"], None, None
    if position:
        mark = s["marks"].get(position["market"])
        if mark and 0 <= now - mark["at"] <= 30:
            unrealized = money(position["quantity"]) * money(mark["bid"]) * (1 - money(mark["fee"])) - money(position["cost"])
    else:
        unrealized = money(0)
    if unrealized is not None:
        equity = money(s["capital"]) + money(s["realized"]) + unrealized
    pnl, fees = money(0), money(0)
    previous_pnl, previous_fees = money(0), money(0)
    for row in store.db.execute("SELECT at,payload FROM events ORDER BY id"):
        event = json.loads(row["payload"])["state"]
        included = period == "all" or (period == "day" and local_day(row["at"]) == local_day(now)) or (period == "week" and now - 7 * 86400 <= row["at"] <= now)
        if included:
            pnl += money(event["realized"]) - previous_pnl
            fees += money(event["fees"]) - previous_fees
        previous_pnl, previous_fees = money(event["realized"]), money(event["fees"])
    return {"mode": "SIMULACIÓN", "cash": s["cash"], "reserved": s["reserved"], "capital": s["capital"],
            "realized": str(pnl), "fees": str(fees), "unrealized_estimate": str(unrealized) if unrealized is not None else None,
            "equity_estimate": str(equity) if equity is not None else None,
            "return_pct_estimate": str((equity / money(s["capital"]) - 1) * 100) if equity is not None else None,
            "position": position, "paused": s["paused"], "emergency": s["emergency"], "reconciled": s["reconciled"],
            "day_loss": s["day_loss"], "closed_trades": s["closed_trades"], "simulation_days": max(0, (now-s["started"]) // 86400),
            "sample_target_met": now - s["started"] >= 14 * 86400 and s["closed_trades"] >= 100,
            "live_authorized": False, "last_error": s["last_error"], "last_cycle": s["last_cycle"], "period": period,
            "timezone": "America/Cancun", "markets": s["markets"]}


_REASONS = {
    "insufficient_candles": "velas recientes insuficientes",
    "stale_candles": "velas desactualizadas",
    "candle_gap_or_duplicate": "velas incompletas",
    "irregular_volume": "volumen irregular",
    "safetrade_http_404": "mercado sin datos disponibles",
    "fee_unknown": "comisiones no verificadas",
    "stale_or_future_book": "libro de órdenes desactualizado",
    "wrong_market_depth": "libro de órdenes incorrecto",
}


def _market_message(raw):
    status = str(raw)
    if status.startswith("descartado: "):
        code = status[len("descartado: "):]
        return "descartado: " + _REASONS.get(code, code.replace("_", " "))
    return status


def render(store, action, now, policy, *, runtime_mode="paper"):
    r = report(store, now, "day" if action == "diario" else "week" if action == "semanal" else "all")
    prefix = "HACHE-TRADER · SIMULACIÓN\n"
    state = store.read()
    observer = runtime_mode == "observe"
    at = r["last_cycle"]
    timestamp = (datetime.fromtimestamp(at, ZoneInfo("America/Cancun")).strftime("%d/%m/%Y %H:%M") + " (Cancún)") if at else "sin lecturas"
    if action in ("estado", "start"):
        if observer:
            return (prefix + "Radar: " + state.get("observer_state", "iniciando") + " (solo consulta pública)\n"
                    + "Operaciones automáticas: deshabilitadas\n"
                    + f"Último barrido: {timestamp}\n"
                    + f"Mercados elegibles por volumen: {state.get('observer_eligible', 0)}\n"
                    + f"Error: {r['last_error'] or 'ninguno'}\n"
                    + f"Posiciones abiertas: {'sí' if r['position'] else 'no'}")
        return prefix + f"Pausado: {r['paused']} · Emergencia: {r['emergency']}\nConciliado: {r['reconciled']}\nÚltimo ciclo: {timestamp}\nError: {r['last_error'] or 'ninguno'}\nOperaciones cerradas: {r['closed_trades']}"
    if action in ("saldo", "ganancias", "diario", "semanal"):
        return prefix + f"Período: {r['period']}\nDisponible: {r['cash']} USDT\nReservado: {r['reserved']} USDT\nRealizado neto: {r['realized']} USDT\nNo realizado estimado: {r['unrealized_estimate'] or 'no disponible'}\nComisiones: {r['fees']} USDT\nLos saldos son simulados; la valoración no garantiza liquidación."
    if action == "posiciones":
        return prefix + (json.dumps(r["position"], ensure_ascii=False) if r["position"] else "Sin posiciones abiertas.")
    if action == "mercados":
        if not r["markets"]:
            return prefix + "Radar Spot USDT: todavía no hay lecturas verificadas."
        header = (f"Radar Spot USDT · {timestamp}\n"
                  f"Mercados elegibles por volumen: {state.get('observer_eligible', 'no disponible')}\n"
                  + ("Sin órdenes ni movimientos de fondos.\n" if observer
                     else "Consultar /historial para operaciones simuladas.\n"))
        rows = []
        for entry in r["markets"][:25]:
            ident = entry["market"]
            symbol = ident[:-4].upper() + "/USDT" if ident.endswith("usdt") else ident.upper()
            rows.append(f"{symbol}: {_market_message(entry['status'])}")
        return prefix + header + "\n".join(rows)
    if action == "historial":
        orders = list(store.read()["orders"].items())[-10:]
        return prefix + "\n".join(f"{key}: {o['side']} {o['market']} · {o['status']} · ejecutado {o['filled']}" for key, o in orders)
    if action == "riesgo":
        return prefix + f"Capital {policy.capital}; por operación {policy.max_order}; posiciones 1.\nPérdida diaria acumulada {r['day_loss']} / {policy.daily_loss} USDT.\nPérdida prevista por operación ≤ {policy.trade_loss}.\nObjetivo neto {policy.target_net}; no garantizado. Sin apalancamiento ni retiros."
    if observer:
        return prefix + "/estado /saldo /mercados /posiciones /historial /ganancias /diario /semanal /riesgo /pausar /emergencia /ayuda\nRadar de solo lectura. No ejecuta órdenes ni permite reanudar operaciones."
    return prefix + "/estado /saldo /mercados /posiciones /historial /ganancias /diario /semanal /riesgo /pausar /reanudar /emergencia /ayuda\nPausar bloquea entradas. Emergencia solicita salidas simuladas si hay datos y liquidez; no garantiza cerrar."
