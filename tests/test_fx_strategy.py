import unittest
from datetime import datetime, timedelta, timezone

from fxbot.config import Settings
from fxbot.indicators import atr
from fxbot.strategy import Candle, Signal, entry_window_open, evaluate

UTC = timezone.utc
S = Settings()
H1_END = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)    # last completed H1 opens 08:00 Tue
M15_END = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)   # last completed M15 opens 09:00 Tue


def h1_series(step, count=600, start=1.0):
    t0 = H1_END - timedelta(hours=count - 1)
    out = []
    for i in range(count):
        c = start + i * step
        out.append(Candle(t0 + timedelta(hours=i), c, c + 0.0002, c - 0.0002, c))
    return out


def m15_flat(count=100, mid=1.1000):
    t0 = M15_END - timedelta(minutes=15 * (count - 1))
    return [Candle(t0 + timedelta(minutes=15 * i), mid, mid + 0.0005, mid - 0.0005, mid)
            for i in range(count)]


def with_last(m15, close, high, low):
    last = m15[-1]
    return m15[:-1] + [Candle(last.time, m15[-2].close, high, low, close)]


def expected_stop(m15):
    w = m15[-S.m15_window:]
    a = atr([c.high for c in w], [c.low for c in w], [c.close for c in w], S.atr_period)[-1]
    return S.stop_atr_mult * a


class EvaluateTests(unittest.TestCase):
    def test_long_breakout_in_uptrend(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        sig = evaluate("EUR_USD", h1_series(+0.0001), m15, S)
        self.assertEqual(sig, Signal("EUR_USD", +1, M15_END, sig.stop_distance))
        self.assertAlmostEqual(sig.stop_distance, expected_stop(m15), places=12)

    def test_short_breakdown_in_downtrend(self):
        m15 = with_last(m15_flat(), close=1.0990, high=1.1002, low=1.0988)
        sig = evaluate("EUR_USD", h1_series(-0.0001, start=1.2), m15, S)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, -1)

    def test_breakout_against_trend_is_ignored(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(-0.0001, start=1.2), m15, S))

    def test_no_trend_no_signal(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(0.0), m15, S))

    def test_close_equal_to_range_high_is_not_breakout(self):
        m15 = with_last(m15_flat(), close=1.1005, high=1.1006, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001), m15, S))

    def test_stale_breakout_is_not_fresh(self):
        m15 = m15_flat()
        prev = m15[-2]
        m15[-2] = Candle(prev.time, prev.open, 1.1009, prev.low, 1.1008)   # already broke out
        m15 = with_last(m15, close=1.1012, high=1.1013, low=1.1004)
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001), m15, S))

    def test_insufficient_warmup(self):
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001, count=599), m15, S))
        self.assertIsNone(evaluate("EUR_USD", h1_series(+0.0001), m15[-99:], S))

    def test_candles_older_than_window_do_not_change_result(self):
        # live fetches exactly the window; backtest may hold more history — results must match
        h1 = h1_series(+0.0001)
        m15 = with_last(m15_flat(), close=1.1010, high=1.1012, low=1.0998)
        older_h1 = [Candle(h1[0].time - timedelta(hours=i + 1), 5.0, 9.0, 0.1, 0.2) for i in range(50)][::-1]
        older_m15 = [Candle(m15[0].time - timedelta(minutes=15 * (i + 1)), 5.0, 9.0, 0.1, 0.2) for i in range(50)][::-1]
        self.assertEqual(evaluate("EUR_USD", h1, m15, S),
                         evaluate("EUR_USD", older_h1 + h1, older_m15 + m15, S))


class EntryWindowTests(unittest.TestCase):
    def check(self, y, mo, d, hh, mm, expected):
        self.assertEqual(entry_window_open(datetime(y, mo, d, hh, mm, tzinfo=UTC), S), expected)

    def test_winter_session(self):           # November: London = UTC
        self.check(2026, 11, 3, 9, 15, True)
        self.check(2026, 11, 3, 6, 45, False)
        self.check(2026, 11, 3, 7, 0, True)
        self.check(2026, 11, 3, 20, 0, False)

    def test_summer_time(self):              # July: London = UTC+1
        self.check(2026, 7, 7, 6, 15, True)    # 07:15 London
        self.check(2026, 7, 7, 19, 15, False)  # 20:15 London

    def test_friday_cutoff_and_weekend(self):
        self.check(2026, 11, 6, 17, 45, True)   # Friday 17:45
        self.check(2026, 11, 6, 18, 0, False)   # Friday 18:00
        self.check(2026, 11, 7, 10, 0, False)   # Saturday


if __name__ == "__main__":
    unittest.main()
