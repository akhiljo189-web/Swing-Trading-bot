# Forex Bot — Design Spec

Date: 2026-11-02
Status: Revision 2 — awaiting user review (risk level confirmed: 0.25%/trade, 1% total)

## 1. Purpose and scope

An autonomous forex trading bot for OANDA, deployed on Railway, that trades a
research-informed 15-minute momentum breakout strategy across five major pairs,
with multiple simultaneous positions governed by one central risk manager.

**This is a hypothesis to test, not a strategy known to be profitable.** It starts
on an OANDA practice (demo) account. Real money (£1,000 initial, monthly
contributions, compounding) is only enabled after Gates A–D (section 10).

The bot replaces the existing stocks/ETF workbench (`trading/`). Forex only.

### Out of scope for version 1
Web dashboard, paid news API, machine learning, additional pairs or strategies,
5-second-candle replay.

## 2. Platform decisions

| Decision | Choice | Reason |
|---|---|---|
| Broker | OANDA v20 REST API | Linux-friendly REST API, FCA-regulated, practice accounts, 1-unit sizing, historical bid/ask candles |
| Hosting | Railway, 1 replica, persistent volume at `/data` | Runs 24/5 independent of the user's laptop |
| Monitoring & control | OANDA mobile app + Telegram alerts and commands + healthchecks.io dead-man's switch | App shows/closes trades; Telegram shows bot state and accepts commands; dead-man's switch detects a dead bot |
| Language | Python 3.12, standard library only | Simplest deployment, nothing third-party to break |
| Backtest data | OANDA historical bid/ask candles (H1, M15; S10 for entry fills) | Same source as live trading |
| News data | Forex Factory weekly feed (live); historical calendar CSV (backtest) | Free; paid API considered only if the strategy passes the gates |
| Account home currency | GBP | All sizing and reporting in GBP |

## 3. Strategy rules

All parameters live in `config.py` and are initial testing assumptions.

| Setting | Value |
|---|---|
| Pairs | EUR_USD, GBP_USD, USD_JPY, AUD_USD, USD_CAD |
| Trend timeframe | H1 |
| Entry timeframe | M15 |
| Sessions (new entries) | 07:00–20:00 Europe/London |
| Indicators | Computed on mid prices over a fixed trailing window: 600 H1 candles and 100 M15 candles. EMA seeded with the SMA of its first N values in the window. Backtest uses the identical trailing window per decision, never the full history, so live and simulated values match exactly. |

**Buy** (all true):
1. Latest completed H1: EMA(50) > EMA(200) and H1 close > EMA(50).
2. Latest completed M15 close > highest high of the previous 20 completed M15 candles,
   **and** the M15 candle before it did not already close above its own 20-candle high
   (fresh breakout only).
3. Cost filter passes (section 5).
4. No high-impact news for either currency within 30 minutes before/after now.
5. Risk manager approves (section 6).

**Sell**: mirror image (EMA(50) < EMA(200), H1 close < EMA(50), M15 close below
previous 20-candle low, fresh).

**Entry**: market order immediately after the signal candle closes.
**Stop**: 1.5 × ATR(14) of M15, attached on fill as a distance.
**Target**: set by price right after fill: fill ± 2 × actual stop distance (2R).
**Time exit** (configurable on/off; backtested both ways): close 120 minutes after fill.
**Limits**: max 2 entries per pair per UK day; one open position per pair.
Only completed candles are ever used. No catch-up trading on missed signals.

## 4. Components

Package `fxbot/`. Strategy, filters and risk are pure functions (no network, no DB).

| Module | Responsibility |
|---|---|
| `config.py` | Parameters; credentials from env (`OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV`) |
| `instruments.py` | Pip size, USD side, P&L direction per pair |
| `indicators.py` | EMA, ATR, rolling high/low |
| `strategy.py` | Candles → signal (direction, stop distance) or none; sessions; fresh breakout |
| `filters.py` | Cost filter, news blackout check |
| `news.py` | Live feed fetch (fail → block entries), historical CSV loader |
| `risk.py` | Sizing and portfolio approval |
| `broker.py` | `Broker` interface; `OandaBroker` (urllib) and `SimBroker` (backtest/tests) |
| `engine.py` | One cycle; shared by live and backtest |
| `ledger.py` | SQLite: intents, trades, decisions with reasons, deposits, daily snapshots, last transaction ID |
| `costs.py` | Single cost model (spread, per-side slippage, commission) used by filter, sizing, SimBroker and reports |
| `control.py` | Telegram command polling and authentication; commands persisted in ledger |
| `alerts.py` | Telegram messages, healthchecks.io ping on independent timer |
| `backtest.py` | Replay, walk-forward, baselines, slippage sensitivity, report |
| `__main__.py` | `fetch-data`, `backtest`, `walkforward`, `live`, `status`, `resume`, `flatten` |

