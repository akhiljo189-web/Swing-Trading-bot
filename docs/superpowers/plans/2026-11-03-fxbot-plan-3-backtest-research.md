# FX Bot — Plan 3 of 4: Backtest and Research

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay historical bid/ask data through the live engine and produce the research report that decides Gate B: walk-forward out-of-sample results, slippage sensitivity, time-exit and news-filter variants, trend and no-skill baselines, bootstrap confidence intervals, clustering, and the automated Gate B checks.

**Architecture:** The backtest reuses the live `Engine`, risk manager, strategy and cost model unchanged; only the broker differs (`BacktestBroker` over historical candles). `backtest.run()` replays M15 closes (decisions 10 s after each close), skipping only cycles where nothing can happen. `metrics.py` summarises results; `research.py` orchestrates walk-forward, variants, baselines (no-skill runs in parallel processes) and Gate B; `python -m fxbot backtest` writes `research.json` and `research.md`.

**Tech Stack:** Python 3.12 standard library (`csv`, `bisect`, `random`, `concurrent.futures`, `multiprocessing` fork context), `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-02-forex-bot-design.md` §8 and §10 (Gate B). Builds on Plans 1–2 — read the "Carried forward" sections of both plan files.

## Global Constraints

- Everything from Plans 1–2 still applies (Python 3.12, stdlib only, flat `tests/test_fx_*.py`, UTC-aware datetimes, risk numbers, safety rules).
- **Identical decisions:** the backtest must call the live `Engine`, `risk.evaluate`, `strategy.evaluate` and `costs.round_trip_cost`. No re-implementation of trading rules in backtest code.
- Execution model (spec §8): entries and market exits 10 s after the decision candle closes — S10 quote if available, else next M15 open with **double** slippage; buys at ask + slippage, sells at bid − slippage; stops checked on bid (long) / ask (short) highs and lows; a candle opening beyond the stop (not the entry candle) exits at that open; stop exits pay slippage; targets fill exactly; stop and target in one candle → stop first.
- Spread for the cost filter = bid/ask of the decision candle's close (Plan 1 carry-forward).
- Mid candles = (bid + ask) / 2 per field. **Plan 4 must derive live mids the same way** (request OANDA `BA` candles), so indicators match.
- Slippage sensitivity at 0, 0.3, 0.6, 1.0 pips **per side**; Gate B slippage test at 0.6 per side.
- Baselines use identical risk, exits, caps and costs. Trend baseline: same H1 filter, entry at the first M15 close of each session (07:00 and 13:00 London). No-skill: random direction at the strategy's own entry times, 1,000 seeded runs.
- Confidence intervals: block bootstrap **by UK trading day**.
- Historical data files: `<dir>/<PAIR>_<H1|M15>.csv`, header `time,bid_o,bid_h,bid_l,bid_c,ask_o,ask_h,ask_l,ask_c`; news CSV header `time,currency,impact`. Fetching data from OANDA is Plan 4.

## Decisions made while writing this plan (review these)

