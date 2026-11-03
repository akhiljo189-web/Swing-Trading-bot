import unittest
from datetime import datetime, timedelta, timezone

from fxbot.broker import Price, is_bot
from fxbot.simbroker import SimBroker

NOW = datetime(2026, 11, 3, 9, 15, tzinfo=timezone.utc)


def broker():
    sim = SimBroker(1000.0, NOW)
    sim.price_data["EUR_USD"] = Price("EUR_USD", NOW - timedelta(seconds=5), 1.10000, 1.10002,
                                      True, 1 / 1.3, 1 / 1.3)
    return sim


class SimBrokerTests(unittest.TestCase):
    def test_fill_attaches_stop_at_distance_from_fill(self):
        sim = broker()
        r = sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.1001)
        self.assertEqual((r.status, r.fill_price, r.units), ("filled", 1.10002, 1000))
        self.assertAlmostEqual(r.stop_price, 1.10002 - 0.0015, places=10)
        self.assertEqual(sim.txns[-1].kind, "FILL")

    def test_sell_fills_at_bid_with_stop_above(self):
        r = broker().market_order("EUR_USD", -1000, "fxb-s", 0.0015, 1.0999)
        self.assertEqual(r.fill_price, 1.10000)
        self.assertAlmostEqual(r.stop_price, 1.1015, places=10)

    def test_price_bound_rejects_excess_slippage(self):
        r = broker().market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.10001)
        self.assertEqual((r.status, r.reason), ("rejected", "BOUNDS_VIOLATION"))

    def test_duplicate_open_client_id_rejected(self):
        sim = broker()
        sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2)
        r = sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2)
        self.assertEqual(r.reason, "CLIENT_ORDER_ID_ALREADY_EXISTS")

    def test_scripted_timeout_and_failures(self):
        sim = broker()
        sim.order_modes = ["timeout_filled", "timeout_lost"]
        self.assertEqual(sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2).status, "unknown")
        self.assertEqual(len(sim.trades), 1)                     # it did fill
        self.assertEqual(sim.market_order("EUR_USD", 1000, "fxb-b", 0.0015, 1.2).status, "unknown")
        self.assertEqual(len(sim.trades), 1)                     # this one never arrived
        sim.fail_next = {"account"}
        with self.assertRaises(Exception):
            sim.account()
        sim.account()                                            # fails only once

    def test_close_records_pl_and_nav(self):
        sim = broker()
        r = sim.market_order("EUR_USD", 1000, "fxb-a", 0.0015, 1.2)
        sim.close_at_broker(r.trade_id, 1.10102)
        self.assertAlmostEqual(sim.txns[-1].amount, 1000 * 0.001 / 1.3, places=9)
        self.assertAlmostEqual(sim.nav, 1000 + 1000 * 0.001 / 1.3, places=9)

    def test_is_bot(self):
        self.assertTrue(is_bot("fxb-EUR_USD-20261103T0900-B"))
        self.assertFalse(is_bot(None))
        self.assertFalse(is_bot("manual"))


if __name__ == "__main__":
    unittest.main()
