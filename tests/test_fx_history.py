import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fxbot.history import HistCandle, History, Series, load, save
from fxbot.news import NewsEvent, load_calendar

UTC = timezone.utc
T = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def candle(t, mid=1.1, spread=0.0002):
    h = spread / 2
    return HistCandle(t, mid - h, mid + 0.001 - h, mid - 0.001 - h, mid - h,
                      mid + h, mid + 0.001 + h, mid - 0.001 + h, mid + h)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_round_trip_and_mid(self):
        candles = [candle(T + timedelta(minutes=15 * i), 1.1 + i * 0.001) for i in range(3)]
        save(self.dir / "EUR_USD_M15.csv", candles)
        loaded = load(self.dir / "EUR_USD_M15.csv")
        self.assertEqual(loaded, candles)
        self.assertAlmostEqual(loaded[1].mid().close, 1.101, places=12)

    def test_rejects_unsorted_and_inconsistent_data(self):
        with self.assertRaises(ValueError):
            save(self.dir / "x.csv", [candle(T), candle(T)])
        bad = HistCandle(T, 1.1, 1.2, 1.0, 1.1, 1.0, 1.2, 1.0, 1.0)   # ask below bid
        with self.assertRaises(ValueError):
            save(self.dir / "x.csv", [bad])

    def test_rejects_wrong_header(self):
        (self.dir / "bad.csv").write_text("time,open,high\n")
        with self.assertRaises(ValueError):
            load(self.dir / "bad.csv")

    def test_completed_count_only_counts_closed_candles(self):
        s = Series([candle(T + timedelta(minutes=15 * i)) for i in range(4)], "M15")
        self.assertEqual(s.completed_count(T + timedelta(minutes=15)), 1)          # first just closed
        self.assertEqual(s.completed_count(T + timedelta(minutes=29)), 1)
        self.assertEqual(s.completed_count(T + timedelta(minutes=30, seconds=10)), 2)
        self.assertEqual(s.at_or_after(T + timedelta(minutes=1)).time, T + timedelta(minutes=15))

    def test_from_dir_loads_every_pair_and_granularity(self):
        for g, step in (("M15", 15), ("H1", 60)):
            save(self.dir / f"EUR_USD_{g}.csv", [candle(T + timedelta(minutes=step * i)) for i in range(2)])
        h = History.from_dir(self.dir, ("EUR_USD",))
        self.assertEqual(len(h.get("EUR_USD", "H1").candles), 2)
        self.assertIsNone(h.get("GBP_USD", "M15"))

    def test_news_calendar_csv(self):
        (self.dir / "news.csv").write_text("time,currency,impact\n2026-01-09T13:30:00+00:00,USD,High\n")
        self.assertEqual(load_calendar(self.dir / "news.csv"),
                         [NewsEvent(datetime(2026, 1, 9, 13, 30, tzinfo=UTC), "USD", "High")])
        (self.dir / "naive.csv").write_text("time,currency,impact\n2026-01-09T13:30:00,USD,High\n")
        with self.assertRaises(ValueError):
            load_calendar(self.dir / "naive.csv")


if __name__ == "__main__":
    unittest.main()
