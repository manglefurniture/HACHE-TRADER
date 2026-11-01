"""Paper limit-order state machine. All fills and cash changes are atomic."""
from pathlib import Path
from .domain import GuardError, digest, money
from .store import local_day


class PaperEngine:
    def __init__(self, store, policy, kill_file):
        self.store, self.policy, self.kill_file = store, policy, Path(kill_file)

    def _day(self, s, now):
        day = local_day(now)
        if day != s["day"]:
            s["day"], s["day_loss"] = day, "0"

    def submit(self, ident, market, side, quantity, price, now, *, stop=None, target=None, expected_loss=None):
        if side not in ("buy", "sell") or not isinstance(ident, str) or not 1 <= len(ident) <= 100:
            raise GuardError("invalid_order")
        quantity, price = money(quantity), money(price)
        market.validate_order(quantity, price)
        terms = [market.id, side, str(quantity), str(price), str(stop), str(target)]
        with self.store.transaction():
            s = self.store.read()
            self._day(s, now)
            if ident in s["orders"]:
                if s["orders"][ident]["terms"] != digest(terms):
                    raise GuardError("idempotency_conflict")
                return ident
            if not market.verified or not s["reconciled"]:
                raise GuardError("unverified_or_unreconciled")
            if any(o["status"] in ("open", "partial") for o in s["orders"].values()):
                raise GuardError("active_order_exists")
            reserve = money(0)
            if side == "buy":
                # Hard ceiling independent of policy/config: protect the $50
                # paper trial from repeated intraday entry attempts.
                entries_today = sum(
                    1 for o in s["orders"].values()
                    if o["side"] == "buy" and local_day(o["created"]) == local_day(now)
                )
                if entries_today >= 3:
                    raise GuardError("paper_daily_entry_limit")
                if self.kill_file.exists() or s["paused"] or s["emergency"] or s["last_error"]:
                    raise GuardError("entries_blocked")
                if not 0 <= now - s["last_cycle"] <= 120:
                    raise GuardError("stale_worker")
                if s["position"]:
                    raise GuardError("one_position_no_averaging")
                if stop is None or target is None or expected_loss is None:
                    raise GuardError("risk_plan_required")
                stop, target, expected_loss = money(stop), money(target), money(expected_loss)
                if not 0 < stop < price < target:
                    raise GuardError("invalid_exit_plan")
                reserve = quantity * price * (1 + market.fee)
                worst = reserve - quantity * stop * (1 - money(self.policy.max_slippage)) * (1 - market.fee)
                if worst > money(self.policy.trade_loss) or expected_loss < worst:
                    raise GuardError("risk_plan_understates_loss")
                if money(s["day_loss"]) + worst > money(self.policy.daily_loss):
                    raise GuardError("daily_loss_budget_exhausted")
                if reserve > money(self.policy.max_order) or reserve > money(s["cash"]):
                    raise GuardError("exposure_or_cash_exceeded")
                s["cash"] = str(money(s["cash"]) - reserve)
                s["reserved"] = str(money(s["reserved"]) + reserve)
            elif not s["position"] or s["position"]["market"] != market.id or quantity > money(s["position"]["quantity"]):
                raise GuardError("sell_exceeds_position")
            s["orders"][ident] = {"terms": digest(terms), "market": market.id, "side": side,
                "quantity": str(quantity), "remaining": str(quantity), "price": str(price), "reserve": str(reserve),
                "fee": str(market.fee), "status": "open", "created": now, "expires": now + self.policy.order_ttl,
                "stop": str(stop), "target": str(target), "filled": "0"}
            self.store.save(s, "order_submitted_paper", {"id": ident}, now)
        return ident

    def cancel(self, ident, now):
        with self.store.transaction():
            s = self.store.read()
            order = s["orders"][ident]
            if order["status"] not in ("open", "partial"):
                return
            s["cash"] = str(money(s["cash"]) + money(order["reserve"]))
            s["reserved"] = str(money(s["reserved"]) - money(order["reserve"]))
            order["reserve"], order["status"] = "0", "cancelled"
            self.store.save(s, "paper_order_cancelled", {"id": ident}, now)

    def match(self, ident, market, book, now):
        book.fresh(now, self.policy.max_age)
        with self.store.transaction():
            s = self.store.read()
            self._day(s, now)
            o = s["orders"][ident]
            if o["status"] not in ("open", "partial"):
                return money(0)
            if o["market"] != market.id or book.market != market.id:
                raise GuardError("book_order_mismatch")
            if now >= o["expires"]:
                raise GuardError("order_expired")
            if book.timestamp < o["created"]:
                raise GuardError("book_precedes_order")
            last = s["last_books"].get(market.id)
            # Never reuse a depth snapshot, even across restart or multiple orders.
            if last and (book.timestamp <= last["timestamp"] or book.fingerprint == last["fingerprint"] or (book.sequence and book.sequence <= last.get("sequence", 0))):
                raise GuardError("book_already_consumed")
            if o["side"] == "buy" and (self.kill_file.exists() or s["paused"] or s["emergency"]):
                raise GuardError("entries_blocked")
            remaining, filled = money(o["remaining"]), money(0)
            fee, limit = money(o["fee"]), money(o["price"])
            levels = book.asks if o["side"] == "buy" else book.bids
            for price, visible in levels:
                if (o["side"] == "buy" and price > limit) or (o["side"] == "sell" and price < limit):
                    break
                quantity = min(remaining, market.quantity(visible * money(self.policy.participation)))
                if quantity <= 0:
                    continue
                gross, charge = quantity * price, quantity * price * fee
                s["fees"] = str(money(s["fees"]) + charge)
                if o["side"] == "buy":
                    released = quantity * limit * (1 + fee)
                    o["reserve"] = str(money(o["reserve"]) - released)
                    s["reserved"] = str(money(s["reserved"]) - released)
                    s["cash"] = str(money(s["cash"]) + released - gross - charge)
                    pos = s["position"] or {"market": market.id, "quantity": "0", "cost": "0",
                                           "stop": o["stop"], "target": o["target"], "opened": now,
                                           "trade_net": "0", "trade_fees": "0"}
                    pos["quantity"] = str(money(pos["quantity"]) + quantity)
                    pos["cost"] = str(money(pos["cost"]) + gross + charge)
                    # Keep estimated fees on the position so its complete net
                    # result can be reported after one or several sell fills.
                    if "trade_fees" in pos:
                        pos["trade_fees"] = str(money(pos["trade_fees"]) + charge)
                    s["position"] = pos
                else:
                    pos = s["position"]
                    basis = money(pos["cost"]) * quantity / money(pos["quantity"])
                    net, realized = gross - charge, gross - charge - basis
                    s["cash"] = str(money(s["cash"]) + net)
                    s["realized"] = str(money(s["realized"]) + realized)
                    s["day_loss"] = str(money(s["day_loss"]) + max(money(0), -realized))
                    complete_history = "trade_net" in pos and "trade_fees" in pos
                    if complete_history:
                        pos["trade_net"] = str(money(pos["trade_net"]) + realized)
                        pos["trade_fees"] = str(money(pos["trade_fees"]) + charge)
                    pos["quantity"] = str(money(pos["quantity"]) - quantity)
                    pos["cost"] = str(money(pos["cost"]) - basis)
                    if money(pos["quantity"]) == 0:
                        s["position"] = None
                        s["closed_trades"] += 1
                        name = market.base.upper() + "/USDT"
                        if complete_history:
                            pnl = money(pos["trade_net"])
                            fees = money(pos["trade_fees"])
                            result = ("Ganancia" if pnl > 0 else
                                      "Pérdida" if pnl < 0 else "Sin ganancia ni pérdida")
                            detail = (f"{result}: {pnl:+.6f} USDT netos ficticios\n"
                                      f"Comisiones estimadas (compra y ventas): {fees:.6f} USDT\n")
                        else:
                            # An open position from an older release may have
                            # been partially sold previously. Never invent
                            # a full-position P&L from incomplete history.
                            detail = "Neto individual no disponible (posición anterior a la actualización).\n"
                        self.store.enqueue("paper:closed:" + str(s["closed_trades"]), {
                            "text": ("SIMULACIÓN · Posición CERRADA: " + name + "\n"
                                     + detail
                                     + f"Saldo ficticio disponible: {money(s['cash']):.6f} USDT\n"
                                     + "No se utilizaron fondos reales.")
                        })
                remaining -= quantity
                filled += quantity
                if remaining == 0:
                    break
            o["remaining"] = str(remaining)
            o["filled"] = str(money(o["filled"]) + filled)
            o["status"] = "filled" if remaining == 0 else "partial" if money(o["filled"]) else "open"
            s["last_books"][market.id] = {"timestamp": book.timestamp, "fingerprint": book.fingerprint, "sequence": book.sequence}
            self.store.save(s, "paper_match", {"id": ident, "filled": str(filled), "book": book.fingerprint}, now)
            return filled

    def manage_exit(self, market, book, now):
        s = self.store.read()
        pos = s["position"]
        if not pos or pos["market"] != market.id:
            return None
        book.fresh(now, self.policy.max_age)
        bid = book.bids[0][0]
        estimated_loss = money(pos["cost"]) - money(pos["quantity"]) * bid * (1 - market.fee)
        if not (s["emergency"] or bid <= money(pos["stop"]) or bid >= money(pos["target"]) or estimated_loss >= money(self.policy.trade_loss) or estimated_loss + money(s["day_loss"]) >= money(self.policy.daily_loss)):
            return None
        price = market.price(bid * (1 - money(self.policy.max_slippage)))
        ident = f"exit:{market.id}:{book.timestamp}"
        self.submit(ident, market, "sell", money(pos["quantity"]), price, now)
        self.match(ident, market, book, now)
        self.cancel(ident, now)  # IOC remainder; position remains visible if partial.
        return ident
