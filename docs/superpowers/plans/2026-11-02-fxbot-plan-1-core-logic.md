# FX Bot — Plan 1 of 4: Core Trading Logic

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure, fully unit-tested decision core of the forex bot: settings, instrument facts, indicators, strategy signal, session window, cost model, news blackout check, and the central risk manager.

**Architecture:** A new `fxbot/` package of pure functions and frozen dataclasses. No network, no database, no clock reads: every input is passed in. Later plans (engine, backtest, live) call these functions, so live trading and backtests make identical decisions.

**Tech Stack:** Python 3.12, standard library only (`dataclasses`, `math`, `zoneinfo`, `datetime`), `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-02-forex-bot-design.md` (revision 2). Read sections 3, 5 (cost model) and 6 before starting.

## Roadmap (this plan is 1 of 4)

| Plan | Delivers | Depends on |
|---|---|---|
| **1 — Core logic (this plan)** | `config`, `instruments`, `indicators`, `strategy`, `costs`, `news` (blackout check), `risk` | — |
| 2 — Engine & ledger | `ledger` (SQLite), `broker` interface + `SimBroker`, `engine` cycle (reconcile, halts, latches, time exits, Friday flatten), concurrency & failure-recovery tests | 1 |
| 3 — Backtest & research | data store, `backtest` replay, S10 entry fills, walk-forward, baselines, slippage sensitivity, block bootstrap, clustering, report | 1, 2 |
| 4 — Live operations | `OandaBroker`, news feed fetch, Telegram `control`/`alerts`, healthcheck timer, `live` runner, CLI, Dockerfile/Railway, practice-account integration suite, removal of old `trading/` package | 1, 2 |

Plans 2–4 are written after Plan 1 is merged, so they build on the real interfaces.

### Carried forward from the Plan 1 code review (must be honoured by later plans)
- **Plan 2:** call `entry_window_open(signal.candle_time + 15 min)` — it takes the candle *close*
  time; pass the decision time as `at` to `news_blocked`; assert both in engine tests.
- **Plan 2:** `OpenPosition.risk_gbp` and post-fill risk recomputation use stop risk + cost risk
  (spec §6 rule 2), the same basis as new candidates.
- **Plan 2/3:** pass only *completed* H1 candles to `strategy.evaluate`; a forming H1 candle makes it
  return `None` by design (no-lookahead guard).
- **Plan 2:** consider a small relative epsilon at the cost filter and risk caps for price-unit
  rounding (current behaviour fails safe by rejecting).
