"""Allowlisted GET-only SafeTrade adapter; no order or withdrawal transport."""
from decimal import Decimal
import hashlib
import hmac
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from .domain import Book, GuardError, Market, money


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SafeTrade:
    BASE = "https://safe.trade/api/v2"
    PUBLIC = re.compile(r"/trade/public/(markets(?:/[a-z0-9]{3,40}(?:/(?:depth|k-line|trades))?)?|tickers(?:/[a-z0-9]{3,40})?|trading_fees)")
    PRIVATE = frozenset(("/trade/account/balances/spot", "/trade/market/orders", "/trade/market/trades"))

    def __init__(self, *, transport=None, clock=time.time, sleep=time.sleep, credentials=None):
        self.transport = transport or urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect()).open
        self.clock, self.sleep, self.credentials = clock, sleep, credentials
        self.last_request, self.failures, self.blocked_until, self.nonce = 0, 0, 0, 0
        self.received_at = 0

    def get(self, path, query=None):
        private = path in self.PRIVATE
        if not self.PUBLIC.fullmatch(path) and not private:
            raise GuardError("endpoint_not_allowed")
        if private and not self.credentials:
            raise GuardError("readonly_credentials_missing")
        query = query or {}
        if any(k not in {"page", "limit", "market", "period", "time_from", "time_to", "state", "market_id"} or isinstance(v, bool) or not isinstance(v, (int, str)) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", str(v)) for k, v in query.items()):
            raise GuardError("invalid_query")
        if self.clock() < self.blocked_until:
            raise GuardError("circuit_open")
        wait = max(0, 1 - (self.clock() - self.last_request))
        if wait:
            self.sleep(wait)
        headers = {"Accept": "application/json", "User-Agent": "HACHE-TRADER/0.1 public-paper"}
        if private:
            key, secret = self.credentials
            self.nonce = max(int(self.clock() * 1000), self.nonce + 1)
            nonce = str(self.nonce)
            headers.update({"X-Auth-Apikey": key, "X-Auth-Nonce": nonce,
                            "X-Auth-Signature": hmac.new(secret.encode(), (nonce + key).encode(), hashlib.sha256).hexdigest()})
        url = self.BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        request = urllib.request.Request(url, headers=headers, method="GET")
        self.last_request = self.clock()
        try:
            with self.transport(request, timeout=12) as response:
                if response.status != 200:
                    raise GuardError("unexpected_http_status")
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise GuardError("response_too_large")
                result = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(result, (list, dict)):
                    raise GuardError("invalid_response")
                self.received_at = int(self.clock())
                server_date = parsedate_to_datetime(response.headers.get("Date", ""))
                if server_date.tzinfo is None or not -5 <= self.received_at - server_date.timestamp() <= 30 or int(response.headers.get("Age", "0")) > 30:
                    raise GuardError("stale_http_response")
        except urllib.error.HTTPError as exc:
            # A listed pair can lack a public orderbook or candles. Its 404 is
            # a per-market absence, not an exchange-wide outage or rate limit.
            if exc.code == 404 and path.startswith("/trade/public/"):
                raise GuardError("safetrade_http_404") from None
            self.failures += 1
            cooldown = min(300, 2 ** min(self.failures, 8))
            if exc.code == 429:
                retry = exc.headers.get("Retry-After", "60")
                cooldown = max(60, min(int(retry), 3600)) if retry.isdigit() else 60
            self.blocked_until = self.clock() + cooldown
            raise GuardError("safetrade_http_" + str(exc.code)) from None
        except (OSError, ValueError) as exc:
            self.failures += 1
            self.blocked_until = self.clock() + min(300, 2 ** min(self.failures, 8))
            raise GuardError("safetrade_transport_or_schema_error") from None
        self.failures = 0
        return result

    def markets(self):
        rows = self.get("/trade/public/markets")
        if not isinstance(rows, list) or len(rows) > 2000 or len({r["id"] for r in rows}) != len(rows):
            raise GuardError("markets_schema_unverified")
        return rows

    def book(self, market):
        raw = self.get(f"/trade/public/markets/{market}/depth", {"limit": 100})
        try:
            # API provides sequence, not matching-engine timestamp. HTTP Date bounds
            # response freshness only; immutable sequence prevents replayed depth fills.
            if type(raw["sequence"]) is not int or raw["sequence"] <= 0:
                raise GuardError("invalid_book_sequence")
            return Book(market, self.received_at, tuple((money(p), money(q)) for p, q in raw["bids"]),
                        tuple((money(p), money(q)) for p, q in raw["asks"]), raw["sequence"])
        except (KeyError, TypeError, ValueError):
            raise GuardError("depth_schema_unverified") from None

    def candles(self, market):
        now = int(self.clock())
        return self.get(f"/trade/public/markets/{market}/k-line", {"period": 1, "limit": 30, "time_from": now - 1800, "time_to": now})

    def ticker(self, market):
        raw = self.get(f"/trade/public/tickers/{market}")
        try:
            if raw["id"] != market or raw["quote_unit"] != "usdt":
                raise GuardError("ticker_market_mismatch")
            return self.received_at, money(raw["volume"])
        except (KeyError, TypeError, ValueError):
            raise GuardError("ticker_schema_unverified") from None


def market_from_api(row, fee, *, schema_verified=True, fee_source="public_estimate"):
    try:
        if row["state"] != "enabled":
            raise GuardError("market_disabled")
        return Market(row["id"], row["base_unit"], row["quote_unit"], money(row["min_amount"]),
                      money(0), row["amount_precision"], row["price_precision"],
                      money(fee), fee_source, schema_verified, money(row["min_price"]), money(row["max_price"]))
    except (KeyError, TypeError):
        raise GuardError("market_schema_unverified") from None
