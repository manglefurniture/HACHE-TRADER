# HACHE-TRADER

Experimental **paper-trading-only** Spot scanner for SafeTrade with a private Telegram control interface. This project is isolated from HACHE-MINER and the Hache Natación production website.

> **No real-money trading is implemented or authorized.** `Policy(mode="real")` is rejected and `LiveExecutor` has no order transport. The API adapter permits only allowlisted HTTPS GET endpoints and does not send withdrawals, orders, or cancellations.

## Current functionality

- Scans enabled USDT pairs in bounded, rotating batches; never hard-codes a single coin.
- Filters by recent closed candles, published liquidity, market precision, spread, depth and volatility.
- Simulates limit orders, partial fills, cancellation, entry/exit planning, fees and persistent ledger accounting.
- Requires one position at a time. Default simulation policy: 50 USDT capital, 25 USDT per entry, 0.50 USDT anticipated loss per entry, 1.50 USDT daily realized-loss budget.
- Stops new entries after exchange-wide API failures, invalid account data, audit/reconciliation errors, risk control or a manual pause.
- Allows a *missing individual market* to be skipped; its unavailable data must not be interpreted as a profitable signal.
- Uses SQLite with a chained event audit trail, recovery reconciliation and an independent `STOP` marker.
- Has optional Telegram owner-only commands in Spanish; requires a **separate** bot token and exact chat/user IDs.

The decision engine currently uses transparent numerical rules, not an LLM. An AI adviser may be added later **without permission to bypass the risk engine**.

## Running tests (no network, no real funds)

```sh
python3 -m compileall -q hache_trader tests
python3 -m unittest discover -s tests -v
```

GitHub Actions runs the offline test suite and checks that live order creation remains impossible.

## One-shot public market scan

```sh
python3 -m hache_trader policy
python3 -m hache_trader diagnose --market pearlusdt
python3 -m hache_trader scan --state-dir ./local-paper-state
python3 -m hache_trader status --state-dir ./local-paper-state
```

The diagnostic command performs only public GET requests, does not create a trading ledger, and reports the failing API stage without revealing private data. An HTTP 200 or `ok=true` is **not** evidence of strategy profitability or execution readiness.\n\nA scan starts **paused**. The CLI never obtains trading permissions or moves USDT. When the public API returns 403/429, is stale, or fails validation, the scanner fails closed. Do not disable TLS, bypass exchange restrictions or invent market prices.

The canonical documented API host is `https://safe.trade/api/v2`. API responses must be checked again before a real implementation is considered. The account's actual fees, minimum order value, and execution quality are **not verified** by the public scanner.

## Operational controls

```sh
python3 -m hache_trader pause --state-dir ./local-paper-state
python3 -m hache_trader emergency --state-dir ./local-paper-state
python3 -m hache_trader reconcile --state-dir ./local-paper-state
```

After a healthy scan and reconciliation, a deliberate `resume` can enable *paper* entries; it **never** activates real money trading. An emergency marker blocks new buys and must not be removed casually. An emergency does not prove that an existing paper position has closed.

The Telegram interface accepts `/estado`, `/saldo`, `/mercados`, `/posiciones`, `/historial`, `/ganancias`, `/riesgo`, `/pausar`, `/reanudar`, and `/emergencia`. The `/reanudar` action requires a short-lived private confirmation and a recent healthy scan.

## Current limitations

- A paper fill is illustrative, not an exchange execution receipt; order queue position and real slippage are unknown.
- Current public fee values are not authenticated account fees. Profits are estimates, **not withdrawable gains**.
- Public endpoints can return HTTP 403 for individual routes on some networks. No production deployment should depend on an unverified endpoint.
- Actual account balances and private trading endpoints have not been validated with a read-only account key.
- A new independent Telegram bot, protected credentials, service packaging, operational monitoring and a full live-data soak test are still pending.
- The original local `tests/test_integrations.py` from the interrupted Work session is not in this repository; its transfer was blocked. Independent regression coverage has been added in `tests/test_exchange_telegram.py` and `tests/test_worker.py`, but it is not a claim of identical coverage.

## Deployment preparation\n\nSee `docs/OPERACION-Y-DESPLIEGUE.md` and the **not-installed** `deployment/systemd/hache-trader-paper.service` template. Installing a system service or connecting a new Telegram bot requires its own authorization and protected credentials.\n\n## Delivery policy

GitHub is the sole versioned source of truth. Keep this PR as a draft until CI, review, deployment prerequisites and paper-market stability have been verified. No manual production edits; any deployment must use an approved, reversible release and avoid modifying Hache Natación, mining, or other live services.
