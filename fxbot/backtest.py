"""Replay history through the live Engine with a BacktestBroker (spec §8).

The engine, risk manager, strategy and cost model are the live ones; only the broker differs.
Cycles run 10 s after every M15 close. A cycle is skipped only when no bot trade is open and no
pair has a signal: nothing could happen in it, so skipping it cannot change the result.
"""
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fxbot.backtest_broker import BacktestBroker, S10Lookup
from fxbot.config import Settings
from fxbot.engine import Engine, weekend_phase
from fxbot.history import History
from fxbot.ledger import Ledger
from fxbot.news import NewsEvent
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
    nav_marks: list[float]   # NAV after every replayed M15 close (for drawdown)
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
        s10: S10Lookup | None = None, skip_idle: bool = True) -> BacktestResult:
    """Replay M15 candles opening in [start, end). `news=None` means no calendar (filter off).

    Trades still open after the last candle are closed at the next available price, so every
    trade is counted. `skip_idle=False` runs every cycle (slow; for verification only).
    """
    events = list(news or [])
    broker = BacktestBroker(history, settings, nav, s10)
    sig = cached(signal_fn)
    series = history.get(settings.pairs[0], "M15")
    times = [t for t in series.times if start <= t < end]
    marks: list[float] = []
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
            signals = [sig(p, broker.candles(p, "H1", settings.h1_window),
                           broker.candles(p, "M15", settings.m15_window), settings)
                       for p in settings.pairs]   # every pair, so the no-skill base is complete
            if broker.trades or any(signals) or not skip_idle:
                engine.run_cycle(now)
            marks.append(broker.account().nav)
        if broker.trades:   # close at the last decision time so every trade is counted
            for trade_id in list(broker.trades):
                if not broker.close_trade(trade_id):          # no price at the window end
                    broker.close_at_last_price(trade_id)
            marks.append(broker.account().nav)
        risk = {p.trade_id: (p.risk_gbp or 0.0) for p in ledger.all_positions() if p.trade_id}
        decisions = ledger.decisions()
        halted = ledger.halt_reason()
        ledger.close()
    trades = [TradeRecord(c.pair, 1 if c.units > 0 else -1, abs(c.units), c.open_time, c.close_time,
                          c.entry, c.exit, c.pl, risk.get(c.trade_id, 0.0), c.reason)
              for c in broker.closed]
    end_nav = broker.account().nav if times else nav
    return BacktestResult(nav, end_nav, trades, marks,
                          Counter(r for _, o, r in decisions if o == "rejected"),
                          Counter(r for _, o, r in decisions if o == "blocked"),
                          alerts, halted, len(broker.trades), dict(sig.memo))
