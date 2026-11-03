# FX Bot — Plan 2 of 4: Engine and Ledger

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the trading cycle that turns Plan 1's pure decisions into safe, reconciled orders: the broker interface, a scriptable simulated broker, the SQLite ledger, the 15-minute engine cycle, and the Friday flatten.

**Architecture:** `broker.py` defines one normalised interface (dataclasses + `Protocol`); `simbroker.py` implements it in memory with scriptable failures; `ledger.py` persists intents, positions, decisions, contributions and bot state in SQLite under an exclusive process lock; `engine.py` runs one cycle (reconcile → protect → manage → daily loss → health → scan → risk → submit → confirm). Plan 4 adds `OandaBroker` behind the same interface; Plan 3 drives the same engine for backtests.

**Tech Stack:** Python 3.12 standard library (`sqlite3`, `fcntl`, `dataclasses`, `typing.Protocol`), `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-02-forex-bot-design.md` (revision 2) — sections 5, 6 (daily loss), 7, 9. Builds on Plan 1: `docs/superpowers/plans/2026-11-02-fxbot-plan-1-core-logic.md` (read its "Carried forward" list).

## Global Constraints

- Everything from Plan 1's Global Constraints still applies (Python 3.12, stdlib only, flat `tests/test_fx_*.py`, UTC-aware datetimes, side `+1`/`-1`, risk numbers).
- Safety rule (spec §7): when anything is uncertain, **new entries stop; existing trades stay protected**. Every uncertain state below must block entries.
- An order intent is written to the ledger **before** the order is sent; a client ID is **never reused**, in any state (spec §5 step 8).
- Bot client IDs start with `fxb-` and have the form `fxb-<PAIR>-<YYYYMMDDTHHMM of signal candle open>-<B|S>`.
- Any unresolved intent after reconciliation **halts all new entries** until the user resumes; resume marks such intents `abandoned` (never resent).
- A bot trade without a stop is closed immediately and the bot halts.
- Untagged trades or standalone pending orders on the account pause entries.
- Daily-loss breach is latched until the next UK day, even if equity recovers. Deposits/withdrawals after the day's snapshot are excluded from P&L.
- Friday: flatten from 19:30 London, retry every call; critical alert if any bot trade remains at/after 20:00.
- Engine calls `entry_window_open(signal.candle_time + 15 min)` and `news_blocked(pair, now, ...)` (Plan 1 carry-forward).
- Open-position risk = stop risk + cost risk, recomputed from the actual fill (spec §6 rule 2).
- The old `trading/` package stays untouched (removed in Plan 4).

## Decisions made while writing this plan (review these)

