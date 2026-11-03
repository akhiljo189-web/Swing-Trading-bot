import unittest
from datetime import datetime, timedelta, timezone

from fxbot.backtest_broker import BacktestBroker
from fxbot.broker import BrokerError
from fxbot.config import Settings
from fxbot.history import HistCandle, History

UTC = timezone.utc
T = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)       # Tuesday
SLIP = 0.00003                                    # 0.3 pip per side
SPREAD = 0.0002


def at(minutes: int) -> datetime:
    return T + timedelta(minutes=minutes)


def c(minutes, o, h, low, close, spread=SPREAD):
    return HistCandle(at(minutes), o, h, low, close, o + spread, h + spread, low + spread, close + spread)


def flat(level, n=8, spread=SPREAD):
    return [c(15 * i, level, level, level, level, spread) for i in range(n)]


def broker(eur, s10=None, settings=Settings()):
    data = {("EUR_USD", "M15"): eur, ("GBP_USD", "M15"): flat(1.25 - 0.0001),   # GBP/USD mid 1.25
            ("USD_JPY", "M15"): flat(150.0 - 0.01, spread=0.02)}                  # USD/JPY mid 150
    return BacktestBroker(History(data), settings, 1000.0, s10)


EUR = [c(0, 1.1000, 1.1005, 1.0995, 1.1000),
       c(15, 1.1000, 1.1004, 1.0998, 1.1001),     # entry candle: decision at 09:15:10
       c(30, 1.1001, 1.1006, 1.0999, 1.1003),
       c(45, 1.1003, 1.1008, 1.1000, 1.1005),
       c(60, 1.1005, 1.1009, 1.1001, 1.1006)]


