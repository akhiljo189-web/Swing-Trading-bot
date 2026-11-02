import unittest
from datetime import datetime, timedelta, timezone

from fxbot.config import Settings
from fxbot.costs import passes_cost_filter, round_trip_cost
from fxbot.news import NewsEvent, news_blocked

S = Settings()
AT = datetime(2026, 11, 6, 13, 15, tzinfo=timezone.utc)


class CostTests(unittest.TestCase):
    def test_round_trip_cost_eur_usd(self):
        # 0.6 pip spread + 2 x 0.3 pip slippage = 1.2 pips
        self.assertAlmostEqual(round_trip_cost("EUR_USD", 0.00006, S), 0.00012, places=12)

    def test_round_trip_cost_uses_jpy_pip(self):
        self.assertAlmostEqual(round_trip_cost("USD_JPY", 0.008, S), 0.014, places=12)

    def test_commission_included(self):
        s = Settings(commission_pips_round_trip=0.5)
        self.assertAlmostEqual(round_trip_cost("EUR_USD", 0.00006, s), 0.00017, places=12)

    def test_filter(self):
        self.assertTrue(passes_cost_filter(0.00012, 0.0010, S))    # 12% of stop
        self.assertFalse(passes_cost_filter(0.00012, 0.0006, S))   # 20% of stop
        self.assertTrue(passes_cost_filter(0.15, 1.0, S))          # exactly 15% passes


class NewsTests(unittest.TestCase):
    def test_high_impact_on_either_currency_blocks(self):
        usd = [NewsEvent(AT + timedelta(minutes=15), "USD", "High")]
        eur = [NewsEvent(AT - timedelta(minutes=30), "EUR", "High")]
        self.assertTrue(news_blocked("EUR_USD", AT, usd, S))
        self.assertTrue(news_blocked("EUR_USD", AT, eur, S))   # boundary inclusive

    def test_outside_window_or_other_currency_or_low_impact(self):
        events = [NewsEvent(AT + timedelta(minutes=31), "USD", "High"),
                  NewsEvent(AT, "JPY", "High"),
                  NewsEvent(AT, "USD", "Medium")]
        self.assertFalse(news_blocked("EUR_USD", AT, events, S))


if __name__ == "__main__":
    unittest.main()
