import unittest
from datetime import timedelta

from fx_history import START, make_history
from fxbot.config import Settings
from fxbot.backtest import run
from fxbot.history import History
from fxbot.instruments import usd_direction
from fxbot.strategy import LONDON

S = Settings()
HISTORY = make_history(70)
BEGIN, END = START + timedelta(days=40), START + timedelta(days=70)


class BacktestRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run(HISTORY, S, BEGIN, END)

    def test_runs_cleanly_and_trades(self):
        r = self.result
        self.assertIsNone(r.halted)
        self.assertEqual(r.alerts, [])
        self.assertGreater(len(r.trades), 20)

    def test_accounting_balances(self):
        r = self.result
        self.assertEqual(r.open_at_end, 0)
        self.assertAlmostEqual(r.end_nav - r.start_nav, sum(t.pl_gbp for t in r.trades), places=6)

    def test_deterministic(self):
        again = run(HISTORY, S, BEGIN, END)
        self.assertEqual(again.trades, self.result.trades)

    def test_risk_limits_hold_throughout(self):
        trades = self.result.trades
        self.assertTrue(all(0 < t.risk_gbp <= 0.0025 * 1200 for t in trades))
        for t in trades:   # at every entry, open risk and USD-direction risk stay within the caps
            open_now = [o for o in trades if o.open_time <= t.open_time < o.close_time]
            self.assertLessEqual(len(open_now), 4)
            self.assertLessEqual(sum(o.risk_gbp for o in open_now), 0.01 * 1200)
            same = [o for o in open_now if usd_direction(o.pair, o.side) == usd_direction(t.pair, t.side)]
            self.assertLessEqual(sum(o.risk_gbp for o in same), 0.005 * 1200 + 1e-9)

    def test_no_position_held_over_the_weekend(self):
        for t in self.result.trades:
            self.assertLess((t.close_time - t.open_time).days, 2)
            local = t.open_time.astimezone(LONDON)
            self.assertFalse(local.weekday() == 4 and local.hour >= 18)

    def test_time_exits_happen_on_time(self):
        market = [t for t in self.result.trades if t.reason == "market"]
        self.assertTrue(market)
        for t in market:
            self.assertLessEqual(t.close_time - t.open_time, timedelta(minutes=135))

    def test_friday_flatten_without_time_exit(self):
        r = run(HISTORY, Settings(time_exit_minutes=None), BEGIN, END)
        self.assertEqual(r.open_at_end, 0)
        for t in r.trades:
            local = t.close_time.astimezone(LONDON)
            self.assertFalse(local.weekday() == 4 and local.hour >= 20, t)
            self.assertLess((t.close_time - t.open_time).days, 2)

    def test_no_lookahead(self):
        """Removing all data after a cut-off must not change any trade closed before it."""
        cut = START + timedelta(days=55)
        truncated = History({key: [c for c in s.candles if c.time < cut] for key, s in HISTORY.series.items()})
        early = run(truncated, S, BEGIN, cut)
        full = [t for t in self.result.trades if t.close_time < cut - timedelta(minutes=30)]
        self.assertEqual([t for t in early.trades if t.close_time < cut - timedelta(minutes=30)], full)

    def test_injected_signal_function_is_used(self):
        r = run(HISTORY, S, BEGIN, BEGIN + timedelta(days=5), signal_fn=lambda *a: None)
        self.assertEqual(r.trades, [])

    def test_costs_reduce_results(self):
        free = run(HISTORY, Settings(slippage_pips_per_side=0.0), BEGIN, END)
        self.assertGreater(sum(t.pl_gbp for t in free.trades), sum(t.pl_gbp for t in self.result.trades))


if __name__ == "__main__":
    unittest.main()
