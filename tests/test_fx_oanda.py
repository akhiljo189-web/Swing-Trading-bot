import unittest
from datetime import datetime, timezone

from fxbot.broker import BrokerError
from fxbot.http import HttpError
from fxbot.oanda import OandaBroker, parse_time

UTC = timezone.utc
ACCT = "/v3/accounts/001-004-1234567-001"
EUR_INFO = {"instruments": [{"name": "EUR_USD", "displayPrecision": 5, "marginRate": "0.0333",
                             "maximumOrderUnits": "100000000.0"}]}


def candle(t, o, complete=True):
    side = lambda d: {"o": str(o + d), "h": str(o + d + 0.001), "l": str(o + d - 0.001), "c": str(o + d)}
    return {"complete": complete, "time": t, "volume": 10, "bid": side(0), "ask": side(0.0002)}


class FakeTransport:
    """Answers requests from a list of (method, path-prefix, status, body) routes; records calls."""

    def __init__(self, routes):
        self.routes, self.calls = list(routes), []

    def __call__(self, method, url, headers, body, timeout):
        path = url.split(".com", 1)[1]
        self.calls.append((method, path, body, headers))
        for i, (m, prefix, status, data) in enumerate(self.routes):
            if m == method and path.startswith(prefix):
                if isinstance(data, Exception):
                    raise data
                return status, data
        raise AssertionError(f"unexpected request {method} {path}")


def broker(*routes):
    t = FakeTransport(routes)
    return OandaBroker("token", "001-004-1234567-001", "practice", transport=t), t


class ReadTests(unittest.TestCase):
    def test_parse_time_with_nanoseconds(self):
        self.assertEqual(parse_time("2026-11-03T09:00:00.123456789Z"),
                         datetime(2026, 11, 3, 9, 0, 0, 123456, tzinfo=UTC))
        self.assertEqual(parse_time("2026-11-03T09:00:00Z"), datetime(2026, 11, 3, 9, tzinfo=UTC))

    def test_account_and_auth_header(self):
        b, t = broker(("GET", ACCT + "/summary", 200,
                       {"account": {"NAV": "1012.5", "marginUsed": "56.1", "lastTransactionID": "77"}}))
        self.assertEqual((b.account().nav, b.account().last_txn_id), (1012.5, 77))
        self.assertEqual(t.calls[0][3]["Authorization"], "Bearer token")
        self.assertTrue(b.base.startswith("https://api-fxpractice"))

    def test_candles_drop_incomplete_and_derive_mid_from_bid_ask(self):
        b, t = broker(("GET", "/v3/instruments/EUR_USD/candles", 200, {"candles": [
            candle("2026-11-03T08:45:00.000000000Z", 1.1), candle("2026-11-03T09:00:00.000000000Z", 1.2),
            candle("2026-11-03T09:15:00.000000000Z", 1.3, complete=False)]}))
        got = b.candles("EUR_USD", "M15", 2)
        self.assertEqual([c.time.minute for c in got], [45, 0])
        self.assertAlmostEqual(got[1].close, 1.2001, places=10)
        self.assertIn("price=BA", t.calls[0][1])

    def test_prices_use_home_conversion_for_quote_currency(self):
        b, _ = broker(("GET", ACCT + "/pricing", 200, {
            "prices": [{"instrument": "USD_JPY", "time": "2026-11-03T09:15:05.1Z", "tradeable": True,
                        "bids": [{"price": "150.010"}], "asks": [{"price": "150.018"}]}],
            "homeConversions": [{"currency": "JPY", "accountGain": "0.0051", "accountLoss": "0.0052",
                                 "positionValue": "0.00515"}]}))
        p = b.prices(("USD_JPY",))["USD_JPY"]
        self.assertEqual((p.bid, p.ask, p.loss_factor, p.value_factor), (150.010, 150.018, 0.0052, 0.00515))

    def test_missing_price_or_conversion_is_an_error(self):
        b, _ = broker(("GET", ACCT + "/pricing", 200, {"prices": [], "homeConversions": []}))
        with self.assertRaises(BrokerError):
            b.prices(("EUR_USD",))

    def test_open_trades_and_standalone_pending_orders(self):
        b, _ = broker(
            ("GET", ACCT + "/openTrades", 200, {"trades": [
                {"id": "41", "instrument": "EUR_USD", "price": "1.10020", "openTime": "2026-11-03T09:15:10.5Z",
                 "currentUnits": "-1500", "clientExtensions": {"id": "fxb-EUR_USD-20261103T0900-S"},
                 "stopLossOrder": {"price": "1.10170"}},
                {"id": "42", "instrument": "GBP_USD", "price": "1.3", "openTime": "2026-11-03T09:00:00Z",
                 "currentUnits": "100"}]}),
            ("GET", ACCT + "/pendingOrders", 200, {"orders": [
                {"id": "50", "type": "STOP_LOSS", "tradeID": "41"},
                {"id": "51", "type": "LIMIT", "clientExtensions": {"id": "manual"}}]}))
        t1, t2 = b.open_trades()
        self.assertEqual((t1.units, t1.client_id, t1.stop_price, t1.take_profit_price),
                         (-1500, "fxb-EUR_USD-20261103T0900-S", 1.1017, None))
        self.assertIsNone(t2.client_id)
        self.assertEqual([(o.order_id, o.client_id) for o in b.pending_orders()], [("51", "manual")])

    def test_network_failure_and_bad_credentials(self):
        b, _ = broker(("GET", ACCT + "/summary", 0, HttpError("timed out")))
        with self.assertRaises(BrokerError):
            b.account()
        b, _ = broker(("GET", ACCT + "/summary", 401, {"errorMessage": "Insufficient authorization"}))
        with self.assertRaises(BrokerError):
            b.account()
        self.assertTrue(b.auth_failed)

    def test_instrument_info_is_cached(self):
        b, t = broker(("GET", ACCT + "/instruments", 200, EUR_INFO))
        self.assertEqual(b.instrument("EUR_USD").max_units, 100_000_000)
        b.instrument("EUR_USD")
        self.assertEqual(len(t.calls), 1)


