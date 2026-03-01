# Polymarket Trading Bot

Automated trading bot for Polymarket prediction markets using the official `py-clob-client` SDK.

---

## ⚠️ Risk Warnings

> **This software involves real money. Read carefully before using.**

- **Prediction markets are highly speculative.** You can lose all capital deployed.
- **No guaranteed profits.** Past performance does not imply future results.
- **Start with paper trading** (`PAPER_TRADING=true`) to understand the bot before risking real funds.
- **Use a dedicated wallet** with a strictly limited amount of funds — never your main wallet.
- **Bot bugs can cause losses.** Review the code, audit the strategies, and set conservative risk parameters.
- **API / network failures** can leave open positions. Monitor the bot actively.

---

## Features

- **Three trading strategies**: arbitrage, midpoint-reversion, and market making
- **Paper trading mode**: simulate all strategies without placing real orders
- **Risk management**: Kelly / fixed-fractional sizing, max-exposure limits, per-market caps, stop-loss
- **Rate limiting**: configurable max orders per minute
- **Real-time price monitoring**: WebSocket feed with REST polling fallback
- **Alerts**: console logging + optional Telegram notifications
- **Structured logging** via Python `logging` module
- **Retry logic** and graceful error handling throughout

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/mitchellray-gh/polymarket-trading-bot.git
cd polymarket-trading-bot

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env — add your POLYMARKET_PRIVATE_KEY at minimum

# 5. Run in paper-trading mode first (highly recommended)
python scripts/paper_trade.py --strategy arbitrage

# 6. Scan markets (no orders)
python scripts/scan_markets.py --arbitrage

# 7. Run the live bot (only after understanding the risks)
python scripts/run_bot.py --strategy arbitrage --paper
```

---

## Configuration

All settings are loaded from environment variables (`.env` file).  
Copy `.env.example` to `.env` and fill in your values.

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | *(required)* | Ethereum private key for signing orders |
| `POLYMARKET_FUNDER_ADDRESS` | `""` | Funder address for proxy wallet mode |
| `POLYMARKET_CHAIN_ID` | `137` | Polygon chain ID |
| `POLYMARKET_HOST` | `https://clob.polymarket.com` | CLOB API endpoint |
| `SIGNATURE_TYPE` | `0` | `0`=EOA, `1`=Magic/Gnosis, `2`=Proxy |
| `MIN_PROFIT_THRESHOLD` | `0.005` | Minimum profit (0.5%) to execute a trade |
| `FEE_RATE` | `0.02` | Estimated fee rate (2%) |
| `MAX_EXPOSURE_PCT` | `0.50` | Max % of balance deployed at once |
| `MAX_PER_MARKET_PCT` | `0.10` | Max % of balance per market |
| `STOP_LOSS_PCT` | `0.05` | Stop-loss as fraction of starting balance |
| `MAX_ORDERS_PER_MINUTE` | `30` | Rate limit: max orders per minute |
| `SCAN_INTERVAL` | `10` | Seconds between market scans |
| `PAPER_TRADING` | `false` | Set `true` to simulate without real orders |
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram bot token for alerts (optional) |
| `TELEGRAM_CHAT_ID` | `""` | Telegram chat ID for alerts (optional) |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`) |

---

## Strategies

### 1. Arbitrage (`--strategy arbitrage`)

Detects markets where:

```
YES ask price + NO ask price < 1.0 - fee_threshold
```

Since one outcome must resolve to $1.00, buying both YES and NO for less than $1.00 guarantees profit (minus fees). Uses Fill-Or-Kill market orders for fast execution.

**Configuration**: `MIN_PROFIT_THRESHOLD`, `FEE_RATE`

### 2. Midpoint Reversion (`--strategy midpoint`)

Monitors the midpoint price of YES tokens against a rolling moving average. Places limit orders in the direction of mean-reversion when the deviation exceeds a configurable threshold.

**Parameters**: `lookback` (default 20 samples), `deviation_threshold` (default 3%)

### 3. Market Making (`--strategy market_making`)

Posts symmetric bid/ask limit orders around the current midpoint. Earns the spread when both sides fill. Automatically cancels stale orders after a configurable TTL.

**Parameters**: `spread` (default 2%), `order_size` (default $10), `order_ttl` (default 60s)

---

## Architecture

```
polymarket-trading-bot/
├── config/settings.py          # All configuration from env vars
├── bot/
│   ├── client.py               # CLOB SDK wrapper (retry, rate-limit)
│   ├── market_scanner.py       # Market discovery & arbitrage detection
│   ├── strategies/
│   │   ├── base.py             # Abstract base strategy
│   │   ├── arbitrage.py        # YES+NO arbitrage
│   │   ├── midpoint.py         # Midpoint mean-reversion
│   │   └── market_making.py    # Spread-based market making
│   ├── execution/
│   │   ├── order_manager.py    # Order lifecycle management
│   │   └── risk_manager.py     # Position sizing, stop-loss, rate limits
│   ├── monitoring/
│   │   ├── websocket_feed.py   # Real-time WebSocket price feed
│   │   └── alerts.py           # Console + Telegram alerts
│   └── utils/
│       ├── logger.py           # Structured logging
│       └── helpers.py          # Retry decorator, utilities
├── scripts/
│   ├── run_bot.py              # Main bot entry point
│   ├── scan_markets.py         # Standalone market scanner
│   └── paper_trade.py          # Paper trading with CSV logging
└── tests/
    ├── test_strategies.py
    ├── test_risk_manager.py
    └── test_market_scanner.py
```

---

## Running Tests

```bash
python -m pytest tests/ -v
# or
python -m unittest discover tests/
```

---

## Disclaimer

> **This software is provided for educational and research purposes only.**  
> It is NOT financial advice. Trading prediction markets carries substantial risk of loss.  
> The authors are not responsible for any financial losses incurred through use of this software.  
> Use at your own risk.