## 5. Cycle data flow

Wake 10 s after each M15 close (:00/:15/:30/:45). Sleep while market is closed.

1. **Fetch**: account summary, open trades, pending orders, pricing (with
   `includeHomeConversions=true` → `homeConversions`: accountLoss/accountGain/positionValue factors), last 600 H1 and 100 M15 completed bid/ask/mid candles.
2. **Health**: latest M15 candle timestamp must equal the expected close; retry up to
   ~60 s for late candles. Prices fresh and tradeable; conversion factors present.
   Any failure → no new entries this cycle (steps 3–4 still run).
3. **Reconcile** from OANDA transactions since the stored last transaction ID:
   fills, closes, cancels, rejects, deposits/withdrawals matched to ledger by client ID.
   - Ledger rows in `submitting` are resolved from transactions (filled or failed);
     any unresolved row → **halt all new entries** (account exposure unknown); never resent automatically.
   - Untagged trades or pending orders on the account → pause entries, alert.
4. **Time exits** by elapsed time since fill (works even after missed cycles).
5. **Daily loss**: see section 6.
6. **Scan** pairs: strategy → session → cost filter → news → candidates sorted by
   cost/stop ratio ascending.
7. **Risk manager** sizes and approves each candidate in order.
8. **Submit**: write intent row (`submitting`, unique client ID = pair + candle time +
   direction) to ledger **before** sending; refuse any client ID already in the ledger
   in any state. Market order with stop-loss distance on fill and a max-slippage price bound.
9. **Confirm**: verify fill and stop. Set take-profit by price from actual fill.
   Missing stop → close trade immediately and **halt**. Take-profit failure → retry
   (trade remains stop-protected), alert on repeated failure. Recompute reserved risk
   from actual units/fill/stop before the next candidate.
10. **Record** every decision (including rejections with reason) and fill.

**Cost model** (`costs.py`, one function used everywhere):
`expected_round_trip_cost` = live spread + 2 × per-side slippage + round-trip commission.
Initial values: slippage 0.3 pips **per side**; commission 0 (OANDA standard pricing, configurable).
**Cost filter**: `expected_round_trip_cost` ≤ 15% of stop distance.

## 6. Risk manager

### Sizing
```
risk_budget_gbp   = 0.25% × NAV
loss_per_unit_gbp = (stop_distance + expected_round_trip_cost) × loss_conversion_factor
units             = floor(risk_budget_gbp / loss_per_unit_gbp)
```
`loss_conversion_factor` is OANDA's `homeConversions.accountLoss` factor (the less favourable
side). Units always round down. Cost is included in sizing so the expected loss at the stop,
including costs, matches the budget. The ledger records the stop component and cost
component separately, and reports show them separately.

Example (illustrative): NAV £1,000 → £2.50. EUR_USD stop 0.00060 + cost 0.00008 =
0.00068 USD/unit; at GBP/USD 1.30 ≈ £0.000523/unit → 4,780 units.

### Approval (all required; rejection reason logged)
1. Open bot positions < 4.
2. Total open risk + new risk ≤ 1% NAV. Trade risk = units × (|entry − stop| + expected_round_trip_cost)
   × factor, i.e. stop risk + cost risk, used identically for new candidates, open positions
   and post-fill recomputation.
3. Risk in same USD direction ≤ 0.5% NAV. Long USD = buy USD_JPY/USD_CAD or sell
   EUR_USD/GBP_USD/AUD_USD; short USD is the reverse.
4. Entries (fills) in this pair today (UK date) < 2.
5. Daily loss cutoff not reached.
6. No open position in this pair.
7. Margin used after trade ≤ 50% NAV.
8. **Gross notional of all open positions + new ≤ 10× NAV** (configurable). Note: a single
   0.25% trade with a 6-pip EUR_USD stop is ≈ 4.6× NAV, so this cap binds at 2–3
   simultaneous tight-stop trades. Backtest reports how often it binds.
9. No unresolved intent, untagged trade/order, halt, or failed health check.
10. 1 ≤ units ≤ instrument maximum.

No code path increases size after losses, adds to positions, or trades to meet activity targets.

