"""Practice-account integration suite (spec §9, Gate A).

Verifies the real OANDA response formats that tests/test_fx_oanda.py assumes. It places and closes
one 1-unit EUR/USD trade on the PRACTICE account. Skipped unless both variables are set:

    OANDA_PRACTICE_TOKEN=...  OANDA_PRACTICE_ACCOUNT=...  python -m unittest tests.test_fx_oanda_integration -v

Run it while the FX market is open, on an account with no other EUR/USD trades.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

from fxbot.broker import is_bot
from fxbot.oanda import OandaBroker

TOKEN, ACCOUNT = os.environ.get("OANDA_PRACTICE_TOKEN"), os.environ.get("OANDA_PRACTICE_ACCOUNT")


@unittest.skipUnless(TOKEN and ACCOUNT, "set OANDA_PRACTICE_TOKEN and OANDA_PRACTICE_ACCOUNT to run")
class PracticeAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.broker = OandaBroker(TOKEN, ACCOUNT, "practice")       # practice only, never live
        assert "fxpractice" in cls.broker.base

    def test_read_endpoints(self):
        b = self.broker
        acct = b.account()
        self.assertGreater(acct.nav, 0)
        self.assertGreater(acct.last_txn_id, 0)
        candles = b.candles("EUR_USD", "M15", 100)
        self.assertEqual(len(candles), 100)
        self.assertTrue(all(c.time.tzinfo is not None for c in candles))
        self.assertEqual(candles[-1].time.minute % 15, 0)
        prices = b.prices(("EUR_USD", "USD_JPY", "USD_CAD", "GBP_USD", "AUD_USD"))
        for p in prices.values():
            self.assertLess(p.bid, p.ask)
            self.assertGreater(p.loss_factor, 0)
            self.assertLess(abs(datetime.now(timezone.utc) - p.time), timedelta(minutes=10))
        info = b.instrument("EUR_USD")
        self.assertGreater(info.margin_rate, 0)
        self.assertGreaterEqual(info.max_units, 1)
        b.open_trades()
        b.pending_orders()
        hist = b.fetch_history("EUR_USD", "H1", datetime.now(timezone.utc) - timedelta(days=3),
                               datetime.now(timezone.utc))
        self.assertGreater(len(hist), 20)

    def test_order_lifecycle(self):
        b = self.broker
        start_txn = b.account().last_txn_id
        cid = f"fxb-TEST-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-B"
        ask = b.prices(("EUR_USD",))["EUR_USD"].ask
        result = b.market_order("EUR_USD", 1, cid, 0.0050, ask + 0.0010)
        self.assertEqual(result.status, "filled", result.reason)
        try:
            self.assertIsNotNone(result.stop_price, "stop-loss was not attached on fill")
            self.assertAlmostEqual(result.fill_price - result.stop_price, 0.0050, places=4)
            self.assertTrue(b.set_take_profit(result.trade_id, result.fill_price + 0.0100))
            trade = [t for t in b.open_trades() if t.trade_id == result.trade_id][0]
            self.assertEqual(trade.client_id, cid)
            self.assertTrue(is_bot(trade.client_id))
            self.assertIsNotNone(trade.take_profit_price)
            self.assertNotIn(cid, [o.client_id for o in b.pending_orders()])   # SL/TP are not standalone
            again = b.market_order("EUR_USD", 1, cid, 0.0050, ask + 0.0010)     # duplicate while open
            self.assertEqual(again.status, "rejected")
        finally:
            self.assertTrue(b.close_trade(result.trade_id))
        bound_cid = cid[:-1] + "X"
        far = b.market_order("EUR_USD", 1, bound_cid, 0.0050, ask - 0.0100)     # price bound cannot be met
        self.assertEqual(far.status, "rejected")
        txns = b.transactions_since(start_txn)
        self.assertIn(bound_cid, [t.client_id for t in txns if t.kind == "REJECT"])
        fills = [t for t in txns if t.kind == "FILL" and t.client_id == cid]
        closes = [t for t in txns if t.kind == "CLOSE" and t.trade_id == result.trade_id]
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(closes), 1)
        self.assertNotEqual(closes[0].amount, None)


if __name__ == "__main__":
    unittest.main()
