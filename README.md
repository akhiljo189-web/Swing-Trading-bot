# Project Trading

A local paper-trading workbench for a daily stocks-and-ETFs breakout strategy.

**This version simulates trades locally. It does not place orders at Interactive Brokers or trade real money.** The included demo uses synthetic prices, not market history. Its results are a software demonstration, not evidence of profitability.

## Start

Requires Python 3.12 or later. The local simulator and dashboard have no external Python dependencies.

On Windows, right-click `start.ps1` and run with PowerShell, or run:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Alternatively, from this folder:

```text
python -m trading serve
```

Open http://127.0.0.1:8765. Use **Load demo** to exercise the full local simulation. A demo portfolio cannot later accept real data; use a separate database for a clean portfolio. Reloading the dashboard does not reset it.

## What it does

- Calculates signals from completed daily bars and fills eligible entries on the following supplied session.
- Applies cash, position-size, cost and portfolio-risk limits; skips trades that do not fit.
- Simulates stops, gap losses and time/trend exits; keeps exits active while new entries are paused.
- Persists the portfolio in SQLite and prevents duplicate session processing.
- Separates cash contributions from strategy performance.
- Displays positions, an equity curve, activity and risk settings on a local dashboard.
- Includes an optional, read-only IBKR paper-account connectivity check.

## Commands

```text
python -m trading status
python -m trading demo
python -m trading import path/to/daily-bars.csv
python -m trading deposit 250 --reference example-contribution-001
python -m trading export runtime/report.json
python -m unittest discover -s tests -v
```

Run `python -m trading --help` for options, including a separate database path. All simulation prices and amounts are in GBP. Convert other currencies and GBX/pence to GBP before importing; this version does not perform currency conversion.

## Data input

CSV columns:

```csv
date,symbol,open,high,low,close,volume,asset_type,earnings_safe
2026-01-02,EXAMPLE,10.0,10.3,9.8,10.1,1000000,ETF,true
```

Supply at least 201 sessions to warm up the strategy and subsequent sessions for fills. Supply one row per symbol per session for a fixed universe, with rows in ascending session order. Only import **completed** sessions. `asset_type` is `STOCK` or `ETF`; a stock requires `earnings_safe=true` to open a position. That value must come from a checked earnings calendar covering the next five trading sessions. The application cannot independently verify it.

Use consistent corporate-action treatment for all OHLC prices. Do not splice splits into an open-position series; this simulator does not process split or dividend events. There is no bundled historical market feed, automatic exchange-calendar validation, point-in-time universe or guaranteed liquidity model. The supplied dates determine the trading sessions. See [strategy and limitations](docs/strategy.md).

## Interactive Brokers

Follow [IBKR setup](docs/ibkr-setup.md) for the optional read-only connection check. Broker order submission is deliberately absent from this increment. It requires a subsequent tested integration for contracts, price increments, partial fills, protective orders and restart reconciliation.

## Online storage

The GitHub repository stores source, tests and documentation. Runtime databases, personal reports, local configuration and credentials are excluded from Git. No background upload is enabled. Keep personal records local while the repository is public; never commit passwords, tokens or API keys, even to a private repository. Raw market data may have redistribution restrictions.

## Development skills

The user's [Skills-Plugins library](https://github.com/akhiljo189-web/Skills-Plugins) is the reference source for relevant skills. Compatible installed planning, testing and review skills were used in this build. Uploading a ZIP to the library does not automatically install its tools or dependencies.
