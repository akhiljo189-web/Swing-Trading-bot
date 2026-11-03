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