- **Health failures block the whole cycle**, not one pair (spec §5 step 2). The 60-second retry for late candles belongs to the live scheduler (Plan 4); the engine reports `stale candles <pair>` and the scheduler re-runs the cycle.
- **Daily snapshot without rebuilding history:** on the first cycle of a UK day the bot records NAV as start-of-day. If the ledger shows any trade opened or closed earlier that same UK day (bot restarted mid-day with a lost or new database), the snapshot cannot be trusted and entries are blocked for the rest of the day. This is the spec's "if impossible, block entries for the day" branch; rebuilding NAV from transactions is not attempted.
- **Lost database:** bot-tagged trades found at the broker are adopted into the ledger (risk recomputed with the current cost estimate), and the day is blocked as above.
- **A ledger position missing at the broker without a CLOSE transaction** is marked closed with P&L 0 and a warning alert (should not happen; never blocks).
- **`OandaBroker.pending_orders()` (Plan 4) must return only standalone entry orders**, not the stop-loss/take-profit orders attached to trades — otherwise every bot trade would look like an untagged order.
- **Alert de-duplication** (e.g. the untagged-trade pause repeating every 15 minutes) is the alert channel's job in Plan 4, not the engine's.
- **Take-profit price precision** (rounding to the instrument's decimals) is `OandaBroker`'s job in Plan 4; `SimBroker` accepts any float.

## File Structure

| File | Responsibility |
|---|---|
| `fxbot/config.py` (modify) | Add `price_bound_pips`, `price_max_age_seconds` |
| `fxbot/broker.py` | Normalised broker types, `Broker` protocol, `BrokerError`, `BOT_TAG`, `is_bot()` |
| `fxbot/simbroker.py` | `SimBroker`: in-memory broker with scriptable failures, for tests and Plan 3 |
| `fxbot/ledger.py` | `Ledger`: SQLite persistence + process lock; `Position`; `LedgerError`, `LedgerLocked` |
| `fxbot/engine.py` | `Engine.run_cycle()`, `Engine.flatten()`, `CycleReport`, `client_id()`, `weekend_phase()` |
| `tests/fx_fixtures.py` | Synthetic markets and the `Harness` (SimBroker + Ledger + Engine) |
| `tests/test_fx_simbroker.py`, `test_fx_ledger.py`, `test_fx_engine.py`, `test_fx_flatten.py` | Tests |

---

### Task 1: Broker interface and simulated broker

**Files:**
- Modify: `fxbot/config.py` (add two fields after `max_gross_notional_multiple`)
- Modify: `tests/test_fx_config.py` (add one test)
- Create: `fxbot/broker.py`, `fxbot/simbroker.py`
- Test: `tests/test_fx_simbroker.py`

**Interfaces:**
- Consumes: `Candle` (Plan 1 `fxbot.strategy`).
- Produces:
  - `Account(nav, margin_used, last_txn_id)`, `Price(pair, time, bid, ask, tradeable, loss_factor, value_factor)`, `InstrumentInfo(margin_rate, max_units)`, `Trade(trade_id, pair, units, open_price, open_time, client_id, stop_price, take_profit_price)`, `PendingOrder(order_id, client_id)`, `Txn(txn_id, kind, time, client_id=None, trade_id=None, pair=None, units=0, price=0.0, amount=0.0)` with `kind` in `FILL|CLOSE|REJECT|TRANSFER`, `OrderResult(status, trade_id=None, fill_price=0.0, units=0, stop_price=None, reason="")` with `status` in `filled|rejected|unknown`.
  - `Broker` protocol methods: `account()`, `candles(pair, granularity, count)`, `prices(pairs)`, `instrument(pair)`, `open_trades()`, `pending_orders()`, `transactions_since(txn_id)`, `market_order(pair, units, client_id, stop_distance, price_bound)`, `set_take_profit(trade_id, price) -> bool`, `close_trade(trade_id) -> bool`. Failures raise `BrokerError`.
  - `SimBroker(nav, now)` implementing `Broker`, plus test controls: `candle_data[(pair, granularity)]`, `price_data[pair]`, `fail_next: set[str]`, `order_modes: list[str]` (`fill|reject|no_stop|timeout_filled|timeout_lost`), `tp_failures`, `close_failures`, `orders_sent`, `deposit(amount)`, `add_trade(pair, units, price, client_id, stop_price=None)`, `close_at_broker(trade_id, price)`.
  - `Settings.price_bound_pips = 1.0`, `Settings.price_max_age_seconds = 60`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fx_config.py` inside `class SettingsTests`, before `test_settings_are_immutable`:
```python
    def test_execution_safety_defaults(self):
        s = Settings()
        self.assertEqual((s.price_bound_pips, s.price_max_age_seconds), (1.0, 60))
        self.assertEqual((s.friday_flatten_start, s.friday_flatten_deadline), (time(19, 30), time(20)))
```

`tests/test_fx_simbroker.py`:
```python
import unittest
from datetime import datetime, timedelta, timezone

from fxbot.broker import Price, is_bot
from fxbot.simbroker import SimBroker

NOW = datetime(2026, 11, 3, 9, 15, tzinfo=timezone.utc)


def broker():
    sim = SimBroker(1000.0, NOW)
    sim.price_data["EUR_USD"] = Price("EUR_USD", NOW - timedelta(seconds=5), 1.10000, 1.10002,
                                      True, 1 / 1.3, 1 / 1.3)
    return sim


class SimBrokerTests(unittest.TestCase):
    def test_fill_attaches_stop_at_distance_from_fill(self):
        sim = broker()
        r = sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.1001)
        self.assertEqual((r.status, r.fill_price, r.units), ("filled", 1.10002, 1000))
        self.assertAlmostEqual(r.stop_price, 1.10002 - 0.0015, places=10)
        self.assertEqual(sim.txns[-1].kind, "FILL")

    def test_sell_fills_at_bid_with_stop_above(self):
        r = broker().market_order("EUR_USD", -1000, "fxb-s", 0.0015, 1.0999)
        self.assertEqual(r.fill_price, 1.10000)
        self.assertAlmostEqual(r.stop_price, 1.1015, places=10)

    def test_price_bound_rejects_excess_slippage(self):
        r = broker().market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.10001)
        self.assertEqual((r.status, r.reason), ("rejected", "BOUNDS_VIOLATION"))

    def test_duplicate_open_client_id_rejected(self):
        sim = broker()
        sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2)
        r = sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2)
        self.assertEqual(r.reason, "CLIENT_ORDER_ID_ALREADY_EXISTS")

    def test_scripted_timeout_and_failures(self):
        sim = broker()
        sim.order_modes = ["timeout_filled", "timeout_lost"]
        self.assertEqual(sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2).status, "unknown")
        self.assertEqual(len(sim.trades), 1)                     # it did fill
        self.assertEqual(sim.market_order("EUR_USD", 1000, "fxb-b", 0.0015, 1.2).status, "unknown")
        self.assertEqual(len(sim.trades), 1)                     # this one never arrived
        sim.fail_next = {"account"}
        with self.assertRaises(Exception):
            sim.account()
        sim.account()                                            # fails only once

    def test_close_records_pl_and_nav(self):
        sim = broker()
        r = sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2)
        sim.close_at_broker(r.trade_id, 1.10102)
        self.assertAlmostEqual(sim.txns[-1].amount, 1000 * 0.001 / 1.3, places=9)
        self.assertAlmostEqual(sim.nav, 1000 + 1000 * 0.001 / 1.3, places=9)

    def test_is_bot(self):
        self.assertTrue(is_bot("fxb-EUR_USD-20261103T0900-B"))
        self.assertFalse(is_bot(None))
        self.assertFalse(is_bot("manual"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_*.py" -v`
Expected: ERROR in `test_fx_simbroker` (`No module named 'fxbot.broker'`) and an `AttributeError` for `price_bound_pips` in `test_execution_safety_defaults`.

- [ ] **Step 3: Write the implementation**

In `fxbot/config.py`, directly after the line `    max_gross_notional_multiple: float = 10.0`, insert:
```python
    # execution safety
    price_bound_pips: float = 1.0        # max entry slippage accepted by the broker
    price_max_age_seconds: int = 60      # older prices count as stale
```

`fxbot/broker.py`:
```python
"""Broker interface shared by OandaBroker (Plan 4) and SimBroker (spec §4).

All broker-specific formats are normalised into these types, so the engine never sees
OANDA JSON. Units are signed: positive = long, negative = short.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fxbot.strategy import Candle

BOT_TAG = "fxb-"   # client IDs created by this bot start with this prefix


class BrokerError(Exception):
    """A broker request failed or returned something unusable."""


@dataclass(frozen=True)
class Account:
    nav: float
    margin_used: float
    last_txn_id: int


@dataclass(frozen=True)
class Price:
    pair: str
    time: datetime
    bid: float
    ask: float
    tradeable: bool
    loss_factor: float    # homeConversions.accountLoss: GBP per unit of quote currency
    value_factor: float   # homeConversions.positionValue


@dataclass(frozen=True)
class InstrumentInfo:
    margin_rate: float
    max_units: int


@dataclass(frozen=True)
class Trade:
    trade_id: str
    pair: str
    units: int
    open_price: float
    open_time: datetime
    client_id: str | None
    stop_price: float | None
    take_profit_price: float | None


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    client_id: str | None


@dataclass(frozen=True)
class Txn:
    """Normalised transaction. kind is one of FILL, CLOSE, REJECT, TRANSFER."""
    txn_id: int
    kind: str
    time: datetime
    client_id: str | None = None
    trade_id: str | None = None
    pair: str | None = None
    units: int = 0
    price: float = 0.0
    amount: float = 0.0   # realised P&L (CLOSE) or funds moved (TRANSFER), GBP


@dataclass(frozen=True)
class OrderResult:
    """status: 'filled', 'rejected', or 'unknown' (request outcome not known, e.g. timeout)."""
    status: str
    trade_id: str | None = None
    fill_price: float = 0.0
    units: int = 0
    stop_price: float | None = None
    reason: str = ""


class Broker(Protocol):
    def account(self) -> Account: ...
    def candles(self, pair: str, granularity: str, count: int) -> list[Candle]: ...
    def prices(self, pairs: tuple[str, ...]) -> dict[str, Price]: ...
    def instrument(self, pair: str) -> InstrumentInfo: ...
    def open_trades(self) -> list[Trade]: ...
    def pending_orders(self) -> list[PendingOrder]: ...
    def transactions_since(self, txn_id: int) -> list[Txn]: ...
    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult: ...
    def set_take_profit(self, trade_id: str, price: float) -> bool: ...
    def close_trade(self, trade_id: str) -> bool: ...


def is_bot(client_id: str | None) -> bool:
    return client_id is not None and client_id.startswith(BOT_TAG)
```

`fxbot/simbroker.py`:
```python
"""In-memory broker for tests (and the Plan 3 backtest). Scriptable failures included."""
from datetime import datetime

from fxbot.broker import (Account, BrokerError, InstrumentInfo, OrderResult, PendingOrder,
                          Price, Trade, Txn)
from fxbot.strategy import Candle


class SimBroker:
    def __init__(self, nav: float, now: datetime):
        self.nav = nav
        self.now = now
        self.candle_data: dict[tuple[str, str], list[Candle]] = {}
        self.price_data: dict[str, Price] = {}
        self.info = InstrumentInfo(margin_rate=1 / 30, max_units=100_000_000)
        self.trades: dict[str, Trade] = {}
        self.pending: list[PendingOrder] = []
        self.txns: list[Txn] = []
        # scripted behaviour
        self.fail_next: set[str] = set()     # method names that raise BrokerError once
        self.order_modes: list[str] = []     # fill | reject | no_stop | timeout_filled | timeout_lost
        self.tp_failures = 0                 # next N set_take_profit calls fail
        self.close_failures = 0              # next N close_trade calls fail
        self.orders_sent: list[str] = []     # client IDs received, in order
        self._next_trade = 1

    # --- helpers for tests -------------------------------------------------
    def _check(self, name: str) -> None:
        if name in self.fail_next:
            self.fail_next.discard(name)
            raise BrokerError(f"simulated {name} failure")

    def _txn(self, kind: str, **fields) -> Txn:
        txn = Txn(len(self.txns) + 1, kind, self.now, **fields)
        self.txns.append(txn)
        return txn

    def deposit(self, amount: float) -> None:
        self.nav += amount
        self._txn("TRANSFER", amount=amount)

    def add_trade(self, pair: str, units: int, price: float, client_id: str | None,
                  stop_price: float | None = None) -> Trade:
        trade = Trade(str(self._next_trade), pair, units, price, self.now, client_id,
                      stop_price, None)
        self._next_trade += 1
        self.trades[trade.trade_id] = trade
        return trade

    def close_at_broker(self, trade_id: str, price: float) -> None:
        """Simulate the broker closing a trade (stop or target hit)."""
        trade = self.trades.pop(trade_id)
        pl = trade.units * (price - trade.open_price) * self.price_data[trade.pair].loss_factor
        self.nav += pl
        self._txn("CLOSE", trade_id=trade_id, pair=trade.pair, units=-trade.units,
                  price=price, amount=pl)

    # --- Broker interface --------------------------------------------------
    def account(self) -> Account:
        self._check("account")
        margin = sum(abs(t.units) * t.open_price * self.price_data[t.pair].value_factor
                     * self.info.margin_rate for t in self.trades.values())
        return Account(self.nav, margin, len(self.txns))

    def candles(self, pair: str, granularity: str, count: int) -> list[Candle]:
        self._check("candles")
        return self.candle_data[(pair, granularity)][-count:]

    def prices(self, pairs: tuple[str, ...]) -> dict[str, Price]:
        self._check("prices")
        return {p: self.price_data[p] for p in pairs}

    def instrument(self, pair: str) -> InstrumentInfo:
        self._check("instrument")
        return self.info

    def open_trades(self) -> list[Trade]:
        self._check("open_trades")
        return list(self.trades.values())

    def pending_orders(self) -> list[PendingOrder]:
        self._check("pending_orders")
        return list(self.pending)

    def transactions_since(self, txn_id: int) -> list[Txn]:
        self._check("transactions_since")
        return [t for t in self.txns if t.txn_id > txn_id]

    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult:
        self._check("market_order")
        self.orders_sent.append(client_id)
        mode = self.order_modes.pop(0) if self.order_modes else "fill"
        if mode == "timeout_lost":
            return OrderResult("unknown", reason="timeout")
        if any(t.client_id == client_id for t in self.trades.values()):
            self._txn("REJECT", client_id=client_id, pair=pair)
            return OrderResult("rejected", reason="CLIENT_ORDER_ID_ALREADY_EXISTS")
        p = self.price_data[pair]
        fill = p.ask if units > 0 else p.bid
        if mode == "reject" or (units > 0 and fill > price_bound) or (units < 0 and fill < price_bound):
            self._txn("REJECT", client_id=client_id, pair=pair)
            return OrderResult("rejected", reason="BOUNDS_VIOLATION" if mode != "reject" else "REJECTED")
        stop = None if mode == "no_stop" else (fill - stop_distance if units > 0 else fill + stop_distance)
        trade = self.add_trade(pair, units, fill, client_id, stop)
        self._txn("FILL", client_id=client_id, trade_id=trade.trade_id, pair=pair,
                  units=units, price=fill)
        if mode == "timeout_filled":
            return OrderResult("unknown", reason="timeout")
        return OrderResult("filled", trade.trade_id, fill, units, stop)

    def set_take_profit(self, trade_id: str, price: float) -> bool:
        self._check("set_take_profit")
        if self.tp_failures:
            self.tp_failures -= 1
            return False
        t = self.trades[trade_id]
        self.trades[trade_id] = Trade(t.trade_id, t.pair, t.units, t.open_price, t.open_time,
                                      t.client_id, t.stop_price, price)
        return True

    def close_trade(self, trade_id: str) -> bool:
        self._check("close_trade")
        if self.close_failures:
            self.close_failures -= 1
            return False
        p = self.price_data[self.trades[trade_id].pair]
        self.close_at_broker(trade_id, p.bid if self.trades[trade_id].units > 0 else p.ask)
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_fx_*.py" -v`
Expected: all OK (Plan 1's 62 tests + 1 new config test + 7 SimBroker tests = 70).

- [ ] **Step 5: Commit**

```bash
git add fxbot/config.py fxbot/broker.py fxbot/simbroker.py tests/test_fx_config.py tests/test_fx_simbroker.py
git commit -m "fxbot: add broker interface and simulated broker"
```

---

### Task 2: Ledger

**Files:**
- Create: `fxbot/ledger.py`
- Test: `tests/test_fx_ledger.py`

**Interfaces:**
- Consumes: `uk_trading_day` (Plan 1 `fxbot.risk`).
- Produces: `Ledger(path)` (raises `LedgerLocked` if another process holds `<path>.lock`, `LedgerError` if the file is corrupt) with: `close()`; `get(key, default)`, `set(key, value|None)`; `last_txn_id` (property), `set_last_txn_id(n)`; `halt(reason)` (keeps the first reason), `halt_reason()`, `pause(reason)`, `pause_reason()`, `resume() -> list[str]` (abandoned client IDs); `has_client_id(cid)`, `create_intent(cid, pair, side, candle_time, now)` (raises `LedgerError` on any reuse), `mark_filled(cid, trade_id, units, fill_price, stop_price, cost, stop_risk_gbp, cost_risk_gbp, notional_gbp, opened_at)`, `mark_failed(cid, note)`, `mark_closed(trade_id, closed_at, realized_pl) -> bool`, `set_take_profit(cid, price)`, `position(cid) -> Position|None`, `all_positions()`, `submitting() -> list[str]`, `open_positions()`, `entries_today(day) -> dict[str, int]`; `record(at, pair, outcome, reason)`, `decisions()`; `add_contribution(txn_id, at, amount)` (idempotent), `contributions_since(at) -> (deposits, withdrawals)`; `day_snapshot() -> (date, start_nav, at)|None`, `set_day_snapshot(day, nav, at)`, `latch_daily_loss(day)`, `daily_loss_latched(day)`.
- `Position` frozen dataclass fields: `client_id, pair, side, candle_time, status, trade_id, units, fill_price, stop_price, tp_price, cost, risk_gbp, stop_risk_gbp, cost_risk_gbp, notional_gbp, opened_at, closed_at, realized_pl`. `status` ∈ `submitting|filled|closed|failed|abandoned`.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_ledger.py`:
```python
import tempfile
import unittest
from datetime import date, datetime, timezone

from fxbot.ledger import Ledger, LedgerError, LedgerLocked

UTC = timezone.utc
T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = f"{tmp.name}/ledger.db"
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.path)

    def fill(self, cid="fxb-a", pair="EUR_USD", at=T0):
        self.ledger.create_intent(cid, pair, 1, T0, at)
        self.ledger.mark_filled(cid, "T" + cid, 1000, 1.1, 1.099, 0.0001, 1.0, 0.1, 846.0, at)

    def test_second_process_is_refused(self):
        with self.assertRaises(LedgerLocked):
            Ledger(self.path)

    def test_corrupt_file_is_refused(self):
        bad = self.path + ".bad"
        with open(bad, "wb") as f:
            f.write(b"this is not a database" * 100)
        with self.assertRaises(LedgerError):
            Ledger(bad)

    def test_client_id_can_never_be_reused(self):
        self.fill()
        self.ledger.mark_closed("Tfxb-a", T0, -2.5)
        with self.assertRaises(LedgerError):
            self.ledger.create_intent("fxb-a", "EUR_USD", 1, T0, T0)

    def test_fill_records_split_risk(self):
        self.fill()
        p = self.ledger.position("fxb-a")
        self.assertEqual((p.status, p.units, p.trade_id), ("filled", 1000, "Tfxb-a"))
        self.assertAlmostEqual(p.risk_gbp, 1.1)
        self.assertEqual((p.stop_risk_gbp, p.cost_risk_gbp), (1.0, 0.1))

    def test_resume_abandons_unresolved_intents(self):
        self.ledger.create_intent("fxb-x", "EUR_USD", 1, T0, T0)
        self.ledger.halt("halted: unresolved order fxb-x")
        self.ledger.halt("halted: something later")          # first reason is kept
        self.assertEqual(self.ledger.halt_reason(), "halted: unresolved order fxb-x")
        self.assertEqual(self.ledger.resume(), ["fxb-x"])
        self.assertIsNone(self.ledger.halt_reason())
        self.assertEqual(self.ledger.position("fxb-x").status, "abandoned")
        self.assertTrue(self.ledger.has_client_id("fxb-x"))

    def test_entries_counted_by_uk_day(self):
        self.fill("fxb-1", at=datetime(2026, 7, 7, 22, 30, tzinfo=UTC))   # 23:30 BST, 7 July
        self.fill("fxb-2", at=datetime(2026, 7, 7, 23, 30, tzinfo=UTC))   # 00:30 BST, 8 July
        self.assertEqual(self.ledger.entries_today(date(2026, 7, 8)), {"EUR_USD": 1})

    def test_contributions_after_snapshot_only(self):
        self.ledger.add_contribution(1, datetime(2026, 11, 3, 8, 0, tzinfo=UTC), 100.0)
        self.ledger.add_contribution(2, datetime(2026, 11, 3, 10, 0, tzinfo=UTC), 300.0)
        self.ledger.add_contribution(3, datetime(2026, 11, 3, 11, 0, tzinfo=UTC), -50.0)
        self.ledger.add_contribution(2, datetime(2026, 11, 3, 10, 0, tzinfo=UTC), 300.0)  # replay
        self.assertEqual(self.ledger.contributions_since(T0), (300.0, 50.0))

    def test_state_survives_restart(self):
        self.ledger.set_last_txn_id(42)
        self.ledger.set_day_snapshot(date(2026, 11, 3), 1000.0, T0)
        self.ledger.latch_daily_loss(date(2026, 11, 3))
        self.ledger.pause("paused by user")
        self.reopen()
        self.assertEqual(self.ledger.last_txn_id, 42)
        self.assertEqual(self.ledger.day_snapshot(), (date(2026, 11, 3), 1000.0, T0))
        self.assertTrue(self.ledger.daily_loss_latched(date(2026, 11, 3)))
        self.assertFalse(self.ledger.daily_loss_latched(date(2026, 11, 4)))
        self.assertEqual(self.ledger.pause_reason(), "paused by user")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_ledger.py" -v`
Expected: ERROR — `No module named 'fxbot.ledger'`

- [ ] **Step 3: Write the implementation**

`fxbot/ledger.py`:
```python
"""SQLite ledger: order intents and positions, decisions, contributions, bot state (spec §4-§7).

One process at a time: the constructor takes an exclusive lock on `<path>.lock` and raises
LedgerLocked if another process holds it. Every write commits immediately.
"""
import fcntl
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from fxbot.risk import uk_trading_day

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    client_id TEXT PRIMARY KEY, pair TEXT NOT NULL, side INTEGER NOT NULL,
    candle_time TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
    trade_id TEXT, units INTEGER, fill_price REAL, stop_price REAL, tp_price REAL,
    cost REAL, risk_gbp REAL, stop_risk_gbp REAL, cost_risk_gbp REAL, notional_gbp REAL,
    opened_at TEXT, closed_at TEXT, realized_pl REAL, note TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, pair TEXT, outcome TEXT NOT NULL,
    reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contributions (
    txn_id INTEGER PRIMARY KEY, at TEXT NOT NULL, amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

OPEN_STATUSES = ("filled",)


class LedgerError(Exception):
    """The ledger cannot be used safely (corrupt, unreadable, or invalid operation)."""


class LedgerLocked(LedgerError):
    """Another process already holds this ledger."""


@dataclass(frozen=True)
class Position:
    client_id: str
    pair: str
    side: int
    candle_time: datetime
    status: str            # submitting | filled | closed | failed | abandoned
    trade_id: str | None
    units: int | None
    fill_price: float | None
    stop_price: float | None
    tp_price: float | None
    cost: float | None
    risk_gbp: float | None
    stop_risk_gbp: float | None
    cost_risk_gbp: float | None
    notional_gbp: float | None
    opened_at: datetime | None
    closed_at: datetime | None
    realized_pl: float | None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Ledger:
    def __init__(self, path: str):
        self._lock = open(path + ".lock", "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise LedgerLocked(f"ledger {path} is in use by another process") from None
        try:
            self._db = sqlite3.connect(path, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LedgerError("ledger failed integrity check")
            self._db.executescript(SCHEMA)
        except sqlite3.DatabaseError as exc:
            self.close()
            raise LedgerError(f"ledger unreadable: {exc}") from exc

    def close(self) -> None:
        if getattr(self, "_db", None) is not None:
            self._db.close()
            self._db = None
        self._lock.close()

    # --- key/value state ---------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self._db.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            self._db.execute("INSERT OR REPLACE INTO state(key, value) VALUES(?, ?)", (key, value))

    @property
    def last_txn_id(self) -> int:
        return int(self.get("last_txn_id", "0"))

    def set_last_txn_id(self, txn_id: int) -> None:
        self.set("last_txn_id", str(txn_id))

    # --- halt / pause ------------------------------------------------------
    def halt(self, reason: str) -> None:
        if self.get("halt_reason") is None:
            self.set("halt_reason", reason)

    def halt_reason(self) -> str | None:
        return self.get("halt_reason")

    def pause(self, reason: str) -> None:
        self.set("pause_reason", reason)

    def pause_reason(self) -> str | None:
        return self.get("pause_reason")

    def resume(self) -> list[str]:
        """User confirmation: clear halt and pause; unresolved intents become 'abandoned'.

        Abandoned client IDs are never reused, so an order cannot be resent.
        """
        abandoned = self.submitting()
        self._db.execute("UPDATE positions SET status='abandoned' WHERE status='submitting'")
        self.set("halt_reason", None)
        self.set("pause_reason", None)
        return abandoned

    # --- positions ---------------------------------------------------------
    def has_client_id(self, client_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM positions WHERE client_id=?",
                                (client_id,)).fetchone() is not None

    def create_intent(self, client_id: str, pair: str, side: int, candle_time: datetime,
                      now: datetime) -> None:
        """Durably record an order BEFORE it is sent. Refuses any client ID ever used."""
        if self.has_client_id(client_id):
            raise LedgerError(f"client ID {client_id} already used")
        self._db.execute(
            "INSERT INTO positions(client_id, pair, side, candle_time, status, created_at)"
            " VALUES(?, ?, ?, ?, 'submitting', ?)",
            (client_id, pair, side, candle_time.isoformat(), now.isoformat()))

    def mark_filled(self, client_id: str, trade_id: str, units: int, fill_price: float,
                    stop_price: float | None, cost: float, stop_risk_gbp: float,
                    cost_risk_gbp: float, notional_gbp: float, opened_at: datetime) -> None:
        self._db.execute(
            "UPDATE positions SET status='filled', trade_id=?, units=?, fill_price=?, stop_price=?,"
            " cost=?, risk_gbp=?, stop_risk_gbp=?, cost_risk_gbp=?, notional_gbp=?, opened_at=?"
            " WHERE client_id=?",
            (trade_id, units, fill_price, stop_price, cost, stop_risk_gbp + cost_risk_gbp,
             stop_risk_gbp, cost_risk_gbp, notional_gbp, opened_at.isoformat(), client_id))

    def mark_failed(self, client_id: str, note: str) -> None:
        self._db.execute("UPDATE positions SET status='failed', note=? WHERE client_id=?",
                         (note, client_id))

    def mark_closed(self, trade_id: str, closed_at: datetime, realized_pl: float) -> bool:
        cur = self._db.execute(
            "UPDATE positions SET status='closed', closed_at=?, realized_pl=?"
            " WHERE trade_id=? AND status='filled'",
            (closed_at.isoformat(), realized_pl, trade_id))
        return cur.rowcount == 1

    def set_take_profit(self, client_id: str, price: float) -> None:
        self._db.execute("UPDATE positions SET tp_price=? WHERE client_id=?", (price, client_id))

    def _positions(self, where: str, args: tuple = ()) -> list[Position]:
        rows = self._db.execute(
            "SELECT client_id, pair, side, candle_time, status, trade_id, units, fill_price,"
            " stop_price, tp_price, cost, risk_gbp, stop_risk_gbp, cost_risk_gbp, notional_gbp,"
            f" opened_at, closed_at, realized_pl FROM positions WHERE {where} ORDER BY created_at",
            args).fetchall()
        return [Position(r[0], r[1], r[2], _dt(r[3]), r[4], r[5], r[6], r[7], r[8], r[9], r[10],
                         r[11], r[12], r[13], r[14], _dt(r[15]), _dt(r[16]), r[17]) for r in rows]

    def position(self, client_id: str) -> Position | None:
        found = self._positions("client_id=?", (client_id,))
        return found[0] if found else None

    def all_positions(self) -> list[Position]:
        return self._positions("1=1")

    def submitting(self) -> list[str]:
        return [p.client_id for p in self._positions("status='submitting'")]

    def open_positions(self) -> list[Position]:
        return self._positions("status='filled'")

    def entries_today(self, day: date) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self._positions("status IN ('filled', 'closed')"):
            if uk_trading_day(p.opened_at) == day:
                counts[p.pair] = counts.get(p.pair, 0) + 1
        return counts

    # --- decisions ---------------------------------------------------------
    def record(self, at: datetime, pair: str | None, outcome: str, reason: str) -> None:
        self._db.execute("INSERT INTO decisions(at, pair, outcome, reason) VALUES(?, ?, ?, ?)",
                         (at.isoformat(), pair, outcome, reason))

    def decisions(self) -> list[tuple[str | None, str, str]]:
        return [(r[0], r[1], r[2]) for r in self._db.execute(
            "SELECT pair, outcome, reason FROM decisions ORDER BY id").fetchall()]

    # --- contributions -----------------------------------------------------
    def add_contribution(self, txn_id: int, at: datetime, amount: float) -> None:
        self._db.execute("INSERT OR IGNORE INTO contributions(txn_id, at, amount) VALUES(?, ?, ?)",
                         (txn_id, at.isoformat(), amount))

    def contributions_since(self, since: datetime) -> tuple[float, float]:
        """(deposits, withdrawals) strictly after `since`, both as positive numbers."""
        deposits = withdrawals = 0.0
        for at, amount in self._db.execute("SELECT at, amount FROM contributions").fetchall():
            if datetime.fromisoformat(at) > since:
                if amount >= 0:
                    deposits += amount
                else:
                    withdrawals -= amount
        return deposits, withdrawals

    # --- daily snapshot and latch -----------------------------------------
    def day_snapshot(self) -> tuple[date, float, datetime] | None:
        day = self.get("day")
        if day is None:
            return None
        return date.fromisoformat(day), float(self.get("day_start_nav")), _dt(self.get("day_start_at"))

    def set_day_snapshot(self, day: date, start_nav: float, at: datetime) -> None:
        self.set("day", day.isoformat())
        self.set("day_start_nav", repr(start_nav))
        self.set("day_start_at", at.isoformat())

    def latch_daily_loss(self, day: date) -> None:
        self.set("daily_loss_latched", day.isoformat())

    def daily_loss_latched(self, day: date) -> bool:
        return self.get("daily_loss_latched") == day.isoformat()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest discover -s tests -p "test_fx_ledger.py" -v`
Expected: 8 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/ledger.py tests/test_fx_ledger.py
git commit -m "fxbot: add SQLite ledger with process lock"
```

---

### Task 3: Engine cycle

**Files:**
- Create: `fxbot/engine.py`, `tests/fx_fixtures.py`
- Test: `tests/test_fx_engine.py`

**Interfaces:**
- Consumes: everything from Plan 1, `broker.py`, `simbroker.py`, `ledger.py`.
- Produces:
  - `Engine(broker, ledger, settings, news, alert)` where `news(now) -> list[NewsEvent] | None` (`None` = feed unavailable) and `alert(level, text)` with `level` ∈ `warning|critical`.
  - `Engine.run_cycle(now) -> CycleReport`; `CycleReport(at, blocked: str|None, filled: list[cid], rejected: list[(pair, reason)], exits: list[trade_id])`.
  - `client_id(pair, side, candle_time) -> str`.
  - Block reasons (exact strings, shown to the user): ledger halt/pause reason; `"broker unavailable"`; `"paused: untagged trade or order on the account"`; `"halted: unresolved order <cid>"`; `"halted: trade <id> had no stop-loss"`; `"no reliable daily snapshot today"`; `"daily loss limit reached"`; `"news feed unavailable"`; `"stale candles <pair>"`; `"stale or untradeable price <pair>"`. Per-pair rejections: `"outside session"`, `"cost NN% of stop"`, `"news blackout"`, `"duplicate signal"`, `"broker rejected: <reason>"`, `"unresolved order <cid>"`, plus Plan 1 risk reasons.

Cycle order (spec §5): fetch account/trades/pending/prices → reconcile transactions (FILL/CLOSE/REJECT/TRANSFER), adopt lost bot trades → untagged check → unresolved intents halt → close bot trades without stop and halt → take-profit retries and 120-minute time exits → daily snapshot/latch → news feed check → if blocked, record and stop → health (candle times, price freshness/tradeable/factors) → signals → session, cost, news filters → sort by cost/stop → risk approval with in-cycle reservation → intent → market order → confirm stop → set take-profit from actual fill.

- [ ] **Step 1: Write the test fixtures and failing tests**

`tests/fx_fixtures.py`:
```python
"""Shared synthetic market data for fxbot engine tests (not a test module itself)."""
import tempfile
from datetime import datetime, timedelta, timezone

from fxbot.broker import Price
from fxbot.config import PAIRS, Settings
from fxbot.engine import Engine
from fxbot.ledger import Ledger
from fxbot.simbroker import SimBroker
from fxbot.strategy import Candle

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)   # Tuesday; last M15 opens 09:00, last H1 08:00
SCALE = {"EUR_USD": 1.0, "GBP_USD": 1.2, "USD_JPY": 100.0, "AUD_USD": 0.6, "USD_CAD": 1.3}


def last_closed(now: datetime) -> tuple[datetime, datetime]:
    m15 = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0) - timedelta(minutes=15)
    h1 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return h1, m15


