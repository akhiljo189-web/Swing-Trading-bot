"""Live high-impact news calendar from the free Forex Factory weekly feed (spec §2, §3).

If the feed cannot be fetched and the last good copy is too old, the source returns None,
which the engine treats as "news feed unavailable" and blocks new entries.
"""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fxbot.http import HttpError, Transport, urllib_transport
from fxbot.news import NewsEvent

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def parse_feed(items: list) -> list[NewsEvent]:
    events = []
    for item in items:
        try:
            when = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
            events.append(NewsEvent(when, item["country"], item["impact"]))
        except (KeyError, TypeError, ValueError):
            continue   # all-day or malformed entries carry no usable time
    return events


def fetch_forex_factory(transport: Transport = urllib_transport) -> list[NewsEvent]:
    status, data = transport("GET", FF_URL, {"User-Agent": "fxbot"}, None, 15)
    if status != 200 or not isinstance(data, list):
        raise HttpError(f"news feed returned {status}")
    return parse_feed(data)


class LiveNewsFeed:
    def __init__(self, fetch: Callable[[], list[NewsEvent]] = fetch_forex_factory,
                 refresh: timedelta = timedelta(hours=1), max_age: timedelta = timedelta(hours=3)):
        self.fetch, self.refresh, self.max_age = fetch, refresh, max_age
        self.events: list[NewsEvent] = []
        self.fetched_at: datetime | None = None
        self.attempted_at: datetime | None = None

    def __call__(self, now: datetime) -> list[NewsEvent] | None:
        due = self.fetched_at is None or now - self.fetched_at >= self.refresh
        if due and (self.attempted_at is None or now - self.attempted_at >= timedelta(minutes=5)):
            self.attempted_at = now
            try:
                events = self.fetch()
                # a calendar whose events are all over a week old is last week's file: not fresh
                if not events or max(e.time for e in events) >= now - timedelta(days=7):
                    self.events, self.fetched_at = events, now
            except Exception:   # keep the last good copy; staleness is checked below
                pass
        if self.fetched_at is None or now - self.fetched_at > self.max_age:
            return None
        return self.events
