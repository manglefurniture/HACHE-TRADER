"""Pure deterministic domain types. Monetary values never use binary floats."""
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import re


class GuardError(ValueError):
    """Safe, constant error code suitable for operator reports."""


def money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise GuardError("invalid_decimal_type")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise GuardError("invalid_decimal") from None
    if not result.is_finite() or abs(result) > Decimal("1e18") or result.as_tuple().exponent < -40:
        raise GuardError("decimal_out_of_bounds")
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    version: int = 1
    capital: str = "50"
    max_order: str = "25"
    daily_loss: str = "1.50"
    trade_loss: str = "0.50"
    target_net: str = "0.30"
    max_spread: str = "0.004"
    max_slippage: str = "0.002"
    min_quote_volume: str = "10000"
    max_volatility: str = "0.03"
    participation: str = "0.10"
    max_age: int = 30
    order_ttl: int = 60
    max_positions: int = 1
    mode: str = "paper"

    def __post_init__(self):
        if self.mode != "paper" or type(self.version) is not int or self.version < 1:
            raise GuardError("live_mode_disabled")
        limits = {"capital": "50", "max_order": "25", "daily_loss": "1.50",
                  "trade_loss": "0.50", "target_net": "0.40", "max_spread": "0.004",
                  "max_slippage": "0.002", "max_volatility": "0.03", "participation": "0.10"}
        for name, maximum in limits.items():
            if not 0 < money(getattr(self, name)) <= money(maximum):
                raise GuardError("policy_exceeds_protected_limit")
        if money(self.min_quote_volume) < 10000 or money(self.target_net) < money("0.20"):
            raise GuardError("policy_below_protected_minimum")
        if type(self.max_positions) is not int or self.max_positions != 1:
            raise GuardError("one_position_only")
        if any(type(v) is not int for v in (self.max_age, self.order_ttl)) or not 1 <= self.max_age <= 30 or not 1 <= self.order_ttl <= 60:
            raise GuardError("invalid_policy_time")
        if money(self.max_order) > money(self.capital):
            raise GuardError("order_exceeds_capital")

    @property
    def fingerprint(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class Market:
    id: str
    base: str
    quote: str
    min_amount: Decimal
    min_total: Decimal
    amount_precision: int
    price_precision: int
    fee: Decimal
    fee_source: str
    verified: bool = False
    min_price: Decimal = Decimal("0")
    max_price: Decimal = Decimal("1e18")

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9]{3,40}", self.id) or not re.fullmatch(r"[a-z0-9]{1,20}", self.base) or self.quote != "usdt":
            raise GuardError("spot_usdt_only")
        if self.id != self.base + self.quote:
            raise GuardError("market_identity_mismatch")
        if any(type(x) is not int or not 0 <= x <= 12 for x in (self.amount_precision, self.price_precision)):
            raise GuardError("invalid_precision")
        if money(self.min_amount) <= 0 or money(self.min_total) < 0 or not 0 <= money(self.fee) <= money("0.02"):
            raise GuardError("invalid_market_rules")
        if self.fee_source not in ("verified_account", "public_estimate", "fixture"):
            raise GuardError("unknown_fee_source")

    def quantity(self, amount):
        return money(amount).quantize(Decimal(1).scaleb(-self.amount_precision), rounding=ROUND_DOWN)

    def price(self, amount):
        return money(amount).quantize(Decimal(1).scaleb(-self.price_precision), rounding=ROUND_DOWN)

    def validate_order(self, quantity, price):
        if quantity <= 0 or price <= 0 or quantity != self.quantity(quantity) or price != self.price(price):
            raise GuardError("invalid_order_precision")
        if quantity < self.min_amount or quantity * price < self.min_total:
            raise GuardError("below_market_minimum")
        if not self.min_price <= price <= self.max_price:
            raise GuardError("price_outside_market_limits")


@dataclass(frozen=True)
class Book:
    market: str
    timestamp: int
    bids: tuple
    asks: tuple
    sequence: int = 0

    def __post_init__(self):
        if type(self.timestamp) is not int or self.timestamp <= 0 or not self.bids or not self.asks:
            raise GuardError("missing_book")
        for levels, reverse in ((self.bids, True), (self.asks, False)):
            if len(levels) > 1000 or any(len(row) != 2 or money(row[0]) <= 0 or money(row[1]) <= 0 for row in levels):
                raise GuardError("invalid_book")
            prices = [row[0] for row in levels]
            if prices != sorted(set(prices), reverse=reverse):
                raise GuardError("unordered_book")
        if self.bids[0][0] >= self.asks[0][0]:
            raise GuardError("crossed_book")

    def fresh(self, now, max_age=30):
        if type(now) is not int or not 0 <= now - self.timestamp <= max_age:
            raise GuardError("stale_or_future_book")

    @property
    def fingerprint(self):
        return digest([self.market, self.sequence, [[str(p), str(q)] for p, q in self.bids],
                       [[str(p), str(q)] for p, q in self.asks]])

    @property
    def spread(self):
        return (self.asks[0][0] - self.bids[0][0]) / self.asks[0][0]


class LiveExecutor:
    """Deliberate hard interlock: no transport, credentials, or enable flag."""
    def submit(self, *args, **kwargs):
        raise GuardError("live_execution_requires_separate_audited_release")

    cancel = submit
    withdraw = submit