- **Plan 2/3:** P&L direction helpers (spec §4 `instruments.py`) are added where first needed.
- **Plan 3:** the backtest cost filter must use a documented spread source (bid/ask of the
  decision candle's close) so live and backtest reject the same trades.
- **Plan 4:** credentials from environment variables (spec §4 `config.py`).

## Global Constraints

- Python 3.12 (CI uses `actions/setup-python` with `'3.12'`); standard library only — no third-party packages.
- Tests use `unittest`; files live flat in `tests/` named `test_fx_*.py`; CI runs `python -m unittest discover -s tests -v`.
- Pairs: `EUR_USD`, `GBP_USD`, `USD_JPY`, `AUD_USD`, `USD_CAD` (OANDA naming).
- Indicators on mid prices over fixed trailing windows: 600 H1, 100 M15. EMA seeded with SMA of first N values in the window.
- Risk 0.25% per trade; 1% total open risk; 0.5% per USD direction; max 4 positions; max 2 entries per pair per UK day; one open position per pair; daily loss 1.5% of start-of-day NAV; margin ≤ 50% NAV; gross notional ≤ 10× NAV.
- Stop 1.5 × ATR(14) of M15; target 2R; cost filter: round-trip cost ≤ 15% of stop distance; slippage 0.3 pips per side; commission 0 (configurable).
- Sessions 07:00–20:00 Europe/London, Mon–Fri; no new entries Friday from 18:00 London.
- All datetimes are timezone-aware UTC; London time derived via `zoneinfo.ZoneInfo("Europe/London")`.
- Side convention: `+1` = buy, `-1` = sell. USD direction: `+1` = long USD, `-1` = short USD.
- The old `trading/` package and its tests stay untouched in this plan.
- Module note: the spec's `filters.py` is folded into `costs.py` (cost filter) and `news.py` (blackout check) — fewer files, same behaviour.

## File Structure

| File | Responsibility |
|---|---|
| `fxbot/__init__.py` | Package marker |
| `fxbot/config.py` | `Settings` frozen dataclass with every parameter |
| `fxbot/instruments.py` | `Instrument` facts, `INSTRUMENTS`, `usd_direction()` |
| `fxbot/indicators.py` | `ema()`, `atr()` |
| `fxbot/strategy.py` | `Candle`, `Signal`, `evaluate()`, `entry_window_open()` |
| `fxbot/costs.py` | `round_trip_cost()`, `passes_cost_filter()` |
| `fxbot/news.py` | `NewsEvent`, `news_blocked()` |
| `fxbot/risk.py` | `OpenPosition`, `AccountState`, `Candidate`, `Decision`, `size_units()`, `evaluate()`, `daily_pnl()`, `daily_loss_breached()`, `uk_trading_day()` |
| `tests/test_fx_*.py` | One test file per module |

---

### Task 1: Package, settings and instruments

**Files:**
- Create: `fxbot/__init__.py`, `fxbot/config.py`, `fxbot/instruments.py`
- Test: `tests/test_fx_config.py`

**Interfaces:**
- Produces: `fxbot.config.Settings` (frozen dataclass, fields below), `fxbot.config.PAIRS: tuple[str, ...]`; `fxbot.instruments.Instrument(name: str, pip: float, usd_is_base: bool)`, `INSTRUMENTS: dict[str, Instrument]`, `usd_direction(pair: str, side: int) -> int`, `currencies(pair: str) -> tuple[str, str]`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_config.py`:
```python
import unittest
from datetime import time

from fxbot.config import PAIRS, Settings
from fxbot.instruments import INSTRUMENTS, currencies, usd_direction


class SettingsTests(unittest.TestCase):
    def test_defaults_match_spec(self):
        s = Settings()
        self.assertEqual(s.pairs, ("EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD"))
        self.assertEqual((s.h1_window, s.m15_window), (600, 100))
        self.assertEqual((s.ema_fast, s.ema_slow, s.breakout_lookback, s.atr_period), (50, 200, 20, 14))
        self.assertEqual((s.stop_atr_mult, s.reward_r, s.time_exit_minutes), (1.5, 2.0, 120))
        self.assertEqual((s.session_start, s.session_end, s.friday_entry_cutoff), (time(7), time(20), time(18)))
        self.assertEqual((s.slippage_pips_per_side, s.commission_pips_round_trip, s.max_cost_to_stop), (0.3, 0.0, 0.15))
        self.assertEqual((s.risk_per_trade, s.max_open_risk, s.max_usd_direction_risk), (0.0025, 0.01, 0.005))
        self.assertEqual((s.max_positions, s.max_entries_per_pair_per_day, s.daily_loss_limit), (4, 2, 0.015))
        self.assertEqual((s.max_margin_fraction, s.max_gross_notional_multiple), (0.5, 10.0))
        self.assertEqual(s.news_window_minutes, 30)

    def test_settings_are_immutable(self):
        with self.assertRaises(Exception):
            Settings().risk_per_trade = 0.025


class InstrumentTests(unittest.TestCase):
    def test_every_pair_has_instrument_facts(self):
        self.assertEqual(set(INSTRUMENTS), set(PAIRS))
        self.assertEqual(INSTRUMENTS["EUR_USD"].pip, 0.0001)
        self.assertEqual(INSTRUMENTS["USD_JPY"].pip, 0.01)

    def test_usd_direction(self):
        # buying a USD-quoted pair sells USD; buying a USD-based pair buys USD
        self.assertEqual(usd_direction("EUR_USD", +1), -1)
        self.assertEqual(usd_direction("EUR_USD", -1), +1)
        self.assertEqual(usd_direction("GBP_USD", +1), -1)
        self.assertEqual(usd_direction("AUD_USD", -1), +1)
        self.assertEqual(usd_direction("USD_JPY", +1), +1)
        self.assertEqual(usd_direction("USD_CAD", -1), -1)

    def test_currencies(self):
        self.assertEqual(currencies("USD_CAD"), ("USD", "CAD"))

    def test_bad_side_rejected(self):
        with self.assertRaises(ValueError):
            usd_direction("EUR_USD", 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_config.py" -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'fxbot'`

- [ ] **Step 3: Write minimal implementation**

`fxbot/__init__.py`:
```python
"""Autonomous OANDA forex bot. See docs/superpowers/specs/2026-11-02-forex-bot-design.md."""
```

`fxbot/config.py`:
```python
"""All strategy and risk parameters. Values are initial testing assumptions (spec §3, §6)."""
from dataclasses import dataclass
from datetime import time

PAIRS = ("EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD")


@dataclass(frozen=True)
class Settings:
    pairs: tuple = PAIRS
    # indicator windows and parameters
    h1_window: int = 600
    m15_window: int = 100
    ema_fast: int = 50
    ema_slow: int = 200
    breakout_lookback: int = 20
    atr_period: int = 14
    # exits
    stop_atr_mult: float = 1.5
    reward_r: float = 2.0
    time_exit_minutes: int | None = 120
    # sessions (Europe/London)
    session_start: time = time(7, 0)
    session_end: time = time(20, 0)
    friday_entry_cutoff: time = time(18, 0)
    friday_flatten_start: time = time(19, 30)
    friday_flatten_deadline: time = time(20, 0)
    # costs
    slippage_pips_per_side: float = 0.3
    commission_pips_round_trip: float = 0.0
    max_cost_to_stop: float = 0.15
    # news
    news_window_minutes: int = 30
    # risk
    risk_per_trade: float = 0.0025
    max_open_risk: float = 0.01
    max_usd_direction_risk: float = 0.005
    max_positions: int = 4
    max_entries_per_pair_per_day: int = 2
    daily_loss_limit: float = 0.015
    max_margin_fraction: float = 0.5
    max_gross_notional_multiple: float = 10.0
```

`fxbot/instruments.py`:
```python
"""Static facts about traded pairs."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    name: str
    pip: float
    usd_is_base: bool


INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", 0.0001, False),
    "GBP_USD": Instrument("GBP_USD", 0.0001, False),
    "USD_JPY": Instrument("USD_JPY", 0.01, True),
    "AUD_USD": Instrument("AUD_USD", 0.0001, False),
    "USD_CAD": Instrument("USD_CAD", 0.0001, True),
}


def currencies(pair: str) -> tuple[str, str]:
    base, quote = pair.split("_")
    return base, quote


def usd_direction(pair: str, side: int) -> int:
    """+1 if the position is long USD, -1 if short USD."""
    if side not in (1, -1):
        raise ValueError("side must be +1 (buy) or -1 (sell)")
    return side if INSTRUMENTS[pair].usd_is_base else -side
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_config.py" -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/__init__.py fxbot/config.py fxbot/instruments.py tests/test_fx_config.py
git commit -m "fxbot: add settings and instrument facts"
```

---

### Task 2: Indicators

**Files:**
- Create: `fxbot/indicators.py`
- Test: `tests/test_fx_indicators.py`

**Interfaces:**
- Produces: `ema(values: Sequence[float], period: int) -> list[float | None]`; `atr(highs, lows, closes: Sequence[float], period: int) -> list[float | None]`. Both return a list the same length as the input; entries before the first computable value are `None`.

Definitions (must match exactly; the backtest relies on them):
- EMA: first value at index `period-1` = SMA of the first `period` values; then `ema[i] = values[i]*k + ema[i-1]*(1-k)`, `k = 2/(period+1)`.
- True range: index 0 = `high-low`; else `max(high-low, |high-prev_close|, |low-prev_close|)`.
- ATR (Wilder): first value at index `period-1` = mean of first `period` TRs; then `atr[i] = (atr[i-1]*(period-1) + tr[i]) / period`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_indicators.py`:
```python
import unittest

from fxbot.indicators import atr, ema


class EmaTests(unittest.TestCase):
    def test_hand_calculated(self):
        # SMA(1,2,3)=2 at idx2; k=0.5: idx3=4*.5+2*.5=3; idx4=5*.5+3*.5=4
        self.assertEqual(ema([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_too_short_is_all_none(self):
        self.assertEqual(ema([1, 2], 3), [None, None])

    def test_bad_period(self):
        with self.assertRaises(ValueError):
            ema([1, 2, 3], 0)


class AtrTests(unittest.TestCase):
    def test_hand_calculated(self):
        highs = [10, 11, 12, 13]
        lows = [8, 9, 9, 11]
        closes = [9, 10, 11, 12]
        # TR = [2, 2, 3, 2]; ATR3 idx2 = 7/3; idx3 = (7/3*2 + 2)/3 = 20/9
        result = atr(highs, lows, closes, 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertAlmostEqual(result[2], 7 / 3, places=12)
        self.assertAlmostEqual(result[3], 20 / 9, places=12)

    def test_gap_uses_previous_close(self):
        # second candle gaps up: TR = |high - prev_close| = 15 - 9 = 6
        result = atr([10, 15], [8, 14], [9, 14.5], 2)
        self.assertAlmostEqual(result[1], (2 + 6) / 2, places=12)

    def test_length_mismatch(self):
        with self.assertRaises(ValueError):
            atr([1, 2], [1], [1, 2], 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_indicators.py" -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'fxbot.indicators'`

- [ ] **Step 3: Write minimal implementation**

`fxbot/indicators.py`:
```python
"""Indicator definitions shared by live trading and backtests (spec §3)."""
from collections.abc import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int) -> list[float | None]:
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("highs, lows and closes must have equal length")
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(highs)
    tr = []
    for i in range(n):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    out: list[float | None] = [None] * n
    if n < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_indicators.py" -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/indicators.py tests/test_fx_indicators.py
git commit -m "fxbot: add EMA and Wilder ATR indicators"
```

---

### Task 3: Strategy signal and session window

**Files:**
- Create: `fxbot/strategy.py`
- Test: `tests/test_fx_strategy.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `ema`, `atr` (Task 2).
- Produces:
  - `Candle(time: datetime, open: float, high: float, low: float, close: float)` — frozen; `time` is the UTC **open** time; mid prices; completed candles only.
  - `Signal(pair: str, side: int, candle_time: datetime, stop_distance: float)` — frozen; `candle_time` is the signal M15 candle's open time.
  - `evaluate(pair: str, h1: Sequence[Candle], m15: Sequence[Candle], settings: Settings) -> Signal | None`
  - `entry_window_open(close_time_utc: datetime, settings: Settings) -> bool`

Rules implemented by `evaluate` (spec §3):
1. Use only the last `settings.h1_window` H1 and last `settings.m15_window` M15 candles. If fewer are supplied, return `None` (insufficient warm-up).
2. Trend: `ema50 > ema200 and close > ema50` → long allowed; `ema50 < ema200 and close < ema50` → short allowed; on the latest H1 candle.
3. Breakout (long): latest M15 close > max high of the 20 M15 candles before it, **and** the previous M15 close ≤ max high of the 20 candles before *it* (fresh). Short mirrors with lows.
4. `stop_distance = stop_atr_mult × ATR(14)` of the M15 window at the latest candle.

`entry_window_open` works on the signal candle's **close** time (open + 15 min): Mon–Fri, London time in `[session_start, session_end)`, and on Friday strictly before `friday_entry_cutoff`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_strategy.py`:
```python
import unittest
from datetime import datetime, timedelta, timezone

from fxbot.config import Settings
from fxbot.indicators import atr
from fxbot.strategy import Candle, Signal, entry_window_open, evaluate

UTC = timezone.utc
S = Settings()
H1_END = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)    # last completed H1 opens 08:00 Tue
M15_END = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)   # last completed M15 opens 09:00 Tue


def h1_series(step, count=600, start=1.0):
    t0 = H1_END - timedelta(hours=count - 1)
    out = []
    for i in range(count):
        c = start + i * step
        out.append(Candle(t0 + timedelta(hours=i), c, c + 0.0002, c - 0.0002, c))
    return out


def m15_flat(count=100, mid=1.1000):
    t0 = M15_END - timedelta(minutes=15 * (count - 1))
    return [Candle(t0 + timedelta(minutes=15 * i), mid, mid + 0.0005, mid - 0.0005, mid)
            for i in range(count)]


def with_last(m15, close, high, low):
    last = m15[-1]
    return m15[:-1] + [Candle(last.time, m15[-2].close, high, low, close)]


def expected_stop(m15):
    w = m15[-S.m15_window:]
    a = atr([c.high for c in w], [c.low for c in w], [c.close for c in w], S.atr_period)[-1]
    return S.stop_atr_mult * a


class EvaluateTests(unittest.TestCase):
    def test_long_breakout_in_uptrend(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        sig = evaluate("EUR_USD", h1_series(+0.0001), m15, S)
        self.assertEqual(sig, Signal("EUR_USD", +1, M15_END, sig.stop_distance))
        self.assertAlmostEqual(sig.stop_distance, expected_stop(m15), places=12)

    def test_short_breakdown_in_downtrend(self):
        m15 = with_last(m15_flat(), close=1.0990, high=1.1002, low=1.0988)
        sig = evaluate("EUR_USD", h1_series(-0.0001, start=1.2), m15, S)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, -1)

    def test_breakout_against_trend_is_ignored(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(-0.0001, start=1.2), m15, S))

    def test_no_trend_no_signal(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(0.0), m15, S))

    def test_close_equal_to_range_high_is_not_breakout(self):
        m15 = with_last(m15_flat(), close=1.1005, high=1.1006, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001), m15, S))

    def test_stale_breakout_is_not_fresh(self):
        m15 = m15_flat()
        prev = m15[-2]
        m15[-2] = Candle(prev.time, prev.open, 1.1009, prev.low, 1.1008)   # already broke out
        m15 = with_last(m15, close=1.1012, high=1.1013, low=1.1004)
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001), m15, S))

    def test_insufficient_warmup(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001, count=599), m15, S))
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001), m15[-99:], S))

    def test_candles_older_than_window_do_not_change_result(self):
        # live fetches exactly the window; backtest may hold more history — results must match
        h1 = h1_series(+0.0001)
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        older_h1 = [Candle(h1[0].time - timedelta(hours=i + 1), 5.0, 9.0, 0.1, 0.2) for i in range(50)][::-1]
        older_m15 = [Candle(m15[0].time - timedelta(minutes=15 * (i + 1)), 5.0, 9.0, 0.1, 0.2) for i in range(50)][::-1]
        self.assertEqual(evaluate("EUR_USD", h1, m15, S),
                         evaluate("EUR_USD", older_h1 + h1, older_m15 + m15, S))


class EntryWindowTests(unittest.TestCase):
    def check(self, y, mo, d, hh, mm, expected):
        self.assertEqual(entry_window_open(datetime(y, mo, d, hh, mm, tzinfo=UTC), S), expected)

    def test_winter_session(self):           # November: London = UTC
        self.check(2026, 11, 3, 9, 15, True)
        self.check(2026, 11, 3, 6, 45, False)
        self.check(2026, 11, 3, 7, 0, True)
        self.check(2026, 11, 3, 20, 0, False)

    def test_summer_time(self):              # July: London = UTC+1
        self.check(2026, 7, 7, 6, 15, True)    # 07:15 London
        self.check(2026, 7, 7, 19, 15, False)  # 20:15 London

    def test_friday_cutoff_and_weekend(self):
        self.check(2026, 11, 6, 17, 45, True)   # Friday 17:45
        self.check(2026, 11, 6, 18, 0, False)   # Friday 18:00
        self.check(2026, 11, 7, 10, 0, False)   # Saturday


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_strategy.py" -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'fxbot.strategy'`

- [ ] **Step 3: Write minimal implementation**

`fxbot/strategy.py`:
```python
"""15-minute breakout with 1-hour trend filter (spec §3). Pure functions only."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from fxbot.config import Settings
from fxbot.indicators import atr, ema

LONDON = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class Candle:
    time: datetime  # UTC open time
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Signal:
    pair: str
    side: int               # +1 buy, -1 sell
    candle_time: datetime   # open time of the signal M15 candle
    stop_distance: float    # in price units


def _trend(h1: Sequence[Candle], s: Settings) -> int:
    closes = [c.close for c in h1]
    fast = ema(closes, s.ema_fast)[-1]
    slow = ema(closes, s.ema_slow)[-1]
    last = closes[-1]
    if fast > slow and last > fast:
        return 1
    if fast < slow and last < fast:
        return -1
    return 0


def _breakout(m15: Sequence[Candle], n: int) -> int:
    last, prev = m15[-1], m15[-2]
    hi_last = max(c.high for c in m15[-n - 1:-1])
    lo_last = min(c.low for c in m15[-n - 1:-1])
    hi_prev = max(c.high for c in m15[-n - 2:-2])
    lo_prev = min(c.low for c in m15[-n - 2:-2])
    if last.close > hi_last and prev.close <= hi_prev:
        return 1
    if last.close < lo_last and prev.close >= lo_prev:
        return -1
    return 0


def evaluate(pair: str, h1: Sequence[Candle], m15: Sequence[Candle],
             settings: Settings) -> Signal | None:
    s = settings
    if len(h1) < s.h1_window or len(m15) < s.m15_window:
        return None
    h1 = h1[-s.h1_window:]
    m15 = m15[-s.m15_window:]
    trend = _trend(h1, s)
    if trend == 0 or _breakout(m15, s.breakout_lookback) != trend:
        return None
    a = atr([c.high for c in m15], [c.low for c in m15], [c.close for c in m15], s.atr_period)[-1]
    return Signal(pair, trend, m15[-1].time, s.stop_atr_mult * a)


def entry_window_open(close_time_utc: datetime, settings: Settings) -> bool:
    local = close_time_utc.astimezone(LONDON)
    if local.weekday() >= 5:
        return False
    t = local.time()
    if not (settings.session_start <= t < settings.session_end):
        return False
    if local.weekday() == 4 and t >= settings.friday_entry_cutoff:
        return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_strategy.py" -v`
Expected: 11 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/strategy.py tests/test_fx_strategy.py
git commit -m "fxbot: add breakout strategy signal and session window"
```

---

### Task 4: Cost model and news blackout

**Files:**
- Create: `fxbot/costs.py`, `fxbot/news.py`
- Test: `tests/test_fx_costs_news.py`

**Interfaces:**
- Consumes: `Settings`, `INSTRUMENTS`, `currencies`.
- Produces:
  - `round_trip_cost(pair: str, spread: float, settings: Settings) -> float` — price units: `spread + 2 × slippage_pips_per_side × pip + commission_pips_round_trip × pip`. **The only cost function**; filter, sizing, SimBroker and reports all call it.
  - `passes_cost_filter(cost: float, stop_distance: float, settings: Settings) -> bool` — `cost <= max_cost_to_stop × stop_distance`.
  - `NewsEvent(time: datetime, currency: str, impact: str)` — frozen; `impact` is `"High"`, `"Medium"` or `"Low"`.
  - `news_blocked(pair: str, at: datetime, events: Iterable[NewsEvent], settings: Settings) -> bool` — True if any `"High"` event for either currency of the pair is within `news_window_minutes` before or after `at` (inclusive).

- [ ] **Step 1: Write the failing test**

`tests/test_fx_costs_news.py`:
```python
import unittest
from datetime import datetime, timedelta, timezone

from fxbot.config import Settings
from fxbot.costs import passes_cost_filter, round_trip_cost
from fxbot.news import NewsEvent, news_blocked

S = Settings()
AT = datetime(2026, 11, 6, 13, 15, tzinfo=timezone.utc)


class CostTests(unittest.TestCase):
    def test_round_trip_cost_eur_usd(self):
        # 0.6 pip spread + 2 x 0.3 pip slippage = 1.2 pips
        self.assertAlmostEqual(round_trip_cost("EUR_USD", 0.00006, S), 0.00012, places=12)

    def test_round_trip_cost_uses_jpy_pip(self):
        self.assertAlmostEqual(round_trip_cost("USD_JPY", 0.008, S), 0.014, places=12)

    def test_commission_included(self):
        s = Settings(commission_pips_round_trip=0.5)
        self.assertAlmostEqual(round_trip_cost("EUR_USD", 0.00006, s), 0.00017, places=12)

    def test_filter(self):
        self.assertTrue(passes_cost_filter(0.00012, 0.0010, S))    # 12% of stop
        self.assertFalse(passes_cost_filter(0.00012, 0.0006, S))   # 20% of stop


class NewsTests(unittest.TestCase):
    def test_high_impact_on_either_currency_blocks(self):
        usd = [NewsEvent(AT + timedelta(minutes=15), "USD", "High")]
        eur = [NewsEvent(AT - timedelta(minutes=30), "EUR", "High")]
        self.assertTrue(news_blocked("EUR_USD", AT, usd, S))
        self.assertTrue(news_blocked("EUR_USD", AT, eur, S))   # boundary inclusive

    def test_outside_window_or_other_currency_or_low_impact(self):
        events = [NewsEvent(AT + timedelta(minutes=31), "USD", "High"),
                  NewsEvent(AT, "JPY", "High"),
                  NewsEvent(AT, "USD", "Medium")]
        self.assertFalse(news_blocked("EUR_USD", AT, events, S))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_costs_news.py" -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'fxbot.costs'`

- [ ] **Step 3: Write minimal implementation**

`fxbot/costs.py`:
```python
"""The single cost model used by filter, sizing, simulator and reports (spec §5)."""
from fxbot.config import Settings
from fxbot.instruments import INSTRUMENTS


def round_trip_cost(pair: str, spread: float, settings: Settings) -> float:
    pip = INSTRUMENTS[pair].pip
    return (spread
            + 2 * settings.slippage_pips_per_side * pip
            + settings.commission_pips_round_trip * pip)


def passes_cost_filter(cost: float, stop_distance: float, settings: Settings) -> bool:
    return cost <= settings.max_cost_to_stop * stop_distance
```

`fxbot/news.py`:
```python
"""High-impact news blackout check (spec §3). Feed fetching is added in Plan 4."""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from fxbot.config import Settings
from fxbot.instruments import currencies


@dataclass(frozen=True)
class NewsEvent:
    time: datetime
    currency: str
    impact: str


def news_blocked(pair: str, at: datetime, events: Iterable[NewsEvent],
                 settings: Settings) -> bool:
    window = timedelta(minutes=settings.news_window_minutes)
    ccys = set(currencies(pair))
    return any(e.impact == "High" and e.currency in ccys and abs(e.time - at) <= window
               for e in events)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_costs_news.py" -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/costs.py fxbot/news.py tests/test_fx_costs_news.py
git commit -m "fxbot: add shared cost model and news blackout check"
```

---

### Task 5: Risk manager — sizing and approval

**Files:**
- Create: `fxbot/risk.py`
- Test: `tests/test_fx_risk.py`

**Interfaces:**
- Consumes: `Settings`, `Signal`, `usd_direction`.
- Produces:
  - `OpenPosition(pair: str, side: int, units: int, risk_gbp: float, notional_gbp: float)` — frozen.
  - `AccountState(nav: float, margin_used: float, positions: tuple[OpenPosition, ...], entries_today: Mapping[str, int], block_reason: str | None)` — frozen. `block_reason` is set by the engine (Plan 2) for halts, pauses, daily-loss latch, unresolved orders, failed health checks.
  - `Candidate(signal: Signal, cost: float, price: float, loss_factor: float, value_factor: float, margin_rate: float, max_units: int)` — frozen. `loss_factor` = OANDA `homeConversions.accountLoss` (GBP per 1 unit of quote currency); `value_factor` = `homeConversions.positionValue`; `price` = expected entry price.
  - `Decision(approved: bool, reason: str, units: int = 0, risk_gbp: float = 0.0, stop_risk_gbp: float = 0.0, cost_risk_gbp: float = 0.0, notional_gbp: float = 0.0, margin_gbp: float = 0.0)` — frozen.
  - `size_units(nav: float, stop_distance: float, cost: float, loss_factor: float, risk_fraction: float) -> int`
  - `evaluate(candidate: Candidate, account: AccountState, settings: Settings) -> Decision`

Rejection reasons (exact strings; ledger and Telegram show them), checked in this order:
1. `account.block_reason` (returned verbatim)
2. `"position below minimum size"` (units < 1)
3. `"above instrument maximum units"`
4. `"max positions"`
5. `"position already open in pair"`
6. `"max entries for pair today"`
7. `"max open risk"`
8. `"max USD direction risk"`
9. `"max gross notional"`
10. `"max margin"`
Approved decisions use reason `"approved"`.

Formulas:
- `loss_per_unit = (stop_distance + cost) × loss_factor`; `units = floor(risk_fraction × nav / loss_per_unit + 1e-9)` (epsilon guards float error like 4999.9999).
- `stop_risk_gbp = units × stop_distance × loss_factor`; `cost_risk_gbp = units × cost × loss_factor`; `risk_gbp` = sum.
- `notional_gbp = units × price × value_factor`; `margin_gbp = notional_gbp × margin_rate`.
- Caps compare against `settings.* × account.nav`; USD direction risk sums existing positions whose `usd_direction(pair, side)` equals the candidate's.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_risk.py`:
```python
import unittest
from datetime import datetime, timezone

from fxbot.config import Settings
from fxbot.risk import AccountState, Candidate, OpenPosition, evaluate, size_units
from fxbot.strategy import Signal

S = Settings()
T = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
GBPUSD = 1.30


def candidate(pair="EUR_USD", side=+1, stop=0.0006, cost=0.00008, price=1.10,
              loss_factor=1 / GBPUSD, value_factor=1 / GBPUSD, max_units=100_000_000):
    return Candidate(Signal(pair, side, T, stop), cost, price, loss_factor, value_factor,
                     1 / 30, max_units)


def account(nav=1000.0, margin_used=0.0, positions=(), entries=None, block=None):
    return AccountState(nav, margin_used, tuple(positions), entries or {}, block)


def pos(pair, side, risk=2.5, notional=2000.0):
    return OpenPosition(pair, side, 1000, risk, notional)


class SizingTests(unittest.TestCase):
    def test_eur_usd_worked_example(self):
        # £2.50 / ((0.0006 + 0.00008) / 1.30) = 4779.4 -> 4779 units
        self.assertEqual(size_units(1000, 0.0006, 0.00008, 1 / GBPUSD, 0.0025), 4779)

    def test_usd_jpy_example(self):
        # JPY->GBP factor = 1/(150 * 1.30); £2.50 / (0.135 / 195) = 3611.1 -> 3611
        self.assertEqual(size_units(1000, 0.12, 0.015, 1 / 195, 0.0025), 3611)

    def test_rounds_down_with_float_guard(self):
        self.assertEqual(size_units(1000, 0.0004, 0.0001, 1.0, 0.0025), 5000)

    def test_compounds_with_nav(self):
        self.assertEqual(size_units(2000, 0.0006, 0.00008, 1 / GBPUSD, 0.0025), 9558)


class ApprovalTests(unittest.TestCase):
    def test_approves_base_case_with_cost_split(self):
        d = evaluate(candidate(), account(), S)
        self.assertTrue(d.approved)
        self.assertEqual((d.reason, d.units), ("approved", 4779))
        self.assertAlmostEqual(d.stop_risk_gbp, 4779 * 0.0006 / GBPUSD, places=9)
        self.assertAlmostEqual(d.cost_risk_gbp, 4779 * 0.00008 / GBPUSD, places=9)
        self.assertAlmostEqual(d.risk_gbp, d.stop_risk_gbp + d.cost_risk_gbp, places=9)
        self.assertLessEqual(d.risk_gbp, 2.5)
        self.assertAlmostEqual(d.notional_gbp, 4779 * 1.10 / GBPUSD, places=6)
        self.assertAlmostEqual(d.margin_gbp, d.notional_gbp / 30, places=6)

    def reject(self, cand, acct, reason):
        d = evaluate(cand, acct, S)
        self.assertFalse(d.approved)
        self.assertEqual(d.reason, reason)
        self.assertEqual(d.units, 0)

    def test_block_reason_wins(self):
        self.reject(candidate(), account(block="halted: missing stop"), "halted: missing stop")

    def test_too_small(self):
        self.reject(candidate(stop=10.0), account(), "position below minimum size")

    def test_above_max_units(self):
        self.reject(candidate(max_units=1000), account(), "above instrument maximum units")

    def test_max_positions(self):
        positions = [pos("GBP_USD", 1, 0.5), pos("USD_JPY", 1, 0.5),
                     pos("AUD_USD", -1, 0.5), pos("USD_CAD", -1, 0.5)]
        self.reject(candidate(), account(positions=positions), "max positions")

    def test_one_position_per_pair(self):
        self.reject(candidate(side=-1), account(positions=[pos("EUR_USD", 1)]),
                    "position already open in pair")

    def test_entries_per_pair_per_day(self):
        self.reject(candidate(), account(entries={"EUR_USD": 2}), "max entries for pair today")

    def test_max_open_risk(self):
        positions = [pos("GBP_USD", 1, 4.0), pos("USD_JPY", 1, 4.0)]   # 8.0 + 2.5 > 10.0
        self.reject(candidate(), account(positions=positions), "max open risk")

    def test_usd_direction_cap(self):
        positions = [pos("GBP_USD", 1, 2.5), pos("AUD_USD", 1, 2.5)]   # 5.0 short USD already
        self.reject(candidate(), account(positions=positions), "max USD direction risk")

    def test_opposite_usd_direction_allowed(self):
        positions = [pos("USD_JPY", 1, 2.5), pos("USD_CAD", 1, 2.5)]   # 5.0 long USD
        self.assertTrue(evaluate(candidate(), account(positions=positions), S).approved)

    def test_gross_notional_cap(self):
        positions = [pos("USD_JPY", 1, 0.1, 7000.0)]                   # 7000 + 4044 > 10000
        self.reject(candidate(), account(positions=positions), "max gross notional")

    def test_margin_cap(self):
        self.reject(candidate(), account(margin_used=400.0), "max margin")   # 400 + 135 > 500


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_risk.py" -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'fxbot.risk'`

- [ ] **Step 3: Write minimal implementation**

`fxbot/risk.py`:
```python
"""Central risk manager: sizing and portfolio approval (spec §6). Pure functions only."""
import math
from collections.abc import Mapping
from dataclasses import dataclass

from fxbot.config import Settings
from fxbot.instruments import usd_direction
from fxbot.strategy import Signal


@dataclass(frozen=True)
class OpenPosition:
    pair: str
    side: int
    units: int
    risk_gbp: float
    notional_gbp: float


@dataclass(frozen=True)
class AccountState:
    nav: float
    margin_used: float
    positions: tuple[OpenPosition, ...]
    entries_today: Mapping[str, int]
    block_reason: str | None


@dataclass(frozen=True)
class Candidate:
    signal: Signal
    cost: float          # round-trip cost in price units (fxbot.costs.round_trip_cost)
    price: float         # expected entry price
    loss_factor: float   # homeConversions.accountLoss: GBP per unit of quote currency
    value_factor: float  # homeConversions.positionValue
    margin_rate: float
    max_units: int


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str
    units: int = 0
    risk_gbp: float = 0.0
    stop_risk_gbp: float = 0.0
    cost_risk_gbp: float = 0.0
    notional_gbp: float = 0.0
    margin_gbp: float = 0.0


def size_units(nav: float, stop_distance: float, cost: float, loss_factor: float,
               risk_fraction: float) -> int:
    loss_per_unit = (stop_distance + cost) * loss_factor
    return math.floor(risk_fraction * nav / loss_per_unit + 1e-9)


def evaluate(candidate: Candidate, account: AccountState, settings: Settings) -> Decision:
    s, c, a, sig = settings, candidate, account, candidate.signal
    if a.block_reason:
        return Decision(False, a.block_reason)
    units = size_units(a.nav, sig.stop_distance, c.cost, c.loss_factor, s.risk_per_trade)
    if units < 1:
        return Decision(False, "position below minimum size")
    if units > c.max_units:
        return Decision(False, "above instrument maximum units")
    if len(a.positions) >= s.max_positions:
        return Decision(False, "max positions")
    if any(p.pair == sig.pair for p in a.positions):
        return Decision(False, "position already open in pair")
    if a.entries_today.get(sig.pair, 0) >= s.max_entries_per_pair_per_day:
        return Decision(False, "max entries for pair today")

    stop_risk = units * sig.stop_distance * c.loss_factor
    cost_risk = units * c.cost * c.loss_factor
    risk = stop_risk + cost_risk
    if sum(p.risk_gbp for p in a.positions) + risk > s.max_open_risk * a.nav:
        return Decision(False, "max open risk")
    direction = usd_direction(sig.pair, sig.side)
    same_dir = sum(p.risk_gbp for p in a.positions if usd_direction(p.pair, p.side) == direction)
    if same_dir + risk > s.max_usd_direction_risk * a.nav:
        return Decision(False, "max USD direction risk")
    notional = units * c.price * c.value_factor
    if sum(p.notional_gbp for p in a.positions) + notional > s.max_gross_notional_multiple * a.nav:
        return Decision(False, "max gross notional")
    margin = notional * c.margin_rate
    if a.margin_used + margin > s.max_margin_fraction * a.nav:
        return Decision(False, "max margin")
    return Decision(True, "approved", units, risk, stop_risk, cost_risk, notional, margin)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_risk.py" -v`
Expected: 16 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/risk.py tests/test_fx_risk.py
git commit -m "fxbot: add central risk manager sizing and approval"
```

---

### Task 6: Daily loss and UK trading day

**Files:**
- Modify: `fxbot/risk.py` (append functions)
- Test: `tests/test_fx_daily.py`

**Interfaces:**
- Produces:
  - `uk_trading_day(at_utc: datetime) -> date` — London calendar date.
  - `daily_pnl(start_nav: float, current_nav: float, deposits: float, withdrawals: float) -> float` — `current − start − deposits + withdrawals`.
  - `daily_loss_breached(start_nav, current_nav, deposits, withdrawals, settings: Settings) -> bool` — `daily_pnl <= −daily_loss_limit × start_nav`.
- The **latch** (stays breached until the next UK day even if equity recovers) is persisted by the ledger in Plan 2; these functions only compute.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_daily.py`:
```python
import unittest
from datetime import date, datetime, timezone

from fxbot.config import Settings
from fxbot.risk import daily_loss_breached, daily_pnl, uk_trading_day

S = Settings()


class DailyLossTests(unittest.TestCase):
    def test_deposit_is_not_profit(self):
        self.assertAlmostEqual(daily_pnl(1000, 1290, 300, 0), -10.0)

    def test_withdrawal_is_not_loss(self):
        self.assertAlmostEqual(daily_pnl(1000, 900, 0, 100), 0.0)

    def test_breach(self):
        self.assertTrue(daily_loss_breached(1000, 984.9, 0, 0, S))
        self.assertFalse(daily_loss_breached(1000, 985.5, 0, 0, S))

    def test_deposit_cannot_hide_a_breach(self):
        self.assertTrue(daily_loss_breached(1000, 1284.0, 300, 0, S))   # -16 after removing deposit


class TradingDayTests(unittest.TestCase):
    def test_uk_date_in_summer(self):
        self.assertEqual(uk_trading_day(datetime(2026, 7, 7, 23, 30, tzinfo=timezone.utc)), date(2026, 7, 8))

    def test_uk_date_in_winter(self):
        self.assertEqual(uk_trading_day(datetime(2026, 11, 3, 23, 30, tzinfo=timezone.utc)), date(2026, 11, 3))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_daily.py" -v`
Expected: ERROR — `ImportError: cannot import name 'daily_loss_breached'`

- [ ] **Step 3: Write minimal implementation**

Append to `fxbot/risk.py` (and add `from datetime import date, datetime` and `from fxbot.strategy import LONDON` to the imports at the top):
```python
def uk_trading_day(at_utc: datetime) -> date:
    return at_utc.astimezone(LONDON).date()


def daily_pnl(start_nav: float, current_nav: float, deposits: float, withdrawals: float) -> float:
    return current_nav - start_nav - deposits + withdrawals


def daily_loss_breached(start_nav: float, current_nav: float, deposits: float,
                        withdrawals: float, settings: Settings) -> bool:
    pnl = daily_pnl(start_nav, current_nav, deposits, withdrawals)
    return pnl <= -settings.daily_loss_limit * start_nav
```

The import lines at the top of `fxbot/risk.py` become:
```python
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from fxbot.config import Settings
from fxbot.instruments import usd_direction
from fxbot.strategy import LONDON, Signal
```

- [ ] **Step 4: Run all tests to verify everything passes**

Run: `python -m unittest discover -s tests -v`
Expected: all fxbot tests (51) plus the existing 27 `trading` tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/risk.py tests/test_fx_daily.py
git commit -m "fxbot: add daily loss calculation and UK trading day"
```

---

## Self-review (done while writing)

- **Spec coverage (Plan 1 scope):** §3 rules → Task 3; indicator windows/EMA seeding → Tasks 2–3; cost model and filter → Task 4; news blackout → Task 4; §6 sizing incl. cost split, all 10 approval rules, USD mapping, gross notional, margin → Task 5; daily loss formula with deposits/withdrawals → Task 6. Latch persistence, halts, reconciliation, time exits, Friday flatten, Telegram, healthcheck, backtest and OANDA items are explicitly assigned to Plans 2–4 in the roadmap.
- **Placeholders:** none; every code step has full code.
- **Type consistency:** `Signal` defined in Task 3, used in Task 5; `Settings` fields used in Tasks 3–6 all defined in Task 1; `LONDON` defined in Task 3 and reused in Task 6.