### Daily loss
- `start_nav` = NAV at first cycle after UK midnight, persisted in the ledger.
- `day_pnl = current_nav − start_nav − deposits_today + withdrawals_today`.
- `day_pnl ≤ −1.5% × start_nav` → breach is **latched** in the ledger: no new entries until the
  next UK day **even if equity recovers**; existing trades managed.
- No snapshot on mid-day start → rebuild from transactions; if impossible, block entries for the day.

### Contributions
Deposits/withdrawals detected from OANDA transactions and recorded as contributions,
not performance. Compounding follows from sizing on NAV.

## 7. Failure handling and operations

| State | Meaning | Clears |
|---|---|---|
| Entries paused | No new trades; existing managed | Automatically when cause clears (stale data, news feed down, Friday cutoff); daily-loss latch clears at next UK day; `/pause` clears only via `/resume` |
| Halted | No new trades until user confirms | `/resume` (Telegram) or `resume` CLI. Triggers: missing stop, any unresolved order, auth failure, untagged trades/orders, corrupt or unreadable DB, repeated unexplained errors |

- Reads retried 3× with backoff; order submits never blindly retried.
- Crash/restart: Railway restarts the service; reconciliation runs before anything else.
- Lost DB: rebuild open trades from tagged OANDA trades and transactions; block entries for the day.
- Single instance: 1 replica, exclusive file lock on the DB, client-ID dedupe.
- Live mode requires `OANDA_ENV=live` **and** `LIVE_TRADING_CONFIRMED=yes`; otherwise practice only.
- Time stored in UTC; sessions and trading day in Europe/London.

### Weekend
- No new entries after Friday 18:00 UK.
- From Friday 19:30 UK: flatten loop, retry every 60 s until no bot trades remain;
  each close confirmed via transactions.
- Any bot trade still open at 20:00 UK → urgent Telegram alert; keep retrying until market close.
- Closing cannot be guaranteed if the market or broker is unavailable.

**Stops are protection, not guaranteed execution prices.** Gaps and fast markets can
fill beyond the stop; losses can exceed 1R.

### Phone controls (Telegram commands)
Railway variables are read only at startup and need a redeploy, so they are **not** the
emergency control. Instead the bot polls Telegram every 10 s, independent of trading cycles:
- `/status` — state, open trades, open risk, day P&L.
- `/pause` — no new entries until `/resume`.
- `/resume` — clears pause/halt (requires a confirmation reply).
- `/flatten` — closes all bot trades using the flatten loop (requires a confirmation reply).
- Accepted only from the configured chat ID; every command and result persisted in the ledger
  and acknowledged in the chat; persisted state survives restarts.
- `TRADING_ENABLED=false` remains as a startup default only.
- If the bot is unreachable, the OANDA app remains the independent way to close positions.

### Alerts
Telegram: halts, entries paused > 1 hour, fills, closes, daily summary.
healthchecks.io: ping every 5 minutes on an independent timer, **including weekends**, so a
closed market never looks like a dead bot; alert fires if pings stop.

### Deployment
Dockerfile (Python 3.12 + tzdata), Railway service, 1 replica, restart on failure,
volume `/data`, logs to stdout, secrets in Railway variables only.

## 8. Backtesting

- Same `engine.py` with `SimBroker`.
- Entry fill modelled at the live timing: the OANDA **S10 candle** covering signal close + 10 s,
  at ask (buy) / bid (sell) plus per-side slippage. Fetched per simulated entry and cached.
  If unavailable: next M15 open plus double slippage.
- All costs come from `costs.py`, identical to live.
- Stops/targets checked against bid/ask high/low; both touched in one candle → stop first.
- Candle opening beyond the stop (gap) → fill at that open price.
- Slippage sensitivity: 0, 0.3, 0.6, 1.0 pips **per side**.
- Walk-forward: repeated optimise-on-earlier, evaluate-on-later windows.
- Baselines (same risk, exits, caps and cost model):
  - **Trend baseline**: same H1 trend filter; entry at the first M15 close of each session
    when the filter is true, no breakout required.
  - **No-skill baseline**: random direction at the strategy's own entry times; 1,000 seeded runs,
    reported as a distribution.
- Report: net expectancy per trade, profit factor, max drawdown, per-pair and per-period
  results, rejections per filter, how often the gross notional cap binds, time exit on vs off,
  news filter on vs off.
- Statistics: confidence intervals from a **block bootstrap by trading day** (not assuming
  independent trades); trade-clustering analysis (simultaneous positions, losing-streak length,
  correlation of same-day outcomes).