def set_market(sim: SimBroker, pair: str, now: datetime = NOW, trend: int = +1,
               breakout: int = 0, spread_pips: float = 0.2) -> None:
    """600 trending H1 candles and 100 flat M15 candles; optional breakout on the last M15."""
    k = SCALE[pair]
    h1_end, m15_end = last_closed(now)
    h1 = []
    for i in range(600):
        c = k * (1.0 + i * 0.0001) if trend > 0 else k * (1.2 - i * 0.0001)
        h1.append(Candle(h1_end - timedelta(hours=599 - i), c, c + 0.0002 * k, c - 0.0002 * k, c))
    mid = 1.1 * k
    m15 = [Candle(m15_end - timedelta(minutes=15 * (99 - i)), mid, mid + 0.0005 * k, mid - 0.0005 * k, mid)
           for i in range(100)]
    if breakout:
        close = mid + breakout * 0.0010 * k
        high = max(close, mid) + 0.0002 * k
        low = min(close, mid) - 0.0002 * k
        m15[-1] = Candle(m15_end, mid, high, low, close)
    sim.candle_data[(pair, "H1")] = h1
    sim.candle_data[(pair, "M15")] = m15
    half = spread_pips * (0.01 if pair == "USD_JPY" else 0.0001) / 2
    last = m15[-1].close
    factor = 1 / 1.3 / (last if pair.startswith("USD_") else 1.0)   # GBP per unit of quote currency
    sim.price_data[pair] = Price(pair, now - timedelta(seconds=5), last - half, last + half, True,
                                 factor, factor)


