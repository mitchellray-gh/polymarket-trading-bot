# Polymarket Trading Engine

A high-speed binary-arbitrage trading engine for [Polymarket](https://polymarket.com), built on the official [`py-clob-client`](https://github.com/Polymarket/py-clob-client).

---

## How Polymarket Works (and where the edge comes from)

Every market on Polymarket is a **binary prediction market** with two outcome tokens:

| Token | Settles at |
|-------|-----------|
| **YES** | **$1.00** if the event resolves YES, **$0** otherwise |
| **NO**  | **$1.00** if the event resolves NO,  **$0** otherwise |

In an efficient market the prices must satisfy:

```
price(YES) + price(NO) = 1.00
```

When this equality breaks down, **risk-free profit** exists:

| Condition | Action | Profit |
|-----------|--------|--------|
| `ask(YES) + ask(NO) < 1.00` | **BUY BOTH** legs | `1.00 − (ask_YES + ask_NO)` |
| `bid(YES) + bid(NO) > 1.00` | **SELL BOTH** legs | `(bid_YES + bid_NO) − 1.00` |

This is classic binary-market arbitrage — no directional bet, no market risk, guaranteed by contract settlement.

---

## Architecture

```
main.py
└── TradingEngine (engine/trading_engine.py)
    ├── MarketScanner      — async, batched order-book discovery
    ├── OpportunityDetector — classifies each market as BUY_BOTH / SELL_BOTH / NO_EDGE
    ├── TradeExecutor       — concurrent FOK orders for both legs
    └── PositionManager     — cap enforcement, one-legged risk tracking, P&L
```

### Signal types

| Signal | Meaning | Action |
|--------|---------|--------|
| `BUY_BOTH`    | Combined ask price < $1.00 | Buy YES + buy NO (FOK market orders) |
| `SELL_BOTH`   | Combined bid price > $1.00 | Sell YES + sell NO (FOK market orders) |
| `EQUAL_MONEY` | Both legs ≈ $0.50          | No edge; logged only |
| `NO_EDGE`     | Efficiently priced         | Skip |

### Speed optimisations
- All order-book fetches for a batch of markets run **concurrently** via `aiohttp`.
- Both trade legs post **simultaneously** via `asyncio.gather`.
- Blocking CLOB calls are offloaded to a **thread-pool executor** to avoid stalling the event loop.
- A persistent HTTP session with connection pooling eliminates TCP handshake overhead.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — fill in PRIVATE_KEY, FUNDER_ADDRESS
```

> **DRY_RUN=true** is the default.  No real orders are placed until you explicitly set `DRY_RUN=false`.

### 3. Run a single scan (no trading)

```bash
python main.py --scan
```

This fetches live order books across all active markets and prints any arbitrage opportunities it finds — completely read-only.

### 4. Start the trading loop

```bash
python main.py
```

The engine runs continuously, scanning, detecting, and (in live mode) executing trades.  Press **Ctrl+C** to stop.

### 5. Force dry-run from CLI

```bash
python main.py --dry-run
```

---

## Configuration reference

All settings live in `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | *(required)* | EVM wallet private key |
| `FUNDER_ADDRESS` | *(required)* | Address holding USDC on Polymarket |
| `CHAIN_ID` | `137` | `137` = Polygon Mainnet, `80002` = Amoy Testnet |
| `SIGNATURE_TYPE` | `0` | `0` = EOA, `1` = Magic/Email, `2` = Proxy wallet |
| `MAX_POSITION_SIZE_USDC` | `50` | Max USDC per trade leg |
| `MIN_PROFIT_THRESHOLD` | `0.005` | Min net profit (0.5 ¢ per $) to fire a trade |
| `MAX_OPEN_POSITIONS` | `10` | Cap on simultaneous open positions |
| `SCAN_INTERVAL_SECONDS` | `2` | Seconds between market scans |
| `SCAN_BATCH_SIZE` | `20` | Parallel order-book requests per batch |
| `DRY_RUN` | `true` | `false` to enable live trading |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `LOG_FILE` | `trading_engine.log` | Rotating log file path |

---

## Risks & caveats

> **This software is for educational purposes. Automated trading involves real financial risk. Use at your own risk.**

1. **One-legged fills** — If one leg fills and the other is rejected, you hold directional exposure.  The `PositionManager` flags these as `ONE_LEGGED`; you must resolve them manually.
2. **Taker fees** — Polymarket charges ~0.1 % on market orders.  Set `MIN_PROFIT_THRESHOLD` above `0.002` (0.2 %) to stay profitable after fees.
3. **Gas costs** — Transactions settle on Polygon; gas costs are negligible but non-zero.  Account for them in your threshold.
4. **Slippage** — FOK orders are all-or-nothing.  If the top-of-book liquidity is insufficient, the order is rejected.
5. **Market resolution** — Positions that are not fully hedged before resolution may result in a loss.
6. **US geoblocking** — Polymarket is geoblocked in the United States.  Ensure you comply with local laws.
7. **Token allowances** — If using an EOA/MetaMask wallet, you must set USDC and conditional-token allowances before trading.  See the [py-clob-client README](https://github.com/Polymarket/py-clob-client#important-token-allowances-for-metamaskeoausers) for the one-time setup script.

---

## Project layout

```
polymarket-trading-engine/
├── .env.example              ← copy to .env and fill in secrets
├── requirements.txt
├── main.py                   ← entry point
└── engine/
    ├── __init__.py
    ├── config.py             ← env-var configuration
    ├── logger_setup.py       ← colourised console + rotating file logs
    ├── client_manager.py     ← ClobClient singleton
    ├── market_scanner.py     ← async market + order-book discovery
    ├── opportunity_detector.py ← BUY_BOTH / SELL_BOTH signal classification
    ├── trade_executor.py     ← concurrent dual-leg order execution
    ├── position_manager.py   ← position cap, one-legged tracking, P&L
    └── trading_engine.py     ← main trading loop
```
