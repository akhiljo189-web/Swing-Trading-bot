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
