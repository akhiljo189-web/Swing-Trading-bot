import unittest
from datetime import date, datetime, timezone

from fxbot.config import Settings
from fxbot.risk import daily_loss_breached, daily_pnl, uk_trading_day

S = Settings()


class DailyLossTests(unittest.TestCase):
    def test_deposit_is_not_profit(self):
        self.assertAlmostEqual(daily_pnl(1000, 1290, 300, 0), -10.0)

    def test_withdrawal_is_not_loss(self):
        self.assertAlmostEqual(daily_pnl(1000, 900, 0, 100), 0.0)

    def test_breach(self):
        self.assertTrue(daily_loss_breached(1000, 984.9, 0, 0, S))
        self.assertFalse(daily_loss_breached(1000, 985.5, 0, 0, S))

    def test_deposit_cannot_hide_a_breach(self):
        self.assertTrue(daily_loss_breached(1000, 1284.0, 300, 0, S))   # -16 after removing deposit


class TradingDayTests(unittest.TestCase):
    def test_uk_date_in_summer(self):
        self.assertEqual(uk_trading_day(datetime(2026, 7, 7, 23, 30, tzinfo=timezone.utc)), date(2026, 7, 8))

    def test_uk_date_in_winter(self):
        self.assertEqual(uk_trading_day(datetime(2026, 11, 3, 23, 30, tzinfo=timezone.utc)), date(2026, 11, 3))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            uk_trading_day(datetime(2026, 11, 3, 23, 30))


if __name__ == "__main__":
    unittest.main()
