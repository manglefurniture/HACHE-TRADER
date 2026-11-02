"""Continuous multi-market paper worker. Market errors stop new entries."""
import json
import os
import sqlite3
from pathlib import Path
import time
from .domain import GuardError, money
from .engine import PaperEngine
from .reports import report
from .safetrade import market_from_api
from .strategy import signals, study_signals, entry_plan
from .store import local_day
from .scan_diagnostics import append_scan


class ProcessLock:
    """OS-held lock released automatically on process death."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        if self.path.is_symlink():
            raise GuardError("unsafe_lock_path")
        self.file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if self.file.read(1) == b"":
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise GuardError("worker_already_running") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class Worker:
    def __init__(self, store, policy, api, kill_file, *, clock=time.time):
        self.store, self.policy, self.api, self.clock = store, policy, api, clock
        self.engine = PaperEngine(store, policy, kill_file)
        self.offset = 0

    def fees(self):
        rows = []
        for page in range(1, 21):
            batch = self.api.get("/trade/public/trading_fees", {"page": page, "limit": 100})
            if not isinstance(batch, list):
                raise GuardError("fee_schema_invalid")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise GuardError("fee_pagination_incomplete")

    def _market_signal(self, ident):
        """Use native timeframe evidence; never synthesize absent candles."""
        fallback = {"insufficient_candles", "stale_candles",
                    "candle_gap_or_duplicate", "irregular_volume"}
        last_error = None
        valid_data = False
        study_candidate = None
        for interval in (1, 5, 15):
            seconds = interval * 60
            try:
                # Provider may reject or omit sparse intervals as well as the
                # validator. Both recover only for allowlisted data gaps.
                candles = (self.api.candles(ident) if interval == 1
                           else self.api.candles(ident, period=interval))
                now = int(self.clock())
                ideas = signals(candles, now, period=seconds)
                valid_data = True
                if not ideas and interval != 1 and study_candidate is None:
                    exploratory = study_signals(candles, now, seconds)
                    if exploratory:
                        study_candidate = (candles, seconds, exploratory)
            except GuardError as exc:
                if str(exc) not in fallback:
                    raise
                last_error = exc
                continue
            if ideas:
                # Any genuine trend/pullback outranks a study-only hypothesis,
                # even if the study quota is exhausted on this calendar day.
                return candles, seconds, ideas
        if study_candidate is not None:
            return study_candidate
        if valid_data:
            return None, None, []
        raise last_error or GuardError("insufficient_candles")

    @staticmethod
    def fee_for(rows, market):
        # Conservative public estimate across groups; never claim account verification.
        fees = [money(row["taker"]) for row in rows if row.get("market_id") in (market, "any")]
        if not fees:
            raise GuardError("fee_unknown")
        return max(fees)

    def mark(self, market, book):
        with self.store.transaction():
            s = self.store.read()
            s["marks"][market.id] = {"bid": str(book.bids[0][0]), "fee": str(market.fee), "at": book.timestamp}
            self.store.save(s, "market_mark", {"market": market.id}, int(self.clock()))

    def _save_diagnostic(self, statuses, eligible, batch, signals, candidates, decision):
        # Observability must never change an execution or pause the engine.
        try:
            with self.store.transaction():
                append_scan(self.store, int(self.clock()), statuses=statuses,
                            eligible=len(eligible), checked=len(batch),
                            signals=signals, candidates=len(candidates),
                            decision=decision)
        except (sqlite3.Error, ValueError, RuntimeError):
            pass

    def cycle(self):
        try:
            return self._cycle()
        except (GuardError, KeyError, TypeError, OSError, ArithmeticError):
            now = int(self.clock())
            with self.store.transaction():
                s = self.store.read()
                changed = s["last_error"] != "market_or_execution_unavailable"
                s["last_error"], s["paused"] = "market_or_execution_unavailable", True
                self.store.save(s, "worker_failed_closed", {}, now)
                if changed:
                    self.store.enqueue("error:"+str(now), {"text": "SIMULACIÓN · Datos o ejecución no verificables. Entradas pausadas; las posiciones no se consideran cerradas."})
            return False

    def _cycle(self):
        now = int(self.clock())
        for ident, order in self.store.read()["orders"].items():
            if order["status"] in ("open", "partial"):
                self.engine.cancel(ident, now)
        fee_rows = self.fees()
        rows = self.api.markets()
        universe = {r["id"]: r for r in rows if r.get("quote_unit") == "usdt" and r.get("state") == "enabled"}
        held = self.store.read()["position"]
        # A cycle that manages an existing position cannot also open a new one.
        # This prevents immediate churn when a stop/target is hit.
        had_position_at_start = held is not None
        if held:
            if held["market"] not in universe:
                raise GuardError("held_market_unavailable")
            market = market_from_api(universe[held["market"]], self.fee_for(fee_rows, held["market"]))
            book = self.api.book(market.id)
            self.mark(market, book)
            exit_id = self.engine.manage_exit(market, book, int(self.clock()))
            if exit_id and self.store.read()["position"] is not None:
                # The engine queues the definitive closing alert atomically.
                # Do not send a second vague "exit evaluated" notification
                # after the position is fully closed.
                with self.store.transaction():
                    self.store.enqueue(exit_id, {"text": "SIMULACIÓN · Salida evaluada con posición restante. Consulta /posiciones para el remanente ficticio."})
        tickers = self.api.get("/trade/public/tickers")
        if not isinstance(tickers, dict):
            raise GuardError("tickers_schema_invalid")
        eligible, statuses = [], []
        for ident in sorted(universe):
            ticker = tickers.get(ident)
            if not ticker or money(ticker["volume"]) < money(self.policy.min_quote_volume):
                statuses.append({"market": ident, "status": "volumen insuficiente o ausente"})
            else:
                eligible.append(ident)
                statuses.append({"market": ident, "status": "pendiente de análisis de profundidad y velas"})
        candidates = []
        signal_markets = 0
        # Rotate bounded batches so a large universe cannot starve exit/Telegram checks.
        batch = [eligible[(self.offset+i) % len(eligible)] for i in range(min(5, len(eligible)))] if eligible else []
        self.offset = (self.offset + len(batch)) % max(1, len(eligible))
        for ident in batch:
            status = next(x for x in statuses if x["market"] == ident)
            try:
                market = market_from_api(universe[ident], self.fee_for(fee_rows, ident))
                candles, period, choices = self._market_signal(ident)
                if not choices:
                    status["status"] = "sin señal explicable"
                    continue
                book = self.api.book(ident)
                self.mark(market, book)
                signal_markets += 1
                is_study = choices[0].strategy == "study_probe"
                plan = entry_plan(market, book, choices[0], money(tickers[ident]["volume"]),
                                  self.policy, int(self.clock()),
                                  notional_cap="10" if is_study else None,
                                  target_cap="0.20" if is_study else None)
                candidates.append((choices[0].score, market, plan, candles, period))
                status["status"] = ("candidato " + choices[0].strategy + " (" +
                                    str(period // 60) + "m, beneficio no garantizado)")
            except GuardError as exc:
                reason = str(exc)
                # A missing or invalid individual market is not a reason to
                # disable healthy markets. Exchange-wide auth/rate/transport
                # failures must still pause all new entries (fail closed).
                if reason in ("circuit_open", "safetrade_transport_or_schema_error",
                              "stale_http_response") or (
                                  reason.startswith("safetrade_http_") and
                                  reason != "safetrade_http_404"):
                    raise
                status["status"] = "descartado: " + reason
        now = int(self.clock())
        with self.store.transaction():
            s = self.store.read()
            s["last_cycle"], s["last_error"], s["markets"] = now, None, statuses
            self.store.save(s, "scan_completed", {"universe": len(universe), "analyzed": batch}, now)
        s = self.store.read()
        entries_today = sum(
            1 for o in s["orders"].values()
            if o["side"] == "buy" and local_day(o["created"]) == local_day(now)
        )
        # A training probe consumes a smaller fictitious notional and is
        # limited to ONE attempt per local day, even if cancelled/unfilled.
        study_today = sum(
            1 for ident, order in s["orders"].items()
            if order["side"] == "buy" and ident.endswith(":study_probe")
            and local_day(order["created"]) == local_day(now)
        )
        executable = ([c for c in candidates if c[2]["strategy"] != "study_probe"]
                      if study_today else candidates)
        # Explain the decision without affecting existing hard risk limits.
        if candidates and not executable:
            diagnostic_decision = "study_daily_limit"
        elif not candidates:
            diagnostic_decision = "no_qualified_opportunity"
        elif entries_today >= 3:
            diagnostic_decision = "daily_limit"
        elif had_position_at_start or s["position"]:
            diagnostic_decision = "position_present"
        elif s["emergency"]:
            diagnostic_decision = "emergency"
        elif s["paused"]:
            diagnostic_decision = "paused"
        elif self.engine.kill_file.exists():
            diagnostic_decision = "stop_file"
        else:
            diagnostic_decision = "other_safety_gate"
        try:
            if (executable and entries_today < 3 and not had_position_at_start
                    and not s["position"] and not s["paused"] and not s["emergency"]
                    and not self.engine.kill_file.exists()):
                _, market, selected_plan, candles, period = max(executable, key=lambda x: (x[0], x[1].id))
                # Revalidate the winning market after the batch; never execute old depth.
                stamp, volume = self.api.ticker(market.id)
                book = self.api.book(market.id)
                now = int(self.clock())
                if not 0 <= now - stamp <= self.policy.max_age:
                    raise GuardError("stale_ticker")
                confirmed = (study_signals(candles, now, period)
                             if selected_plan["strategy"] == "study_probe"
                             else signals(candles, now, period=period))
                choice = next((v for v in confirmed
                               if v.strategy == selected_plan["strategy"]), None)
                if choice is None:
                    raise GuardError("signal_expired")
                is_study = choice.strategy == "study_probe"
                plan = entry_plan(market, book, choice, volume, self.policy, now,
                                  notional_cap="10" if is_study else None,
                                  target_cap="0.20" if is_study else None)
                ident = f"entry:{market.id}:{now//60}:{choice.strategy}"
                # A decision for the same market/minute/strategy is one-shot even
                # after a fill, cancel or restart. Do not reuse its ID with new
                # price/quantity terms or execute the historical order again.
                if ident not in self.store.read()["orders"]:
                    self.engine.submit(ident, market, "buy", plan["quantity"], plan["price"], now,
                                       stop=plan["stop"], target=plan["target"], expected_loss=plan["expected_loss"])
                    execution_book = self.api.book(market.id)
                    filled = self.engine.match(ident, market, execution_book, int(self.clock()))
                    self.engine.cancel(ident, int(self.clock()))
                    diagnostic_decision = "entry_filled" if filled > 0 else "entry_unfilled"
                    with self.store.transaction():
                        self.store.enqueue(ident, {"text": "SIMULACIÓN · Entrada procesada en " + market.id + ". Consulta /historial; puede ser parcial o no ejecutada."})
                else:
                    diagnostic_decision = "duplicate_entry_id"
        except (GuardError, KeyError, TypeError, OSError, ArithmeticError) as exc:
            # Preserve the existing fail-closed behavior while recording which
            # deterministic risk gate prevented the simulated entry.
            risk = {
                "daily_loss_budget_exhausted": "risk_daily_loss",
                "risk_plan_understates_loss": "risk_plan_rejected",
                "exposure_or_cash_exceeded": "risk_exposure",
                "paper_daily_entry_limit": "daily_limit",
                "entries_blocked": "execution_blocked",
                "stale_ticker": "execution_data_invalid",
                "stale_worker": "execution_data_invalid",
            }
            code = str(exc) if isinstance(exc, GuardError) else ""
            diagnostic_decision = risk.get(code, "execution_validation_failed")
            self._save_diagnostic(statuses, eligible, batch, signal_markets,
                                  candidates, diagnostic_decision)
            raise
        with self.store.transaction():
            day = local_day(now)
            self.store.enqueue("daily:"+day, {"text": "SIMULACIÓN · Informe diario\n" + json.dumps(report(self.store, now, "day"), ensure_ascii=False)[:3000]})
        self._save_diagnostic(statuses, eligible, batch, signal_markets,
                              candidates, diagnostic_decision)
        return True
