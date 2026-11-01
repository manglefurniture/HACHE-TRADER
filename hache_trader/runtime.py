"""Continuous multi-market paper worker. Market errors stop new entries."""
import json
import os
from pathlib import Path
import time
from .domain import GuardError, money
from .engine import PaperEngine
from .reports import report
from .safetrade import market_from_api
from .strategy import signals, entry_plan
from .store import local_day


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
        # Rotate bounded batches so a large universe cannot starve exit/Telegram checks.
        batch = [eligible[(self.offset+i) % len(eligible)] for i in range(min(5, len(eligible)))] if eligible else []
        self.offset = (self.offset + len(batch)) % max(1, len(eligible))
        for ident in batch:
            status = next(x for x in statuses if x["market"] == ident)
            try:
                market = market_from_api(universe[ident], self.fee_for(fee_rows, ident))
                candles = self.api.candles(ident)
                book = self.api.book(ident)
                self.mark(market, book)
                choices = signals(candles, int(self.clock()))
                if not choices:
                    status["status"] = "sin señal explicable"
                    continue
                plan = entry_plan(market, book, choices[0], money(tickers[ident]["volume"]), self.policy, int(self.clock()))
                candidates.append((choices[0].score, market, plan, candles))
                status["status"] = "candidato " + choices[0].strategy + " (beneficio no garantizado)"
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
        if (candidates and entries_today < 3 and not had_position_at_start
                and not s["position"] and not s["paused"] and not s["emergency"]
                and not self.engine.kill_file.exists()):
            _, market, _, candles = max(candidates, key=lambda x: (x[0], x[1].id))
            # Revalidate the winning market after the batch; never execute old depth.
            stamp, volume = self.api.ticker(market.id)
            book = self.api.book(market.id)
            now = int(self.clock())
            if not 0 <= now - stamp <= self.policy.max_age:
                raise GuardError("stale_ticker")
            choice = signals(candles, now)[0]
            plan = entry_plan(market, book, choice, volume, self.policy, now)
            ident = f"entry:{market.id}:{now//60}:{choice.strategy}"
            # A decision for the same market/minute/strategy is one-shot even
            # after a fill, cancel or restart. Do not reuse its ID with new
            # price/quantity terms or execute the historical order again.
            if ident not in self.store.read()["orders"]:
                self.engine.submit(ident, market, "buy", plan["quantity"], plan["price"], now,
                                   stop=plan["stop"], target=plan["target"], expected_loss=plan["expected_loss"])
                execution_book = self.api.book(market.id)
                self.engine.match(ident, market, execution_book, int(self.clock()))
                self.engine.cancel(ident, int(self.clock()))
                with self.store.transaction():
                    self.store.enqueue(ident, {"text": "SIMULACIÓN · Entrada procesada en " + market.id + ". Consulta /historial; puede ser parcial o no ejecutada."})
        with self.store.transaction():
            day = local_day(now)
            self.store.enqueue("daily:"+day, {"text": "SIMULACIÓN · Informe diario\n" + json.dumps(report(self.store, now, "day"), ensure_ascii=False)[:3000]})
        return True
