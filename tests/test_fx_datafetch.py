import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)


class DataFetchTests(unittest.TestCase):
    def test_s10_cache_fetches_once_and_persists(self):
        from fxbot.datafetch import S10Cache
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            fetch = lambda pair, at: calls.append(at) or ((1.1, 1.1002) if at.minute == 15 else None)  # noqa: E731
            cache = S10Cache(Path(tmp) / "s10.csv", fetch)
            t1, t2 = NOW, NOW + timedelta(minutes=15)
            self.assertEqual(cache("EUR_USD", t1), (1.1, 1.1002))
            self.assertIsNone(cache("EUR_USD", t2))
            cache("EUR_USD", t1)
            self.assertEqual(len(calls), 2)
            reloaded = S10Cache(Path(tmp) / "s10.csv")
            self.assertEqual((reloaded("EUR_USD", t1), reloaded("EUR_USD", t2)), ((1.1, 1.1002), None))
            frozen = cache.read_only()
            self.assertIsNone(frozen.fetch)                          # workers never call the API
            self.assertEqual(frozen("EUR_USD", t1), (1.1, 1.1002))
            self.assertIsNone(frozen("EUR_USD", t1 + timedelta(hours=1)))
            self.assertEqual(len(calls), 2)

    def test_gap_report(self):
        from fxbot.datafetch import gap_report
        from fx_history import make_history
        h = make_history(10)
        s = h.series[("GBP_USD", "M15")]
        dropped = s.candles[:5] + s.candles[8:]
        from fxbot.history import History
        data = {k: v.candles for k, v in h.series.items()}
        data[("GBP_USD", "M15")] = dropped
        lines = gap_report(History(data), ("EUR_USD", "GBP_USD"))
        self.assertEqual(lines, ["GBP_USD 2026-01: 3 M15 candle(s) missing (relative to EUR_USD)"])

    def test_fetch_all_writes_csv_files(self):
        from fxbot.datafetch import fetch_all
        from fxbot.history import load
        from fx_history import make_history
        h = make_history(3)

        class FakeBroker:
            def fetch_history(self, pair, g, start, end):
                return h.series[(pair, g)].candles
        with tempfile.TemporaryDirectory() as tmp:
            fetch_all(FakeBroker(), ("EUR_USD",), NOW, NOW, Path(tmp), log=lambda s: None)
            self.assertEqual(load(Path(tmp) / "EUR_USD_M15.csv"), h.series[("EUR_USD", "M15")].candles)


if __name__ == "__main__":
    unittest.main()