class ExecutionTests(unittest.TestCase):
    def test_entry_without_s10_uses_next_open_with_double_slippage(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", 1000, "fxb-1", 0.0010, 2.0)
        self.assertAlmostEqual(r.fill_price, 1.1000 + SPREAD + 2 * SLIP, places=10)
        self.assertAlmostEqual(r.stop_price, r.fill_price - 0.0010, places=10)
        self.assertEqual(b.trades[r.trade_id].open_time, at(15) + timedelta(seconds=10))

    def test_entry_with_s10_quote_uses_single_slippage(self):
        b = broker(EUR, s10=lambda pair, t: (1.1001, 1.1003) if t == at(15) + timedelta(seconds=10) else None)
        b.advance(at(15) + timedelta(seconds=10))
        sell = b.market_order("EUR_USD", -1000, "fxb-1", 0.0010, 0.5)
        self.assertAlmostEqual(sell.fill_price, 1.1001 - SLIP, places=10)

    def test_price_bound_rejects(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        self.assertEqual(b.market_order("EUR_USD", 1000, "fxb-1", 0.001, 1.1001).status, "rejected")

    def test_only_completed_candles_and_prices_are_visible(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        self.assertEqual([x.time for x in b.candles("EUR_USD", "M15", 10)], [at(0)])
        p = b.prices(("EUR_USD",))["EUR_USD"]
        self.assertEqual((p.bid, p.time), (1.1000, at(15)))
        self.assertAlmostEqual(p.loss_factor, 1 / 1.25, places=10)

    def test_conversion_factors(self):
        b = broker(EUR)
        b.advance(at(15) + timedelta(seconds=10))
        self.assertAlmostEqual(b.factor("USD_JPY", b.now), 1 / (1.25 * 150.0), places=12)
        with self.assertRaises(BrokerError):
            b.factor("EUR_CHF", b.now)


class ReviewExecutionTests(unittest.TestCase):
    def test_missing_execution_candle_uses_next_candle(self):
        eur = [x for x in EUR if x.time != at(15)]               # no ticks 09:15-09:30
        b = broker(eur)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", 1000, "fxb-1", 0.0010, 2.0)
        self.assertAlmostEqual(r.fill_price, 1.1001 + SPREAD + 2 * SLIP, places=10)   # 09:30 open

    def test_no_price_nearby_rejects_instead_of_raising(self):
        eur = [x for x in EUR if x.time not in (at(15), at(30))]
        b = broker(eur)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", 1000, "fxb-1", 0.0010, 2.0)
        self.assertEqual((r.status, r.reason), ("rejected", "NO_PRICE"))
        self.assertEqual(b.txns[-1].kind, "REJECT")

    def test_close_without_price_returns_false(self):
        b = broker(EUR[:2])                                       # data ends after the entry candle
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", 1000, "fxb-1", 0.0010, 2.0)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertFalse(b.close_trade(r.trade_id))
        self.assertIn(r.trade_id, b.trades)

    def test_margin_rates_follow_uk_rules(self):
        b = broker(EUR)
        self.assertAlmostEqual(b.instrument("EUR_USD").margin_rate, 1 / 30)
        self.assertAlmostEqual(b.instrument("AUD_USD").margin_rate, 0.05)   # AUD is not an ESMA major


class ExitTests(unittest.TestCase):
    def open_long(self, eur, stop_distance=0.0010):
        b = broker(eur)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", 1000, "fxb-1", stop_distance, 2.0)
        return b, r

    def test_stop_exit_pays_slippage(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1002, 1.0990, 1.0992)] + EUR[3:]
        b, r = self.open_long(eur)
        b.advance(at(45) + timedelta(seconds=10))
        closed = b.closed[0]
        self.assertEqual(closed.reason, "stop")
        self.assertAlmostEqual(closed.exit, r.stop_price - SLIP, places=10)
        self.assertAlmostEqual(closed.pl, 1000 * (closed.exit - r.fill_price) / 1.25, places=9)
        self.assertAlmostEqual(b.balance, 1000 + closed.pl, places=9)

    def test_target_exit_at_exact_price(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1040, 1.1000, 1.1030)] + EUR[3:]
        b, r = self.open_long(eur)
        b.set_take_profit(r.trade_id, 1.1030)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual((b.closed[0].reason, b.closed[0].exit), ("target", 1.1030))

    def test_stop_and_target_in_same_candle_assumes_stop(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1040, 1.0980, 1.1000)] + EUR[3:]
        b, r = self.open_long(eur)
        b.set_take_profit(r.trade_id, 1.1030)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "stop")

    def test_gap_through_stop_fills_at_open(self):
        eur = EUR[:2] + [c(30, 1.0980, 1.0985, 1.0975, 1.0982)] + EUR[3:]
        b, r = self.open_long(eur)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "gap")
        self.assertAlmostEqual(b.closed[0].exit, 1.0980 - SLIP, places=10)
        self.assertLess(b.closed[0].exit, r.stop_price - SLIP)          # worse than 1R

    def test_short_stop_uses_ask(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1009, 1.1000, 1.1005)] + EUR[3:]   # ask high 1.1011
        b = broker(eur)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", -1000, "fxb-1", 0.0010, 0.5)      # fill 1.0999 ... stop ~1.1009
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "stop")
        self.assertAlmostEqual(b.closed[0].exit, r.stop_price + SLIP, places=10)

    def test_short_gap_and_short_target(self):
        gap = EUR[:2] + [c(30, 1.1020, 1.1025, 1.1015, 1.1020)] + EUR[3:]      # ask opens 1.1022
        b = broker(gap)
        b.advance(at(15) + timedelta(seconds=10))
        b.market_order("EUR_USD", -1000, "fxb-1", 0.0010, 0.5)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "gap")
        self.assertAlmostEqual(b.closed[0].exit, 1.1022 + SLIP, places=10)
        win = EUR[:2] + [c(30, 1.1000, 1.1001, 1.0960, 1.0970)] + EUR[3:]      # ask low 1.0962
        b = broker(win)
        b.advance(at(15) + timedelta(seconds=10))
        r = b.market_order("EUR_USD", -1000, "fxb-2", 0.0010, 0.5)
        b.set_take_profit(r.trade_id, 1.0970)
        b.advance(at(45) + timedelta(seconds=10))
        self.assertEqual((b.closed[0].reason, b.closed[0].exit), ("target", 1.0970))

    def test_entry_candle_is_never_treated_as_a_gap(self):
        b, r = self.open_long(EUR, stop_distance=0.0001)          # stop above the entry candle's bid open
        b.advance(at(30) + timedelta(seconds=10))
        self.assertEqual(b.closed[0].reason, "stop")
        self.assertAlmostEqual(b.closed[0].exit, r.stop_price - SLIP, places=10)

    def test_no_exit_before_candle_completes(self):
        eur = EUR[:2] + [c(30, 1.1001, 1.1002, 1.0990, 1.0992)] + EUR[3:]
        b, _ = self.open_long(eur)
        b.advance(at(30) + timedelta(seconds=10))     # the 09:30 candle has not closed yet
        self.assertEqual(b.closed, [])

    def test_market_close_and_unrealized_nav(self):
        b, r = self.open_long(EUR)
        b.advance(at(45) + timedelta(seconds=10))
        expected = 1000 + 1000 * (1.1003 - r.fill_price) / 1.25      # marked at last bid close
        self.assertAlmostEqual(b.account().nav, expected, places=9)
        self.assertTrue(b.close_trade(r.trade_id))
        self.assertAlmostEqual(b.closed[0].exit, 1.1003 - 2 * SLIP, places=10)   # 09:45 open bid
        self.assertEqual(b.closed[0].reason, "market")
        self.assertFalse(b.close_trade(r.trade_id))


if __name__ == "__main__":
    unittest.main()
