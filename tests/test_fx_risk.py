import unittest
from datetime import datetime, timezone

from fxbot.config import Settings
from fxbot.risk import AccountState, Candidate, OpenPosition, evaluate, size_units
from fxbot.strategy import Signal

S = Settings()
T = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
GBPUSD = 1.30


def candidate(pair="EUR_USD", side=+1, stop=0.0006, cost=0.00008, price=1.10,
              loss_factor=1 / GBPUSD, value_factor=1 / GBPUSD, max_units=100_000_000):
    return Candidate(Signal(pair, side, T, stop), cost, price, loss_factor, value_factor,
                     1 / 30, max_units)


def account(nav=1000.0, margin_used=0.0, positions=(), entries=None, block=None):
    return AccountState(nav, margin_used, tuple(positions), entries or {}, block)


def pos(pair, side, risk=2.5, notional=2000.0):
    return OpenPosition(pair, side, 1000, risk, notional)


class SizingTests(unittest.TestCase):
    def test_eur_usd_worked_example(self):
        # £2.50 / ((0.0006 + 0.00008) / 1.30) = 4779.4 -> 4779 units
        self.assertEqual(size_units(1000, 0.0006, 0.00008, 1 / GBPUSD, 0.0025), 4779)

    def test_usd_jpy_example(self):
        # JPY->GBP factor = 1/(150 * 1.30); £2.50 / (0.135 / 195) = 3611.1 -> 3611
        self.assertEqual(size_units(1000, 0.12, 0.015, 1 / 195, 0.0025), 3611)

    def test_rounds_down_with_float_guard(self):
        self.assertEqual(size_units(1000, 0.0004, 0.0001, 1.0, 0.0025), 5000)

    def test_compounds_with_nav(self):
        self.assertEqual(size_units(2000, 0.0006, 0.00008, 1 / GBPUSD, 0.0025), 9558)


class ApprovalTests(unittest.TestCase):
    def test_approves_base_case_with_cost_split(self):
        d = evaluate(candidate(), account(), S)
        self.assertTrue(d.approved)
        self.assertEqual((d.reason, d.units), ("approved", 4779))
        self.assertAlmostEqual(d.stop_risk_gbp, 4779 * 0.0006 / GBPUSD, places=9)
        self.assertAlmostEqual(d.cost_risk_gbp, 4779 * 0.00008 / GBPUSD, places=9)
        self.assertAlmostEqual(d.risk_gbp, d.stop_risk_gbp + d.cost_risk_gbp, places=9)
        self.assertLessEqual(d.risk_gbp, 2.5)
        self.assertAlmostEqual(d.notional_gbp, 4779 * 1.10 / GBPUSD, places=6)
        self.assertAlmostEqual(d.margin_gbp, d.notional_gbp / 30, places=6)

    def reject(self, cand, acct, reason):
        d = evaluate(cand, acct, S)
        self.assertFalse(d.approved)
        self.assertEqual(d.reason, reason)
        self.assertEqual(d.units, 0)

    def test_accepts_exactly_at_every_cap(self):
        # binary-exact numbers: units = floor(2.5 / 0.5) = 5, risk 2.5, notional 5, margin 2.5
        cand = Candidate(Signal("EUR_USD", +1, T, 0.5), 0.0, 1.0, 1.0, 1.0, 0.5, 1000)
        positions = [OpenPosition("USD_JPY", +1, 1, 5.0, 5000.0),   # long USD
                     OpenPosition("GBP_USD", +1, 1, 2.5, 4995.0)]   # short USD
        # open risk 7.5+2.5=10.0, short-USD 2.5+2.5=5.0, notional 9995+5=10000, margin 497.5+2.5=500
        d = evaluate(cand, account(margin_used=497.5, positions=positions), S)
        self.assertEqual((d.approved, d.units, d.risk_gbp), (True, 5, 2.5))

    def test_empty_block_reason_still_blocks(self):
        self.assertFalse(evaluate(candidate(), account(block=""), S).approved)

    def test_invalid_conversion_factor_rejected(self):
        self.reject(candidate(loss_factor=0.0), account(), "invalid price data")

    def test_block_reason_wins(self):
        self.reject(candidate(), account(block="halted: missing stop"), "halted: missing stop")

    def test_too_small(self):
        self.reject(candidate(stop=10.0), account(), "position below minimum size")

    def test_above_max_units(self):
        self.reject(candidate(max_units=1000), account(), "above instrument maximum units")

    def test_max_positions(self):
        positions = [pos("GBP_USD", 1, 0.5), pos("USD_JPY", 1, 0.5),
                     pos("AUD_USD", -1, 0.5), pos("USD_CAD", -1, 0.5)]
        self.reject(candidate(), account(positions=positions), "max positions")

    def test_one_position_per_pair(self):
        self.reject(candidate(side=-1), account(positions=[pos("EUR_USD", 1)]),
                    "position already open in pair")

    def test_entries_per_pair_per_day(self):
        self.reject(candidate(), account(entries={"EUR_USD": 2}), "max entries for pair today")

    def test_max_open_risk(self):
        positions = [pos("GBP_USD", 1, 4.0), pos("USD_JPY", 1, 4.0)]   # 8.0 + 2.5 > 10.0
        self.reject(candidate(), account(positions=positions), "max open risk")

    def test_usd_direction_cap(self):
        positions = [pos("GBP_USD", 1, 2.5), pos("AUD_USD", 1, 2.5)]   # 5.0 short USD already
        self.reject(candidate(), account(positions=positions), "max USD direction risk")

    def test_opposite_usd_direction_allowed(self):
        positions = [pos("USD_JPY", 1, 2.5), pos("USD_CAD", 1, 2.5)]   # 5.0 long USD
        self.assertTrue(evaluate(candidate(), account(positions=positions), S).approved)

    def test_gross_notional_cap(self):
        positions = [pos("USD_JPY", 1, 0.1, 7000.0)]                   # 7000 + 4044 > 10000
        self.reject(candidate(), account(positions=positions), "max gross notional")

    def test_margin_cap(self):
        self.reject(candidate(), account(margin_used=400.0), "max margin")   # 400 + 135 > 500


if __name__ == "__main__":
    unittest.main()
