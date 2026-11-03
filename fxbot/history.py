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


@dataclass(frozen=True, slots=True)
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

    def coverage_problems(self, pairs: tuple[str, ...], start: datetime, end: datetime,
                          warmup: timedelta = timedelta(days=35), slack: timedelta = timedelta(days=4)) -> list[str]:
        """Human-readable reasons the data cannot support a backtest of [start, end)."""
        problems = []
        for pair in pairs:
            for g in ("H1", "M15"):
                s = self.get(pair, g)
                if s is None or not s.times:
                    problems.append(f"{pair} {g}: no data")
                    continue
                if s.times[0] > start - warmup:
                    problems.append(f"{pair} {g}: starts {s.times[0]:%Y-%m-%d}; need data from "
                                    f"{start - warmup:%Y-%m-%d} for indicator warm-up")
                if s.times[-1] < end - slack:
                    problems.append(f"{pair} {g}: ends {s.times[-1]:%Y-%m-%d}, before {end:%Y-%m-%d}")
        return problems
