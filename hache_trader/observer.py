"""Read-only market radar. It never creates orders, positions or reserves."""
import time

from .domain import GuardError, money
from .safetrade import market_from_api
from .strategy import signals


class MarketObserver:
    """Observe a bounded, rotating subset of USDT markets with no execution path."""

    def __init__(self, store, policy, api, *, clock=time.time, batch_size=3):
        if not 1 <= batch_size <= 5:
            raise GuardError("invalid_observer_batch")
        self.store, self.policy, self.api = store, policy, api
        self.clock, self.batch_size = clock, batch_size

    def _fees(self):
        result = []
        for page in range(1, 21):
            rows = self.api.get("/trade/public/trading_fees", {"page": page, "limit": 100})
            if not isinstance(rows, list):
                raise GuardError("fee_schema_invalid")
            result.extend(rows)
            if len(rows) < 100:
                if not result:
                    raise GuardError("fees_catalog_unavailable")
                return result
        raise GuardError("fee_pagination_incomplete")

    @staticmethod
    def _fee_for(rows, ident):
        rates = [money(x["taker"]) for x in rows if
                 isinstance(x, dict) and x.get("market_id") in (ident, "any")]
        if not rates:
            raise GuardError("fee_unknown")
        return max(rates)

    def cycle(self):
        now = int(self.clock())
        try:
            return self._scan(now)
        except (GuardError, KeyError, TypeError, OSError, ArithmeticError):
            with self.store.transaction():
                state = self.store.read()
                first_failure = state["last_error"] != "observer_market_data_unavailable"
                state["paused"] = True
                state["last_error"] = "observer_market_data_unavailable"
                state["observer_state"] = "sin datos verificables"
                self.store.save(state, "observer_failed_closed", {}, now)
                if first_failure:
                    self.store.enqueue("observer:error:" + str(now), {
                        "text": "SIMULACIÓN · Radar SafeTrade sin datos verificables. "
                                "Se conserva la pausa y no se ejecutan operaciones."
                    })
            return False

    def _scan(self, now):
        # Remote data is consulted only through SafeTrade's GET allowlist.
        fees = self._fees()
        markets = self.api.markets()
        ticker_data = self.api.get("/trade/public/tickers")
        if not isinstance(ticker_data, dict):
            raise GuardError("tickers_schema_invalid")
        universe = {m["id"]: m for m in markets
                    if m.get("quote_unit") == "usdt" and m.get("state") == "enabled"}
        eligible = []
        for ident, row in universe.items():
            ticker = ticker_data.get(ident)
            if not isinstance(ticker, dict):
                continue
            try:
                volume = money(ticker["volume"])
                if volume >= money(self.policy.min_quote_volume):
                    eligible.append((ident, row, volume))
            except (GuardError, KeyError):
                continue
        eligible.sort(key=lambda x: x[0])
        old = self.store.read()
        offset = old.get("observer_offset", 0) % max(1, len(eligible))
        batch = ([eligible[(offset + i) % len(eligible)]
                  for i in range(min(self.batch_size, len(eligible)))]
                 if eligible else [])
        statuses = []
        for ident, row, volume in batch:
            try:
                market = market_from_api(row, self._fee_for(fees, ident))
                candles = self.api.candles(ident)
                book = self.api.book(ident)
                book.fresh(int(self.clock()), self.policy.max_age)
                if book.market != ident:
                    raise GuardError("wrong_market_depth")
                if book.spread > money(self.policy.max_spread):
                    message = "spread demasiado amplio"
                else:
                    found = signals(candles, int(self.clock()))
                    if not found:
                        message = "sin señal"
                    elif found[0].volatility > money(self.policy.max_volatility):
                        message = "volatilidad excesiva"
                    else:
                        message = "señal orientativa " + found[0].strategy + " (sin órdenes)"
                statuses.append({"market": ident, "status": message})
            except GuardError as exc:
                reason = str(exc)
                # Missing/invalid data for one market is not an outage.
                local = {"safetrade_http_404", "insufficient_candles",
                         "invalid_candle", "invalid_candle_prices", "future_candle",
                         "stale_candles", "candle_gap_or_duplicate", "irregular_volume",
                         "missing_book", "invalid_book", "unordered_book",
                         "crossed_book", "stale_or_future_book", "invalid_book_sequence",
                         "depth_schema_unverified", "market_schema_unverified",
                         "fee_unknown",
                         "invalid_market_rules", "market_disabled", "invalid_precision",
                         "spot_usdt_only", "market_identity_mismatch",
                         "wrong_market_depth"}
                if reason not in local:
                    raise
                statuses.append({"market": ident, "status": "descartado: " + reason})
        with self.store.transaction():
            state = self.store.read()
            state["markets"] = statuses
            state["last_cycle"] = now
            state["last_error"] = None
            state["observer_state"] = "solo lectura"
            state["observer_offset"] = (offset + len(batch)) % max(1, len(eligible))
            state["observer_universe"] = len(universe)
            state["observer_eligible"] = len(eligible)
            # Observing markets must never authorize an entry after /reanudar.
            state["paused"] = True
            self.store.save(state, "public_markets_observed",
                            {"inspected": [x[0] for x in batch],
                             "eligible": len(eligible), "universe": len(universe)}, now)
            if old["last_error"] == "observer_market_data_unavailable":
                self.store.enqueue("observer:recovered:" + str(now), {
                    "text": "SIMULACIÓN · Radar SafeTrade recuperó el acceso público. "
                            "Solo observación; entradas aún bloqueadas."
                })
        return True
