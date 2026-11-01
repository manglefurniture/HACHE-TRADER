"""Explainable trend/pullback signals from CLOSED candles only."""
from dataclasses import dataclass
from decimal import Decimal
from .domain import GuardError, money


@dataclass(frozen=True)
class Signal:
    strategy: str
    score: Decimal
    volatility: Decimal
    reason: str


def signals(candles, now, period=60):
    if not isinstance(candles, list) or len(candles) < 21:
        raise GuardError("insufficient_candles")
    rows = []
    for row in candles:
        if len(row) < 6 or type(row[0]) is not int:
            raise GuardError("invalid_candle")
        stamp, op, high, low, close, volume = row[:6]
        op, high, low, close, volume = map(money, (op, high, low, close, volume))
        if min(op, high, low, close) <= 0 or volume < 0 or not low <= min(op, close) <= max(op, close) <= high:
            raise GuardError("invalid_candle_prices")
        if stamp > now:
            raise GuardError("future_candle")
        if stamp + period <= now:
            rows.append((stamp, close, volume, (high - low) / close))
    rows = rows[-20:]
    if len(rows) < 20 or now - rows[-1][0] > period * 2:
        raise GuardError("stale_candles")
    if any(b[0] - a[0] != period for a, b in zip(rows, rows[1:])):
        raise GuardError("candle_gap_or_duplicate")
    volumes = [r[2] for r in rows]
    if min(volumes) <= 0 or max(volumes) > min(volumes) * 20:
        raise GuardError("irregular_volume")
    prices = [r[1] for r in rows]
    fast = sum(prices[-5:]) / 5
    slow = sum(prices) / 20
    vol = max(r[3] for r in rows)
    results = []
    if fast > slow and prices[-1] > prices[-2] and prices[-1] >= fast:
        results.append(Signal("trend", fast / slow - 1, vol, "MA5 > MA20; latest closed candle rising"))
    if fast > slow and prices[-2] < prices[-3] and prices[-1] > prices[-2] and prices[-1] <= fast * money("1.002"):
        results.append(Signal("pullback", fast / slow - 1, vol, "Uptrend with pullback and closed-candle recovery"))
    return sorted(results, key=lambda x: x.score, reverse=True)


def entry_plan(market, book, signal, quote_volume, policy, now):
    book.fresh(now, policy.max_age)
    if book.market != market.id or not market.verified:
        raise GuardError("unverified_market")
    if book.spread > money(policy.max_spread) or money(quote_volume) < money(policy.min_quote_volume):
        raise GuardError("insufficient_liquidity_or_spread")
    if signal.volatility > money(policy.max_volatility):
        raise GuardError("excessive_volatility")
    price = market.price(book.asks[0][0] * (1 + money(policy.max_slippage)))
    quantity = market.quantity(money(policy.max_order) / (price * (1 + market.fee)))
    market.validate_order(quantity, price)
    # Both sides must support this size at the conservative participation fraction.
    buy_depth = sum((q for p, q in book.asks if p <= price), money(0)) * money(policy.participation)
    exit_floor = book.bids[0][0] * (1 - money(policy.max_slippage))
    sell_depth = sum((q for p, q in book.bids if p >= exit_floor), money(0)) * money(policy.participation)
    if min(buy_depth, sell_depth) < quantity:
        raise GuardError("insufficient_depth")
    stop_distance = max(signal.volatility, money("0.005"))
    stop = market.price(book.bids[0][0] * (1 - stop_distance))
    cost = quantity * price * (1 + market.fee)
    loss = cost - quantity * stop * (1 - money(policy.max_slippage)) * (1 - market.fee)
    if loss > money(policy.trade_loss):
        raise GuardError("predicted_loss_exceeds_limit")
    target = market.price((cost + money(policy.target_net)) / (quantity * (1 - market.fee)))
    # Round target upward by a tick so net target is not overstated.
    if quantity * target * (1 - market.fee) - cost < money(policy.target_net):
        target += Decimal(1).scaleb(-market.price_precision)
    return {"quantity": quantity, "price": price, "stop": stop, "target": target,
            "expected_loss": loss, "strategy": signal.strategy}