## 9. Testing (verifiable in development)

`unittest`, run in GitHub Actions. Tests written before code.

- Indicators against hand-calculated values.
- Strategy: each condition, fresh breakout, sessions, incomplete candles ignored,
  no-lookahead (changing future candles never changes a past decision).
- Risk: sizing examples incl. a JPY pair, every cap, USD mapping, daily loss with
  deposits, one position per pair, margin cap, rounding.
- Engine scenarios with `SimBroker`: crash mid-submit (resolved, never resent), missing
  stop (close + halt), untagged trade (pause), stale candle, restart without snapshot,
  duplicate client ID, competition for last slot, risk recompute after fill, Friday
  flatten with rejected closes and 20:00 alert.
- `OandaBroker` against recorded JSON fixtures (to be confirmed on the practice account,
  since the development session cannot reach OANDA).
- Backtest on a synthetic dataset with known expected trades, same-candle stop/target, gap through stop.
- **Concurrency**: all five pairs signal in the same cycle, with delayed fills and API timeouts;
  portfolio limits never exceeded.
- **Failure recovery**: corrupt DB, lost connectivity, restart during an unresolved order,
  monitoring service unavailable, Telegram unavailable. Every uncertain state blocks new entries.
- **Practice-account integration suite** (needs credentials; skipped in CI, run on Railway or
  locally): real response schemas, price/unit precision, stop attached on fill, take-profit set
  from fill, transaction reconciliation, duplicate client-ID behaviour, `homeConversions`.
  Recorded fixtures are refreshed from these real responses.

### Software acceptance criteria (version 1)
1. All tests pass in CI.
2. `backtest` runs on OANDA data for all five pairs and produces the full report.
3. `walkforward` produces an out-of-sample report.
4. Practice-account integration suite passes against the real OANDA API.
5. Practice account on Railway: 1-week **infrastructure smoke test** without unhandled errors;
   trades visible in the OANDA app; Telegram alerts/commands and dead-man's switch confirmed.
   This is not proof of unattended reliability — Gate C remains mandatory.

Items 2–5 require the user's accounts (OANDA practice, Railway, Telegram,
healthchecks.io); a setup checklist is provided.

## 10. Gates to real money

| Gate | Requirement |
|---|---|
| A — Software | All unit, integration, concurrency and failure-recovery tests pass in GitHub Actions; practice-account integration suite passes |
| B — Strategy evidence | Walk-forward, strictly out-of-sample: positive net expectancy after costs, preferably over 200–300 trades (minimum 100); still positive at 0.6 pips **per side** slippage; beats both baselines under identical costs; max drawdown < 10% (hard ceiling); robust across periods and pairs; confidence intervals (block bootstrap by trading day) and clustering analysis reported |
| C — Unattended demo | Minimum 8 weeks (target 12) on Railway + OANDA practice with Telegram and external monitoring; deliberate recovery tests performed; Friday flatten observed; behaviour, trade frequency and real slippage consistent with backtest; **zero** unresolved-order or missing-stop incidents |
| D — Real-money authorisation | Manual review (checklist below), then the user explicitly sets `LIVE_TRADING_CONFIRMED=yes` |

Risk stays at 0.25% per trade and 1% total during all gates. Any increase is a separate
decision made only after Gate B, with evidence.

Failing a gate → reject or revise the hypothesis; never increase trading frequency to compensate.
Passing the gates makes the system more defensible; it does not establish future profitability.

### Gate D checklist
**Results**
- [ ] Gates A–C passed; reports reviewed, including worst drawdown, worst losing streak and confidence intervals.
- [ ] Demo slippage and spreads measured and within backtest assumptions.
- [ ] Losses beyond 1R (gaps) reviewed and acceptable.

**Broker constraints**
- [ ] Live OANDA account home currency is GBP.
- [ ] API access enabled on the live account; separate live API token created.
- [ ] Account position mode (netting/hedging, FIFO) confirmed compatible with one-position-per-pair.
- [ ] Margin rates, minimum trade size and financing (swap) costs confirmed.
- [ ] FCA retail protections understood (leverage limits, negative balance protection).

**Operational**
- [ ] Telegram alerts and dead-man's switch working on the live deployment.
- [ ] Telegram `/pause`, `/flatten` and `/resume` tested from the phone on the live deployment.
- [ ] Friday flatten observed working at least twice on demo.
- [ ] Railway billing and limits understood.

**Authorisation**
- [ ] User explicitly enables live trading. Claude never sets the live flag.
