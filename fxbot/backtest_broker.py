"""Broker over historical bid/ask candles, used by the backtest (spec §8).

Execution model (all deliberately conservative):
- Market orders (entries, time exits, flattening) execute 10 s after the decision candle closes:
  at the S10 candle open covering that moment if available, else at the next M15 open with
  DOUBLE slippage. Buys pay ask + slippage, sells receive bid - slippage. If that M15 candle is
  missing (no ticks), the following one is used; with no price within 30 minutes the order is
  rejected (entries) or retried later (exits) instead of failing.
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


MAJORS = {"USD", "EUR", "JPY", "GBP", "CAD", "CHF"}   # ESMA major currencies


class BacktestBroker:

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

    def _execute(self, pair: str, buy: bool) -> tuple[float, datetime, datetime] | None:
        """(price, fill time, candle open the fill belongs to), or None if no price is near."""
        t = self._decision_close()
        quote = self.s10(pair, t + timedelta(seconds=10)) if self.s10 else None
        mult, candle_time = 1, t
        if quote is None:
            c = self._series(pair).at_or_after(t)
            if c is None or c.time - t > 2 * M15 - timedelta(seconds=1):   # nothing within 30 min
                return None
            quote, mult, candle_time = (c.bid_o, c.ask_o), 2, c.time
        bid, ask = quote
        slip = mult * self._slip(pair)
        return (ask + slip if buy else bid - slip), max(t, candle_time) + timedelta(seconds=10), candle_time

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
            margin += abs(t.units) * mark * f * self.margin_rate(t.pair)
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
        return InstrumentInfo(self.margin_rate(pair), 100_000_000)

    @staticmethod
    def margin_rate(pair: str) -> float:
        """UK retail margin: 1/30 for major pairs, 1/20 for other currency pairs."""
        return 1 / 30 if set(currencies(pair)) <= MAJORS else 1 / 20

    def open_trades(self) -> list[Trade]:
        return list(self.trades.values())

    def pending_orders(self) -> list[PendingOrder]:
        return []

    def transactions_since(self, txn_id: int) -> list[Txn]:
        return self.txns[txn_id:]

    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult:
        executed = self._execute(pair, units > 0)
        if executed is None:
            self._txn("REJECT", self.now, client_id=client_id, pair=pair)
            return OrderResult("rejected", reason="NO_PRICE")
        fill, at, candle_time = executed
        if (units > 0 and fill > price_bound) or (units < 0 and fill < price_bound):
            self._txn("REJECT", at, client_id=client_id, pair=pair)
            return OrderResult("rejected", reason="BOUNDS_VIOLATION")
        self._ids += 1
        stop = fill - stop_distance if units > 0 else fill + stop_distance
        trade = Trade(str(self._ids), pair, units, fill, at, client_id, stop, None)
        self.trades[trade.trade_id] = trade
        idx = bisect.bisect_left(self._series(pair).times, candle_time)
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
        executed = self._execute(t.pair, t.units < 0)
        if executed is None:
            return False
        price, at, _ = executed
        self._close(t, price, at, "market")
        return True