class TransactionTests(unittest.TestCase):
    def test_normalises_fills_closes_rejects_cancels_and_transfers(self):
        page = {"transactions": [
            {"id": "100", "type": "MARKET_ORDER", "time": "2026-11-03T09:15:10Z", "instrument": "EUR_USD",
             "clientExtensions": {"id": "fxb-a"}},
            {"id": "101", "type": "ORDER_FILL", "time": "2026-11-03T09:15:10Z", "orderID": "100",
             "instrument": "EUR_USD", "price": "1.10020",
             "tradeOpened": {"tradeID": "101", "units": "2000", "price": "1.10020",
                             "clientExtensions": {"id": "fxb-a"}}},
            {"id": "102", "type": "MARKET_ORDER", "time": "2026-11-03T09:15:11Z", "clientExtensions": {"id": "fxb-b"}},
            {"id": "103", "type": "ORDER_CANCEL", "time": "2026-11-03T09:15:11Z", "orderID": "102",
             "reason": "BOUNDS_VIOLATION"},
            {"id": "104", "type": "MARKET_ORDER_REJECT", "time": "2026-11-03T09:15:12Z", "instrument": "GBP_USD",
             "clientExtensions": {"id": "fxb-c"}, "rejectReason": "INSUFFICIENT_MARGIN"},
            {"id": "105", "type": "ORDER_FILL", "time": "2026-11-03T10:00:00Z", "orderID": "99",
             "instrument": "EUR_USD", "price": "1.10170",
             "tradesClosed": [{"tradeID": "101", "units": "-2000", "realizedPL": "-2.31"}]},
            {"id": "106", "type": "TRANSFER_FUNDS", "time": "2026-11-04T08:00:00Z", "amount": "300.00"},
            {"id": "107", "type": "DAILY_FINANCING", "time": "2026-11-04T21:00:00Z"}]}
        b, t = broker(("GET", ACCT + "/transactions/sinceid?id=99", 200, page),
                      ("GET", ACCT + "/transactions/sinceid?id=107", 200, {"transactions": []}))
        got = b.transactions_since(99)
        self.assertEqual([(x.txn_id, x.kind, x.client_id) for x in got],
                         [(101, "FILL", "fxb-a"), (103, "REJECT", "fxb-b"), (104, "REJECT", "fxb-c"),
                          (105, "CLOSE", None), (106, "TRANSFER", None)])
        self.assertEqual((got[0].units, got[0].price, got[0].trade_id), (2000, 1.1002, "101"))
        self.assertEqual((got[3].trade_id, got[3].amount), ("101", -2.31))
        self.assertEqual(got[4].amount, 300.0)
        self.assertEqual(len(t.calls), 2)                  # paged until empty

    def test_fill_that_closes_and_opens_keeps_the_id_until_the_last_part(self):
        page = {"transactions": [{"id": "200", "type": "ORDER_FILL", "time": "2026-11-03T10:00:00Z",
                                  "instrument": "EUR_USD", "price": "1.1",
                                  "tradesClosed": [{"tradeID": "150", "units": "-10", "realizedPL": "1.0"}],
                                  "tradeOpened": {"tradeID": "200", "units": "10", "clientExtensions": {"id": "fxb-z"}}}]}
        b, _ = broker(("GET", ACCT + "/transactions/sinceid?id=199", 200, page),
                      ("GET", ACCT + "/transactions/sinceid?id=200", 200, {"transactions": []}))
        self.assertEqual([(x.txn_id, x.kind) for x in b.transactions_since(199)], [(199, "CLOSE"), (200, "FILL")])


