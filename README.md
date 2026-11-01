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

The currently deployed Telegram bot uses `serve --telegram`, reporting the simulated 50 USDT balance without running a market worker. A reviewed observer upgrade will change the unit to `observe --telegram`: it will scan **public market data only**, never submit orders, and keep simulated entries paused. The existing bot stays unchanged until that upgrade is installed.\n\nThe Telegram interface accepts `/estado`, `/saldo`, `/mercados`, `/posiciones`, `/historial`, `/ganancias`, `/riesgo`, `/pausar`, `/reanudar`, and `/emergencia`. In the observer-only mode `/reanudar` is blocked, including callbacks left from previous versions; it can never enable orders.

## Current limitations

- A paper fill is illustrative, not an exchange execution receipt; order queue position and real slippage are unknown.
- Current public fee values are not authenticated account fees. Profits are estimates, **not withdrawable gains**.
- Public endpoints can return HTTP 403 for individual routes on some networks. No production deployment should depend on an unverified endpoint.
- Actual account balances and private trading endpoints have not been validated with a read-only account key.
- A new independent Telegram bot, protected credentials, service packaging, operational monitoring and a full live-data soak test are still pending.
- The original local `tests/test_integrations.py` from the interrupted Work session is not in this repository; its transfer was blocked. Independent regression coverage has been added in `tests/test_exchange_telegram.py` and `tests/test_worker.py`, but it is not a claim of identical coverage.

## Próxima fase: radar público sin operaciones (PR #3)

Antes de reemplazar el servicio actual, puede comprobarse la conexión pública de forma independiente con `python3 -m hache_trader observe-once --state-dir <ruta-aislada>`. No necesita el token del bot ni consulta saldos reales.\n\nEl comando `python3 -m hache_trader observe --telegram --state-dir <ruta>` combina Telegram con consultas públicas periódicas a SafeTrade. El radar examina un lote rotativo de hasta tres pares USDT por ciclo, registra estados de calidad de mercado y sigue **sin órdenes, posiciones ni cambios de saldo**. Se conserva el modo pausado.

El servicio actual instalado con `serve --telegram` no cambia hasta que se despliegue una nueva versión aprobada. La plantilla de systemd en esta rama propone pasar a `observe` después de verificar CI y realizar una sustitución controlada del servicio. Nunca deben correr dos consumidores de `getUpdates` simultáneamente.

Prueba externa aislada (01-11-2026): se pudieron consultar 56 pares USDT activos; 14 superaron el umbral inicial de volumen y los primeros tres analizados tuvieron spreads superiores al límite establecido. No se compró ni vendió nada, ni siquiera en simulación. Esta única muestra **no demuestra rentabilidad ni estabilidad continua**.

**Aviso:** comprobar la visibilidad del repositorio en GitHub; fue observado como público, aunque la especificación del proyecto solicita privado. Ningún token debe guardarse en el repositorio.

## Deployment preparation\n\nSee `docs/OPERACION-Y-DESPLIEGUE.md`, the versioned Telegram unit and the new `deployment/upgrade-telegram-observer.sh` (SHA- and checksum-pinned, backup and rollback). The Telegram bot is already installed in its original control-only configuration. The new observer upgrade is pending until merged, verified and carried out through authorized administrative access.\n\n## Delivery policy

GitHub is the sole versioned source of truth. Keep this PR as a draft until CI, review, deployment prerequisites and paper-market stability have been verified. No manual production edits; any deployment must use an approved, reversible release and avoid modifying Hache Natación, mining, or other live services.