class Harness:
    """A SimBroker + Ledger + Engine wired together, with alerts and news captured."""

    def __init__(self, testcase, nav: float = 1000.0, now: datetime = NOW, settings: Settings = Settings()):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.sim = SimBroker(nav, now)
        for pair in PAIRS:
            set_market(self.sim, pair, now)
        self.ledger = Ledger(f"{self.dir}/ledger.db")
        testcase.addCleanup(self.ledger.close)
        self.alerts: list[tuple[str, str]] = []
        self.news_events: list | None = []
        self.settings = settings
        self.engine = Engine(self.sim, self.ledger, settings, lambda now: self.news_events,
                             lambda level, text: self.alerts.append((level, text)))

    def cycle(self, now: datetime = NOW):
        self.sim.now = now
        return self.engine.run_cycle(now)
```

`tests/test_fx_engine.py`:
```python
import unittest
from datetime import datetime, timedelta

from fx_fixtures import NOW, UTC, Harness, set_market
from fxbot.broker import Price
from fxbot.config import PAIRS
from fxbot.engine import client_id
from fxbot.instruments import usd_direction
from fxbot.ledger import Ledger
from fxbot.news import NewsEvent
from fxbot.risk import uk_trading_day

LATER = NOW + timedelta(seconds=30)        # same candle, second look
SIGNAL_CANDLE = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
EUR_ID = client_id("EUR_USD", +1, SIGNAL_CANDLE)


