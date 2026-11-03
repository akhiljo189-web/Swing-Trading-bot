import unittest
from datetime import datetime

from fx_fixtures import UTC, Harness
from fxbot.config import Settings
from fxbot.engine import weekend_phase

S = Settings()
FRI_1930 = datetime(2026, 11, 6, 19, 30, tzinfo=UTC)   # November: London = UTC
FRI_2000 = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)


class WeekendPhaseTests(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(weekend_phase(datetime(2026, 11, 6, 19, 29, tzinfo=UTC), S), "normal")
        self.assertEqual(weekend_phase(FRI_1930, S), "flatten")
        self.assertEqual(weekend_phase(FRI_2000, S), "deadline")
        self.assertEqual(weekend_phase(datetime(2026, 11, 5, 19, 45, tzinfo=UTC), S), "normal")
        self.assertEqual(weekend_phase(datetime(2026, 7, 10, 18, 30, tzinfo=UTC), S), "flatten")  # BST


class FlattenTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, now=FRI_1930)
        sim = self.h.sim
        for cid, pair, price, stop in (("fxb-1", "EUR_USD", 1.1, 1.099), ("fxb-2", "USD_JPY", 110.0, 109.9)):
            trade = sim.add_trade(pair, 1000, price, cid, stop)
            self.h.ledger.create_intent(cid, pair, 1, FRI_1930, FRI_1930)
            self.h.ledger.mark_filled(cid, trade.trade_id, 1000, price, stop, 0.0, 1.0, 0.1, 800.0, FRI_1930)
        sim.add_trade("GBP_USD", 1000, 1.32, None, 1.31)      # user's own trade: never touched

    def test_retries_until_flat_and_leaves_user_trades(self):
        self.h.sim.close_failures = 1
        self.assertEqual(self.h.engine.flatten(FRI_1930), 1)
        self.assertEqual(self.h.alerts, [])                    # before the deadline: just retry
        self.assertEqual(self.h.engine.flatten(FRI_1930), 0)
        self.assertEqual([t.client_id for t in self.h.sim.trades.values()], [None])

    def test_alerts_if_still_open_at_deadline(self):
        self.h.sim.close_failures = 5
        self.assertEqual(self.h.engine.flatten(FRI_2000), 2)
        self.assertEqual(self.h.alerts[-1][0], "critical")

    def test_broker_down_counts_ledger_positions_as_open(self):
        self.h.sim.fail_next = {"open_trades"}
        self.assertEqual(self.h.engine.flatten(FRI_2000), 2)   # unknown, so assume still open
        self.assertEqual([level for level, _ in self.h.alerts], ["warning", "critical"])


if __name__ == "__main__":
    unittest.main()
