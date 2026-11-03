# fxbot

An autonomous forex trading bot for OANDA, built to run unattended on Railway.

**Status: research and demo stage.** The strategy is a hypothesis to test, not a proven way to make
money. The bot trades only on an OANDA practice account until the gates in the design spec are
passed and you explicitly enable real money.

## What it does

- Watches EUR/USD, GBP/USD, USD/JPY, AUD/USD and USD/CAD during the London and New York sessions.
- Enters on a fresh 20-candle 15-minute breakout in the direction of the 1-hour EMA(50/200) trend.
- Stop at 1.5 × ATR, target at 2R, optional exit after 2 hours; flat before every weekend.
- Risks 0.25% of equity per trade, at most 1% in total and 0.5% per US-dollar direction; stops new
  trades for the day after a 1.5% loss; skips trades that cost too much or fall near major news.
- Places every order with a stop attached at OANDA, never resends an order, and stops opening
  trades whenever anything is uncertain.
- Sends Telegram alerts and accepts `/status`, `/pause`, `/resume` and `/flatten` from your phone.

## Commands

```bash
python -m fxbot backtest --data data --news news.csv --start 2016-03-01 --end 2025-01-01
python -m fxbot fetch-data --start 2016-01-01 --end 2025-01-01 --out data
python -m fxbot live
python -m fxbot status
python -m unittest discover -s tests -v
```

Requires Python 3.12; no third-party packages.

## Documents

- [Setup checklist](docs/setup.md): OANDA, Telegram, healthchecks.io, Railway, the demo period.
- [Design spec](docs/superpowers/specs/2026-11-02-forex-bot-design.md): strategy, risk rules,
  safety behaviour and the gates to real money.
- [Implementation plans](docs/superpowers/plans/): how each part was built and reviewed.
