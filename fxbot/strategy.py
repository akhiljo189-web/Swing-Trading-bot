"""15-minute breakout with 1-hour trend filter (spec §3). Pure functions only."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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


def trend_direction(h1: Sequence[Candle], s: Settings) -> int:
    """+1 / -1 when the H1 EMA filter allows longs / shorts, else 0."""
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
    # no lookahead: the H1 candle must have closed by the time the M15 signal candle closes
    if h1[-1].time + timedelta(hours=1) > m15[-1].time + timedelta(minutes=15):
        return None
    breakout = _breakout(m15, s.breakout_lookback)   # cheap check first; EMAs only on breakouts
    if breakout == 0 or trend_direction(h1, s) != breakout:
        return None
    trend = breakout
    a = atr([c.high for c in m15], [c.low for c in m15], [c.close for c in m15], s.atr_period)[-1]
    return Signal(pair, trend, m15[-1].time, s.stop_atr_mult * a)


def require_aware(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return at


def entry_window_open(close_time_utc: datetime, settings: Settings) -> bool:
    """Takes the signal candle's CLOSE time (Signal.candle_time + 15 minutes)."""
    local = require_aware(close_time_utc).astimezone(LONDON)
    if local.weekday() >= 5:
        return False
    t = local.time()
    if not (settings.session_start <= t < settings.session_end):
        return False
    if local.weekday() == 4 and t >= settings.friday_entry_cutoff:
        return False
    return True