- **Speed without changing decisions.** `strategy.evaluate` now checks the cheap breakout condition before the EMA trend filter (a test proves identical output against Plan 1's order on 2,000+ synthetic windows); a cycle is skipped only when no bot trade is open and no pair has a signal; each signal is computed once per run; backtest ledgers skip disk syncs. Measured: ~1.4 s per simulated month on this machine (~17 s per year).
- **Walk-forward grid** (small on purpose, to limit overfitting): `stop_atr_mult` ∈ {1.0, 1.5, 2.0} × `breakout_lookback` ∈ {20, 30}. Training 24 months, testing 6 months, stepping 6 months; a candidate needs ≥ 30 training trades. Out-of-sample windows are chained with NAV carried forward.
- **Gate B "positive at 0.6 pips"** re-runs the walk-forward test windows with the same chosen parameters at 0.6 pips per side (no re-optimisation).
- **"Beats the no-skill baseline"** means out-of-sample expectancy above the **95th percentile** of the no-skill distribution — stricter than beating its average.
- **Baselines and variants** (slippage, time exit off, news off) run over the whole out-of-sample span with the default parameters, not the walk-forward-chosen ones.
- **GBP conversion** uses mid rates at exit time (no conversion spread); OANDA's real conversion includes a small spread — a minor optimistic bias, noted in the report caveat.
- **Robustness across periods and pairs** is reported (`per_year`, `per_pair`) for manual review; Gate B's automated checks do not judge it.
- Without a `--news` calendar the report runs with the news filter off and prints a warning.

## File Structure

| File | Responsibility |
|---|---|
| `fxbot/history.py` | `HistCandle`, CSV load/save/validate, `Series` (precomputed mids + time index), `History` |
| `fxbot/news.py` (modify) | `load_calendar()` for historical news CSV |
| `fxbot/strategy.py`, `engine.py`, `ledger.py` (modify) | Breakout-first evaluation, public `trend_direction`, `Engine(signal_fn=...)`, `Ledger(durable=False)` |
| `fxbot/backtest_broker.py` | `BacktestBroker` (Broker over history), `ClosedTrade` |
| `fxbot/backtest.py` | `run()`, `BacktestResult`, `TradeRecord`, `cached()` |
| `fxbot/metrics.py` | `summarize()`, `max_drawdown`, `losing_streak`, `max_simultaneous`, `bootstrap_ci` |
| `fxbot/research.py` | Baselines, walk-forward, no-skill pool, `gate_b()`, `research()`, `write_report()` |
| `fxbot/__main__.py` | `python -m fxbot backtest ...` (Plan 4 adds live commands) |
| `tests/fx_history.py` | Deterministic synthetic bid/ask history for all five pairs |

---

### Task 1: Historical data store

**Files:**
- Create: `fxbot/history.py`, `tests/fx_history.py`
- Modify: `fxbot/news.py`
- Test: `tests/test_fx_history.py`

**Interfaces:**
- Produces: `HistCandle(time, bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c)` with `.mid() -> Candle`; `validate(candles)`, `save(path, candles)`, `load(path)`; `Series(candles, granularity)` with `.candles`, `.mids`, `.times`, `.completed_count(now)`, `.at_or_after(t)`; `History(data: dict[(pair, granularity), list[HistCandle]])` with `.series`, `.get(pair, granularity)`, `History.from_dir(dir, pairs, granularities=("H1", "M15"))`; `news.load_calendar(path) -> list[NewsEvent]`; test helper `make_history(days=70, seed=1) -> History` and `START` (Monday 2026-01-05 UTC).

- [ ] **Step 1: Write the test helper and failing tests**

`tests/fx_history.py`:
```python
"""Deterministic synthetic bid/ask history for backtest tests (not a test module itself)."""
import random
from datetime import datetime, timedelta, timezone

from fxbot.history import HistCandle, History

UTC = timezone.utc
START = datetime(2026, 1, 5, tzinfo=UTC)   # a Monday
LEVELS = {"EUR_USD": (1.10, 0.00006), "GBP_USD": (1.30, 0.00008), "USD_JPY": (150.0, 0.008),
          "AUD_USD": (0.65, 0.00006), "USD_CAD": (1.36, 0.00008)}   # (mid level, spread)


def market_open(t: datetime) -> bool:
    return t.weekday() < 4 or (t.weekday() == 4 and t.hour < 22)


def make_m15(pair: str, days: int, seed: int = 1) -> list[HistCandle]:
    level, spread = LEVELS[pair]
    rng = random.Random(f"{pair}-{seed}")
    sd = level * 0.0006                       # per-candle volatility
    half = spread / 2
    mid, drift, out = level, 0.0, []
    t, end = START, START + timedelta(days=days)
    while t < end:
        if market_open(t):
            if t.hour == 0 and t.minute == 0 and t.day % 2 == 0:
                drift = rng.choice((1, -1)) * 0.25 * sd   # trending regimes that change every 2 days
            o = mid
            c = o + drift + rng.gauss(0, sd)
            h = max(o, c) + abs(rng.gauss(0, sd / 2))
            low = min(o, c) - abs(rng.gauss(0, sd / 2))
            out.append(HistCandle(t, o - half, h - half, low - half, c - half,
                                  o + half, h + half, low + half, c + half))
            mid = c
        t += timedelta(minutes=15)
    return out


def to_h1(m15: list[HistCandle]) -> list[HistCandle]:
    groups: dict[datetime, list[HistCandle]] = {}
    for c in m15:
        groups.setdefault(c.time.replace(minute=0), []).append(c)
    return [HistCandle(t, g[0].bid_o, max(c.bid_h for c in g), min(c.bid_l for c in g), g[-1].bid_c,
                       g[0].ask_o, max(c.ask_h for c in g), min(c.ask_l for c in g), g[-1].ask_c)
            for t, g in sorted(groups.items())]


def make_history(days: int = 70, seed: int = 1) -> History:
    data = {}
    for pair in LEVELS:
        m15 = make_m15(pair, days, seed)
        data[(pair, "M15")] = m15
        data[(pair, "H1")] = to_h1(m15)
    return History(data)
```

`tests/test_fx_history.py`:
```python
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fxbot.history import HistCandle, History, Series, load, save
from fxbot.news import NewsEvent, load_calendar

UTC = timezone.utc
T = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def candle(t, mid=1.1, spread=0.0002):
    h = spread / 2
    return HistCandle(t, mid - h, mid + 0.001 - h, mid - 0.001 - h, mid - h,
                      mid + h, mid + 0.001 + h, mid - 0.001 + h, mid + h)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_round_trip_and_mid(self):
        candles = [candle(T + timedelta(minutes=15 * i), 1.1 + i * 0.001) for i in range(3)]
        save(self.dir / "EUR_USD_M15.csv", candles)
        loaded = load(self.dir / "EUR_USD_M15.csv")
        self.assertEqual(loaded, candles)
        self.assertAlmostEqual(loaded[1].mid().close, 1.101, places=12)

    def test_rejects_unsorted_and_inconsistent_data(self):
        with self.assertRaises(ValueError):
            save(self.dir / "x.csv", [candle(T), candle(T)])
        bad = HistCandle(T, 1.1, 1.2, 1.0, 1.1, 1.0, 1.2, 1.0, 1.0)   # ask below bid
        with self.assertRaises(ValueError):
            save(self.dir / "x.csv", [bad])

    def test_rejects_wrong_header(self):
        (self.dir / "bad.csv").write_text("time,open,high\n")
        with self.assertRaises(ValueError):
            load(self.dir / "bad.csv")

    def test_completed_count_only_counts_closed_candles(self):
        s = Series([candle(T + timedelta(minutes=15 * i)) for i in range(4)], "M15")
        self.assertEqual(s.completed_count(T + timedelta(minutes=15)), 1)          # first just closed
        self.assertEqual(s.completed_count(T + timedelta(minutes=29)), 1)
        self.assertEqual(s.completed_count(T + timedelta(minutes=30, seconds=10)), 2)
        self.assertEqual(s.at_or_after(T + timedelta(minutes=1)).time, T + timedelta(minutes=15))

    def test_from_dir_loads_every_pair_and_granularity(self):
        for g, step in (("M15", 15), ("H1", 60)):
            save(self.dir / f"EUR_USD_{g}.csv", [candle(T + timedelta(minutes=step * i)) for i in range(2)])
        h = History.from_dir(self.dir, ("EUR_USD",))
        self.assertEqual(len(h.get("EUR_USD", "H1").candles), 2)
        self.assertIsNone(h.get("GBP_USD", "M15"))

    def test_news_calendar_csv(self):
        (self.dir / "news.csv").write_text("time,currency,impact\n2026-01-09T13:30:00+00:00,USD,High\n")
        self.assertEqual(load_calendar(self.dir / "news.csv"),
                         [NewsEvent(datetime(2026, 1, 9, 13, 30, tzinfo=UTC), "USD", "High")])
        (self.dir / "naive.csv").write_text("time,currency,impact\n2026-01-09T13:30:00,USD,High\n")
        with self.assertRaises(ValueError):
            load_calendar(self.dir / "naive.csv")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_history.py" -v`
Expected: ERROR — `No module named 'fxbot.history'`

- [ ] **Step 3: Write the implementation**

`fxbot/history.py`:
```python
"""Historical bid/ask candles for backtests (spec §8).

Files: `<dir>/<PAIR>_<GRANULARITY>.csv` with header
`time,bid_o,bid_h,bid_l,bid_c,ask_o,ask_h,ask_l,ask_c` (time = UTC candle open, ISO 8601).
Mid candles are derived as (bid + ask) / 2 per field; live trading derives mid the same way
(Plan 4), so indicators match between backtest and live.
"""
import bisect
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fxbot.strategy import Candle

FIELDS = ["time", "bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c"]
DURATION = {"S10": timedelta(seconds=10), "M15": timedelta(minutes=15), "H1": timedelta(hours=1)}


@dataclass(frozen=True)
class HistCandle:
    time: datetime
    bid_o: float
    bid_h: float
    bid_l: float
    bid_c: float
    ask_o: float
    ask_h: float
    ask_l: float
    ask_c: float

    def mid(self) -> Candle:
        return Candle(self.time, (self.bid_o + self.ask_o) / 2, (self.bid_h + self.ask_h) / 2,
                      (self.bid_l + self.ask_l) / 2, (self.bid_c + self.ask_c) / 2)


def validate(candles: list[HistCandle]) -> None:
    for prev, cur in zip(candles, candles[1:]):
        if cur.time <= prev.time:
            raise ValueError(f"candles not strictly increasing at {cur.time}")
    for c in candles:
        if c.time.tzinfo is None:
            raise ValueError("candle times must be timezone-aware UTC")
        if c.ask_o < c.bid_o or c.ask_c < c.bid_c or c.bid_h < c.bid_l or c.ask_h < c.ask_l:
            raise ValueError(f"inconsistent prices at {c.time}")


def save(path: Path, candles: list[HistCandle]) -> None:
    validate(candles)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for c in candles:
            w.writerow([c.time.isoformat(), c.bid_o, c.bid_h, c.bid_l, c.bid_c,
                        c.ask_o, c.ask_h, c.ask_l, c.ask_c])


def load(path: Path) -> list[HistCandle]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FIELDS:
            raise ValueError(f"{path}: expected header {','.join(FIELDS)}")
        candles = [HistCandle(datetime.fromisoformat(r["time"]),
                              *(float(r[k]) for k in FIELDS[1:])) for r in reader]
    validate(candles)
    return candles


class Series:
    """One pair/granularity with precomputed mids and a time index for fast slicing."""

    def __init__(self, candles: list[HistCandle], granularity: str):
        self.candles = candles
        self.mids = [c.mid() for c in candles]
        self.times = [c.time for c in candles]
        self.duration = DURATION[granularity]

    def completed_count(self, now: datetime) -> int:
        """Number of candles fully closed at `now`."""
        return bisect.bisect_right(self.times, now - self.duration)

    def at_or_after(self, t: datetime) -> HistCandle | None:
        i = bisect.bisect_left(self.times, t)
        return self.candles[i] if i < len(self.candles) else None


class History:
    """All series for a backtest, keyed by (pair, granularity)."""

    def __init__(self, data: dict[tuple[str, str], list[HistCandle]]):
        self.series = {key: Series(c, key[1]) for key, c in data.items()}

    @classmethod
    def from_dir(cls, directory: Path, pairs: tuple[str, ...],
                 granularities: tuple[str, ...] = ("H1", "M15")) -> "History":
        return cls({(p, g): load(Path(directory) / f"{p}_{g}.csv") for p in pairs for g in granularities})

    def get(self, pair: str, granularity: str) -> Series | None:
        return self.series.get((pair, granularity))
```

Replace in `fxbot/news.py`:
```python
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

```
with:
```python
import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

```

Append to `fxbot/news.py`:
```python
def load_calendar(path: Path) -> list[NewsEvent]:
    """Historical calendar CSV with header `time,currency,impact` (UTC ISO 8601 times)."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["time", "currency", "impact"]:
            raise ValueError(f"{path}: expected header time,currency,impact")
        events = [NewsEvent(datetime.fromisoformat(r["time"]), r["currency"], r["impact"]) for r in reader]
    if any(e.time.tzinfo is None for e in events):
        raise ValueError(f"{path}: times must be timezone-aware UTC")
    return events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_fx_history.py" -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/history.py fxbot/news.py tests/fx_history.py tests/test_fx_history.py
git commit -m "fxbot: add historical candle store and news calendar loader"
```

---

### Task 2: Backtest hooks in strategy, engine and ledger

**Files:**
- Modify: `fxbot/strategy.py`, `fxbot/engine.py`, `fxbot/ledger.py`
- Test: `tests/test_fx_backtest_hooks.py`

**Interfaces:**
- Produces: `strategy.trend_direction(h1, settings) -> int` (renamed from private `_trend`); `strategy.evaluate` output unchanged but breakout is checked first; `Engine(broker, ledger, settings, news, alert, signal_fn=strategy.evaluate)` and `engine.signal_fn`; `Ledger(path, durable=True)`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_backtest_hooks.py`:
```python
import tempfile
import unittest
from datetime import timedelta

from fx_fixtures import NOW, Harness, set_market
from fx_history import START, make_history
from fxbot.config import Settings
from fxbot.indicators import atr
from fxbot.ledger import Ledger
from fxbot.strategy import Signal, _breakout, evaluate, trend_direction

S = Settings()


def reference_evaluate(pair, h1, m15, s):
    """Plan 1's original order (trend first, then breakout) - the optimised version must match."""
    if len(h1) < s.h1_window or len(m15) < s.m15_window:
        return None
    h1, m15 = h1[-s.h1_window:], m15[-s.m15_window:]
    if h1[-1].time + timedelta(hours=1) > m15[-1].time + timedelta(minutes=15):
        return None
    trend = trend_direction(h1, s)
    if trend == 0 or _breakout(m15, s.breakout_lookback) != trend:
        return None
    a = atr([c.high for c in m15], [c.low for c in m15], [c.close for c in m15], s.atr_period)[-1]
    return Signal(pair, trend, m15[-1].time, s.stop_atr_mult * a)


class OptimisedStrategyTests(unittest.TestCase):
    def test_breakout_first_matches_reference_on_synthetic_history(self):
        h = make_history(60)
        checked = signals = 0
        for pair in ("EUR_USD", "USD_JPY"):
            h1s, m15s = h.get(pair, "H1"), h.get(pair, "M15")
            for t in m15s.times[3000:4500]:
                now = t + timedelta(minutes=15, seconds=10)
                h1 = h1s.mids[:h1s.completed_count(now)]
                m15 = m15s.mids[:m15s.completed_count(now)]
                expected = reference_evaluate(pair, h1, m15, S)
                self.assertEqual(evaluate(pair, h1, m15, S), expected)
                checked += 1
                signals += expected is not None
        self.assertGreater(checked, 2000)
        self.assertGreater(signals, 10)            # the comparison covered real signals


class EngineSignalHookTests(unittest.TestCase):
    def test_engine_uses_injected_signal_function(self):
        h = Harness(self)
        calls = []

        def never(pair, h1, m15, s):
            calls.append(pair)
            return None
        h.engine.signal_fn = never
        set_market(h.sim, "EUR_USD", breakout=+1)
        self.assertEqual(h.cycle().filled, [])
        self.assertEqual(len(calls), 5)


class NonDurableLedgerTests(unittest.TestCase):
    def test_non_durable_ledger_works_and_is_still_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(f"{tmp}/l.db", durable=False)
            ledger.set_last_txn_id(7)
            self.assertEqual(ledger.last_txn_id, 7)
            with self.assertRaises(Exception):
                Ledger(f"{tmp}/l.db")
            ledger.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_backtest_hooks.py" -v`
Expected: ERROR — `cannot import name 'trend_direction' from 'fxbot.strategy'`

- [ ] **Step 3: Write the implementation**

Replace in `fxbot/strategy.py`:
```python
def _trend(h1: Sequence[Candle], s: Settings) -> int:
```
with:
```python
def trend_direction(h1: Sequence[Candle], s: Settings) -> int:
    """+1 / -1 when the H1 EMA filter allows longs / shorts, else 0."""
```

Replace in `fxbot/strategy.py`:
```python
    trend = _trend(h1, s)
    if trend == 0 or _breakout(m15, s.breakout_lookback) != trend:
        return None
```
with:
```python
    breakout = _breakout(m15, s.breakout_lookback)   # cheap check first; EMAs only on breakouts
    if breakout == 0 or trend_direction(h1, s) != breakout:
        return None
    trend = breakout
```

Replace in `fxbot/engine.py`:
```python
Alert = Callable[[str, str], None]                           # (level, text)
```
with:
```python
Alert = Callable[[str, str], None]                           # (level, text)
SignalFn = Callable[..., object]                             # same signature as strategy.evaluate
```

Replace in `fxbot/engine.py`:
```python
    def __init__(self, broker: Broker, ledger: Ledger, settings: Settings,
                 news: NewsSource, alert: Alert):
        self.broker, self.ledger, self.s, self.news, self._alert_fn = broker, ledger, settings, news, alert
```
with:
```python
    def __init__(self, broker: Broker, ledger: Ledger, settings: Settings,
                 news: NewsSource, alert: Alert, signal_fn: SignalFn = signal_for):
        """`signal_fn` defaults to the strategy; backtests pass baselines or a cached wrapper."""
        self.broker, self.ledger, self.s, self.news, self._alert_fn = broker, ledger, settings, news, alert
        self.signal_fn = signal_fn
```

Replace in `fxbot/engine.py`:
```python
            sig = signal_for(pair, h1[pair], m15[pair], s)
```
with:
```python
            sig = self.signal_fn(pair, h1[pair], m15[pair], s)
```

Replace in `fxbot/ledger.py`:
```python
    def __init__(self, path: str):
```
with:
```python
    def __init__(self, path: str, durable: bool = True):
        """durable=False skips disk syncs: only for throwaway backtest ledgers."""
```

Replace in `fxbot/ledger.py`:
```python
            self._db.executescript(SCHEMA)
```
with:
```python
            if not durable:
                self._db.execute("PRAGMA synchronous=OFF")
                self._db.execute("PRAGMA journal_mode=MEMORY")
            self._db.executescript(SCHEMA)
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `python -m unittest discover -s tests -v`
Expected: all OK (the existing strategy, engine and ledger tests must still pass unchanged).

- [ ] **Step 5: Commit**

```bash
git add fxbot/strategy.py fxbot/engine.py fxbot/ledger.py tests/test_fx_backtest_hooks.py
git commit -m "fxbot: add backtest hooks (breakout-first evaluation, signal injection, non-durable ledger)"
```

---

### Task 3: Backtest broker

**Files:**
- Create: `fxbot/backtest_broker.py`
- Test: `tests/test_fx_backtest_broker.py`

**Interfaces:**
- Consumes: `History`, Plan 2 broker types.
- Produces: `BacktestBroker(history, settings, nav, s10=None)` implementing `Broker`, plus `.advance(now)`, `.factor(pair, at)`, `.balance`, `.trades`, `.closed: list[ClosedTrade]`, `margin_rate = 1/30`; `ClosedTrade(trade_id, client_id, pair, units, entry, exit, open_time, close_time, pl, reason)` with `reason` ∈ `stop|gap|target|market`; `S10Lookup = Callable[[pair, datetime], (bid, ask) | None]`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_backtest_broker.py`:
```python
import unittest
from datetime import datetime, timedelta, timezone

from fxbot.backtest_broker import BacktestBroker
from fxbot.broker import BrokerError
from fxbot.config import Settings
from fxbot.history import HistCandle, History

UTC = timezone.utc
T = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)       # Tuesday
SLIP = 0.00003                                    # 0.3 pip per side
SPREAD = 0.0002


def at(minutes: int) -> datetime:
    return T + timedelta(minutes=minutes)


def c(minutes, o, h, low, close, spread=SPREAD):
    return HistCandle(at(minutes), o, h, low, close, o + spread, h + spread, low + spread, close + spread)


def flat(level, n=8, spread=SPREAD):
    return [c(15 * i, level, level, level, level, spread) for i in range(n)]


def broker(eur, s10=None, settings=Settings()):
    data = {("EUR_USD", "M15"): eur, ("GBP_USD", "M15"): flat(1.25 - 0.0001),   # GBP/USD mid 1.25
            ("USD_JPY", "M15"): flat(150.0 - 0.01, spread=0.02)}                  # USD/JPY mid 150
    return BacktestBroker(History(data), settings, 1000.0, s10)


EUR = [c(0, 1.1000, 1.1005, 1.0995, 1.1000),
       c(15, 1.1000, 1.1004, 1.0998, 1.1001),     # entry candle: decision at 09:15:10
       c(30, 1.1001, 1.1006, 1.0999, 1.1003),
       c(45, 1.1003, 1.1008, 1.1000, 1.1005),
       c(60, 1.1005, 1.1009, 1.1001, 1.1006)]


class ExecutionTests(unittest.TestCase):
    def test_entry_without_s10_uses_next_open_with_double_slippage(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", 1000, "fxb-1", 0.0010, 2.0)
        self.assertAlmostEqual(r.fill_price, 1.1000 + SPREAD + 2 * SLIP, places=10)
        self.assertAlmostEqual(r.stop_price, r.fill_price - 0.0010, places=10)
        self.assertEqual(b.trades[r.trade_id].open_time, at(15) + timedelta(seconds=10))

    def test_entry_with_s10_quote_uses_single_slippage(self):
        b = broker(EUR, s10=lambda pair, t: (1.1001, 1.1003) if t == at(15) + timedelta(seconds=10) else None)
        b.advance(at(15) + timedelta(seconds=10))
        sell = b.market_order("EUR_USD", -1000, "fxb-1", 0.0010, 0.5)
        self.assertAlmostEqual(sell.fill_price, 1.1001 - SLIP, places=10)

    def test_price_bound_rejects(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        self.assertEqual(b.market_order("EUR_USD", 1000, "fxb-1", 0.001, 1.1001).status, "rejected")

    def test_only_completed_candles_and_prices_are_visible(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        self.assertEqual([x.time for x in b.candles("EUR_USD", "M15", 10)], [at(0)])
        p = b.prices(("EUR_USD",))["EUR_USD"]
        self.assertEqual((p.bid, p.time), (1.1000, at(15)))
        self.assertAlmostEqual(p.loss_factor, 1 / 1.25, places=10)

    def test_conversion_factors(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        self.assertAlmostEqual(b.factor("USD_JPY", b.now), 1 / (1.25 * 150.0), places=12)
        with self.assertRaises(BrokerError):
            b.factor("EUR_CHF", b.now)


class ExitTests(unittest.TestCase):
    def open_long(self, eur, stop_distance=0.0010):
        b = broker(eur)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", 1000, "fxb-1", stop_distance, 2.0)
        return b, r

    def test_stop_exit_pays_slippage(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1002, 1.0990, 1.0992)] + EUR[3:]
        b, r = self.open_long(eur)
        b.advance(at(45) + timedelta(seconds=10))
        closed = b.closed[0]
        self.assertEqual(closed.reason, "stop")
        self.assertAlmostEqual(closed.exit, r.stop_price - SLIP, places=10)
        self.assertAlmostEqual(closed.pl, 1000 * (closed.exit - r.fill_price) / 1.25, places=9)
        self.assertAlmostEqual(b.balance, 1000 + closed.pl, places=9)

    def test_target_exit_at_exact_price(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1040, 1.1000, 1.1030)] + EUR[3:]
        b, r = self.open_long(eur)
        b.set_take_profit(r.trade_id, 1.1030)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual((b.closed[0].reason, b.closed[0].exit), ("target", 1.1030))

    def test_stop_and_target_in_same_candle_assumes_stop(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1040, 1.0980, 1.1000)] + EUR[3:]
        b, r = self.open_long(eur)
        b.set_take_profit(r.trade_id, 1.1030)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "stop")

    def test_gap_through_stop_fills_at_open(self):
        eur = EUR[:2] + [c(30, 1.0980, 1.0985, 1.0975, 1.0982)] + EUR[3:]
        b, r = self.open_long(eur)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "gap")
        self.assertAlmostEqual(b.closed[0].exit, 1.0980 - SLIP, places=10)
        self.assertLess(b.closed[0].exit, r.stop_price - SLIP)          # worse than 1R

    def test_short_stop_uses_ask(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1009, 1.1000, 1.1005)] + EUR[3:]   # ask high 1.1011
        b = broker(eur)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", -1000, "fxb-1", 0.0010, 0.5)      # fill 1.0999 ... stop ~1.1009
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "stop")
        self.assertAlmostEqual(b.closed[0].exit, r.stop_price + SLIP, places=10)

    def test_no_exit_before_candle_completes(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1002, 1.0990, 1.0992)] + EUR[3:]
        b, _ = self.open_long(eur)
        b.advance(at(30) + timedelta(seconds=10))     # the 09:30 candle has not closed yet
        self.assertEqual(b.closed, [])

    def test_market_close_and_unrealized_nav(self):
        b, r = self.open_long(EUR)
        b.advance(at(45) + timedelta(seconds=10))
        expected = 1000 + 1000 * (1.1003 - r.fill_price) / 1.25      # marked at last bid close
        self.assertAlmostEqual(b.account().nav, expected, places=9)
        self.assertTrue(b.close_trade(r.trade_id))
        self.assertAlmostEqual(b.closed[0].exit, 1.1003 - 2 * SLIP, places=10)   # 09:45 open bid
        self.assertEqual(b.closed[0].reason, "market")
        self.assertFalse(b.close_trade(r.trade_id))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_backtest_broker.py" -v`
Expected: ERROR — `No module named 'fxbot.backtest_broker'`

- [ ] **Step 3: Write the implementation**

`fxbot/backtest_broker.py`:
```python
"""Broker over historical bid/ask candles, used by the backtest (spec §8).

Execution model (all deliberately conservative):
- Market orders (entries, time exits, flattening) execute 10 s after the decision candle closes:
  at the S10 candle open covering that moment if available, else at the next M15 open with
  DOUBLE slippage. Buys pay ask + slippage, sells receive bid - slippage.
- Stops are checked on bid (longs) / ask (shorts) M15 highs and lows. If a candle (other than the
  entry candle) opens beyond the stop, the exit is that open price (gap). Stop exits pay slippage.
- Take-profits fill exactly at their price. If stop and target are both touched in one candle,
  the stop is assumed to have been hit first.
- P&L converts to GBP with the quote currency's GBP rate at exit time (mid prices).
"""
import bisect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from fxbot.broker import (Account, BrokerError, InstrumentInfo, OrderResult, PendingOrder, Price,
                          Trade, Txn)
from fxbot.config import Settings
from fxbot.history import History
from fxbot.instruments import INSTRUMENTS, currencies
from fxbot.strategy import Candle

M15 = timedelta(minutes=15)
S10Lookup = Callable[[str, datetime], tuple[float, float] | None]   # (pair, time) -> (bid, ask)


@dataclass(frozen=True)
class ClosedTrade:
    trade_id: str
    client_id: str
    pair: str
    units: int
    entry: float
    exit: float
    open_time: datetime
    close_time: datetime
    pl: float
    reason: str   # stop | gap | target | market


class BacktestBroker:
    margin_rate = 1 / 30   # UK retail leverage on majors

    def __init__(self, history: History, settings: Settings, nav: float, s10: S10Lookup | None = None):
        self.h, self.s, self.balance, self.s10 = history, settings, nav, s10
        self.now: datetime | None = None
        self.trades: dict[str, Trade] = {}
        self.txns: list[Txn] = []
        self.closed: list[ClosedTrade] = []
        self._next_index: dict[str, int] = {}   # trade_id -> next M15 candle index to check
        self._entry_index: dict[str, int] = {}
        self._ids = 0

    # --- price helpers -----------------------------------------------------
    def _series(self, pair: str, granularity: str = "M15"):
        series = self.h.get(pair, granularity)
        if series is None:
            raise BrokerError(f"no history for {pair} {granularity}")
        return series

    def _last(self, pair: str, at: datetime):
        series = self._series(pair)
        i = series.completed_count(at)
        if i == 0:
            raise BrokerError(f"no completed candle for {pair} at {at}")
        return series.candles[i - 1]

    def _mid_close(self, pair: str, at: datetime) -> float:
        c = self._last(pair, at)
        return (c.bid_c + c.ask_c) / 2

    def factor(self, pair: str, at: datetime) -> float:
        """GBP per unit of the pair's quote currency."""
        quote = currencies(pair)[1]
        if quote == "GBP":
            return 1.0
        gbpusd = self._mid_close("GBP_USD", at)
        if quote == "USD":
            return 1 / gbpusd
        if quote in ("JPY", "CAD"):
            return 1 / (gbpusd * self._mid_close(f"USD_{quote}", at))
        raise BrokerError(f"no GBP conversion for {quote}")

    def _slip(self, pair: str) -> float:
        return self.s.slippage_pips_per_side * INSTRUMENTS[pair].pip

    def _decision_close(self) -> datetime:
        n = self.now
        return n.replace(minute=n.minute - n.minute % 15, second=0, microsecond=0)

    def _execute(self, pair: str, buy: bool) -> tuple[float, datetime]:
        t = self._decision_close()
        quote = self.s10(pair, t + timedelta(seconds=10)) if self.s10 else None
        mult = 1
        if quote is None:
            c = self._series(pair).at_or_after(t)
            if c is None or c.time != t:
                raise BrokerError(f"no price to execute {pair} at {t}")
            quote, mult = (c.bid_o, c.ask_o), 2
        bid, ask = quote
        slip = mult * self._slip(pair)
        return (ask + slip if buy else bid - slip), t + timedelta(seconds=10)

    def _txn(self, kind: str, at: datetime, **fields) -> Txn:
        txn = Txn(len(self.txns) + 1, kind, at, **fields)
        self.txns.append(txn)
        return txn

    def _close(self, trade: Trade, price: float, at: datetime, reason: str) -> None:
        pl = trade.units * (price - trade.open_price) * self.factor(trade.pair, at)
        self.balance += pl
        del self.trades[trade.trade_id]
        self._next_index.pop(trade.trade_id, None)
        self._txn("CLOSE", at, trade_id=trade.trade_id, pair=trade.pair, units=-trade.units,
                  price=price, amount=pl)
        self.closed.append(ClosedTrade(trade.trade_id, trade.client_id, trade.pair, trade.units,
                                       trade.open_price, price, trade.open_time, at, pl, reason))

    # --- time ----------------------------------------------------------------
    def advance(self, now: datetime) -> None:
        """Move the clock to `now`, triggering stops/targets on every candle completed so far."""
        self.now = now
        for trade in sorted(self.trades.values(), key=lambda t: t.open_time):
            series = self._series(trade.pair)
            end = series.completed_count(now)
            i = self._next_index[trade.trade_id]
            while i < end and trade.trade_id in self.trades:
                self._check(self.trades[trade.trade_id], series.candles[i],
                            i == self._entry_index[trade.trade_id])
                i += 1
            if trade.trade_id in self.trades:
                self._next_index[trade.trade_id] = i

    def _check(self, t: Trade, c, entry_candle: bool) -> None:
        at = c.time + M15
        slip = self._slip(t.pair)
        stop, tp = t.stop_price, t.take_profit_price
        if t.units > 0:
            if not entry_candle and c.bid_o <= stop:
                return self._close(t, c.bid_o - slip, c.time, "gap")
            if c.bid_l <= stop:
                return self._close(t, stop - slip, at, "stop")
            if tp is not None and c.bid_h >= tp:
                return self._close(t, tp, at, "target")
        else:
            if not entry_candle and c.ask_o >= stop:
                return self._close(t, c.ask_o + slip, c.time, "gap")
            if c.ask_h >= stop:
                return self._close(t, stop + slip, at, "stop")
            if tp is not None and c.ask_l <= tp:
                return self._close(t, tp, at, "target")

    # --- Broker interface ----------------------------------------------------
    def account(self) -> Account:
        unrealized = margin = 0.0
        for t in self.trades.values():
            c = self._last(t.pair, self.now)
            mark = c.bid_c if t.units > 0 else c.ask_c
            f = self.factor(t.pair, self.now)
            unrealized += t.units * (mark - t.open_price) * f
            margin += abs(t.units) * mark * f * self.margin_rate
        return Account(self.balance + unrealized, margin, len(self.txns))

    def candles(self, pair: str, granularity: str, count: int) -> list[Candle]:
        series = self._series(pair, granularity)
        i = series.completed_count(self.now)
        return series.mids[max(0, i - count):i]

    def prices(self, pairs: tuple[str, ...]) -> dict[str, Price]:
        out = {}
        for pair in pairs:
            c = self._last(pair, self.now)
            f = self.factor(pair, self.now)
            out[pair] = Price(pair, c.time + M15, c.bid_c, c.ask_c, True, f, f)
        return out

    def instrument(self, pair: str) -> InstrumentInfo:
        return InstrumentInfo(self.margin_rate, 100_000_000)

    def open_trades(self) -> list[Trade]:
        return list(self.trades.values())

    def pending_orders(self) -> list[PendingOrder]:
        return []

    def transactions_since(self, txn_id: int) -> list[Txn]:
        return self.txns[txn_id:]

    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult:
        fill, at = self._execute(pair, units > 0)
        if (units > 0 and fill > price_bound) or (units < 0 and fill < price_bound):
            self._txn("REJECT", at, client_id=client_id, pair=pair)
            return OrderResult("rejected", reason="BOUNDS_VIOLATION")
        self._ids += 1
        stop = fill - stop_distance if units > 0 else fill + stop_distance
        trade = Trade(str(self._ids), pair, units, fill, at, client_id, stop, None)
        self.trades[trade.trade_id] = trade
        idx = bisect.bisect_left(self._series(pair).times, self._decision_close())
        self._entry_index[trade.trade_id] = self._next_index[trade.trade_id] = idx
        self._txn("FILL", at, client_id=client_id, trade_id=trade.trade_id, pair=pair, units=units, price=fill)
        return OrderResult("filled", trade.trade_id, fill, units, stop)

    def set_take_profit(self, trade_id: str, price: float) -> bool:
        t = self.trades.get(trade_id)
        if t is None:
            return False
        self.trades[trade_id] = Trade(t.trade_id, t.pair, t.units, t.open_price, t.open_time,
                                      t.client_id, t.stop_price, price)
        return True

    def close_trade(self, trade_id: str) -> bool:
        t = self.trades.get(trade_id)
        if t is None:
            return False
        price, at = self._execute(t.pair, t.units < 0)
        self._close(t, price, at, "market")
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_backtest_broker.py" -v`
Expected: 12 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/backtest_broker.py tests/test_fx_backtest_broker.py
git commit -m "fxbot: add backtest broker with conservative execution model"
```

---

### Task 4: Backtest runner

**Files:**
- Create: `fxbot/backtest.py`
- Test: `tests/test_fx_backtest.py`

**Interfaces:**
- Produces: `run(history, settings, start, end, *, nav=1000.0, news=None, signal_fn=strategy.evaluate, s10=None) -> BacktestResult`; `BacktestResult(start_nav, end_nav, trades, daily_nav, rejections: Counter, blocked: Counter, alerts, halted, open_at_end, signals)`; `TradeRecord(pair, side, units, open_time, close_time, entry, exit, pl_gbp, risk_gbp, reason)` with `.r`; `cached(signal_fn)` (memo exposed as `.memo`).

- [ ] **Step 1: Write the failing test**

`tests/test_fx_backtest.py`:
```python
import unittest
from datetime import timedelta

from fx_history import START, make_history
from fxbot.config import Settings
from fxbot.backtest import run
from fxbot.history import History
from fxbot.instruments import usd_direction
from fxbot.strategy import LONDON

S = Settings()
HISTORY = make_history(70)
BEGIN, END = START + timedelta(days=40), START + timedelta(days=70)


class BacktestRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run(HISTORY, S, BEGIN, END)

    def test_runs_cleanly_and_trades(self):
        r = self.result
        self.assertIsNone(r.halted)
        self.assertEqual(r.alerts, [])
        self.assertGreater(len(r.trades), 20)

    def test_accounting_balances(self):
        r = self.result
        self.assertEqual(r.open_at_end, 0)
        self.assertAlmostEqual(r.end_nav - r.start_nav, sum(t.pl_gbp for t in r.trades), places=6)

    def test_deterministic(self):
        again = run(HISTORY, S, BEGIN, END)
        self.assertEqual(again.trades, self.result.trades)

    def test_risk_limits_hold_throughout(self):
        trades = self.result.trades
        self.assertTrue(all(0 < t.risk_gbp <= 0.0025 * 1200 for t in trades))
        for t in trades:   # at every entry, open risk and USD-direction risk stay within the caps
            open_now = [o for o in trades if o.open_time <= t.open_time < o.close_time]
            self.assertLessEqual(len(open_now), 4)
            self.assertLessEqual(sum(o.risk_gbp for o in open_now), 0.01 * 1200)
            same = [o for o in open_now if usd_direction(o.pair, o.side) == usd_direction(t.pair, t.side)]
            self.assertLessEqual(sum(o.risk_gbp for o in same), 0.005 * 1200 + 1e-9)

    def test_no_position_held_over_the_weekend(self):
        for t in self.result.trades:
            self.assertLess((t.close_time - t.open_time).days, 2)
            local = t.open_time.astimezone(LONDON)
            self.assertFalse(local.weekday() == 4 and local.hour >= 18)

    def test_time_exits_happen_on_time(self):
        market = [t for t in self.result.trades if t.reason == "market"]
        self.assertTrue(market)
        for t in market:
            self.assertLessEqual(t.close_time - t.open_time, timedelta(minutes=135))

    def test_friday_flatten_without_time_exit(self):
        r = run(HISTORY, Settings(time_exit_minutes=None), BEGIN, END)
        self.assertEqual(r.open_at_end, 0)
        for t in r.trades:
            local = t.close_time.astimezone(LONDON)
            self.assertFalse(local.weekday() == 4 and local.hour >= 20, t)
            self.assertLess((t.close_time - t.open_time).days, 2)

    def test_no_lookahead(self):
        """Removing all data after a cut-off must not change any trade closed before it."""
        cut = START + timedelta(days=55)
        truncated = History({key: [c for c in s.candles if c.time < cut] for key, s in HISTORY.series.items()})
        early = run(truncated, S, BEGIN, cut)
        full = [t for t in self.result.trades if t.close_time < cut - timedelta(minutes=30)]
        self.assertEqual([t for t in early.trades if t.close_time < cut - timedelta(minutes=30)], full)

    def test_injected_signal_function_is_used(self):
        r = run(HISTORY, S, BEGIN, BEGIN + timedelta(days=5), signal_fn=lambda *a: None)
        self.assertEqual(r.trades, [])

    def test_costs_reduce_results(self):
        free = run(HISTORY, Settings(slippage_pips_per_side=0.0), BEGIN, END)
        self.assertGreater(sum(t.pl_gbp for t in free.trades), sum(t.pl_gbp for t in self.result.trades))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_backtest.py" -v`
Expected: ERROR — `No module named 'fxbot.backtest'`

- [ ] **Step 3: Write the implementation**

`fxbot/backtest.py`:
```python
"""Replay history through the live Engine with a BacktestBroker (spec §8).

The engine, risk manager, strategy and cost model are the live ones; only the broker differs.
Cycles run 10 s after every M15 close. A cycle is skipped only when no bot trade is open and no
pair has a signal: nothing could happen in it, so skipping it cannot change the result.
"""
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from fxbot.backtest_broker import BacktestBroker, S10Lookup
from fxbot.config import Settings
from fxbot.engine import Engine, weekend_phase
from fxbot.history import History
from fxbot.ledger import Ledger
from fxbot.news import NewsEvent
from fxbot.risk import uk_trading_day
from fxbot.strategy import Signal, evaluate

M15 = timedelta(minutes=15)
DELAY = timedelta(seconds=10)


@dataclass(frozen=True)
class TradeRecord:
    pair: str
    side: int
    units: int
    open_time: datetime
    close_time: datetime
    entry: float
    exit: float
    pl_gbp: float
    risk_gbp: float
    reason: str

    @property
    def r(self) -> float:
        """Result in multiples of the planned risk (stop risk + cost risk)."""
        return self.pl_gbp / self.risk_gbp if self.risk_gbp > 0 else 0.0


@dataclass
class BacktestResult:
    start_nav: float
    end_nav: float
    trades: list[TradeRecord]
    daily_nav: dict[date, float]
    rejections: Counter = field(default_factory=Counter)
    blocked: Counter = field(default_factory=Counter)
    alerts: list[tuple[str, str]] = field(default_factory=list)
    halted: str | None = None
    open_at_end: int = 0
    signals: dict[tuple[str, datetime], Signal | None] = field(default_factory=dict)


def cached(signal_fn):
    """Memoise a signal function per (pair, signal candle) so each is computed once per run."""
    memo: dict = {}

    def fn(pair, h1, m15, settings):
        if not m15:
            return signal_fn(pair, h1, m15, settings)
        key = (pair, m15[-1].time)
        if key not in memo:
            memo[key] = signal_fn(pair, h1, m15, settings)
        return memo[key]
    fn.memo = memo
    return fn


def run(history: History, settings: Settings, start: datetime, end: datetime, *,
        nav: float = 1000.0, news: list[NewsEvent] | None = None, signal_fn=evaluate,
        s10: S10Lookup | None = None) -> BacktestResult:
    """Replay M15 candles opening in [start, end). `news=None` means no calendar (filter off)."""
    events = list(news or [])
    broker = BacktestBroker(history, settings, nav, s10)
    sig = cached(signal_fn)
    series = history.get(settings.pairs[0], "M15")
    times = [t for t in series.times if start <= t < end]
    daily: dict[date, float] = {}
    alerts: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(f"{tmp}/ledger.db", durable=False)
        engine = Engine(broker, ledger, settings, lambda now: events,
                        lambda level, text: alerts.append((level, text)), signal_fn=sig)
        for t in times:
            now = t + M15 + DELAY
            broker.advance(now)
            if broker.trades and weekend_phase(now, settings) != "normal":
                engine.flatten(now)
            if broker.trades or any(
                    sig(p, broker.candles(p, "H1", settings.h1_window),
                        broker.candles(p, "M15", settings.m15_window), settings)
                    for p in settings.pairs):
                engine.run_cycle(now)
            daily[uk_trading_day(now)] = broker.account().nav
        risk = {p.trade_id: (p.risk_gbp or 0.0) for p in ledger.all_positions() if p.trade_id}
        decisions = ledger.decisions()
        halted = ledger.halt_reason()
        ledger.close()
    trades = [TradeRecord(c.pair, 1 if c.units > 0 else -1, abs(c.units), c.open_time, c.close_time,
                          c.entry, c.exit, c.pl, risk.get(c.trade_id, 0.0), c.reason)
              for c in broker.closed]
    end_nav = broker.account().nav if times else nav
    return BacktestResult(nav, end_nav, trades, daily,
                          Counter(r for _, o, r in decisions if o == "rejected"),
                          Counter(r for _, o, r in decisions if o == "blocked"),
                          alerts, halted, len(broker.trades), dict(sig.memo))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_backtest.py" -v`
Expected: 10 tests, OK (takes ~10 s)

- [ ] **Step 5: Commit**

```bash
git add fxbot/backtest.py tests/test_fx_backtest.py
git commit -m "fxbot: add backtest runner over the live engine"
```

---

### Task 5: Metrics

**Files:**
- Create: `fxbot/metrics.py`
- Test: `tests/test_fx_metrics.py`

**Interfaces:**
- Produces: `summarize(result, bootstrap_runs=2000, seed=0) -> dict` with keys `trades, start_nav, end_nav, net_gbp, expectancy_gbp, expectancy_r, win_rate, profit_factor (None if no losses), max_drawdown, worst_losing_streak, expectancy_r_ci95 ((lo, hi) or None), per_pair, per_year, exits, rejections, notional_cap_hits, blocked_cycles, max_simultaneous_positions, days_with_multiple_trades, multi_trade_days_all_lost, open_at_end, halted`; helpers `max_drawdown`, `losing_streak`, `max_simultaneous`, `bootstrap_ci(trades, runs=2000, seed=0, level=0.95)`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_metrics.py`:
```python
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from fxbot.backtest import BacktestResult, TradeRecord
from fxbot.metrics import bootstrap_ci, losing_streak, max_drawdown, max_simultaneous, summarize

UTC = timezone.utc
D = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


def trade(day, hour, pl, pair="EUR_USD", hours=1, risk=2.5):
    o = D + timedelta(days=day, hours=hour)
    return TradeRecord(pair, 1, 1000, o, o + timedelta(hours=hours), 1.1, 1.1, pl, risk, "stop")


TRADES = [trade(0, 0, 5.0), trade(0, 0.5, -2.5, "GBP_USD"), trade(1, 0, -2.5), trade(1, 2, -2.5),
          trade(2, 0, 5.0, "USD_JPY")]


class MetricTests(unittest.TestCase):
    def test_drawdown(self):
        nav = {date(2026, 1, 1): 1000, date(2026, 1, 2): 1100, date(2026, 1, 3): 990, date(2026, 1, 4): 1200}
        self.assertAlmostEqual(max_drawdown(nav), 0.1)

    def test_losing_streak_and_overlap(self):
        self.assertEqual(losing_streak(TRADES), 3)
        self.assertEqual(max_simultaneous(TRADES), 2)          # day 0: 09:00-10:00 and 09:30-10:30
        back_to_back = [trade(0, 0, 1.0), trade(0, 1, 1.0)]     # close and open at the same instant
        self.assertEqual(max_simultaneous(back_to_back), 1)

    def test_summary_numbers(self):
        r = BacktestResult(1000.0, 1002.5, TRADES, {date(2026, 1, 6): 1000.0, date(2026, 1, 8): 1002.5},
                           Counter({"max gross notional": 3, "news blackout": 1}))
        m = summarize(r, bootstrap_runs=0)
        self.assertEqual(m["trades"], 5)
        self.assertAlmostEqual(m["net_gbp"], 2.5)
        self.assertAlmostEqual(m["expectancy_r"], (2 - 1 - 1 - 1 + 2) / 5)
        self.assertAlmostEqual(m["profit_factor"], 10 / 7.5)
        self.assertAlmostEqual(m["win_rate"], 0.4)
        self.assertEqual(m["notional_cap_hits"], 3)
        self.assertEqual(m["per_pair"]["USD_JPY"]["trades"], 1)
        self.assertEqual(m["days_with_multiple_trades"], 2)
        self.assertAlmostEqual(m["multi_trade_days_all_lost"], 0.5)
        self.assertIsNone(m["expectancy_r_ci95"])

    def test_no_losses_gives_no_profit_factor(self):
        m = summarize(BacktestResult(1000.0, 1005.0, [trade(0, 0, 5.0)], {}), bootstrap_runs=0)
        self.assertIsNone(m["profit_factor"])

    def test_bootstrap_resamples_whole_days(self):
        # each day nets +2R and -1R: resampling days always gives 0.5R; resampling trades would not
        days = [trade(d, 0, 5.0) for d in range(3)] + [trade(d, 1, -2.5) for d in range(3)]
        self.assertEqual(bootstrap_ci(days, runs=300, seed=1), (0.5, 0.5))

    def test_bootstrap_resamples_days_deterministically(self):
        lo, hi = bootstrap_ci(TRADES, runs=500, seed=3)
        self.assertEqual(bootstrap_ci(TRADES, runs=500, seed=3), (lo, hi))
        self.assertLessEqual(lo, 0.2)
        self.assertGreaterEqual(hi, 0.2)
        self.assertGreaterEqual(lo, -1.0)        # a resampled day set can be all losers
        self.assertLessEqual(hi, 2.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_metrics.py" -v`
Expected: ERROR — `No module named 'fxbot.metrics'`

- [ ] **Step 3: Write the implementation**

`fxbot/metrics.py`:
```python
"""Performance metrics for backtest results (spec §8, Gate B)."""
import random
from collections import Counter, defaultdict
from datetime import date

from fxbot.backtest import BacktestResult, TradeRecord
from fxbot.risk import uk_trading_day


def max_drawdown(daily_nav: dict[date, float]) -> float:
    """Largest peak-to-trough fall of daily NAV, as a fraction of the peak."""
    peak, worst = None, 0.0
    for day in sorted(daily_nav):
        nav = daily_nav[day]
        peak = nav if peak is None else max(peak, nav)
        worst = max(worst, (peak - nav) / peak)
    return worst


def losing_streak(trades: list[TradeRecord]) -> int:
    streak = worst = 0
    for t in sorted(trades, key=lambda t: t.close_time):
        streak = streak + 1 if t.pl_gbp < 0 else 0
        worst = max(worst, streak)
    return worst


def max_simultaneous(trades: list[TradeRecord]) -> int:
    events = sorted([(t.open_time, 1) for t in trades] + [(t.close_time, -1) for t in trades],
                    key=lambda e: (e[0], e[1]))   # closes before opens at the same instant
    current = best = 0
    for _, step in events:
        current += step
        best = max(best, current)
    return best


def bootstrap_ci(trades: list[TradeRecord], runs: int = 2000, seed: int = 0,
                 level: float = 0.95) -> tuple[float, float] | None:
    """Confidence interval for expectancy (R per trade), resampling whole UK trading days.

    Trades on the same day are not independent, so days - not trades - are resampled.
    """
    days: dict[date, list[float]] = defaultdict(list)
    for t in trades:
        days[uk_trading_day(t.open_time)].append(t.r)
    groups = list(days.values())
    if not groups or runs <= 0:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(runs):
        sample = [rng.choice(groups) for _ in groups]
        n = sum(len(g) for g in sample)
        stats.append(sum(sum(g) for g in sample) / n)
    stats.sort()
    lo = stats[int((1 - level) / 2 * runs)]
    hi = stats[min(runs - 1, int((1 + level) / 2 * runs))]
    return lo, hi


def _group(trades: list[TradeRecord]) -> dict:
    n = len(trades)
    return {"trades": n, "net_gbp": sum(t.pl_gbp for t in trades),
            "expectancy_r": sum(t.r for t in trades) / n if n else 0.0}


def summarize(result: BacktestResult, bootstrap_runs: int = 2000, seed: int = 0) -> dict:
    trades = result.trades
    n = len(trades)
    wins = [t.pl_gbp for t in trades if t.pl_gbp > 0]
    losses = [-t.pl_gbp for t in trades if t.pl_gbp < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    by_pair, by_year, by_day = defaultdict(list), defaultdict(list), defaultdict(list)
    for t in trades:
        by_pair[t.pair].append(t)
        by_year[t.open_time.year].append(t)
        by_day[uk_trading_day(t.open_time)].append(t)
    multi = [d for d in by_day.values() if len(d) >= 2]
    return {
        "trades": n,
        "start_nav": result.start_nav,
        "end_nav": result.end_nav,
        "net_gbp": sum(t.pl_gbp for t in trades),
        "expectancy_gbp": sum(t.pl_gbp for t in trades) / n if n else 0.0,
        "expectancy_r": sum(t.r for t in trades) / n if n else 0.0,
        "win_rate": len(wins) / n if n else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else None,   # None = no losses
        "max_drawdown": max_drawdown(result.daily_nav),
        "worst_losing_streak": losing_streak(trades),
        "expectancy_r_ci95": bootstrap_ci(trades, bootstrap_runs, seed),
        "per_pair": {p: _group(ts) for p, ts in sorted(by_pair.items())},
        "per_year": {y: _group(ts) for y, ts in sorted(by_year.items())},
        "exits": dict(Counter(t.reason for t in trades)),
        "rejections": dict(result.rejections),
        "notional_cap_hits": result.rejections.get("max gross notional", 0),
        "blocked_cycles": dict(result.blocked),
        "max_simultaneous_positions": max_simultaneous(trades),
        "days_with_multiple_trades": len(multi),
        "multi_trade_days_all_lost": (sum(all(t.pl_gbp < 0 for t in d) for d in multi) / len(multi)
                                      if multi else None),
        "open_at_end": result.open_at_end,
        "halted": result.halted,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_metrics.py" -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/metrics.py tests/test_fx_metrics.py
git commit -m "fxbot: add backtest metrics with day-block bootstrap"
```

---

### Task 6: Research report, Gate B and CLI

**Files:**
- Create: `fxbot/research.py`, `fxbot/__main__.py`
- Test: `tests/test_fx_research.py`

**Interfaces:**
- Produces: `GRID`, `SLIPPAGE`, `trend_baseline(pair, h1, m15, settings)`, `random_direction(base, seed)`, `add_months`, `windows(start, end, train, test)`, `grid_settings`, `chain(history, plan, news, nav=1000.0)`, `walk_forward(history, settings, start, end, news, train_months=24, test_months=6, grid=GRID, min_trades=30) -> list[(Settings, start, end)]`, `no_skill(history, settings, start, end, news, base, runs, processes)`, `gate_b(report) -> {checks, no_skill_p95_expectancy_r, passed, manual_review}`, `research(history, settings, start, end, news, *, train_months, test_months, grid, no_skill_runs, processes) -> dict`, `write_report(report, out_dir) -> (json_path, md_path)`; CLI `python -m fxbot backtest --data DIR --start YYYY-MM-DD --end YYYY-MM-DD [--news CSV] [--out DIR] [--train-months N] [--test-months N] [--no-skill-runs N] [--processes N]`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_research.py`:
```python
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fx_history import START, make_history
from fxbot import research
from fxbot.config import Settings
from fxbot.history import save
from fxbot.research import (add_months, gate_b, random_direction, trend_baseline, windows,
                            write_report)
from fxbot.strategy import LONDON, Signal

UTC = timezone.utc
S = Settings()
TINY_GRID = {"stop_atr_mult": (1.5, 2.0)}


class BaselineTests(unittest.TestCase):
    def test_trend_baseline_only_at_session_opens(self):
        h = make_history(40)
        h1s, m15s = h.get("EUR_USD", "H1"), h.get("EUR_USD", "M15")
        seen = set()
        for t in m15s.times[2600:]:
            now = t + timedelta(minutes=15, seconds=10)
            sig = trend_baseline("EUR_USD", h1s.mids[:h1s.completed_count(now)],
                                 m15s.mids[:m15s.completed_count(now)], S)
            if sig:
                seen.add((sig.candle_time + timedelta(minutes=15)).astimezone(LONDON).strftime("%H:%M"))
        self.assertTrue(seen)
        self.assertLessEqual(seen, {"07:00", "13:00"})

    def test_random_direction_keeps_times_and_is_seeded(self):
        t = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)
        base = {("EUR_USD", t): Signal("EUR_USD", 1, t, 0.001), ("EUR_USD", t + timedelta(minutes=15)): None}

        class C:
            time = t
        sides = [random_direction(base, seed)("EUR_USD", [], [C], S).side for seed in range(40)]
        self.assertEqual(set(sides), {1, -1})
        self.assertEqual(random_direction(base, 7)("EUR_USD", [], [C], S),
                         random_direction(base, 7)("EUR_USD", [], [C], S))


class WalkForwardTests(unittest.TestCase):
    def test_add_months(self):
        self.assertEqual(add_months(datetime(2025, 11, 1, tzinfo=UTC), 3), datetime(2026, 2, 1, tzinfo=UTC))

    def test_windows_do_not_overlap_and_test_after_train(self):
        w = windows(datetime(2020, 1, 1, tzinfo=UTC), datetime(2023, 1, 1, tzinfo=UTC), 24, 6)
        self.assertEqual(len(w), 2)
        for tr_s, tr_e, te_s, te_e in w:
            self.assertEqual(tr_e, te_s)
            self.assertLessEqual(te_e, datetime(2023, 1, 1, tzinfo=UTC))
        self.assertEqual(w[0][3], w[1][2])


class GridSelectionTests(unittest.TestCase):
    def test_walk_forward_picks_best_training_expectancy(self):
        from fxbot.backtest import BacktestResult, TradeRecord
        t0 = datetime(2026, 1, 6, 9, tzinfo=UTC)

        def fake_run(history, settings, start, end, **kw):
            r = 1.0 if settings.stop_atr_mult == 2.0 else 0.1     # 2.0 is clearly better
            trades = [TradeRecord("EUR_USD", 1, 1, t0, t0, 1, 1, r * 2.5, 2.5, "target")] * 40
            return BacktestResult(1000, 1000, trades, {})
        original = research.run
        research.run = fake_run
        try:
            plan = research.walk_forward(None, S, datetime(2020, 1, 1, tzinfo=UTC),
                                         datetime(2021, 1, 1, tzinfo=UTC), None, 6, 3,
                                         {"stop_atr_mult": (1.5, 2.0, 1.0)})
        finally:
            research.run = original
        self.assertEqual([s.stop_atr_mult for s, _, _ in plan], [2.0, 2.0])


def fake_report(**over):
    good = {"expectancy_r": 0.2, "net_gbp": 50.0, "trades": 150, "max_drawdown": 0.05, "halted": None}
    report = {"walk_forward": dict(good), "walk_forward_slippage_0.6": dict(good),
              "trend_baseline": {"expectancy_r": 0.05}, "no_skill": {"expectancy_r": [0.0, 0.05, 0.1]}}
    for key, value in over.items():
        section, field = key.split("__")
        report[section][field] = value
    return report


class GateBTests(unittest.TestCase):
    def test_pass(self):
        g = gate_b(fake_report())
        self.assertTrue(g["passed"])
        self.assertFalse(g["checks"]["oos_trades_preferred_200"])   # preferred, not required

    def test_each_requirement_can_fail(self):
        for over in ({"walk_forward__expectancy_r": -0.01}, {"walk_forward__trades": 99},
                     {"walk_forward_slippage_0.6__net_gbp": -1.0}, {"trend_baseline__expectancy_r": 0.3},
                     {"walk_forward__max_drawdown": 0.10}, {"walk_forward__halted": "halted: x"}):
            self.assertFalse(gate_b(fake_report(**over))["passed"], over)
        r = fake_report()
        r["no_skill"]["expectancy_r"] = [0.0, 0.3, 0.3]
        self.assertFalse(gate_b(r)["passed"])


class ResearchEndToEndTests(unittest.TestCase):
    def test_full_report_and_cli(self):
        h = make_history(80)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            for (pair, g), s in h.series.items():
                save(data / f"{pair}_{g}.csv", s.candles)
            original = research.GRID
            research.GRID = TINY_GRID
            try:
                from fxbot.__main__ import main
                code = main(["backtest", "--data", str(data), "--start", "2026-02-02", "--end", "2026-03-23",
                             "--train-months", "1", "--test-months", "1", "--no-skill-runs", "3",
                             "--processes", "1", "--out", f"{tmp}/out"])
            finally:
                research.GRID = original
            self.assertEqual(code, 0)
            report = json.loads((Path(tmp) / "out" / "research.json").read_text())
            self.assertEqual(set(report["slippage_sensitivity"]), {"0.0", "0.3", "0.6", "1.0"})
            self.assertEqual(len(report["no_skill"]["expectancy_r"]), 3)
            self.assertIn("passed", report["gate_b"])
            self.assertGreater(report["walk_forward"]["trades"], 0)
            self.assertIn("Gate B", (Path(tmp) / "out" / "research.md").read_text())

    def test_parallel_no_skill_matches_serial(self):
        h = make_history(50)
        s, e = START + timedelta(days=40), START + timedelta(days=47)
        base = research.run(h, S, s, e).signals
        serial = research.no_skill(h, S, s, e, None, base, 4, processes=1)
        parallel = research.no_skill(h, S, s, e, None, base, 4, processes=2)
        self.assertEqual(serial, parallel)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_research.py" -v`
Expected: ERROR — `cannot import name 'research' from 'fxbot'`

- [ ] **Step 3: Write the implementation**

`fxbot/research.py`:
```python
"""Research report: walk-forward, variants, baselines and the Gate B check (spec §8, §10)."""
import itertools
import json
import multiprocessing
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path

from fxbot.backtest import BacktestResult, run
from fxbot.config import Settings
from fxbot.history import History
from fxbot.indicators import atr
from fxbot.metrics import summarize
from fxbot.news import NewsEvent
from fxbot.strategy import LONDON, Signal, trend_direction

GRID = {"stop_atr_mult": (1.0, 1.5, 2.0), "breakout_lookback": (20, 30)}
SLIPPAGE = (0.0, 0.3, 0.6, 1.0)
SESSION_OPENS = (time(7, 0), time(13, 0))   # London and New York opens, London time
M15 = timedelta(minutes=15)


# --- baselines -----------------------------------------------------------------
def trend_baseline(pair, h1, m15, s: Settings) -> Signal | None:
    """Same H1 trend filter; enters at the first M15 close of each session, no breakout needed."""
    if len(h1) < s.h1_window or len(m15) < s.m15_window:
        return None
    h1, m15 = h1[-s.h1_window:], m15[-s.m15_window:]
    if (m15[-1].time + M15).astimezone(LONDON).time() not in SESSION_OPENS:
        return None
    if h1[-1].time + timedelta(hours=1) > m15[-1].time + M15:
        return None
    side = trend_direction(h1, s)
    if side == 0:
        return None
    a = atr([c.high for c in m15], [c.low for c in m15], [c.close for c in m15], s.atr_period)[-1]
    return Signal(pair, side, m15[-1].time, s.stop_atr_mult * a)


def random_direction(base: dict, seed: int):
    """No-skill baseline: the strategy's own entry times, coin-flip direction."""
    rng = random.Random(seed)

    def fn(pair, h1, m15, s):
        sig = base.get((pair, m15[-1].time)) if m15 else None
        return None if sig is None else replace(sig, side=rng.choice((1, -1)))
    return fn


# --- walk-forward ---------------------------------------------------------------
def add_months(t: datetime, months: int) -> datetime:
    y, m = divmod(t.month - 1 + months, 12)
    return t.replace(year=t.year + y, month=m + 1)


def windows(start: datetime, end: datetime, train: int, test: int) -> list[tuple]:
    """(train_start, train_end, test_start, test_end) with non-overlapping test windows."""
    out, s = [], start
    while add_months(s, train) < end:
        te_s = add_months(s, train)
        out.append((s, te_s, te_s, min(add_months(te_s, test), end)))
        s = add_months(s, test)
    return out


def grid_settings(settings: Settings, grid: dict) -> list[Settings]:
    keys = list(grid)
    return [replace(settings, **dict(zip(keys, values))) for values in itertools.product(*grid.values())]


def chain(history, plan: list[tuple], news, nav: float = 1000.0) -> BacktestResult:
    """Run consecutive (settings, start, end) windows, carrying NAV forward; merge results."""
    merged = BacktestResult(nav, nav, [], {})
    for settings, start, end in plan:
        r = run(history, settings, start, end, nav=merged.end_nav, news=news)
        merged.end_nav = r.end_nav
        merged.trades += r.trades
        merged.daily_nav.update(r.daily_nav)
        merged.rejections += r.rejections
        merged.blocked += r.blocked
        merged.alerts += r.alerts
        merged.halted = merged.halted or r.halted
        merged.open_at_end = r.open_at_end
    return merged


def walk_forward(history, settings, start, end, news, train_months=24, test_months=6,
                 grid=GRID, min_trades=30) -> list[tuple[Settings, datetime, datetime]]:
    """Pick the best grid settings on each training window (expectancy R, at least `min_trades`)."""
    plan = []
    for tr_s, tr_e, te_s, te_e in windows(start, end, train_months, test_months):
        best, best_score = settings, float("-inf")
        for candidate in grid_settings(settings, grid):
            m = summarize(run(history, candidate, tr_s, tr_e, news=news), bootstrap_runs=0)
            score = m["expectancy_r"] if m["trades"] >= min_trades else float("-inf")
            if score > best_score:
                best, best_score = candidate, score
        plan.append((best, te_s, te_e))
    return plan


# --- no-skill distribution (parallel) ------------------------------------------
_WORKER: dict = {}


def _init_worker(history, settings, start, end, news, base):
    _WORKER.update(history=history, settings=settings, start=start, end=end, news=news, base=base)


def _no_skill_run(seed: int) -> float:
    w = _WORKER
    r = run(w["history"], w["settings"], w["start"], w["end"], news=w["news"],
            signal_fn=random_direction(w["base"], seed))
    return summarize(r, bootstrap_runs=0)["expectancy_r"]


def no_skill(history, settings, start, end, news, base, runs: int, processes: int | None) -> list[float]:
    if processes == 1:
        _init_worker(history, settings, start, end, news, base)
        return [_no_skill_run(seed) for seed in range(runs)]
    with ProcessPoolExecutor(processes, mp_context=multiprocessing.get_context("fork"),
                             initializer=_init_worker,
                             initargs=(history, settings, start, end, news, base)) as pool:
        return list(pool.map(_no_skill_run, range(runs), chunksize=8))


# --- Gate B and report -------------------------------------------------------------
def gate_b(report: dict) -> dict:
    wf, wf06 = report["walk_forward"], report["walk_forward_slippage_0.6"]
    ns = sorted(report["no_skill"]["expectancy_r"])
    p95 = ns[int(0.95 * (len(ns) - 1))] if ns else None
    checks = {
        "oos_expectancy_positive": wf["expectancy_r"] > 0 and wf["net_gbp"] > 0,
        "oos_trades_at_least_100": wf["trades"] >= 100,
        "oos_trades_preferred_200": wf["trades"] >= 200,
        "positive_at_0.6_pips_per_side": wf06["expectancy_r"] > 0 and wf06["net_gbp"] > 0,
        "beats_trend_baseline": wf["expectancy_r"] > report["trend_baseline"]["expectancy_r"],
        "beats_no_skill_95th_percentile": p95 is not None and wf["expectancy_r"] > p95,
        "max_drawdown_below_10pct": wf["max_drawdown"] < 0.10,
        "no_halts": wf["halted"] is None,
    }
    required = [k for k in checks if k != "oos_trades_preferred_200"]
    return {"checks": checks, "no_skill_p95_expectancy_r": p95,
            "passed": all(checks[k] for k in required),
            "manual_review": "Check robustness across periods and pairs (per_year, per_pair) "
                             "and the confidence interval before treating a pass as evidence."}


def research(history: History, settings: Settings, start: datetime, end: datetime,
             news: list[NewsEvent] | None, *, train_months=24, test_months=6, grid=GRID,
             no_skill_runs=1000, processes: int | None = None) -> dict:
    plan = walk_forward(history, settings, start, end, news, train_months, test_months, grid)
    if not plan:
        raise ValueError("period too short for one training plus one test window")
    oos_start = plan[0][1]
    wf = chain(history, plan, news)
    wf06 = chain(history, [(replace(s, slippage_pips_per_side=0.6), a, b) for s, a, b in plan], news)
    fixed = run(history, settings, oos_start, end, news=news)
    report = {
        "period": {"start": start.isoformat(), "oos_start": oos_start.isoformat(), "end": end.isoformat()},
        "walk_forward_params": [{"start": a.isoformat(), "end": b.isoformat(),
                                 **{k: getattr(s, k) for k in grid}} for s, a, b in plan],
        "walk_forward": summarize(wf),
        "walk_forward_slippage_0.6": summarize(wf06, bootstrap_runs=0),
        "fixed_params": summarize(fixed),
        "slippage_sensitivity": {str(sl): summarize(run(history, replace(settings, slippage_pips_per_side=sl),
                                                        oos_start, end, news=news), bootstrap_runs=0)
                                 for sl in SLIPPAGE},
        "time_exit_off": summarize(run(history, replace(settings, time_exit_minutes=None),
                                       oos_start, end, news=news), bootstrap_runs=0),
        "news_filter_off": summarize(run(history, settings, oos_start, end, news=None), bootstrap_runs=0),
        "trend_baseline": summarize(run(history, settings, oos_start, end, news=news,
                                        signal_fn=trend_baseline), bootstrap_runs=0),
        "no_skill": {"runs": no_skill_runs,
                     "expectancy_r": no_skill(history, settings, oos_start, end, news, fixed.signals,
                                              no_skill_runs, processes)},
    }
    report["gate_b"] = gate_b(report)
    return report


def _clean(value):
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def write_report(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "research.json", out_dir / "research.md"
    json_path.write_text(json.dumps(_clean(report), indent=2, default=str))
    wf, g = report["walk_forward"], report["gate_b"]
    lines = [
        "# Backtest research report", "",
        "Research output, not evidence of future profitability.", "",
        f"Period {report['period']['start']} to {report['period']['end']}; "
        f"out-of-sample from {report['period']['oos_start']}.", "",
        "## Walk-forward (out of sample)", "",
        f"- Trades: {wf['trades']}",
        f"- Expectancy: {wf['expectancy_r']:.3f} R (95% CI by trading day: {wf['expectancy_r_ci95']})",
        f"- Net: £{wf['net_gbp']:.2f}; profit factor: {wf['profit_factor']}",
        f"- Max drawdown: {wf['max_drawdown']:.1%}; worst losing streak: {wf['worst_losing_streak']}",
        "", "## Slippage sensitivity (fixed parameters)", "", "| Pips per side | Trades | Expectancy R |", "|---|---|---|",
        *[f"| {k} | {v['trades']} | {v['expectancy_r']:.3f} |" for k, v in report["slippage_sensitivity"].items()],
        "", "## Gate B", "", "| Check | Result |", "|---|---|",
        *[f"| {k} | {'PASS' if v else 'FAIL'} |" for k, v in g["checks"].items()],
        "", f"**Gate B automated checks: {'PASSED' if g['passed'] else 'NOT PASSED'}.** {g['manual_review']}", "",
    ]
    md_path.write_text("\n".join(lines))
    return json_path, md_path
```

`fxbot/__main__.py`:
```python
"""Command line. Plan 3 adds `backtest`; Plan 4 adds live-trading commands."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from fxbot.config import Settings
from fxbot.history import History
from fxbot.news import load_calendar
from fxbot.research import research, write_report


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fxbot")
    sub = parser.add_subparsers(dest="command", required=True)
    bt = sub.add_parser("backtest", help="walk-forward research report and Gate B check")
    bt.add_argument("--data", required=True, type=Path, help="folder of <PAIR>_<H1|M15>.csv files")
    bt.add_argument("--start", required=True, type=_date, help="YYYY-MM-DD (UTC)")
    bt.add_argument("--end", required=True, type=_date, help="YYYY-MM-DD (UTC), exclusive")
    bt.add_argument("--news", type=Path, help="historical calendar CSV (time,currency,impact)")
    bt.add_argument("--out", type=Path, default=Path("runtime/research"))
    bt.add_argument("--train-months", type=int, default=24)
    bt.add_argument("--test-months", type=int, default=6)
    bt.add_argument("--no-skill-runs", type=int, default=1000)
    bt.add_argument("--processes", type=int, default=None)
    args = parser.parse_args(argv)

    settings = Settings()
    history = History.from_dir(args.data, settings.pairs)
    news = load_calendar(args.news) if args.news else None
    if news is None:
        print("warning: no --news calendar given; the news filter is off in this report", file=sys.stderr)
    report = research(history, settings, args.start, args.end, news,
                      train_months=args.train_months, test_months=args.test_months,
                      no_skill_runs=args.no_skill_runs, processes=args.processes)
    json_path, md_path = write_report(report, args.out)
    print(f"Gate B automated checks: {'PASSED' if report['gate_b']['passed'] else 'NOT PASSED'}")
    print(f"Report: {md_path}  Data: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all OK (~25 s).

- [ ] **Step 5: Commit**

```bash
git add fxbot/research.py fxbot/__main__.py tests/test_fx_research.py
git commit -m "fxbot: add research report, Gate B checks and backtest CLI"
```

---

## Self-review (done while writing)

- **Spec coverage (§8, §10 Gate B):** same engine with a backtest broker (Task 2–4); S10 entry fills with next-open + double-slippage fallback, bid/ask stop checks, stop-first rule, gap fills at open (Task 3); slippage sensitivity per side, walk-forward, trend and no-skill baselines with identical costs, report with expectancy, profit factor, drawdown, per-pair/per-period results, rejections per filter, notional-cap hits, time exit and news filter on/off, day-block bootstrap CI and clustering (Tasks 4–6); Gate B automated checks with manual-review note (Task 6). Data fetching from OANDA and S10 lookups from the API are Plan 4.
- **Verification before publishing:** all code was run in a scratch copy (suite OK in ~25 s). 16 deliberate mutations (stop-first rule, gap fills, stop slippage, double-slippage fallback, entry price source, completed-candle visibility, short stops on ask, JPY conversion, Friday flatten, cycles while trades are open, day-block bootstrap, drawdown peak, Gate B drawdown and slippage checks, walk-forward selection, breakout-first equivalence) each make the suite fail.
- **Placeholders:** none. **Type consistency:** interfaces above match the code blocks.

## Amendments after the Plan 3 code review (implemented; code in the repo supersedes the blocks above)

Each finding was reproduced with a failing test before fixing; 9 new mutations each fail the suite.

- **Gate B compared mismatched settings (bias toward passing):** the trend baseline, the no-skill
  runs, slippage sensitivity, time-exit-off and news-off variants now all run over the same
  walk-forward windows with each window's chosen settings (`chain(..., signal_fn=...)`;
  `no_skill(history, plan, news, base, runs, processes)` uses the walk-forward run's signals).
  The fixed-default-parameter run is kept as an informational extra (`fixed_params`).
- **A single missing M15 candle halted a whole run:** executions use the next candle when the
  decision candle has no data (double slippage); with no price within 30 minutes an entry is
  rejected (`NO_PRICE`) and an exit is retried later — never a halt.
- **Drawdown was understated:** `BacktestResult.daily_nav` is replaced by `nav_marks` (NAV after
  every replayed M15 close) and `max_drawdown(marks, start_nav)` starts its peak at the start NAV.
- **Smaller fixes:** trades still open at the end of a run are closed and counted; signals are
  evaluated for every pair every cycle (complete no-skill base); AUD_USD margin 5% (not an ESMA
  major); `add_months` clamps month-end days; `History.coverage_problems()` and a CLI check refuse
  data that does not cover the period (with 35 days of warm-up); CLI rejects non-positive numbers
  and reports load errors without tracebacks; `fork` only where available, `gc.freeze()` before
  the pool; `slots=True` on `Candle` and `HistCandle` (less memory on multi-year data);
  `multi_trade_days_all_lost_if_independent` added as the clustering baseline; `research.md` now
  includes baselines, variants, per-pair and per-year tables and caveats; `run(skip_idle=False)`
  added with a test proving skipping idle cycles changes nothing.
- **Run time (for planning real runs):** about 1.5 s per simulated month per run on this machine.
  1,000 no-skill runs over 8 years out of sample is roughly 40 CPU-hours (spread over the
  available processes); walk-forward training runs serially. Use `--no-skill-runs` for quicker
  exploratory reports and the full 1,000 for the Gate B decision.

### Carried forward to Plan 4
- `OandaBroker.candles()` must request `BA` candles and derive mids as (bid + ask) / 2 per field.
- `fetch-data` command: download H1 and M15 bid/ask history for the five pairs into `<PAIR>_<GRAN>.csv`, and add an S10 lookup (cached on disk) wired into the backtest command as `--s10`.
- Document in the setup checklist that the report needs several years of data (OANDA history) and a historical high-impact calendar CSV (scheduled releases only) for the news filter.
- `fetch-data` must skip incomplete candles (`complete=false`) and print a gap report (missing M15 candles per pair per month).
