import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from fxbot.backtest import BacktestResult, TradeRecord
from fxbot.metrics import bootstrap_ci, losing_streak, max_drawdown, max_simultaneous, summarize

UTC = timezone.utc
D = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


def trade(day, hour, pl, pair="EUR_USD", hours=1, risk=2.5):
    o = D + timedelta(days=day, hours=hour)
    return TradeRecord(pair, 1, 1000, o, o + timedelta(hours=hours), 1.1, 1.1, pl, risk, "stop")


TRADES = [trade(0, 0, 5.0), trade(0, 0.5, -2.5, "GBP_USD"), trade(1, 0, -2.5), trade(1, 2, -2.5),
          trade(2, 0, 5.0, "USD_JPY")]


class MetricTests(unittest.TestCase):
    def test_drawdown(self):
        self.assertAlmostEqual(max_drawdown([1000, 1100, 990, 1200], 1000), 0.1)

    def test_drawdown_counts_start_nav_and_every_mark(self):
        self.assertAlmostEqual(max_drawdown([950, 1000], 1000), 0.05)          # loss on day one
        self.assertAlmostEqual(max_drawdown([1010, 960, 1000], 1000), 50 / 1010)

    def test_losing_streak_and_overlap(self):
        self.assertEqual(losing_streak(TRADES), 3)
        self.assertEqual(max_simultaneous(TRADES), 2)          # day 0: 09:00-10:00 and 09:30-10:30
        back_to_back = [trade(0, 0, 1.0), trade(0, 1, 1.0)]     # close and open at the same instant
        self.assertEqual(max_simultaneous(back_to_back), 1)

    def test_summary_numbers(self):
        r = BacktestResult(1000.0, 1002.5, TRADES, [1000.0, 1002.5],
                           Counter({"max gross notional": 3, "news blackout": 1}))
        m = summarize(r, bootstrap_runs=0)
        self.assertEqual(m["trades"], 5)
        self.assertAlmostEqual(m["net_gbp"], 2.5)
        self.assertAlmostEqual(m["expectancy_r"], (2 - 1 - 1 - 1 + 2) / 5)
        self.assertAlmostEqual(m["profit_factor"], 10 / 7.5)
        self.assertAlmostEqual(m["win_rate"], 0.4)
        self.assertEqual(m["notional_cap_hits"], 3)
        self.assertEqual(m["per_pair"]["USD_JPY"]["trades"], 1)
        self.assertEqual(m["days_with_multiple_trades"], 2)
        self.assertAlmostEqual(m["multi_trade_days_all_lost"], 0.5)
        self.assertAlmostEqual(m["multi_trade_days_all_lost_if_independent"], 0.6 ** 2)   # loss rate 3/5
        self.assertIsNone(m["expectancy_r_ci95"])

    def test_no_losses_gives_no_profit_factor(self):
        m = summarize(BacktestResult(1000.0, 1005.0, [trade(0, 0, 5.0)], []), bootstrap_runs=0)
        self.assertIsNone(m["profit_factor"])

    def test_bootstrap_resamples_whole_days(self):
        # each day nets +2R and -1R: resampling days always gives 0.5R; resampling trades would not
        days = [trade(d, 0, 5.0) for d in range(3)] + [trade(d, 1, -2.5) for d in range(3)]
        self.assertEqual(bootstrap_ci(days, runs=300, seed=1), (0.5, 0.5))

    def test_bootstrap_resamples_days_deterministically(self):
        lo, hi = bootstrap_ci(TRADES, runs=500, seed=3)
        self.assertEqual(bootstrap_ci(TRADES, runs=500, seed=3), (lo, hi))
        self.assertLessEqual(lo, 0.2)
        self.assertGreaterEqual(hi, 0.2)
        self.assertGreaterEqual(lo, -1.0)        # a resampled day set can be all losers
        self.assertLessEqual(hi, 2.0)


if __name__ == "__main__":
    unittest.main()