class EntryTests(unittest.TestCase):
    def test_enters_on_signal_with_stop_and_target(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        rep = h.cycle()
        self.assertEqual(rep.filled, [EUR_ID])
        self.assertEqual(EUR_ID, "fxb-EUR_USD-20261103T0900-B")
        p = h.ledger.position(EUR_ID)
        trade = h.sim.trades[p.trade_id]
        stop_dist = p.fill_price - trade.stop_price
        self.assertGreater(stop_dist, 0)
        self.assertAlmostEqual(trade.take_profit_price, p.fill_price + 2 * stop_dist, places=10)
        self.assertAlmostEqual(p.tp_price, trade.take_profit_price, places=10)
        loss = h.sim.price_data["EUR_USD"].loss_factor
        self.assertAlmostEqual(p.risk_gbp, p.units * (stop_dist + p.cost) * loss, places=9)
        self.assertLessEqual(p.risk_gbp, 2.5)

    def test_no_signal_no_order(self):
        h = Harness(self)
        rep = h.cycle()
        self.assertEqual((rep.filled, rep.blocked, h.sim.orders_sent), ([], None, []))

    def test_same_signal_is_never_sent_twice(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        h.sim.close_at_broker(h.ledger.position(EUR_ID).trade_id, 1.1)   # stopped out quickly
        rep = h.cycle(LATER)
        self.assertIn(("EUR_USD", "duplicate signal"), rep.rejected)
        self.assertEqual(h.sim.orders_sent, [EUR_ID])

    def test_broker_rejection_marks_intent_failed(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["reject"]
        rep = h.cycle()
        self.assertEqual(h.ledger.position(EUR_ID).status, "failed")
        self.assertIn(("EUR_USD", "broker rejected: REJECTED"), rep.rejected)

    def test_outside_session_rejected(self):
        early = datetime(2026, 11, 3, 6, 30, 10, tzinfo=UTC)   # signal candle closes 06:30 London
        h = Harness(self, now=early)
        set_market(h.sim, "EUR_USD", now=early, breakout=+1)
        rep = h.cycle(early)
        self.assertIn(("EUR_USD", "outside session"), rep.rejected)
        self.assertEqual(h.sim.orders_sent, [])

    def test_wide_spread_rejected_by_cost_filter(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1, spread_pips=3.0)
        rep = h.cycle()
        self.assertEqual(rep.rejected[0][0], "EUR_USD")
        self.assertTrue(rep.rejected[0][1].startswith("cost "))

    def test_news_blackout_rejects_pair(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.news_events = [NewsEvent(NOW + timedelta(minutes=10), "USD", "High")]
        self.assertIn(("EUR_USD", "news blackout"), h.cycle().rejected)


class ConcurrencyTests(unittest.TestCase):
    def test_all_five_pairs_signal_at_once(self):
        h = Harness(self)
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        rep = h.cycle()
        self.assertEqual(len(rep.filled), 4)
        self.assertEqual(len(rep.rejected), 1)
        self.assertIn(rep.rejected[0][1], ("max positions", "max USD direction risk"))
        positions = h.ledger.open_positions()
        self.assertLessEqual(sum(p.risk_gbp for p in positions), 10.0)          # 1% of NAV
        for direction in (+1, -1):
            same = sum(p.risk_gbp for p in positions if usd_direction(p.pair, p.side) == direction)
            self.assertLessEqual(same, 5.0)                                     # 0.5% of NAV

    def test_timeout_blocks_remaining_candidates_then_reconciles(self):
        h = Harness(self)
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        h.sim.order_modes = ["timeout_filled"]
        rep = h.cycle()
        self.assertEqual(len(h.sim.orders_sent), 1)
        self.assertEqual(rep.filled, [])
        self.assertTrue(all(r.startswith("unresolved order") for _, r in rep.rejected))
        first = h.sim.orders_sent[0]
        h.cycle(LATER)                                   # transaction history resolves it
        self.assertEqual(h.ledger.position(first).status, "filled")
        self.assertIsNone(h.ledger.halt_reason())

    def test_lost_order_halts_and_is_never_resent(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["timeout_lost"]
        h.cycle()
        rep = h.cycle(LATER)
        self.assertEqual(rep.blocked, f"halted: unresolved order {EUR_ID}")
        self.assertIn("critical", [level for level, _ in h.alerts])
        self.assertEqual(h.ledger.resume(), [EUR_ID])
        rep = h.cycle(LATER + timedelta(seconds=5))
        self.assertIn(("EUR_USD", "duplicate signal"), rep.rejected)
        self.assertEqual(h.sim.orders_sent, [EUR_ID])

    def test_order_request_exception_is_treated_as_unknown(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.fail_next = {"market_order"}
        h.cycle()
        self.assertEqual(h.ledger.position(EUR_ID).status, "submitting")
        self.assertTrue(h.cycle(LATER).blocked.startswith("halted: unresolved order"))


class ProtectionTests(unittest.TestCase):
    def test_fill_without_stop_is_closed_and_halts(self):
        h = Harness(self)
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        h.sim.order_modes = ["no_stop"]
        h.cycle()
        self.assertEqual(h.sim.trades, {})                 # unprotected trade closed
        self.assertEqual(len(h.sim.orders_sent), 1)        # nothing else entered
        self.assertTrue(h.ledger.halt_reason().startswith("halted: trade"))
        self.assertEqual(h.alerts[-1][0], "critical")

    def test_existing_trade_losing_its_stop_is_closed_and_halts(self):
        h = Harness(self)
        h.sim.add_trade("EUR_USD", 1000, 1.1, "fxb-EUR_USD-20261103T0800-B", None)   # stop removed
        rep = h.cycle()
        self.assertEqual(h.sim.trades, {})
        self.assertTrue(rep.blocked.startswith("halted: trade"))
        self.assertEqual(h.alerts[-1][0], "critical")

    def test_take_profit_failure_is_retried(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.tp_failures = 1
        h.cycle()
        self.assertIsNone(h.ledger.position(EUR_ID).tp_price)
        self.assertEqual(h.alerts[-1][0], "warning")
        h.cycle(LATER)
        self.assertIsNotNone(h.ledger.position(EUR_ID).tp_price)

    def test_untagged_trade_pauses_entries(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.add_trade("GBP_USD", 1000, 1.32, None, 1.31)
        rep = h.cycle()
        self.assertEqual(rep.blocked, "paused: untagged trade or order on the account")
        self.assertEqual(h.sim.orders_sent, [])

    def test_alert_channel_failure_does_not_stop_safety_actions(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["no_stop"]
        h.engine._alert_fn = lambda level, text: 1 / 0
        h.cycle()
        self.assertEqual(h.sim.trades, {})
        self.assertIsNotNone(h.ledger.halt_reason())


class HealthTests(unittest.TestCase):
    def test_broker_unavailable_blocks_without_crashing(self):
        h = Harness(self)
        h.sim.fail_next = {"account"}
        self.assertEqual(h.cycle().blocked, "broker unavailable")

    def test_stale_candles_block(self):
        h = Harness(self)
        rep = h.cycle(NOW + timedelta(minutes=15))        # the next candle has not arrived
        self.assertEqual(rep.blocked, "stale candles EUR_USD")

    def test_stale_price_blocks(self):
        h = Harness(self)
        p = h.sim.price_data["GBP_USD"]
        h.sim.price_data["GBP_USD"] = Price(p.pair, NOW - timedelta(minutes=5), p.bid, p.ask,
                                            True, p.loss_factor, p.value_factor)
        self.assertEqual(h.cycle().blocked, "stale or untradeable price GBP_USD")

    def test_news_feed_down_blocks(self):
        h = Harness(self)
        h.news_events = None
        self.assertEqual(h.cycle().blocked, "news feed unavailable")


class DailyLossTests(unittest.TestCase):
    def test_breach_latches_until_next_uk_day(self):
        h = Harness(self)
        h.cycle()                                          # snapshot: start NAV 1000
        h.sim.nav = 984.0
        self.assertEqual(h.cycle(LATER).blocked, "daily loss limit reached")
        h.sim.nav = 1000.0                                 # equity recovers
        self.assertEqual(h.cycle(LATER).blocked, "daily loss limit reached")
        tomorrow = NOW + timedelta(days=1)
        for pair in PAIRS:
            set_market(h.sim, pair, now=tomorrow)
        self.assertIsNone(h.cycle(tomorrow).blocked)
        self.assertFalse(h.ledger.daily_loss_latched(uk_trading_day(tomorrow)))

    def test_deposit_cannot_hide_a_loss(self):
        h = Harness(self)
        h.cycle()
        h.sim.now = LATER
        h.sim.deposit(300.0)
        h.sim.nav -= 16.0
        self.assertEqual(h.cycle(LATER).blocked, "daily loss limit reached")

    def test_deposit_alone_is_not_profit_or_loss(self):
        h = Harness(self)
        h.cycle()
        h.sim.now = LATER
        h.sim.deposit(300.0)
        self.assertIsNone(h.cycle(LATER).blocked)


class LifecycleTests(unittest.TestCase):
    def test_broker_close_is_recorded_with_pl(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        p = h.ledger.position(EUR_ID)
        h.sim.close_at_broker(p.trade_id, p.tp_price)
        h.cycle(LATER)
        closed = h.ledger.position(EUR_ID)
        self.assertEqual(closed.status, "closed")
        self.assertGreater(closed.realized_pl, 0)

    def test_time_exit_after_120_minutes(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        trade_id = h.ledger.position(EUR_ID).trade_id
        rep = h.cycle(NOW + timedelta(minutes=120))
        self.assertEqual(rep.exits, [trade_id])
        self.assertNotIn(trade_id, h.sim.trades)

    def test_lost_database_adopts_bot_trades_and_blocks_the_day(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        h.ledger.close()
        h.engine.ledger = h.ledger = Ledger(f"{h.dir}/rebuilt.db")
        rep = h.cycle(LATER)
        self.assertEqual(h.ledger.position(EUR_ID).status, "filled")
        self.assertEqual(rep.blocked, "no reliable daily snapshot today")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_engine.py" -v`
Expected: ERROR — `No module named 'fxbot.engine'`

- [ ] **Step 3: Write the implementation**

`fxbot/engine.py`:
```python
"""One 15-minute trading cycle, shared by live trading and backtests (spec §5-§7).

Safety rule: when anything is uncertain, new entries stop; existing trades stay protected.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fxbot.broker import BOT_TAG, Broker, BrokerError, Price, Trade, is_bot
from fxbot.config import Settings
from fxbot.costs import passes_cost_filter, round_trip_cost
from fxbot.instruments import INSTRUMENTS
from fxbot.ledger import Ledger
from fxbot.news import NewsEvent, news_blocked
from fxbot.risk import (AccountState, Candidate, OpenPosition, daily_loss_breached,
                        evaluate as approve, uk_trading_day)
from fxbot.strategy import LONDON, entry_window_open, evaluate as signal_for

NewsSource = Callable[[datetime], list[NewsEvent] | None]   # None = feed unavailable
Alert = Callable[[str, str], None]                           # (level, text)

M15 = timedelta(minutes=15)


@dataclass
class CycleReport:
    at: datetime
    blocked: str | None = None
    filled: list[str] = field(default_factory=list)            # client IDs
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (pair, reason)
    exits: list[str] = field(default_factory=list)             # trade IDs closed by time exit


def client_id(pair: str, side: int, candle_time: datetime) -> str:
    return f"{BOT_TAG}{pair}-{candle_time:%Y%m%dT%H%M}-{'B' if side > 0 else 'S'}"


class Engine:
    def __init__(self, broker: Broker, ledger: Ledger, settings: Settings,
                 news: NewsSource, alert: Alert):
        self.broker, self.ledger, self.s, self.news, self._alert_fn = broker, ledger, settings, news, alert

    # --- helpers -----------------------------------------------------------
    def alert(self, level: str, text: str) -> None:
        try:
            self._alert_fn(level, text)
        except Exception:   # a broken alert channel must never stop the engine
            pass

    def _fill(self, cid: str, side: int, trade_id: str, units: int, fill: float,
              stop: float | None, cost: float, price: Price, at: datetime) -> None:
        n = abs(units)
        stop_dist = abs(fill - stop) if stop is not None else 0.0
        self.ledger.mark_filled(cid, trade_id, n, fill, stop, cost,
                                n * stop_dist * price.loss_factor, n * cost * price.loss_factor,
                                n * fill * price.value_factor, at)

    def _set_take_profit(self, cid: str, trade_id: str, side: int, fill: float, stop: float) -> None:
        tp = fill + side * self.s.reward_r * abs(fill - stop)
        try:
            ok = self.broker.set_take_profit(trade_id, tp)
        except BrokerError:
            ok = False
        if ok:
            self.ledger.set_take_profit(cid, tp)
        else:
            self.alert("warning", f"take-profit not set on trade {trade_id}; will retry")

    def _close_unprotected(self, trade_id: str, now: datetime) -> None:
        try:
            closed = self.broker.close_trade(trade_id)
        except BrokerError:
            closed = False
        self.ledger.halt(f"halted: trade {trade_id} had no stop-loss")
        self.alert("critical", f"trade {trade_id} had no stop-loss; "
                   f"{'closed' if closed else 'CLOSE FAILED - close it in the OANDA app'}; bot halted")

    # --- reconciliation (spec §5 step 3) ----------------------------------
    def _reconcile(self, now: datetime, trades: list[Trade], prices: dict[str, Price]) -> None:
        by_id = {t.trade_id: t for t in trades}
        for txn in self.broker.transactions_since(self.ledger.last_txn_id):
            if txn.kind == "FILL" and is_bot(txn.client_id):
                pos = self.ledger.position(txn.client_id)
                if pos is not None and pos.status in ("submitting", "abandoned"):
                    t = by_id.get(txn.trade_id)
                    stop = t.stop_price if t else None
                    cost = round_trip_cost(txn.pair, prices[txn.pair].ask - prices[txn.pair].bid, self.s)
                    self._fill(txn.client_id, pos.side, txn.trade_id, txn.units, txn.price, stop,
                               cost, prices[txn.pair], txn.time)
                    self.ledger.record(now, txn.pair, "reconciled", f"fill confirmed {txn.client_id}")
            elif txn.kind == "CLOSE":
                if self.ledger.mark_closed(txn.trade_id, txn.time, txn.amount):
                    self.ledger.record(now, txn.pair, "closed", f"trade {txn.trade_id} P&L {txn.amount:.2f}")
            elif txn.kind == "REJECT" and is_bot(txn.client_id):
                pos = self.ledger.position(txn.client_id)
                if pos is not None and pos.status == "submitting":
                    self.ledger.mark_failed(txn.client_id, "rejected by broker")
            elif txn.kind == "TRANSFER":
                self.ledger.add_contribution(txn.txn_id, txn.time, txn.amount)
            self.ledger.set_last_txn_id(txn.txn_id)

        known = {p.trade_id for p in self.ledger.open_positions()}
        for t in trades:   # bot trades the ledger lost track of (e.g. database rebuilt)
            if is_bot(t.client_id) and t.trade_id not in known:
                side = 1 if t.units > 0 else -1
                if self.ledger.position(t.client_id) is None:
                    self.ledger.create_intent(t.client_id, t.pair, side, t.open_time, now)
                cost = round_trip_cost(t.pair, prices[t.pair].ask - prices[t.pair].bid, self.s)
                self._fill(t.client_id, side, t.trade_id, t.units, t.open_price, t.stop_price,
                           cost, prices[t.pair], t.open_time)
                if t.take_profit_price is not None:
                    self.ledger.set_take_profit(t.client_id, t.take_profit_price)
                self.ledger.record(now, t.pair, "adopted", f"bot trade {t.trade_id} restored from broker")
        for p in self.ledger.open_positions():
            if p.trade_id not in by_id:   # closed at broker but no CLOSE transaction seen
                self.ledger.mark_closed(p.trade_id, now, 0.0)
                self.alert("warning", f"trade {p.trade_id} closed without a transaction; P&L unknown")

    # --- the cycle ---------------------------------------------------------
    def run_cycle(self, now: datetime) -> CycleReport:
        rep, s, ledger = CycleReport(now), self.s, self.ledger
        blocks: list[str] = []

        def block(reason: str) -> None:
            if reason not in blocks:
                blocks.append(reason)

        for reason in (ledger.halt_reason(), ledger.pause_reason()):
            if reason:
                block(reason)
        try:
            account = self.broker.account()
            trades = self.broker.open_trades()
            pending = self.broker.pending_orders()
            prices = self.broker.prices(s.pairs)
            self._reconcile(now, trades, prices)
        except BrokerError as exc:
            self.alert("warning", f"broker unavailable: {exc}")
            ledger.record(now, None, "blocked", "broker unavailable")
            rep.blocked = "broker unavailable"
            return rep

        # account hygiene
        if any(not is_bot(t.client_id) for t in trades) or any(not is_bot(o.client_id) for o in pending):
            block("paused: untagged trade or order on the account")
        unresolved = ledger.submitting()
        if unresolved:
            ledger.halt(f"halted: unresolved order {unresolved[0]}")
            self.alert("critical", f"order {unresolved[0]} could not be confirmed; bot halted")
            block(ledger.halt_reason())
        for t in trades:
            if is_bot(t.client_id) and t.stop_price is None:
                self._close_unprotected(t.trade_id, now)
                block(ledger.halt_reason())

        # take-profit retries and time exits
        broker_trades = {t.trade_id: t for t in trades}
        closed_now: set[str] = set()
        try:
            for p in ledger.open_positions():
                t = broker_trades.get(p.trade_id)
                if t is None or t.stop_price is None:
                    continue
                if p.tp_price is None:
                    if t.take_profit_price is not None:
                        ledger.set_take_profit(p.client_id, t.take_profit_price)
                    else:
                        self._set_take_profit(p.client_id, p.trade_id, p.side, p.fill_price, t.stop_price)
                if s.time_exit_minutes and now - p.opened_at >= timedelta(minutes=s.time_exit_minutes):
                    if self.broker.close_trade(p.trade_id):
                        closed_now.add(p.trade_id)
                        rep.exits.append(p.trade_id)
                        ledger.record(now, p.pair, "exit", f"time exit trade {p.trade_id}")
                    else:
                        self.alert("warning", f"time exit failed for trade {p.trade_id}; will retry")
        except BrokerError as exc:
            self.alert("warning", f"broker error managing trades: {exc}")
            block("broker unavailable")

        # daily loss (spec §6)
        day = uk_trading_day(now)
        snap = ledger.day_snapshot()
        if snap is None or snap[0] != day:
            activity = any((p.opened_at and uk_trading_day(p.opened_at) == day) or
                           (p.closed_at and uk_trading_day(p.closed_at) == day)
                           for p in ledger.all_positions())
            ledger.set_day_snapshot(day, account.nav, now)
            if activity:   # trading already happened today: start-of-day NAV is unknown
                ledger.set("daily_snapshot_unreliable", day.isoformat())
            snap = ledger.day_snapshot()
        if ledger.get("daily_snapshot_unreliable") == day.isoformat():
            block("no reliable daily snapshot today")
        deposits, withdrawals = ledger.contributions_since(snap[2])
        if ledger.daily_loss_latched(day) or daily_loss_breached(snap[1], account.nav, deposits, withdrawals, s):
            if not ledger.daily_loss_latched(day):
                ledger.latch_daily_loss(day)
                self.alert("warning", "daily loss limit reached; no new entries until tomorrow")
            block("daily loss limit reached")

        events = self.news(now)
        if events is None:
            block("news feed unavailable")

        if blocks:
            rep.blocked = blocks[0]
            ledger.record(now, None, "blocked", blocks[0])
            return rep
        try:
            self._scan_and_enter(now, day, account, prices, events, closed_now, blocks, rep)
        except BrokerError as exc:
            self.alert("warning", f"broker error during scan: {exc}")
            ledger.record(now, None, "blocked", "broker unavailable")
            rep.blocked = "broker unavailable"
        return rep

    def _healthy(self, now: datetime, prices: dict[str, Price]) -> tuple[dict, dict, str | None]:
        s = self.s
        last_m15 = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0) - M15
        last_h1 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        h1, m15 = {}, {}
        for pair in s.pairs:
            h1[pair] = self.broker.candles(pair, "H1", s.h1_window)
            m15[pair] = self.broker.candles(pair, "M15", s.m15_window)
            if not h1[pair] or not m15[pair] or h1[pair][-1].time != last_h1 or m15[pair][-1].time != last_m15:
                return h1, m15, f"stale candles {pair}"
            p = prices[pair]
            if (not p.tradeable or p.loss_factor <= 0 or p.value_factor <= 0
                    or now - p.time > timedelta(seconds=s.price_max_age_seconds)):
                return h1, m15, f"stale or untradeable price {pair}"
        return h1, m15, None

    def _scan_and_enter(self, now, day, account, prices, events, closed_now, blocks, rep) -> None:
        s, ledger = self.s, self.ledger
        h1, m15, unhealthy = self._healthy(now, prices)
        if unhealthy:
            rep.blocked = unhealthy
            ledger.record(now, None, "blocked", unhealthy)
            return

        candidates = []
        for pair in s.pairs:
            sig = signal_for(pair, h1[pair], m15[pair], s)
            if sig is None:
                continue
            price = prices[pair]
            cost = round_trip_cost(pair, price.ask - price.bid, s)
            if not entry_window_open(sig.candle_time + M15, s):
                reason = "outside session"
            elif not passes_cost_filter(cost, sig.stop_distance, s):
                reason = f"cost {cost / sig.stop_distance:.0%} of stop"
            elif news_blocked(pair, now, events, s):
                reason = "news blackout"
            else:
                info = self.broker.instrument(pair)
                entry = price.ask if sig.side > 0 else price.bid
                candidates.append(Candidate(sig, cost, entry, price.loss_factor, price.value_factor,
                                            info.margin_rate, info.max_units))
                continue
            rep.rejected.append((pair, reason))
            ledger.record(now, pair, "rejected", reason)
        candidates.sort(key=lambda c: c.cost / c.signal.stop_distance)

        positions = [OpenPosition(p.pair, p.side, p.units, p.risk_gbp, p.notional_gbp)
                     for p in ledger.open_positions() if p.trade_id not in closed_now]
        entries = ledger.entries_today(day)
        margin_used = account.margin_used
        for c in candidates:
            sig = c.signal
            state = AccountState(account.nav, margin_used, tuple(positions), entries,
                                 blocks[0] if blocks else None)
            decision = approve(c, state, s)
            cid = client_id(sig.pair, sig.side, sig.candle_time)
            if decision.approved and ledger.has_client_id(cid):
                decision = type(decision)(False, "duplicate signal")
            if not decision.approved:
                rep.rejected.append((sig.pair, decision.reason))
                ledger.record(now, sig.pair, "rejected", decision.reason)
                continue

            ledger.create_intent(cid, sig.pair, sig.side, sig.candle_time, now)   # before sending
            bound = c.price + sig.side * s.price_bound_pips * INSTRUMENTS[sig.pair].pip
            # a BrokerError here propagates: the intent stays 'submitting' and the next
            # cycle's reconciliation resolves it or halts (never resends)
            result = self.broker.market_order(sig.pair, sig.side * decision.units, cid,
                                              sig.stop_distance, bound)
            if result.status == "rejected":
                ledger.mark_failed(cid, result.reason)
                rep.rejected.append((sig.pair, f"broker rejected: {result.reason}"))
                ledger.record(now, sig.pair, "rejected", f"broker rejected: {result.reason}")
                continue
            if result.status != "filled":
                blocks.append(f"unresolved order {cid}")   # exposure unknown: stop entering
                self.alert("warning", f"order {cid} outcome unknown; checking next cycle")
                ledger.record(now, sig.pair, "unknown", f"order {cid} outcome unknown")
                continue

            price = prices[sig.pair]
            self._fill(cid, sig.side, result.trade_id, result.units, result.fill_price,
                       result.stop_price, c.cost, price, now)
            rep.filled.append(cid)
            ledger.record(now, sig.pair, "filled", f"{cid} {result.units} @ {result.fill_price}")
            if result.stop_price is None:
                self._close_unprotected(result.trade_id, now)
                blocks.append(ledger.halt_reason())
                continue
            self._set_take_profit(cid, result.trade_id, sig.side, result.fill_price, result.stop_price)
            filled = ledger.position(cid)   # risk recomputed from the actual fill
            positions.append(OpenPosition(sig.pair, sig.side, filled.units, filled.risk_gbp,
                                          filled.notional_gbp))
            entries = {**entries, sig.pair: entries.get(sig.pair, 0) + 1}
            margin_used += filled.notional_gbp * c.margin_rate
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_fx_engine.py" -v`
Expected: 26 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/engine.py tests/fx_fixtures.py tests/test_fx_engine.py
git commit -m "fxbot: add engine cycle with reconciliation and safety halts"
```

---

### Task 4: Friday flatten

**Files:**
- Modify: `fxbot/engine.py` (append to the end of the file)
- Test: `tests/test_fx_flatten.py`

**Interfaces:**
- Produces: `Engine.flatten(now) -> int` (bot trades still open; the live scheduler in Plan 4 calls it every 60 s from 19:30 Friday London and on `/flatten` until it returns 0); `weekend_phase(now, settings) -> "normal" | "flatten" | "deadline"`.
- Behaviour: closes only bot-tagged trades; a failed close counts as remaining; if the broker is unreachable, every ledger open position counts as remaining; at `deadline` with anything remaining → `critical` alert.

- [ ] **Step 1: Write the failing test**

`tests/test_fx_flatten.py`:
```python
import unittest
from datetime import datetime

from fx_fixtures import UTC, Harness
from fxbot.config import Settings
from fxbot.engine import weekend_phase

S = Settings()
FRI_1930 = datetime(2026, 11, 6, 19, 30, tzinfo=UTC)   # November: London = UTC
FRI_2000 = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)


class WeekendPhaseTests(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(weekend_phase(datetime(2026, 11, 6, 19, 29, tzinfo=UTC), S), "normal")
        self.assertEqual(weekend_phase(FRI_1930, S), "flatten")
        self.assertEqual(weekend_phase(FRI_2000, S), "deadline")
        self.assertEqual(weekend_phase(datetime(2026, 11, 5, 19, 45, tzinfo=UTC), S), "normal")
        self.assertEqual(weekend_phase(datetime(2026, 7, 10, 18, 30, tzinfo=UTC), S), "flatten")  # BST


class FlattenTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, now=FRI_1930)
        sim = self.h.sim
        for cid, pair, price, stop in (("fxb-1", "EUR_USD", 1.1, 1.099), ("fxb-2", "USD_JPY", 110.0, 109.9)):
            trade = sim.add_trade(pair, 1000, price, cid, stop)
            self.h.ledger.create_intent(cid, pair, 1, FRI_1930, FRI_1930)
            self.h.ledger.mark_filled(cid, trade.trade_id, 1000, price, stop, 0.0, 1.0, 0.1, 800.0, FRI_1930)
        sim.add_trade("GBP_USD", 1000, 1.32, None, 1.31)      # user's own trade: never touched

    def test_retries_until_flat_and_leaves_user_trades(self):
        self.h.sim.close_failures = 1
        self.assertEqual(self.h.engine.flatten(FRI_1930), 1)
        self.assertEqual(self.h.alerts, [])                    # before the deadline: just retry
        self.assertEqual(self.h.engine.flatten(FRI_1930), 0)
        self.assertEqual([t.client_id for t in self.h.sim.trades.values()], [None])

    def test_alerts_if_still_open_at_deadline(self):
        self.h.sim.close_failures = 5
        self.assertEqual(self.h.engine.flatten(FRI_2000), 2)
        self.assertEqual(self.h.alerts[-1][0], "critical")

    def test_broker_down_counts_ledger_positions_as_open(self):
        self.h.sim.fail_next = {"open_trades"}
        self.assertEqual(self.h.engine.flatten(FRI_2000), 2)   # unknown, so assume still open
        self.assertEqual([level for level, _ in self.h.alerts], ["warning", "critical"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest discover -s tests -p "test_fx_flatten.py" -v`
Expected: ERROR — `cannot import name 'weekend_phase' from 'fxbot.engine'`

- [ ] **Step 3: Write the implementation**

Append to the end of `fxbot/engine.py` (the method continues the `Engine` class body, which is the last thing in the file; the function follows it at module level):
```python
    # --- Friday flatten and /flatten (spec §7) ------------------------------
    def flatten(self, now: datetime) -> int:
        """Close every bot trade. Returns how many remain open (call again until 0)."""
        try:
            bot_trades = [t for t in self.broker.open_trades() if is_bot(t.client_id)]
        except BrokerError as exc:
            self.alert("warning", f"flatten: broker unavailable: {exc}")
            remaining = len(self.ledger.open_positions())
        else:
            remaining = 0
            for t in bot_trades:
                try:
                    ok = self.broker.close_trade(t.trade_id)
                except BrokerError:
                    ok = False
                if not ok:
                    remaining += 1
            self.ledger.record(now, None, "flatten", f"{len(bot_trades) - remaining} closed, {remaining} open")
        if remaining and weekend_phase(now, self.s) == "deadline":
            self.alert("critical", f"{remaining} bot trade(s) still open after Friday 20:00 - "
                       "close them in the OANDA app")
        return remaining


def weekend_phase(now: datetime, settings: Settings) -> str:
    """'normal', 'flatten' (Friday from 19:30 London) or 'deadline' (Friday from 20:00)."""
    local = now.astimezone(LONDON)
    if local.weekday() != 4 or local.time() < settings.friday_flatten_start:
        return "normal"
    return "deadline" if local.time() >= settings.friday_flatten_deadline else "flatten"
```

- [ ] **Step 4: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all OK — 108 fxbot tests plus the 27 existing `trading` tests (135).

- [ ] **Step 5: Commit**

```bash
git add fxbot/engine.py tests/test_fx_flatten.py
git commit -m "fxbot: add Friday flatten and weekend phases"
```

---

## Self-review (done while writing)

- **Spec coverage:** §5 steps 1–10 → Task 3 (fetch, health, reconcile incl. pending orders, intents-before-send, never-resend, halt on unresolved, stop on fill, take-profit by price from fill, risk recomputed from fill, decisions recorded); §6 daily loss with latch and contributions → Tasks 2–3; §7 states (pause/halt), single instance lock, lost database, Friday flatten → Tasks 2–4; §9 engine scenarios incl. concurrency (five simultaneous signals, delayed fills/timeouts) and failure recovery (corrupt DB, lost connectivity, restart during unresolved order, alert channel failure) → Tasks 2–4. Telegram commands, healthcheck timer, scheduler retry and OANDA adapter are Plan 4; backtest fills are Plan 3.
- **Verification before publishing:** the plan was applied task by task to a clean checkout (suite totals 97 → 105 → 131 → 135, all OK; resulting files byte-identical to the verified copy). 16 deliberate mutations of the engine's safety rules (unresolved halt, missing stop on fill and on an existing trade, untagged pause, duplicate check, block after unknown order, daily latch, time exit, news-down, stale candles, stale prices, take-profit retry, deadline alert, risk reservation, adoption of lost trades, unreliable snapshot) each make the suite fail. A 17th mutation exposed a redundant exception handler around `market_order`, which was removed (the cycle-level handler gives the same safe outcome).
- **Placeholders:** none. **Type consistency:** names in Interfaces match the code blocks.

## Amendments after the Plan 2 code review (implemented; code in the repo supersedes the blocks above)

The review found one critical and several important issues, each reproduced with a failing test
before fixing (17 regression tests added; 29 safety mutations now all fail the suite):

- **Lost database:** replayed bot FILLs for unknown client IDs are recorded (reserving the ID);
  a new ledger on an account with transaction history (`Account.last_txn_id > 0` on first cycle)
  blocks entries for that UK day. Previously a closed-then-lost trade's client ID could be resent.
- **Daily snapshot:** keyed to `Account.last_txn_id` (`set_day_snapshot(day, nav, txn_id)`,
  `contributions_since(txn_id)`), removing a deposit/snapshot race; trades closed earlier the same
  UK day have their realised P&L subtracted from start NAV instead of blocking the whole day.
- **Fetch order:** transactions are read before open trades; a ledger position missing at the broker
  is only written off on the second consecutive sighting (its CLOSE may not have been read yet).
- **Robustness:** prices are fetched for configured pairs plus any open-trade pair; `_fill` tolerates
  a missing price/unknown instrument; `usd_direction` returns 0 for non-USD pairs (`Instrument.usd_is_base`
  removed); any reconciliation exception halts with a critical alert while protection still runs;
  a `close_trade` error no longer skips the remaining positions; a prices failure still runs protection.
- **Flatten:** broker unreachable never reports 0 remaining.
- **Ledger:** integrity-check failure releases the lock; `resume` and `set_day_snapshot` are atomic.
- **SimBroker:** `late_fill()`, `timeout_rejected` mode; unknown trade IDs return False.
- **Removed:** the in-cycle `entries` update (unobservable: one signal per pair per cycle, and an
  open position already blocks the pair).

### Carried forward to Plans 3–4
- **Plan 4 scheduler** must catch every exception from `run_cycle` (e.g. raw `sqlite3` errors are not
  wrapped), halt and alert, and keep running the protection loop.
- **Plan 4 `OandaBroker`:** `transactions_since(0)` on a fresh ledger must page through the full
  account history; `pending_orders()` returns only standalone entry orders (not attached SL/TP);
  round take-profit prices to instrument precision.
- **Plan 4 alerts:** de-duplicate repeating alerts (e.g. the untagged-trade pause).
- **Operational note:** the first UK day after deploying a brand-new ledger blocks entries (the
  account already has history). Expected and safe; document it in the setup checklist.
- **Plan 3:** extend SimBroker with candle-driven stop/target triggering and partial fills if needed.

