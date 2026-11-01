"""Public-only SafeTrade connection check. This module never reads credentials."""
import re
import time

from .domain import GuardError, money
from .runtime import Worker
from .safetrade import market_from_api


def diagnose(api, market_id="pearlusdt", *, clock=time.time):
    """Return a safe, bounded JSON-ready result without placing any orders."""
    result = {
        "mode": "SIMULACIÓN",
        "live_authorized": False,
        "market": market_id,
        "ok": False,
        "checks": [],
    }
    if not isinstance(market_id, str) or not re.fullmatch(r"[a-z0-9]{3,40}", market_id):
        result["error"] = "invalid_market"
        return result

    step = "markets"
    try:
        rows = api.markets()
        result["checks"].append(step)
        markets = {r["id"]: r for r in rows}
        result["markets_total"] = len(rows)
        result["usdt_markets"] = sum(
            1 for r in rows if r.get("quote_unit") == "usdt" and r.get("state") == "enabled"
        )
        row = markets.get(market_id)
        if row is None or row.get("state") != "enabled" or row.get("quote_unit") != "usdt":
            raise GuardError("market_unavailable")

        step = "fees"
        fees = Worker.fee_for(
            api.get("/trade/public/trading_fees", {"page": 1, "limit": 100}),
            market_id,
        )
        result["checks"].append(step)
        market = market_from_api(row, fees)

        step = "ticker"
        stamp, volume = api.ticker(market_id)
        if not 0 <= int(clock()) - stamp <= 30:
            raise GuardError("stale_ticker")
        result["checks"].append(step)

        step = "depth"
        book = api.book(market_id)
        book.fresh(int(clock()), 30)
        result["checks"].append(step)

        step = "candles"
        candles = api.candles(market_id)
        if not isinstance(candles, list):
            raise GuardError("invalid_candles")
        result["checks"].append(step)
        result.update({
            "ok": True,
            "public_fee_estimate": str(money(fees)),
            "volume_24h_reported": str(volume),
            "best_bid": str(book.bids[0][0]),
            "best_ask": str(book.asks[0][0]),
            "spread_pct": str(book.spread * 100),
            "candles_returned": len(candles),
            "note": "Datos públicos; ninguna orden, saldo privado ni rentabilidad comprobada",
        })
    except (GuardError, OSError, TypeError, KeyError, IndexError, ValueError):
        # Never expose URLs, response bodies, credentials, or untrusted exceptions.
        result["failure_stage"] = step
        result["error"] = "public_api_unavailable_or_invalid"
    return result
