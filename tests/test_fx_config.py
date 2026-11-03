import unittest
from datetime import time

from fxbot.config import PAIRS, Settings
from fxbot.instruments import INSTRUMENTS, currencies, usd_direction


class SettingsTests(unittest.TestCase):
    def test_defaults_match_spec(self):
        s = Settings()
        self.assertEqual(s.pairs, ("EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD"))
        self.assertEqual((s.h1_window, s.m15_window), (600, 100))
        self.assertEqual((s.ema_fast, s.ema_slow, s.breakout_lookback, s.atr_period), (50, 200, 20, 14))
        self.assertEqual((s.stop_atr_mult, s.reward_r, s.time_exit_minutes), (1.5, 2.0, 120))
        self.assertEqual((s.session_start, s.session_end, s.friday_entry_cutoff), (time(7), time(20), time(18)))
        self.assertEqual((s.slippage_pips_per_side, s.commission_pips_round_trip, s.max_cost_to_stop), (0.3, 0.0, 0.15))
        self.assertEqual((s.risk_per_trade, s.max_open_risk, s.max_usd_direction_risk), (0.0025, 0.01, 0.005))
        self.assertEqual((s.max_positions, s.max_entries_per_pair_per_day, s.daily_loss_limit), (4, 2, 0.015))
        self.assertEqual((s.max_margin_fraction, s.max_gross_notional_multiple), (0.5, 10.0))
        self.assertEqual(s.news_window_minutes, 30)

    def test_inconsistent_windows_rejected(self):
        for bad in (dict(ema_slow=601), dict(atr_period=101), dict(breakout_lookback=99)):
            with self.assertRaises(ValueError, msg=bad):
                Settings(**bad)

    def test_execution_safety_defaults(self):
        s = Settings()
        self.assertEqual((s.price_bound_pips, s.price_max_age_seconds), (1.0, 60))
        self.assertEqual((s.friday_flatten_start, s.friday_flatten_deadline), (time(19, 30), time(20)))

    def test_settings_are_immutable(self):
        with self.assertRaises(Exception):
            Settings().risk_per_trade = 0.025


class InstrumentTests(unittest.TestCase):
    def test_every_pair_has_instrument_facts(self):
        self.assertEqual(set(INSTRUMENTS), set(PAIRS))
        self.assertEqual(INSTRUMENTS["EUR_USD"].pip, 0.0001)
        self.assertEqual(INSTRUMENTS["USD_JPY"].pip, 0.01)

    def test_usd_direction(self):
        # buying a USD-quoted pair sells USD; buying a USD-based pair buys USD
        self.assertEqual(usd_direction("EUR_USD", +1), -1)
        self.assertEqual(usd_direction("EUR_USD", -1), +1)
        self.assertEqual(usd_direction("GBP_USD", +1), -1)
        self.assertEqual(usd_direction("AUD_USD", -1), +1)
        self.assertEqual(usd_direction("USD_JPY", +1), +1)
        self.assertEqual(usd_direction("USD_CAD", -1), -1)

    def test_currencies(self):
        self.assertEqual(currencies("USD_CAD"), ("USD", "CAD"))

    def test_bad_side_rejected(self):
        with self.assertRaises(ValueError):
            usd_direction("EUR_USD", 0)


if __name__ == "__main__":
    unittest.main()
