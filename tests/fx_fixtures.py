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
