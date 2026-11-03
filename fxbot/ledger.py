"""SQLite ledger: order intents and positions, decisions, contributions, bot state (spec §4-§7).

One process at a time: the constructor takes an exclusive lock on `<path>.lock` and raises
LedgerLocked if another process holds it. Every write commits immediately.
"""
import fcntl
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime

from fxbot.risk import uk_trading_day

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    client_id TEXT PRIMARY KEY, pair TEXT NOT NULL, side INTEGER NOT NULL,
    candle_time TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
    trade_id TEXT, units INTEGER, fill_price REAL, stop_price REAL, tp_price REAL,
    cost REAL, risk_gbp REAL, stop_risk_gbp REAL, cost_risk_gbp REAL, notional_gbp REAL,
    opened_at TEXT, closed_at TEXT, realized_pl REAL, close_txn_id INTEGER, note TEXT);
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
    close_txn_id: int | None   # broker transaction that closed it (None if written off)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Ledger:
    def __init__(self, path: str, durable: bool = True):
        """durable=False skips disk syncs: only for throwaway backtest ledgers."""
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
            if not durable:
                self._db.execute("PRAGMA synchronous=OFF")
                self._db.execute("PRAGMA journal_mode=MEMORY")
            self._db.executescript(SCHEMA)
        except (sqlite3.DatabaseError, LedgerError) as exc:
            self.close()
            raise LedgerError(f"ledger unreadable: {exc}") from exc

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

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
        with self._transaction():
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

    def mark_closed(self, trade_id: str, closed_at: datetime, realized_pl: float,
                    txn_id: int | None = None) -> bool:
        cur = self._db.execute(
            "UPDATE positions SET status='closed', closed_at=?, realized_pl=?, close_txn_id=?"
            " WHERE trade_id=? AND status='filled'",
            (closed_at.isoformat(), realized_pl, txn_id, trade_id))
        return cur.rowcount == 1

    def set_take_profit(self, client_id: str, price: float) -> None:
        self._db.execute("UPDATE positions SET tp_price=? WHERE client_id=?", (price, client_id))

    def _positions(self, where: str, args: tuple = ()) -> list[Position]:
        rows = self._db.execute(
            "SELECT client_id, pair, side, candle_time, status, trade_id, units, fill_price,"
            " stop_price, tp_price, cost, risk_gbp, stop_risk_gbp, cost_risk_gbp, notional_gbp,"
            f" opened_at, closed_at, realized_pl, close_txn_id FROM positions WHERE {where}"
            " ORDER BY created_at", args).fetchall()
        return [Position(r[0], r[1], r[2], _dt(r[3]), r[4], r[5], r[6], r[7], r[8], r[9], r[10],
                         r[11], r[12], r[13], r[14], _dt(r[15]), _dt(r[16]), r[17], r[18]) for r in rows]

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

    def contributions_since(self, txn_id: int) -> tuple[float, float]:
        """(deposits, withdrawals) in transactions after `txn_id`, both as positive numbers."""
        deposits = withdrawals = 0.0
        for (amount,) in self._db.execute("SELECT amount FROM contributions WHERE txn_id > ?",
                                          (txn_id,)).fetchall():
            if amount >= 0:
                deposits += amount
            else:
                withdrawals -= amount
        return deposits, withdrawals

    # --- daily snapshot and latch -----------------------------------------
    def day_snapshot(self) -> tuple[date, float, int] | None:
        """(UK day, start-of-day NAV, broker transaction ID the NAV was read at)."""
        day = self.get("day")
        if day is None:
            return None
        return date.fromisoformat(day), float(self.get("day_start_nav")), int(self.get("day_start_txn"))

    def set_day_snapshot(self, day: date, start_nav: float, txn_id: int) -> None:
        with self._transaction():
            self.set("day", day.isoformat())
            self.set("day_start_nav", repr(start_nav))
            self.set("day_start_txn", str(txn_id))

    def latch_daily_loss(self, day: date) -> None:
        self.set("daily_loss_latched", day.isoformat())

    def daily_loss_latched(self, day: date) -> bool:
        return self.get("daily_loss_latched") == day.isoformat()
