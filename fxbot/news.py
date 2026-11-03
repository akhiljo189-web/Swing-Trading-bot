"""High-impact news blackout check (spec §3). Feed fetching is added in Plan 4."""
import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

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


def load_calendar(path: Path) -> list[NewsEvent]:
    """Historical calendar CSV with header `time,currency,impact` (UTC ISO 8601 times).

    Use scheduled releases only: unscheduled events in a historical calendar would let the
    backtest avoid news it could not have known about in advance.
    """
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["time", "currency", "impact"]:
            raise ValueError(f"{path}: expected header time,currency,impact")
        events = [NewsEvent(datetime.fromisoformat(r["time"]), r["currency"], r["impact"]) for r in reader]
    if any(e.time.tzinfo is None for e in events):
        raise ValueError(f"{path}: times must be timezone-aware UTC")
    return events