class WriteTests(unittest.TestCase):
    def test_market_order_body_and_filled_result_with_stop_from_trade(self):
        b, t = broker(
            ("GET", ACCT + "/instruments", 200, EUR_INFO),
            ("POST", ACCT + "/orders", 201, {"orderFillTransaction": {"tradeOpened": {"tradeID": "301", "units": "2000"}}}),
            ("GET", ACCT + "/trades/301", 200, {"trade": {"id": "301", "instrument": "EUR_USD", "price": "1.10022",
                                                         "initialUnits": "2000",
                                                         "stopLossOrder": {"price": "1.09872"}}}))
        r = b.market_order("EUR_USD", 2000, "fxb-x", 0.0015, 1.10030)
        self.assertEqual((r.status, r.trade_id, r.fill_price, r.units, r.stop_price),
                         ("filled", "301", 1.10022, 2000, 1.09872))
        order = [c for c in t.calls if c[0] == "POST"][0][2]["order"]
        self.assertEqual((order["type"], order["timeInForce"], order["units"]), ("MARKET", "FOK", "2000"))
        self.assertEqual((order["priceBound"], order["stopLossOnFill"]["distance"]), ("1.10030", "0.00150"))
        self.assertEqual(order["clientExtensions"]["id"], "fxb-x")
        self.assertEqual(order["tradeClientExtensions"]["id"], "fxb-x")

    def test_cancelled_and_rejected_orders(self):
        b, _ = broker(("GET", ACCT + "/instruments", 200, EUR_INFO),
                      ("POST", ACCT + "/orders", 201, {"orderCancelTransaction": {"reason": "BOUNDS_VIOLATION"}}))
        self.assertEqual(b.market_order("EUR_USD", 1, "fxb-1", 0.001, 1.2).reason, "BOUNDS_VIOLATION")
        b, _ = broker(("GET", ACCT + "/instruments", 200, EUR_INFO),
                      ("POST", ACCT + "/orders", 400, {"orderRejectTransaction": {"rejectReason": "CLIENT_ORDER_ID_ALREADY_EXISTS"}}))
        r = b.market_order("EUR_USD", 1, "fxb-1", 0.001, 1.2)
        self.assertEqual((r.status, r.reason), ("rejected", "CLIENT_ORDER_ID_ALREADY_EXISTS"))

    def test_order_timeout_raises_so_the_engine_treats_it_as_unknown(self):
        b, _ = broker(("GET", ACCT + "/instruments", 200, EUR_INFO),
                      ("POST", ACCT + "/orders", 0, HttpError("timed out")))
        with self.assertRaises(BrokerError):
            b.market_order("EUR_USD", 1, "fxb-1", 0.001, 1.2)

    def test_server_error_on_order_is_unknown_not_rejected(self):
        b, _ = broker(("GET", ACCT + "/instruments", 200, EUR_INFO),
                      ("POST", ACCT + "/orders", 503, {"errorMessage": "busy"}))
        with self.assertRaises(BrokerError):
            b.market_order("EUR_USD", 1, "fxb-1", 0.001, 1.2)

    def test_take_profit_rounded_to_instrument_precision_and_close(self):
        b, t = broker(("GET", ACCT + "/trades/301", 200, {"trade": {"id": "301", "instrument": "EUR_USD"}}),
                      ("GET", ACCT + "/instruments", 200, EUR_INFO),
                      ("PUT", ACCT + "/trades/301/orders", 200, {}),
                      ("PUT", ACCT + "/trades/301/close", 200, {}),
                      ("PUT", ACCT + "/trades/999/close", 404, {"errorMessage": "not found"}))
        self.assertTrue(b.set_take_profit("301", 1.103123456))
        self.assertEqual([c for c in t.calls if c[0] == "PUT"][0][2]["takeProfit"]["price"], "1.10312")
        self.assertTrue(b.close_trade("301"))
        self.assertFalse(b.close_trade("999"))


class HistoryTests(unittest.TestCase):
    def test_fetch_history_pages_and_respects_end(self):
        first = {"candles": [candle(f"2026-01-05T{h:02d}:00:00Z", 1.1) for h in range(3)]}
        b, t = broker(("GET", "/v3/instruments/EUR_USD/candles", 200, first))
        got = b.fetch_history("EUR_USD", "H1", datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 5, 2, tzinfo=UTC))
        self.assertEqual([c.time.hour for c in got], [0, 1])
        self.assertIn("includeFirst=true", t.calls[0][1])

    def test_s10_quote_only_if_that_candle_traded(self):
        b, _ = broker(("GET", "/v3/instruments/EUR_USD/candles", 200,
                       {"candles": [candle("2026-11-03T09:15:10Z", 1.1)]}))
        at = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)
        self.assertEqual(b.s10_quote("EUR_USD", at), (1.1, 1.1002))
        b, _ = broker(("GET", "/v3/instruments/EUR_USD/candles", 200,
                       {"candles": [candle("2026-11-03T09:15:20Z", 1.1)]}))
        self.assertIsNone(b.s10_quote("EUR_USD", at))


if __name__ == "__main__":
    unittest.main()
