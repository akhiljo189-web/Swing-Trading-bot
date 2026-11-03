# FX Bot — Plan 4 of 4: Live Operations

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the verified engine to OANDA and run it unattended on Railway: the OANDA v20 adapter, history download for backtests, the live news feed, Telegram alerts and phone controls, the healthcheck dead-man's switch, the 24/5 scheduler, the full command line, the Docker/Railway deployment, a practice-account integration suite and the setup checklist; and remove the old stocks workbench.

**Architecture:** `OandaBroker` implements Plan 2's `Broker` protocol over a small injectable JSON transport (`http.py`), so it is tested with recorded-style responses and verified against a real practice account by a separate suite. `LiveRunner.step(now)` is a pure scheduling function driven by a one-second loop: trading cycles after each M15 close (with late-candle retries), Friday and on-demand flattening, Telegram polling and the healthcheck ping. Everything external (Telegram, news, healthcheck) degrades safely: failures block new entries or are skipped, never crash the loop.

**Tech Stack:** Python 3.12 standard library (`urllib`, `json`, `zoneinfo`, `argparse`), `unittest`; Docker (`python:3.12-slim`), Railway.

**Spec:** `docs/superpowers/specs/2026-11-02-forex-bot-design.md` §2, §4, §5, §7, §9, §10. Builds on Plans 1–3 — read their "Carried forward" sections.

## Global Constraints

- Everything from Plans 1–3 still applies (Python 3.12, stdlib only, flat `tests/test_fx_*.py`, UTC-aware datetimes, risk numbers, safety rules).
- Live mode requires `OANDA_ENV=live` **and** `LIVE_TRADING_CONFIRMED=yes`; otherwise only the practice host is used. Nothing in this plan sets either.
- Credentials only from environment variables: `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_URL`, `DATA_DIR` (default `/data`), `TRADING_ENABLED` (startup default only).
- Live candles are requested as `BA` and mids derived as (bid + ask) / 2, exactly like backtests (Plan 3 carry-forward).
- `OandaBroker.pending_orders()` returns only standalone orders (not SL/TP attached to trades); `transactions_since()` pages until empty; prices are formatted to the instrument's `displayPrecision`; unknown instruments and missing prices raise `BrokerError` (Plan 2 carry-forwards).
- An order request that times out or returns a server error raises `BrokerError`, so the engine treats it as **unknown** (never as rejected) and reconciles it from transactions.
- Telegram commands are accepted only from `TELEGRAM_CHAT_ID`; `/resume` and `/flatten` need a YES reply within 2 minutes; command state lives in the ledger.
- Repeated identical warnings are sent at most once per hour (Plan 2 carry-forward).
- The scheduler catches every exception from a cycle, halts new entries with a critical alert, and keeps running protection, commands and the healthcheck (Plan 2 carry-forward).
- Healthcheck pings every 5 minutes, weekends included; Telegram polled every 10 s; flatten retried every 60 s.
- One Railway replica with a volume at `/data`; restart on failure.

## Decisions made while writing this plan (review these)

- **No network access in development.** OANDA, Telegram and the news feed cannot be reached from the build environment, so `test_fx_oanda.py` uses responses written from OANDA's v20 documentation as understood. `tests/test_fx_oanda_integration.py` must pass on your practice account before any deployment (setup guide step 2); it is part of Gate A.
- **Stop confirmation after a fill** reads the trade back (`GET /trades/{id}`) because the order response does not include the stop order's price. If that read fails, the engine sees an unknown outcome and reconciles next cycle.
- **Order/cancel mapping:** OANDA reports a price-bound cancellation as `ORDER_CANCEL` referencing the order ID; the adapter remembers `MARKET_ORDER` client IDs to translate cancels into `REJECT`. If the mapping is lost (restart between order and cancel), the intent stays unresolved and the bot halts — the safe direction.
- **One OANDA transaction → several normalised ones:** all but the last carry the previous ID, so a crash mid-way re-reads the transaction (replays are idempotent).
- **News feed:** Forex Factory's free weekly JSON, refreshed hourly (failed fetches retried after 5 minutes); entries are blocked once the last good copy is older than 3 hours.
- **Market hours:** Sunday 17:00 to Friday 17:00 New York time. Outside them no cycles run, but Telegram and the healthcheck continue.
- **Daily summary** is sent at the first loop step of each new UK day; fill and close messages after each cycle.
- **`status`, `pause`, `resume` on the command line** only work while the bot is stopped (it holds the ledger lock); while running, use Telegram.
- **S10 quotes** are cached in a CSV; only the main process fetches them, worker processes get a read-only copy (no concurrent writes or API calls).
- **The old `trading/` stocks workbench is deleted**, as the approved spec says it is replaced. It remains in git history.

## File Structure

| File | Responsibility |
|---|---|
| `fxbot/http.py` | JSON-over-HTTPS transport and `HttpError` |
| `fxbot/oanda.py` | `OandaBroker` (Broker over OANDA v20), history and S10 fetch |
| `fxbot/newsfeed.py` | Live Forex Factory calendar with staleness rule |
| `fxbot/alerts.py` | `Telegram`, `Alerter` (de-duplicated), `Healthcheck` |
| `fxbot/control.py` | Telegram commands with confirmations |
| `fxbot/datafetch.py` | History download, gap report, `S10Cache` |
| `fxbot/live.py` | Config from environment, market hours, status text, `LiveRunner` |
| `fxbot/__main__.py` | All commands |
| `Dockerfile`, `.dockerignore`, `railway.json` | Railway deployment |
| `docs/setup.md`, `README.md` | Setup checklist and overview |
| `tests/fx_telegram.py` | Fake Telegram for tests |

---

### Task 1: HTTP transport and OANDA adapter

**Files:**
- Create: `fxbot/http.py`, `fxbot/oanda.py`
- Test: `tests/test_fx_oanda.py`

**Interfaces:**
- Consumes: Plan 2 broker types (`Account`, `Price`, `InstrumentInfo`, `Trade`, `PendingOrder`, `Txn`, `OrderResult`, `BrokerError`), Plan 3 `HistCandle`.
- Produces: `http.Transport` (`(method, url, headers, body, timeout) -> (status, json)`), `http.HttpError`, `http.urllib_transport`; `oanda.parse_time(str) -> datetime`, `oanda.hist_candle(dict) -> HistCandle`, `OandaBroker(token, account_id, environment="practice", transport=urllib_transport, timeout=10.0)` implementing `Broker`, plus `.auth_failed`, `.fetch_history(pair, granularity, start, end) -> list[HistCandle]`, `.s10_quote(pair, at) -> (bid, ask) | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_fx_oanda.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_oanda.py" -v`
Expected: ERROR — `No module named 'fxbot.http'`

- [ ] **Step 3: Write the implementation**

`fxbot/http.py`:
```python
"""Minimal JSON-over-HTTPS transport (standard library only), injectable for tests."""
import json
import urllib.error
import urllib.request
from collections.abc import Callable

# (method, url, headers, json_body_or_None, timeout_seconds) -> (status_code, parsed_json_or_{})
Transport = Callable[[str, str, dict, dict | None, float], tuple[int, dict]]


class HttpError(Exception):
    """The request did not produce an HTTP response (network failure, timeout, bad JSON)."""


def urllib_transport(method: str, url: str, headers: dict, body: dict | None,
                     timeout: float) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:      # 4xx/5xx still carry a JSON body worth reading
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(str(exc)) from exc
    try:
        return status, (json.loads(raw) if raw else {})
    except ValueError as exc:
        raise HttpError(f"invalid JSON from {url}") from exc
```

`fxbot/oanda.py`:
```python
"""OANDA v20 REST adapter implementing the Broker interface (spec §2, §5).

Everything OANDA-specific is normalised here; the engine never sees OANDA JSON.
The response formats below follow OANDA's v20 documentation as understood when this was written;
`tests/test_fx_oanda_integration.py` verifies them against a real practice account.
"""
from datetime import datetime

from fxbot.broker import (Account, BrokerError, InstrumentInfo, OrderResult, PendingOrder, Price,
                          Trade, Txn)
from fxbot.history import HistCandle
from fxbot.http import HttpError, Transport, urllib_transport
from fxbot.instruments import currencies
from fxbot.strategy import Candle

HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}
MAX_CANDLES = 5000


def parse_time(value: str) -> datetime:
    """OANDA RFC3339 times carry nanoseconds ('...00.000000000Z'); Python takes microseconds."""
    value = value.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        digits = rest[:len(rest) - len(rest.lstrip("0123456789"))]   # fraction only, not the offset
        value = f"{head}.{digits[:6].ljust(6, '0')}{rest[len(digits):]}"
    return datetime.fromisoformat(value)


def _rfc3339(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def hist_candle(c: dict) -> HistCandle:
    b, a = c["bid"], c["ask"]
    return HistCandle(parse_time(c["time"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                      float(a["o"]), float(a["h"]), float(a["l"]), float(a["c"]))


class OandaBroker:
    def __init__(self, token: str, account_id: str, environment: str = "practice",
                 transport: Transport = urllib_transport, timeout: float = 10.0):
        if environment not in HOSTS:
            raise ValueError(f"environment must be one of {sorted(HOSTS)}")
        self.base, self.account_id, self.timeout, self._send = HOSTS[environment], account_id, timeout, transport
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "Accept-Datetime-Format": "RFC3339"}
        self._info: dict[str, dict] = {}
        self._order_client: dict[str, str] = {}   # order ID -> client ID (to map cancels)
        self.auth_failed = False

    # --- plumbing --------------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None,
                 ok: tuple[int, ...] = (200,)) -> tuple[int, dict]:
        try:
            status, data = self._send(method, self.base + path, self._headers, body, self.timeout)
        except HttpError as exc:
            raise BrokerError(f"OANDA unreachable: {exc}") from exc
        if status in (401, 403):
            self.auth_failed = True
            raise BrokerError(f"OANDA rejected the credentials ({status})")
        if status not in ok:
            raise BrokerError(f"OANDA {method} {path} -> {status}: {data.get('errorMessage', '')}")
        return status, data

    def _get(self, path: str) -> dict:
        return self._request("GET", path)[1]

    def _acct(self, suffix: str) -> str:
        return f"/v3/accounts/{self.account_id}{suffix}"

    def _instrument(self, pair: str) -> dict:
        if pair not in self._info:
            found = self._get(self._acct(f"/instruments?instruments={pair}"))["instruments"]
            if not found:
                raise BrokerError(f"unknown instrument {pair}")
            self._info[pair] = found[0]
        return self._info[pair]

    def _fmt(self, pair: str, value: float) -> str:
        return f"{value:.{int(self._instrument(pair)['displayPrecision'])}f}"

    # --- Broker interface: reads -----------------------------------------------------
    def account(self) -> Account:
        a = self._get(self._acct("/summary"))["account"]
        return Account(float(a["NAV"]), float(a["marginUsed"]), int(a["lastTransactionID"]))

    def candles(self, pair: str, granularity: str, count: int) -> list[Candle]:
        """Completed candles only; mid = (bid + ask) / 2 per field, exactly as in backtests."""
        data = self._get(f"/v3/instruments/{pair}/candles?granularity={granularity}&price=BA&count={count + 1}")
        return [hist_candle(c).mid() for c in data["candles"] if c.get("complete")][-count:]

    def prices(self, pairs: tuple[str, ...]) -> dict[str, Price]:
        data = self._get(self._acct(f"/pricing?instruments={','.join(pairs)}&includeHomeConversions=true"))
        conv = {c["currency"]: c for c in data.get("homeConversions", [])}
        out = {}
        for p in data["prices"]:
            quote = currencies(p["instrument"])[1]
            if quote not in conv:
                raise BrokerError(f"no home conversion for {quote}")
            out[p["instrument"]] = Price(p["instrument"], parse_time(p["time"]), float(p["bids"][0]["price"]),
                                         float(p["asks"][0]["price"]), bool(p.get("tradeable")),
                                         float(conv[quote]["accountLoss"]), float(conv[quote]["positionValue"]))
        missing = set(pairs) - set(out)
        if missing:
            raise BrokerError(f"no price for {sorted(missing)}")
        return out

    def instrument(self, pair: str) -> InstrumentInfo:
        i = self._instrument(pair)
        return InstrumentInfo(float(i["marginRate"]), int(float(i["maximumOrderUnits"])))

    def open_trades(self) -> list[Trade]:
        trades = []
        for t in self._get(self._acct("/openTrades"))["trades"]:
            sl, tp = t.get("stopLossOrder"), t.get("takeProfitOrder")
            trades.append(Trade(t["id"], t["instrument"], int(float(t["currentUnits"])), float(t["price"]),
                                parse_time(t["openTime"]), (t.get("clientExtensions") or {}).get("id"),
                                float(sl["price"]) if sl else None, float(tp["price"]) if tp else None))
        return trades

    def pending_orders(self) -> list[PendingOrder]:
        """Standalone orders only: stop-loss / take-profit orders attached to trades are excluded."""
        return [PendingOrder(o["id"], (o.get("clientExtensions") or {}).get("id"))
                for o in self._get(self._acct("/pendingOrders"))["orders"] if "tradeID" not in o]

    def transactions_since(self, txn_id: int) -> list[Txn]:
        """All transactions after `txn_id`, paging until OANDA returns none.

        One OANDA transaction can yield several normalised ones; all but the last carry the
        previous ID, so a crash part-way through re-reads the whole transaction (replays are
        idempotent in the engine).
        """
        out: list[Txn] = []
        last = txn_id
        while True:
            batch = self._get(self._acct(f"/transactions/sinceid?id={last}")).get("transactions", [])
            if not batch:
                return out
            for t in batch:
                out.extend(self._normalise(t))
            last = int(batch[-1]["id"])

    def _normalise(self, t: dict) -> list[Txn]:
        tid, kind, at = int(t["id"]), t["type"], parse_time(t["time"])
        found: list[Txn] = []
        if kind == "MARKET_ORDER" and t.get("clientExtensions"):
            self._order_client[t["id"]] = t["clientExtensions"]["id"]
        elif kind == "ORDER_FILL":
            for closed in t.get("tradesClosed", []):
                found.append(Txn(tid, "CLOSE", at, trade_id=closed["tradeID"], pair=t["instrument"],
                                 units=int(float(closed["units"])), price=float(t["price"]),
                                 amount=float(closed["realizedPL"])))
            opened = t.get("tradeOpened")
            if opened:
                cid = (opened.get("clientExtensions") or {}).get("id") or self._order_client.get(t.get("orderID"))
                found.append(Txn(tid, "FILL", at, client_id=cid, trade_id=opened["tradeID"], pair=t["instrument"],
                                 units=int(float(opened["units"])), price=float(opened.get("price", t["price"]))))
        elif kind == "MARKET_ORDER_REJECT":
            found.append(Txn(tid, "REJECT", at, client_id=(t.get("clientExtensions") or {}).get("id"),
                             pair=t.get("instrument")))
        elif kind == "ORDER_CANCEL" and t.get("orderID") in self._order_client:
            found.append(Txn(tid, "REJECT", at, client_id=self._order_client[t["orderID"]]))
        elif kind == "TRANSFER_FUNDS":
            found.append(Txn(tid, "TRANSFER", at, amount=float(t["amount"])))
        return [Txn(tid - 1 if i < len(found) - 1 else tid, x.kind, x.time, x.client_id, x.trade_id,
                    x.pair, x.units, x.price, x.amount) for i, x in enumerate(found)]

    # --- Broker interface: writes ------------------------------------------------------
    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult:
        ext = {"id": client_id, "tag": "fxbot"}
        body = {"order": {"type": "MARKET", "instrument": pair, "units": str(units), "timeInForce": "FOK",
                          "positionFill": "DEFAULT", "priceBound": self._fmt(pair, price_bound),
                          "stopLossOnFill": {"distance": self._fmt(pair, stop_distance), "timeInForce": "GTC"},
                          "clientExtensions": ext, "tradeClientExtensions": ext}}
        status, data = self._request("POST", self._acct("/orders"), body, ok=(201, 400, 404))
        if "orderFillTransaction" in data and data["orderFillTransaction"].get("tradeOpened"):
            opened = data["orderFillTransaction"]["tradeOpened"]
            trade = self._get(self._acct(f"/trades/{opened['tradeID']}"))["trade"]
            sl = trade.get("stopLossOrder")
            return OrderResult("filled", opened["tradeID"], float(trade["price"]), int(float(trade["initialUnits"])),
                               float(sl["price"]) if sl else None)
        if "orderCancelTransaction" in data:
            return OrderResult("rejected", reason=data["orderCancelTransaction"].get("reason", "CANCELLED"))
        if "orderRejectTransaction" in data:
            return OrderResult("rejected", reason=data["orderRejectTransaction"].get("rejectReason", "REJECTED"))
        return OrderResult("unknown", reason=f"unexpected response {status}")

    def set_take_profit(self, trade_id: str, price: float) -> bool:
        trade = self._get(self._acct(f"/trades/{trade_id}"))["trade"]
        body = {"takeProfit": {"price": self._fmt(trade["instrument"], price), "timeInForce": "GTC"}}
        status, _ = self._request("PUT", self._acct(f"/trades/{trade_id}/orders"), body, ok=(200, 400, 404))
        return status == 200

    def close_trade(self, trade_id: str) -> bool:
        status, _ = self._request("PUT", self._acct(f"/trades/{trade_id}/close"), {"units": "ALL"},
                                  ok=(200, 400, 404))
        return status == 200

    # --- history (backtests) ---------------------------------------------------------
    def fetch_history(self, pair: str, granularity: str, start: datetime, end: datetime) -> list[HistCandle]:
        """Completed bid/ask candles opening in [start, end), paging 5,000 at a time."""
        out: list[HistCandle] = []
        cursor, first = start, True
        while cursor < end:
            data = self._get(f"/v3/instruments/{pair}/candles?granularity={granularity}&price=BA"
                             f"&from={_rfc3339(cursor)}&count={MAX_CANDLES}&includeFirst={str(first).lower()}")
            batch = [hist_candle(c) for c in data["candles"] if c.get("complete")]
            if not batch:
                break
            out.extend(c for c in batch if c.time < end)
            cursor, first = batch[-1].time, False
            if len(data["candles"]) < MAX_CANDLES:
                break
        return out

    def s10_quote(self, pair: str, at: datetime) -> tuple[float, float] | None:
        """(bid, ask) at the open of the 10-second candle starting at `at`, if it traded."""
        data = self._get(f"/v3/instruments/{pair}/candles?granularity=S10&price=BA"
                         f"&from={_rfc3339(at)}&count=1&includeFirst=true")
        if not data["candles"]:
            return None
        c = hist_candle(data["candles"][0])
        return (c.bid_o, c.ask_o) if c.time == at else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_fx_oanda.py" -v`
Expected: 17 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/http.py fxbot/oanda.py tests/test_fx_oanda.py
git commit -m "fxbot: add OANDA v20 adapter"
```

---

### Task 2: Live news feed, Telegram alerts and healthcheck

**Files:**
- Create: `fxbot/newsfeed.py`, `fxbot/alerts.py`, `tests/fx_telegram.py`
- Test: `tests/test_fx_alerts.py`

**Interfaces:**
- Produces: `newsfeed.parse_feed(items) -> list[NewsEvent]`, `newsfeed.fetch_forex_factory(transport) -> list[NewsEvent]`, `LiveNewsFeed(fetch=fetch_forex_factory, refresh=1h, max_age=3h)` callable `(now) -> list[NewsEvent] | None` (the engine's `news` source); `alerts.Telegram(token, chat_id, transport)` with `.send(text) -> bool`, `.updates(offset) -> list[dict]`, `.chat_id`; `alerts.Alerter(telegram | None, clock, dedupe=1h, log=print)` callable `(level, text)` (the engine's `alert`) plus `.info(text)`; `alerts.Healthcheck(url, get_status=http_status)` with `.ping() -> bool`; test helper `fx_telegram.FakeTelegram`, `fx_telegram.msg(update_id, text, chat=42)`.

- [ ] **Step 1: Write the failing tests**

`tests/fx_telegram.py`:
```python
"""Fake Telegram client for control and alert tests (not a test module itself)."""
from fxbot.alerts import Telegram


class FakeTelegram(Telegram):
    """Records sent messages; serves queued updates."""

    def __init__(self, chat_id="42"):
        super().__init__("t", chat_id, transport=None)
        self.sent, self.inbox = [], []

    def send(self, text):
        self.sent.append(text)
        return True

    def updates(self, offset):
        return [u for u in self.inbox if u["update_id"] >= offset]


def msg(update_id, text, chat=42):
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}
```

`tests/test_fx_alerts.py`:
```python
import unittest
from datetime import datetime, timedelta, timezone

from fx_telegram import FakeTelegram
from fxbot.alerts import Alerter, Healthcheck, Telegram
from fxbot.http import HttpError
from fxbot.news import NewsEvent
from fxbot.newsfeed import LiveNewsFeed, fetch_forex_factory, parse_feed

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)


class NewsFeedTests(unittest.TestCase):
    def test_parse_forex_factory_items(self):
        items = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
                  "impact": "High"},
                 {"title": "Bank Holiday", "country": "GBP", "date": "", "impact": "Holiday"}]
        self.assertEqual(parse_feed(items), [NewsEvent(datetime(2026, 11, 6, 13, 30, tzinfo=UTC), "USD", "High")])

    def test_fetch_rejects_bad_responses(self):
        with self.assertRaises(HttpError):
            fetch_forex_factory(lambda *a: (500, {}))
        self.assertEqual(fetch_forex_factory(lambda *a: (200, [])), [])

    def test_keeps_last_good_copy_then_reports_unavailable(self):
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) > 1:
                raise HttpError("down")
            return [NewsEvent(NOW, "USD", "High")]
        feed = LiveNewsFeed(fetch)
        self.assertEqual(len(feed(NOW)), 1)
        self.assertEqual(len(feed(NOW + timedelta(hours=2))), 1)          # refresh failed, copy still fresh
        self.assertIsNone(feed(NOW + timedelta(hours=3, minutes=1)))      # too old: block entries
        self.assertIsNone(LiveNewsFeed(lambda: (_ for _ in ()).throw(HttpError("x")))(NOW))

    def test_failed_refresh_is_not_retried_every_call(self):
        calls = []
        feed = LiveNewsFeed(lambda: calls.append(1) or (_ for _ in ()).throw(HttpError("x")))
        feed(NOW)
        feed(NOW + timedelta(minutes=1))
        feed(NOW + timedelta(minutes=6))
        self.assertEqual(len(calls), 2)


class AlertTests(unittest.TestCase):
    def test_repeated_warnings_are_muted_for_an_hour(self):
        tg, now = FakeTelegram(), [NOW]
        a = Alerter(tg, lambda: now[0], log=lambda s: None)
        a("warning", "paused: untagged trade")
        a("warning", "paused: untagged trade")
        now[0] = NOW + timedelta(minutes=61)
        a("warning", "paused: untagged trade")
        a("critical", "no stop-loss")
        self.assertEqual(tg.sent, ["Warning: paused: untagged trade", "Warning: paused: untagged trade",
                                   "CRITICAL: no stop-loss"])

    def test_telegram_request_shape(self):
        seen = []
        tg = Telegram("abc", "42", transport=lambda *a: seen.append(a) or (200, {"ok": True}))
        self.assertTrue(tg.send("hi"))
        method, url, _, body, _ = seen[0]
        self.assertEqual((method, url, body), ("POST", "https://api.telegram.org/botabc/sendMessage",
                                               {"chat_id": "42", "text": "hi"}))
        failing = Telegram("abc", "42", transport=lambda *a: (_ for _ in ()).throw(HttpError("down")))
        self.assertFalse(failing.send("hi"))

    def test_healthcheck_ping(self):
        self.assertTrue(Healthcheck("https://hc-ping.com/x", get_status=lambda url: 200).ping())
        self.assertFalse(Healthcheck("https://hc-ping.com/x", get_status=lambda url: 0).ping())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_alerts.py" -v`
Expected: ERROR — `No module named 'fxbot.alerts'`

- [ ] **Step 3: Write the implementation**

`fxbot/newsfeed.py`:
```python
"""Live high-impact news calendar from the free Forex Factory weekly feed (spec §2, §3).

If the feed cannot be fetched and the last good copy is too old, the source returns None,
which the engine treats as "news feed unavailable" and blocks new entries.
"""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fxbot.http import HttpError, Transport, urllib_transport
from fxbot.news import NewsEvent

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def parse_feed(items: list) -> list[NewsEvent]:
    events = []
    for item in items:
        try:
            when = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
            events.append(NewsEvent(when, item["country"], item["impact"]))
        except (KeyError, TypeError, ValueError):
            continue   # all-day or malformed entries carry no usable time
    return events


def fetch_forex_factory(transport: Transport = urllib_transport) -> list[NewsEvent]:
    status, data = transport("GET", FF_URL, {"User-Agent": "fxbot"}, None, 15)
    if status != 200 or not isinstance(data, list):
        raise HttpError(f"news feed returned {status}")
    return parse_feed(data)


class LiveNewsFeed:
    def __init__(self, fetch: Callable[[], list[NewsEvent]] = fetch_forex_factory,
                 refresh: timedelta = timedelta(hours=1), max_age: timedelta = timedelta(hours=3)):
        self.fetch, self.refresh, self.max_age = fetch, refresh, max_age
        self.events: list[NewsEvent] = []
        self.fetched_at: datetime | None = None
        self.attempted_at: datetime | None = None

    def __call__(self, now: datetime) -> list[NewsEvent] | None:
        due = self.fetched_at is None or now - self.fetched_at >= self.refresh
        if due and (self.attempted_at is None or now - self.attempted_at >= timedelta(minutes=5)):
            self.attempted_at = now
            try:
                self.events, self.fetched_at = self.fetch(), now
            except Exception:   # keep the last good copy; staleness is checked below
                pass
        if self.fetched_at is None or now - self.fetched_at > self.max_age:
            return None
        return self.events
```

`fxbot/alerts.py`:
```python
"""Telegram messages, de-duplicated warnings, and the healthchecks.io ping (spec §7)."""
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from fxbot.http import HttpError, Transport, urllib_transport


class Telegram:
    def __init__(self, token: str, chat_id: str, transport: Transport = urllib_transport):
        self.base, self.chat_id, self._send = f"https://api.telegram.org/bot{token}", str(chat_id), transport

    def send(self, text: str) -> bool:
        try:
            status, _ = self._send("POST", f"{self.base}/sendMessage", {"Content-Type": "application/json"},
                                   {"chat_id": self.chat_id, "text": text[:4000]}, 10)
        except HttpError:
            return False
        return status == 200

    def updates(self, offset: int) -> list[dict]:
        status, data = self._send("GET", f"{self.base}/getUpdates?offset={offset}&timeout=0", {}, None, 10)
        if status != 200 or not data.get("ok"):
            raise HttpError(f"telegram getUpdates returned {status}")
        return data.get("result", [])


class Alerter:
    """The engine's alert callback. Repeats of the same warning are muted for `dedupe`."""

    def __init__(self, telegram: Telegram | None, clock, dedupe: timedelta = timedelta(hours=1), log=print):
        self.telegram, self.clock, self.dedupe, self.log = telegram, clock, dedupe, log
        self._last: dict[tuple[str, str], datetime] = {}

    def __call__(self, level: str, text: str) -> None:
        now = self.clock()
        self.log(f"{now.isoformat()} {level.upper()} {text}")
        key = (level, text)
        if key in self._last and now - self._last[key] < self.dedupe:
            return
        self._last[key] = now
        if self.telegram:
            self.telegram.send(f"{'CRITICAL' if level == 'critical' else 'Warning'}: {text}")

    def info(self, text: str) -> None:
        self.log(f"{self.clock().isoformat()} INFO {text}")
        if self.telegram:
            self.telegram.send(text)


def http_status(url: str) -> int:
    """GET a URL and return only its status code (healthchecks.io replies with plain text)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fxbot"}),
                                    timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


class Healthcheck:
    """Dead-man's switch: if these pings stop, healthchecks.io alerts the user."""

    def __init__(self, url: str, get_status=http_status):
        self.url, self._get = url, get_status

    def ping(self) -> bool:
        return self._get(self.url) == 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_fx_alerts.py" -v`
Expected: 7 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/newsfeed.py fxbot/alerts.py tests/fx_telegram.py tests/test_fx_alerts.py
git commit -m "fxbot: add live news feed, Telegram alerts and healthcheck"
```

---

### Task 3: Telegram phone controls

**Files:**
- Create: `fxbot/control.py`
- Test: `tests/test_fx_control.py`

**Interfaces:**
- Consumes: `alerts.Telegram`, `Ledger` (`get/set`, `pause`, `resume`, `record`).
- Produces: `Control(telegram, ledger, status: Callable[[], str])` with `.poll(now)` and `.handle(text, now)`; ledger keys `telegram_offset`, `pending_command`, `pending_until`, `flatten_requested`.

- [ ] **Step 1: Write the failing tests**

`tests/test_fx_control.py`:
```python
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fx_telegram import FakeTelegram, msg
from fxbot.control import Control
from fxbot.ledger import Ledger

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)


class ControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Ledger(f"{tmp.name}/l.db")
        self.addCleanup(self.ledger.close)
        self.tg = FakeTelegram()
        self.control = Control(self.tg, self.ledger, lambda: "STATUS")

    def say(self, *texts, chat=42, at=NOW):
        base = int(self.ledger.get("telegram_offset", "0"))
        self.tg.inbox += [msg(base + i, t, chat) for i, t in enumerate(texts)]
        self.control.poll(at)

    def test_status_and_pause(self):
        self.say("/status", "/pause")
        self.assertEqual(self.tg.sent[0], "STATUS")
        self.assertEqual(self.ledger.pause_reason(), "paused by user (Telegram)")

    def test_other_chats_are_ignored(self):
        self.say("/pause", chat=999)
        self.assertIsNone(self.ledger.pause_reason())
        self.assertEqual(self.tg.sent, [])

    def test_resume_needs_yes_within_two_minutes(self):
        self.ledger.halt("halted: x")
        self.say("/resume")
        self.assertEqual(self.ledger.halt_reason(), "halted: x")
        self.say("YES", at=NOW + timedelta(minutes=3))                   # too late
        self.assertEqual(self.ledger.halt_reason(), "halted: x")
        self.say("/resume", "yes")
        self.assertIsNone(self.ledger.halt_reason())

    def test_flatten_sets_request_after_confirmation(self):
        self.say("/flatten")
        self.assertIsNone(self.ledger.get("flatten_requested"))
        self.say("YES")
        self.assertIsNotNone(self.ledger.get("flatten_requested"))

    def test_updates_are_processed_once_across_restarts(self):
        self.say("/pause")
        self.ledger.resume()
        Control(self.tg, self.ledger, lambda: "S").poll(NOW)           # same inbox, new instance
        self.assertIsNone(self.ledger.pause_reason())

    def test_unknown_text_gets_help(self):
        self.say("hello")
        self.assertIn("/status", self.tg.sent[-1])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_control.py" -v`
Expected: ERROR — `No module named 'fxbot.control'`

- [ ] **Step 3: Write the implementation**

`fxbot/control.py`:
```python
"""Phone controls over Telegram (spec §7): /status, /pause, /resume, /flatten.

Only messages from the configured chat are accepted. /resume and /flatten need a "YES" reply
within two minutes. Commands, pending confirmations and the update offset live in the ledger,
so a restart neither loses nor replays them.
"""
from collections.abc import Callable
from datetime import datetime, timedelta

from fxbot.alerts import Telegram
from fxbot.http import HttpError
from fxbot.ledger import Ledger

CONFIRM_WINDOW = timedelta(minutes=2)
HELP = "Commands: /status, /pause, /resume, /flatten"


class Control:
    def __init__(self, telegram: Telegram, ledger: Ledger, status: Callable[[], str]):
        self.telegram, self.ledger, self.status = telegram, ledger, status

    def _reply(self, text: str) -> None:
        self.telegram.send(text)

    def poll(self, now: datetime) -> None:
        offset = int(self.ledger.get("telegram_offset", "0"))
        try:
            updates = self.telegram.updates(offset)
        except HttpError:
            return
        for update in updates:
            self.ledger.set("telegram_offset", str(update["update_id"] + 1))
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id"))
            text = (message.get("text") or "").strip()
            if chat != self.telegram.chat_id:
                self.ledger.record(now, None, "command", f"ignored message from chat {chat}")
                continue
            self.ledger.record(now, None, "command", text[:100])
            self.handle(text, now)

    def handle(self, text: str, now: datetime) -> None:
        command = text.split()[0].lower() if text else ""
        if command == "/status":
            self._reply(self.status())
        elif command == "/pause":
            self.ledger.pause("paused by user (Telegram)")
            self._reply("Paused: no new trades. Open trades stay protected. Send /resume to continue.")
        elif command in ("/resume", "/flatten"):
            self.ledger.set("pending_command", command)
            self.ledger.set("pending_until", (now + CONFIRM_WINDOW).isoformat())
            action = "resume trading" if command == "/resume" else "close ALL bot trades"
            self._reply(f"Reply YES within 2 minutes to {action}.")
        elif command == "yes":
            pending, until = self.ledger.get("pending_command"), self.ledger.get("pending_until")
            self.ledger.set("pending_command", None)
            self.ledger.set("pending_until", None)
            if not pending or now > datetime.fromisoformat(until):
                self._reply("Nothing to confirm (or it expired). " + HELP)
            elif pending == "/resume":
                abandoned = self.ledger.resume()
                note = f" Unconfirmed orders marked abandoned: {', '.join(abandoned)}." if abandoned else ""
                self._reply("Resumed." + note)
            else:
                self.ledger.set("flatten_requested", now.isoformat())
                self._reply("Closing all bot trades now. You will get a message when done.")
        else:
            self._reply(HELP)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_fx_control.py" -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/control.py tests/test_fx_control.py
git commit -m "fxbot: add Telegram phone controls"
```

---

### Task 4: History download, gap report and S10 quote cache

**Files:**
- Create: `fxbot/datafetch.py`
- Modify: `fxbot/research.py` (pass an optional `s10` lookup through every run; full file below)
- Test: `tests/test_fx_datafetch.py`

**Interfaces:**
- Produces: `datafetch.fetch_all(broker, pairs, start, end, out_dir, granularities=("H1", "M15"), log=print)`, `datafetch.gap_report(history, pairs, reference=None) -> list[str]`, `datafetch.S10Cache(path, fetch=None)` callable `(pair, at) -> (bid, ask) | None` with `.read_only()`; `research.chain/walk_forward/no_skill/research` gain an optional `s10` argument (no-skill workers receive `s10.read_only()`), and the report gains `"entry_fills"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_fx_datafetch.py`:
```python
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)


class DataFetchTests(unittest.TestCase):
    def test_s10_cache_fetches_once_and_persists(self):
        from fxbot.datafetch import S10Cache
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            fetch = lambda pair, at: calls.append(at) or ((1.1, 1.1002) if at.minute == 15 else None)  # noqa: E731
            cache = S10Cache(Path(tmp) / "s10.csv", fetch)
            t1, t2 = NOW, NOW + timedelta(minutes=15)
            self.assertEqual(cache("EUR_USD", t1), (1.1, 1.1002))
            self.assertIsNone(cache("EUR_USD", t2))
            cache("EUR_USD", t1)
            self.assertEqual(len(calls), 2)
            reloaded = S10Cache(Path(tmp) / "s10.csv")
            self.assertEqual((reloaded("EUR_USD", t1), reloaded("EUR_USD", t2)), ((1.1, 1.1002), None))
            frozen = cache.read_only()
            self.assertIsNone(frozen.fetch)                          # workers never call the API
            self.assertEqual(frozen("EUR_USD", t1), (1.1, 1.1002))
            self.assertIsNone(frozen("EUR_USD", t1 + timedelta(hours=1)))
            self.assertEqual(len(calls), 2)

    def test_gap_report(self):
        from fxbot.datafetch import gap_report
        from fx_history import make_history
        h = make_history(10)
        s = h.series[("GBP_USD", "M15")]
        dropped = s.candles[:5] + s.candles[8:]
        from fxbot.history import History
        data = {k: v.candles for k, v in h.series.items()}
        data[("GBP_USD", "M15")] = dropped
        lines = gap_report(History(data), ("EUR_USD", "GBP_USD"))
        self.assertEqual(lines, ["GBP_USD 2026-01: 3 M15 candle(s) missing (relative to EUR_USD)"])

    def test_fetch_all_writes_csv_files(self):
        from fxbot.datafetch import fetch_all
        from fxbot.history import load
        from fx_history import make_history
        h = make_history(3)

        class FakeBroker:
            def fetch_history(self, pair, g, start, end):
                return h.series[(pair, g)].candles
        with tempfile.TemporaryDirectory() as tmp:
            fetch_all(FakeBroker(), ("EUR_USD",), NOW, NOW, Path(tmp), log=lambda s: None)
            self.assertEqual(load(Path(tmp) / "EUR_USD_M15.csv"), h.series[("EUR_USD", "M15")].candles)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_datafetch.py" -v`
Expected: ERROR — `No module named 'fxbot.datafetch'`

- [ ] **Step 3: Write the implementation**

`fxbot/datafetch.py`:
```python
"""Download OANDA history for backtests, report gaps, and cache S10 entry quotes (spec §8)."""
import csv
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fxbot.history import History, save

S10Fetch = Callable[[str, datetime], tuple[float, float] | None]


def fetch_all(broker, pairs: tuple[str, ...], start: datetime, end: datetime, out_dir: Path,
              granularities: tuple[str, ...] = ("H1", "M15"), log=print) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for pair in pairs:
        for g in granularities:
            candles = broker.fetch_history(pair, g, start, end)
            save(out_dir / f"{pair}_{g}.csv", candles)
            log(f"{pair} {g}: {len(candles)} candles")


def gap_report(history: History, pairs: tuple[str, ...], reference: str | None = None) -> list[str]:
    """M15 candles present for the reference pair but missing for another pair, per month."""
    reference = reference or pairs[0]
    ref = set(history.get(reference, "M15").times)
    lines = []
    for pair in pairs:
        if pair == reference:
            continue
        missing = ref - set(history.get(pair, "M15").times)
        for month, n in sorted(Counter(t.strftime("%Y-%m") for t in missing).items()):
            lines.append(f"{pair} {month}: {n} M15 candle(s) missing (relative to {reference})")
    return lines


class S10Cache:
    """(pair, time) -> (bid, ask) or None, stored in a CSV so each quote is fetched once."""

    def __init__(self, path: Path, fetch: S10Fetch | None = None):
        self.path, self.fetch = Path(path), fetch
        self.quotes: dict[tuple[str, str], tuple[float, float] | None] = {}
        if self.path.exists():
            with open(self.path, newline="") as f:
                for row in csv.DictReader(f):
                    self.quotes[(row["pair"], row["time"])] = (
                        (float(row["bid"]), float(row["ask"])) if row["bid"] else None)

    def read_only(self) -> "S10Cache":
        """A copy that never fetches (for parallel worker processes)."""
        copy = S10Cache.__new__(S10Cache)
        copy.path, copy.fetch, copy.quotes = self.path, None, dict(self.quotes)
        return copy

    def __call__(self, pair: str, at: datetime) -> tuple[float, float] | None:
        key = (pair, at.isoformat())
        if key in self.quotes or self.fetch is None:
            return self.quotes.get(key)
        try:
            quote = self.fetch(pair, at)
        except Exception:   # not cached: a later run may succeed
            return None
        self.quotes[key] = quote
        new = not self.path.exists()
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["pair", "time", "bid", "ask"])
            w.writerow([pair, at.isoformat(), *(quote if quote else ("", ""))])
        return quote
```

Replace the whole of `fxbot/research.py`:
```python
"""Research report: walk-forward, variants, baselines and the Gate B check (spec §8, §10)."""
import calendar
import gc
import itertools
import json
import multiprocessing
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path

from fxbot.backtest import BacktestResult, run
from fxbot.config import Settings
from fxbot.history import History
from fxbot.indicators import atr
from fxbot.metrics import summarize
from fxbot.news import NewsEvent
from fxbot.strategy import LONDON, Signal, trend_direction

GRID = {"stop_atr_mult": (1.0, 1.5, 2.0), "breakout_lookback": (20, 30)}
SLIPPAGE = (0.0, 0.3, 0.6, 1.0)
SESSION_OPENS = (time(7, 0), time(13, 0))   # London and New York opens, London time
M15 = timedelta(minutes=15)


# --- baselines -----------------------------------------------------------------
def trend_baseline(pair, h1, m15, s: Settings) -> Signal | None:
    """Same H1 trend filter; enters at the first M15 close of each session, no breakout needed."""
    if len(h1) < s.h1_window or len(m15) < s.m15_window:
        return None
    h1, m15 = h1[-s.h1_window:], m15[-s.m15_window:]
    if (m15[-1].time + M15).astimezone(LONDON).time() not in SESSION_OPENS:
        return None
    if h1[-1].time + timedelta(hours=1) > m15[-1].time + M15:
        return None
    side = trend_direction(h1, s)
    if side == 0:
        return None
    a = atr([c.high for c in m15], [c.low for c in m15], [c.close for c in m15], s.atr_period)[-1]
    return Signal(pair, side, m15[-1].time, s.stop_atr_mult * a)


def random_direction(base: dict, seed: int):
    """No-skill baseline: the strategy's own entry times, coin-flip direction."""
    rng = random.Random(seed)

    def fn(pair, h1, m15, s):
        sig = base.get((pair, m15[-1].time)) if m15 else None
        return None if sig is None else replace(sig, side=rng.choice((1, -1)))
    return fn


# --- walk-forward ---------------------------------------------------------------
def add_months(t: datetime, months: int) -> datetime:
    y, m = divmod(t.month - 1 + months, 12)
    year, month = t.year + y, m + 1
    return t.replace(year=year, month=month, day=min(t.day, calendar.monthrange(year, month)[1]))


def windows(start: datetime, end: datetime, train: int, test: int) -> list[tuple]:
    """(train_start, train_end, test_start, test_end) with non-overlapping test windows."""
    out, s = [], start
    while add_months(s, train) < end:
        te_s = add_months(s, train)
        out.append((s, te_s, te_s, min(add_months(te_s, test), end)))
        s = add_months(s, test)
    return out


def grid_settings(settings: Settings, grid: dict) -> list[Settings]:
    keys = list(grid)
    return [replace(settings, **dict(zip(keys, values))) for values in itertools.product(*grid.values())]


def chain(history, plan: list[tuple], news, nav: float = 1000.0, signal_fn=None, s10=None) -> BacktestResult:
    """Run consecutive (settings, start, end) windows, carrying NAV forward; merge results."""
    merged = BacktestResult(nav, nav, [], [])
    for settings, start, end in plan:
        extra = {"signal_fn": signal_fn} if signal_fn is not None else {}
        r = run(history, settings, start, end, nav=merged.end_nav, news=news, s10=s10, **extra)
        merged.end_nav = r.end_nav
        merged.trades += r.trades
        merged.nav_marks += r.nav_marks
        merged.signals.update(r.signals)
        merged.rejections += r.rejections
        merged.blocked += r.blocked
        merged.alerts += r.alerts
        merged.halted = merged.halted or r.halted
        merged.open_at_end += r.open_at_end
    return merged


def walk_forward(history, settings, start, end, news, train_months=24, test_months=6,
                 grid=GRID, min_trades=30, s10=None) -> list[tuple[Settings, datetime, datetime]]:
    """Pick the best grid settings on each training window (expectancy R, at least `min_trades`)."""
    plan = []
    for tr_s, tr_e, te_s, te_e in windows(start, end, train_months, test_months):
        best, best_score = settings, float("-inf")
        for candidate in grid_settings(settings, grid):
            m = summarize(run(history, candidate, tr_s, tr_e, news=news, s10=s10), bootstrap_runs=0)
            score = m["expectancy_r"] if m["trades"] >= min_trades else float("-inf")
            if score > best_score:
                best, best_score = candidate, score
        plan.append((best, te_s, te_e))
    return plan


# --- no-skill distribution (parallel) ------------------------------------------
_WORKER: dict = {}


def _init_worker(history, plan, news, base, s10=None):
    _WORKER.update(history=history, plan=plan, news=news, base=base, s10=s10)


def _no_skill_run(seed: int) -> float:
    w = _WORKER
    r = chain(w["history"], w["plan"], w["news"], signal_fn=random_direction(w["base"], seed), s10=w.get("s10"))
    return summarize(r, bootstrap_runs=0)["expectancy_r"]


def no_skill(history, plan, news, base, runs: int, processes: int | None, s10=None) -> list[float]:
    """Expectancy (R) of `runs` seeded random-direction runs over the walk-forward windows.

    Workers get a read-only S10 cache: they never call the API or write the cache file.
    """
    if s10 is not None and hasattr(s10, "read_only"):
        s10 = s10.read_only()
    if processes == 1:
        _init_worker(history, plan, news, base, s10)
        return [_no_skill_run(seed) for seed in range(runs)]
    methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in methods else None)
    gc.freeze()   # keep forked workers from copying the shared history pages
    try:
        with ProcessPoolExecutor(processes, mp_context=context, initializer=_init_worker,
                                 initargs=(history, plan, news, base, s10)) as pool:
            return list(pool.map(_no_skill_run, range(runs), chunksize=8))
    finally:
        gc.unfreeze()


# --- Gate B and report -------------------------------------------------------------
def gate_b(report: dict) -> dict:
    wf, wf06 = report["walk_forward"], report["walk_forward_slippage_0.6"]
    ns = sorted(report["no_skill"]["expectancy_r"])
    p95 = ns[int(0.95 * (len(ns) - 1))] if ns else None
    checks = {
        "oos_expectancy_positive": wf["expectancy_r"] > 0 and wf["net_gbp"] > 0,
        "oos_trades_at_least_100": wf["trades"] >= 100,
        "oos_trades_preferred_200": wf["trades"] >= 200,
        "positive_at_0.6_pips_per_side": wf06["expectancy_r"] > 0 and wf06["net_gbp"] > 0,
        "beats_trend_baseline": wf["expectancy_r"] > report["trend_baseline"]["expectancy_r"],
        "beats_no_skill_95th_percentile": p95 is not None and wf["expectancy_r"] > p95,
        "max_drawdown_below_10pct": wf["max_drawdown"] < 0.10,
        "no_halts": wf["halted"] is None,
    }
    required = [k for k in checks if k != "oos_trades_preferred_200"]
    return {"checks": checks, "no_skill_p95_expectancy_r": p95,
            "passed": all(checks[k] for k in required),
            "manual_review": "Check robustness across periods and pairs (per_year, per_pair) "
                             "and the confidence interval before treating a pass as evidence."}


def research(history: History, settings: Settings, start: datetime, end: datetime,
             news: list[NewsEvent] | None, *, train_months=24, test_months=6, grid=GRID,
             no_skill_runs=1000, processes: int | None = None, s10=None) -> dict:
    """Every comparison in Gate B uses the same walk-forward windows and chosen settings."""
    plan = walk_forward(history, settings, start, end, news, train_months, test_months, grid, s10=s10)
    if not plan:
        raise ValueError("period too short for one training plus one test window")
    oos_start = plan[0][1]

    def variant(**changes) -> BacktestResult:
        return chain(history, [(replace(s, **changes), a, b) for s, a, b in plan], news, s10=s10)

    wf = chain(history, plan, news, s10=s10)
    sensitivity = {str(sl): summarize(variant(slippage_pips_per_side=sl), bootstrap_runs=0)
                   for sl in SLIPPAGE}
    report = {
        "period": {"start": start.isoformat(), "oos_start": oos_start.isoformat(), "end": end.isoformat()},
        "walk_forward_params": [{"start": a.isoformat(), "end": b.isoformat(),
                                 **{k: getattr(s, k) for k in grid}} for s, a, b in plan],
        "walk_forward": summarize(wf),
        "walk_forward_slippage_0.6": sensitivity["0.6"],
        "slippage_sensitivity": sensitivity,
        "time_exit_off": summarize(variant(time_exit_minutes=None), bootstrap_runs=0),
        "news_filter_off": summarize(chain(history, plan, None, s10=s10), bootstrap_runs=0),
        "trend_baseline": summarize(chain(history, plan, news, signal_fn=trend_baseline, s10=s10),
                                    bootstrap_runs=0),
        "no_skill": {"runs": no_skill_runs,
                     "expectancy_r": no_skill(history, plan, news, wf.signals, no_skill_runs, processes, s10)},
        "fixed_params": summarize(run(history, settings, oos_start, end, news=news, s10=s10), bootstrap_runs=0),
        "entry_fills": "S10 quotes where available" if s10 is not None else "next M15 open + double slippage",
    }
    report["gate_b"] = gate_b(report)
    return report


def _clean(value):
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def write_report(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "research.json", out_dir / "research.md"
    json_path.write_text(json.dumps(_clean(report), indent=2, default=str))
    wf, g = report["walk_forward"], report["gate_b"]
    lines = [
        "# Backtest research report", "",
        "Research output, not evidence of future profitability.", "",
        f"Period {report['period']['start']} to {report['period']['end']}; "
        f"out-of-sample from {report['period']['oos_start']}.", "",
        "## Walk-forward (out of sample)", "",
        f"- Trades: {wf['trades']}",
        f"- Expectancy: {wf['expectancy_r']:.3f} R (95% CI by trading day: {wf['expectancy_r_ci95']})",
        f"- Net: £{wf['net_gbp']:.2f}; profit factor: {wf['profit_factor']}",
        f"- Max drawdown: {wf['max_drawdown']:.1%}; worst losing streak: {wf['worst_losing_streak']}",
        f"- Trades left open at the end (P&L in NAV but not in trade stats): {wf['open_at_end']}",
        "", "## Slippage sensitivity (walk-forward settings)", "", "| Pips per side | Trades | Expectancy R |",
        "|---|---|---|",
        *[f"| {k} | {v['trades']} | {v['expectancy_r']:.3f} |" for k, v in report["slippage_sensitivity"].items()],
        "", "## Baselines (same windows, settings and costs)", "",
        f"- Trend baseline: {report['trend_baseline']['trades']} trades, "
        f"{report['trend_baseline']['expectancy_r']:.3f} R",
        f"- No-skill ({report['no_skill']['runs']} runs): 95th percentile {g['no_skill_p95_expectancy_r']} R",
        "", "## Variants", "", "| Variant | Trades | Expectancy R | Max drawdown |", "|---|---|---|---|",
        *[f"| {name} | {report[key]['trades']} | {report[key]['expectancy_r']:.3f} | {report[key]['max_drawdown']:.1%} |"
          for name, key in (("Time exit off", "time_exit_off"), ("News filter off", "news_filter_off"),
                            ("Fixed default parameters", "fixed_params"))],
        "", "## Per pair (walk-forward)", "", "| Pair | Trades | Net £ | Expectancy R |", "|---|---|---|---|",
        *[f"| {p} | {v['trades']} | {v['net_gbp']:.2f} | {v['expectancy_r']:.3f} |" for p, v in wf["per_pair"].items()],
        "", "## Per year (walk-forward)", "", "| Year | Trades | Net £ | Expectancy R |", "|---|---|---|---|",
        *[f"| {y} | {v['trades']} | {v['net_gbp']:.2f} | {v['expectancy_r']:.3f} |" for y, v in wf["per_year"].items()],
        "", "Caveats: GBP conversion uses mid rates (no conversion spread); overnight financing is not "
        "modelled (matters only for the time-exit-off variant).",
        "", "## Gate B", "", "| Check | Result |", "|---|---|",
        *[f"| {k} | {'PASS' if v else 'FAIL'} |" for k, v in g["checks"].items()],
        "", f"**Gate B automated checks: {'PASSED' if g['passed'] else 'NOT PASSED'}.** {g['manual_review']}", "",
    ]
    md_path.write_text("\n".join(lines))
    return json_path, md_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -v`
Expected: all OK (the existing research tests still pass)

- [ ] **Step 5: Commit**

```bash
git add fxbot/datafetch.py fxbot/research.py tests/test_fx_datafetch.py
git commit -m "fxbot: add history download, gap report and S10 quote cache"
```

---

### Task 5: Live scheduler and configuration

**Files:**
- Create: `fxbot/live.py`
- Test: `tests/test_fx_live.py`

**Interfaces:**
- Consumes: `Engine`, `Ledger`, `Settings`, `Alerter`, `Control`, `Healthcheck`, `weekend_phase`, `uk_trading_day`.
- Produces: `ConfigError`, `LiveConfig(token, account_id, environment, data_dir, trading_enabled, telegram_token, telegram_chat_id, healthcheck_url)`, `load_config(env=os.environ) -> LiveConfig`, `market_open(now) -> bool`, `status_text(ledger) -> str`, `LiveRunner(engine, ledger, settings, alerter, control=None, health=None, broker=None)` with `.step(now)` and `.run_forever(clock, sleep)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_fx_live.py`:
```python
import unittest
from datetime import datetime, timedelta, timezone

from fx_fixtures import NOW, Harness, set_market
from fxbot.alerts import Alerter
from fxbot.config import PAIRS, Settings
from fxbot.live import ConfigError, LiveRunner, load_config, market_open, status_text

UTC = timezone.utc
S = Settings()
ENV = {"OANDA_API_TOKEN": "t", "OANDA_ACCOUNT_ID": "001"}


class ConfigTests(unittest.TestCase):
    def test_defaults_to_practice(self):
        cfg = load_config(ENV)
        self.assertEqual((cfg.environment, cfg.data_dir, cfg.trading_enabled), ("practice", "/data", True))

    def test_live_needs_explicit_confirmation(self):
        with self.assertRaises(ConfigError):
            load_config({**ENV, "OANDA_ENV": "live"})
        self.assertEqual(load_config({**ENV, "OANDA_ENV": "live", "LIVE_TRADING_CONFIRMED": "yes"}).environment, "live")

    def test_invalid_configs(self):
        for env in ({}, {**ENV, "OANDA_ENV": "demo"}, {**ENV, "TELEGRAM_BOT_TOKEN": "x"}):
            with self.assertRaises(ConfigError, msg=env):
                load_config(env)
        self.assertFalse(load_config({**ENV, "TRADING_ENABLED": "false"}).trading_enabled)


class MarketHoursTests(unittest.TestCase):
    def test_market_hours(self):
        self.assertTrue(market_open(NOW))                                              # Tuesday
        self.assertFalse(market_open(datetime(2026, 11, 7, 12, tzinfo=UTC)))           # Saturday
        self.assertFalse(market_open(datetime(2026, 11, 6, 22, 30, tzinfo=UTC)))       # Fri 17:30 New York
        self.assertTrue(market_open(datetime(2026, 11, 6, 21, 30, tzinfo=UTC)))
        self.assertTrue(market_open(datetime(2026, 11, 8, 22, 30, tzinfo=UTC)))        # Sun 17:30 New York
        self.assertFalse(market_open(datetime(2026, 11, 8, 21, 30, tzinfo=UTC)))


class Recorder:
    def __init__(self):
        self.messages = []

    def __call__(self, text):
        self.messages.append(text)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.log = Recorder()
        self.alerter = Alerter(None, lambda: self.now, log=self.log)
        self.now = NOW
        self.cycles = []
        real = self.h.engine.run_cycle
        self.h.engine.run_cycle = lambda now: self.cycles.append(now) or real(now)
        self.runner = LiveRunner(self.h.engine, self.h.ledger, S, self.alerter, broker=self.h.sim)

    def step(self, at):
        self.now = at
        self.h.sim.now = at
        self.runner.step(at)

    def test_one_cycle_per_candle_close(self):
        self.step(NOW - timedelta(seconds=5))          # 09:15:05: too early (needs close + 10 s)
        self.step(NOW)
        self.step(NOW + timedelta(seconds=30))
        self.assertEqual(self.cycles, [NOW])

    def test_late_candles_are_retried_for_up_to_a_minute(self):
        later = NOW + timedelta(minutes=15)            # 09:30:10, but data still ends at 09:15
        for s in range(0, 90, 5):
            self.step(later + timedelta(seconds=s))
        self.assertEqual(len(self.cycles), 7)          # +0, +10 ... +60 s, then gives up until next close
        self.assertEqual(self.cycles[-1], later + timedelta(seconds=60))

    def test_unexpected_error_halts_but_loop_survives(self):
        self.h.engine.run_cycle = lambda now: 1 / 0
        self.step(NOW)
        self.assertTrue(self.h.ledger.halt_reason().startswith("halted: unexpected error"))
        self.assertIn("CRITICAL", self.log.messages[-1])
        self.step(NOW + timedelta(minutes=15))         # still stepping

    def test_fill_and_close_notifications(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        self.step(NOW)
        self.assertTrue(any("Opened EUR_USD long" in m for m in self.log.messages))
        p = self.h.ledger.open_positions()[0]
        self.h.sim.close_at_broker(p.trade_id, p.tp_price)
        for pair in PAIRS:
            set_market(self.h.sim, pair, now=NOW + timedelta(minutes=15))
        self.step(NOW + timedelta(minutes=15))
        self.assertTrue(any("Closed EUR_USD: P&L" in m for m in self.log.messages))

    def test_confirmed_flatten_closes_and_clears_request(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        self.step(NOW)
        self.h.ledger.set("flatten_requested", NOW.isoformat())
        self.step(NOW + timedelta(seconds=20))
        self.assertEqual(self.h.sim.trades, {})
        self.assertIsNone(self.h.ledger.get("flatten_requested"))
        self.assertTrue(any("Flatten complete" in m for m in self.log.messages))

    def test_friday_flatten_window(self):
        friday = datetime(2026, 11, 6, 19, 30, 10, tzinfo=UTC)
        self.h.sim.now = friday - timedelta(minutes=30)          # opened 30 min earlier: no time exit yet
        self.h.sim.add_trade("EUR_USD", 1000, 1.1, "fxb-x", 1.099)
        self.step(friday - timedelta(minutes=5))                 # 19:25: before the flatten window
        self.assertEqual(len(self.h.sim.trades), 1)
        self.step(friday)
        self.assertEqual(self.h.sim.trades, {})

    def test_auth_failure_halts(self):
        self.h.sim.auth_failed = True
        self.step(NOW)
        self.assertEqual(self.h.ledger.halt_reason(), "halted: OANDA rejected the credentials")

    def test_daily_summary_on_new_uk_day(self):
        self.step(NOW)
        self.step(NOW + timedelta(days=1))
        self.assertTrue(any(m.endswith("Open now: 0.") and "Daily summary 2026-11-03" in m for m in self.log.messages))

    def test_healthcheck_every_five_minutes_even_at_weekends(self):
        pings = []

        class HC:
            def ping(self):
                pings.append(1)
        self.runner.health = HC()
        saturday = datetime(2026, 11, 7, 12, tzinfo=UTC)
        for minute in range(0, 16):
            self.step(saturday + timedelta(minutes=minute))
        self.assertEqual(len(pings), 4)                # 0, 5, 10, 15
        self.assertEqual(self.cycles, [])              # no trading while closed

    def test_status_text(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        self.step(NOW)
        text = status_text(self.h.ledger)
        self.assertIn("Open bot trades: 1", text)
        self.assertIn("EUR_USD long", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_live.py" -v`
Expected: ERROR — `No module named 'fxbot.live'`

- [ ] **Step 3: Write the implementation**

`fxbot/live.py`:
```python
"""Live scheduler for Railway (spec §5, §7): one process, a loop that wakes every second.

- Trading cycle 10 s after each M15 close while the market is open; while candles are late
  ("stale candles"), retried every 10 s for up to 60 s.
- Friday from 19:30 London, and after a confirmed /flatten: flatten every 60 s until flat.
- Telegram commands every 10 s; healthcheck ping every 5 minutes, weekends included.
- Any unexpected exception from a cycle halts new entries and alerts; the loop keeps running
  so protection, commands and the healthcheck continue.
"""
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fxbot.alerts import Alerter, Healthcheck
from fxbot.config import Settings
from fxbot.control import Control
from fxbot.engine import Engine, weekend_phase
from fxbot.ledger import Ledger
from fxbot.risk import uk_trading_day

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
M15 = timedelta(minutes=15)
DELAY = timedelta(seconds=10)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class LiveConfig:
    token: str
    account_id: str
    environment: str
    data_dir: str
    trading_enabled: bool
    telegram_token: str | None
    telegram_chat_id: str | None
    healthcheck_url: str | None


def load_config(env=os.environ) -> LiveConfig:
    environment = env.get("OANDA_ENV", "practice")
    if environment not in ("practice", "live"):
        raise ConfigError("OANDA_ENV must be 'practice' or 'live'")
    if environment == "live" and env.get("LIVE_TRADING_CONFIRMED") != "yes":
        raise ConfigError("OANDA_ENV=live also needs LIVE_TRADING_CONFIRMED=yes (set only after Gate D)")
    missing = [k for k in ("OANDA_API_TOKEN", "OANDA_ACCOUNT_ID") if not env.get(k)]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    if bool(env.get("TELEGRAM_BOT_TOKEN")) != bool(env.get("TELEGRAM_CHAT_ID")):
        raise ConfigError("set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither")
    return LiveConfig(env["OANDA_API_TOKEN"], env["OANDA_ACCOUNT_ID"], environment,
                      env.get("DATA_DIR", "/data"), env.get("TRADING_ENABLED", "true").lower() != "false",
                      env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID"), env.get("HEALTHCHECK_URL"))


def market_open(now: datetime) -> bool:
    """FX trades from Sunday 17:00 to Friday 17:00 New York time."""
    ny = now.astimezone(NEW_YORK)
    if ny.weekday() == 5:
        return False
    if ny.weekday() == 4 and ny.hour >= 17:
        return False
    if ny.weekday() == 6 and ny.hour < 17:
        return False
    return True


def status_text(ledger: Ledger) -> str:
    lines = [f"Halted: {ledger.halt_reason()}" if ledger.halt_reason() else "Not halted",
             f"Paused: {ledger.pause_reason()}" if ledger.pause_reason() else "Not paused"]
    positions = ledger.open_positions()
    lines.append(f"Open bot trades: {len(positions)}")
    for p in positions:
        lines.append(f"  {p.pair} {'long' if p.side > 0 else 'short'} {p.units} @ {p.fill_price} "
                     f"stop {p.stop_price} target {p.tp_price} risk £{p.risk_gbp or 0:.2f}")
    snap = ledger.day_snapshot()
    if snap:
        lines.append(f"Day {snap[0]}: start NAV £{snap[1]:.2f}"
                     + (" (daily loss limit reached)" if ledger.daily_loss_latched(snap[0]) else ""))
    return "\n".join(lines)


class LiveRunner:
    def __init__(self, engine: Engine, ledger: Ledger, settings: Settings, alerter: Alerter,
                 control: Control | None = None, health: Healthcheck | None = None, broker=None):
        self.engine, self.ledger, self.s, self.alerter = engine, ledger, settings, alerter
        self.control, self.health, self.broker = control, health, broker
        self.done_close: datetime | None = None       # M15 close already handled
        self.retry_at: datetime | None = None
        self.last_poll = self.last_ping = self.last_flatten = None
        self.reported_closed = {p.client_id for p in ledger.all_positions() if p.status == "closed"}
        self.summary_day = None

    @staticmethod
    def _close_time(now: datetime) -> datetime:
        return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)

    def _every(self, last: datetime | None, now: datetime, period: timedelta) -> bool:
        return last is None or now - last >= period

    def step(self, now: datetime) -> None:
        if self.control and self._every(self.last_poll, now, timedelta(seconds=10)):
            self.last_poll = now
            self.control.poll(now)
        if self.health and self._every(self.last_ping, now, timedelta(minutes=5)):
            self.last_ping = now
            self.health.ping()
        self._daily_summary(now)
        if not market_open(now):
            return
        flatten_requested = self.ledger.get("flatten_requested") is not None
        if (flatten_requested or weekend_phase(now, self.s) != "normal") and \
                self._every(self.last_flatten, now, timedelta(seconds=60)):
            self.last_flatten = now
            self._guard(lambda: self._flatten(now, flatten_requested), now)
        close = self._close_time(now)
        if now >= close + DELAY and self.done_close != close and (self.retry_at is None or now >= self.retry_at):
            report = self._guard(lambda: self.engine.run_cycle(now), now)
            late = report is not None and (report.blocked or "").startswith("stale candles")
            if late and now < close + DELAY + timedelta(seconds=60):
                self.retry_at = now + timedelta(seconds=10)
            else:
                self.done_close, self.retry_at = close, None
            if report is not None:
                self._notify(report)
        if self.broker is not None and getattr(self.broker, "auth_failed", False):
            if not self.ledger.halt_reason():
                self.ledger.halt("halted: OANDA rejected the credentials")
                self.alerter("critical", "OANDA rejected the API credentials; bot halted")

    def _daily_summary(self, now: datetime) -> None:
        day = uk_trading_day(now)
        if self.summary_day is None:
            self.summary_day = day
        elif day != self.summary_day:
            closed = [p for p in self.ledger.all_positions()
                      if p.closed_at and uk_trading_day(p.closed_at) == self.summary_day]
            pl = sum(p.realized_pl or 0.0 for p in closed)
            self.alerter.info(f"Daily summary {self.summary_day}: {len(closed)} trade(s) closed, P&L £{pl:.2f}. "
                              f"Open now: {len(self.ledger.open_positions())}.")
            self.summary_day = day

    def _flatten(self, now: datetime, requested: bool) -> None:
        remaining = self.engine.flatten(now)
        if requested and remaining == 0:
            self.ledger.set("flatten_requested", None)
            self.alerter.info("Flatten complete: no bot trades open.")

    def _guard(self, action, now: datetime):
        try:
            return action()
        except Exception as exc:   # keep the loop (and protection) alive; stop new entries
            self.ledger.halt(f"halted: unexpected error: {exc}")
            self.alerter("critical", f"unexpected error ({type(exc).__name__}: {exc}); bot halted")
            return None

    def _notify(self, report) -> None:
        for cid in report.filled:
            p = self.ledger.position(cid)
            self.alerter.info(f"Opened {p.pair} {'long' if p.side > 0 else 'short'} {p.units} @ {p.fill_price}, "
                              f"stop {p.stop_price}, risk £{p.risk_gbp:.2f}")
        for p in self.ledger.all_positions():
            if p.status == "closed" and p.client_id not in self.reported_closed:
                self.reported_closed.add(p.client_id)
                self.alerter.info(f"Closed {p.pair}: P&L £{p.realized_pl:.2f}")

    def run_forever(self, clock=lambda: datetime.now(UTC), sleep=time.sleep) -> None:
        while True:
            self.step(clock())
            sleep(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_fx_live.py" -v`
Expected: 14 tests, OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/live.py tests/test_fx_live.py
git commit -m "fxbot: add live scheduler and configuration"
```

---

### Task 6: Command line

**Files:**
- Modify: `fxbot/__main__.py` (full file below)
- Test: `tests/test_fx_cli.py`

**Interfaces:**
- Produces: `python -m fxbot backtest | fetch-data | live | status | pause | resume`; `backtest` gains `--s10 PATH`; `status/pause/resume` take `--data-dir` (default `$DATA_DIR` or `/data`) and refuse while the running bot holds the ledger lock.

- [ ] **Step 1: Write the failing tests**

`tests/test_fx_cli.py`:
```python
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from fxbot.__main__ import main
from fxbot.ledger import Ledger

NO_CREDS = {k: v for k, v in os.environ.items() if not k.startswith(("OANDA_", "TELEGRAM_"))}


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_pause_resume_status_while_stopped(self):
        code, out, _ = run("pause", "--data-dir", self.dir)
        self.assertEqual(code, 0)
        self.assertIn("Paused: paused by user (command line)", out)
        code, out, _ = run("resume", "--data-dir", self.dir)
        self.assertIn("Not paused", out)

    def test_status_with_missing_data_dir_is_a_clean_error(self):
        code, _, err = run("status", "--data-dir", f"{self.dir}/does-not-exist")
        self.assertEqual(code, 2)
        self.assertIn("does-not-exist", err)

    def test_status_refuses_while_bot_holds_the_ledger(self):
        held = Ledger(f"{self.dir}/ledger.db")
        self.addCleanup(held.close)
        code, _, err = run("status", "--data-dir", self.dir)
        self.assertEqual(code, 2)
        self.assertIn("Telegram", err)

    def test_live_and_fetch_data_need_credentials(self):
        with mock.patch.dict(os.environ, NO_CREDS, clear=True):
            self.assertEqual(run("live")[0], 2)
            code, _, err = run("fetch-data", "--start", "2024-01-01", "--end", "2024-02-01", "--out", self.dir)
            self.assertEqual(code, 2)
            self.assertIn("OANDA_API_TOKEN", err)

    def test_live_refuses_real_money_without_confirmation(self):
        env = {**NO_CREDS, "OANDA_API_TOKEN": "t", "OANDA_ACCOUNT_ID": "1", "OANDA_ENV": "live"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, _, err = run("live")
        self.assertEqual(code, 2)
        self.assertIn("LIVE_TRADING_CONFIRMED", err)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_fx_cli.py" -v`
Expected: FAIL/ERROR — `invalid choice: 'pause'` (exit via SystemExit)

- [ ] **Step 3: Write the implementation**

Replace the whole of `fxbot/__main__.py`:
```python
"""Command line: backtest, fetch-data, live, status, pause, resume.

`status`, `pause` and `resume` open the ledger directly, so they only work while the bot is
stopped (the running bot holds the ledger lock). While it runs, use the Telegram commands.
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from fxbot.config import Settings
from fxbot.history import History
from fxbot.ledger import Ledger, LedgerError, LedgerLocked
from fxbot.news import load_calendar
from fxbot.research import research, write_report


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _oanda_from_env(required: bool):
    from fxbot.live import ConfigError, load_config
    from fxbot.oanda import OandaBroker
    try:
        cfg = load_config()
    except ConfigError as exc:
        if required:
            raise
        return None
    return OandaBroker(cfg.token, cfg.account_id, cfg.environment)


def cmd_backtest(args) -> int:
    settings = Settings()
    try:
        history = History.from_dir(args.data, settings.pairs)
        news = load_calendar(args.news) if args.news else None
    except (OSError, ValueError) as exc:
        print(f"error: cannot load data: {exc}", file=sys.stderr)
        return 2
    problems = history.coverage_problems(settings.pairs, args.start, args.end)
    if problems:
        print("error: historical data does not cover the requested period:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    if news is None:
        print("warning: no --news calendar given; the news filter is off in this report", file=sys.stderr)
    s10 = None
    if args.s10:
        from fxbot.datafetch import S10Cache
        broker = _oanda_from_env(required=False)
        s10 = S10Cache(args.s10, broker.s10_quote if broker else None)
        if broker is None:
            print("note: no OANDA credentials; using cached S10 quotes only", file=sys.stderr)
    try:
        report = research(history, settings, args.start, args.end, news,
                          train_months=args.train_months, test_months=args.test_months,
                          no_skill_runs=args.no_skill_runs, processes=args.processes, s10=s10)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    json_path, md_path = write_report(report, args.out)
    print(f"Gate B automated checks: {'PASSED' if report['gate_b']['passed'] else 'NOT PASSED'}")
    print(f"Report: {md_path}  Data: {json_path}")
    return 0


def cmd_fetch_data(args) -> int:
    from fxbot.datafetch import fetch_all, gap_report
    from fxbot.live import ConfigError
    try:
        broker = _oanda_from_env(required=True)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pairs = Settings().pairs
    fetch_all(broker, pairs, args.start, args.end, args.out)
    gaps = gap_report(History.from_dir(args.out, pairs), pairs)
    print("\n".join(gaps) if gaps else "No missing M15 candles relative to the reference pair.")
    return 0


def cmd_live(args) -> int:
    from fxbot.alerts import Alerter, Healthcheck, Telegram
    from fxbot.control import Control
    from fxbot.engine import Engine
    from fxbot.live import ConfigError, LiveRunner, load_config, status_text
    from fxbot.newsfeed import LiveNewsFeed
    from fxbot.oanda import OandaBroker
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    try:
        ledger = Ledger(str(Path(cfg.data_dir) / "ledger.db"))
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    clock = lambda: datetime.now(timezone.utc)   # noqa: E731
    telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id) if cfg.telegram_token else None
    alerter = Alerter(telegram, clock)
    if not cfg.trading_enabled and not ledger.pause_reason():
        ledger.pause("paused: TRADING_ENABLED=false at startup")
    broker = OandaBroker(cfg.token, cfg.account_id, cfg.environment)
    settings = Settings()
    engine = Engine(broker, ledger, settings, LiveNewsFeed(), alerter)
    control = Control(telegram, ledger, lambda: status_text(ledger)) if telegram else None
    health = Healthcheck(cfg.healthcheck_url) if cfg.healthcheck_url else None
    alerter.info(f"fxbot started on the OANDA {cfg.environment} account.\n{status_text(ledger)}")
    LiveRunner(engine, ledger, settings, alerter, control, health, broker).run_forever()
    return 0


def _open_ledger(args) -> Ledger | None:
    if not Path(args.data_dir).is_dir():
        print(f"error: data directory {args.data_dir} does not exist (set --data-dir or DATA_DIR)",
              file=sys.stderr)
        return None
    try:
        return Ledger(str(Path(args.data_dir) / "ledger.db"))
    except LedgerLocked:
        print("The bot is running and holds the ledger. Use the Telegram commands instead.", file=sys.stderr)
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
    return None


def cmd_ledger(args) -> int:
    from fxbot.live import status_text
    ledger = _open_ledger(args)
    if ledger is None:
        return 2
    try:
        if args.command == "pause":
            ledger.pause("paused by user (command line)")
        elif args.command == "resume":
            abandoned = ledger.resume()
            if abandoned:
                print(f"Unconfirmed orders marked abandoned: {', '.join(abandoned)}")
        print(status_text(ledger))
    finally:
        ledger.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fxbot")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="walk-forward research report and Gate B check")
    bt.add_argument("--data", required=True, type=Path, help="folder of <PAIR>_<H1|M15>.csv files")
    bt.add_argument("--start", required=True, type=_date, help="YYYY-MM-DD (UTC)")
    bt.add_argument("--end", required=True, type=_date, help="YYYY-MM-DD (UTC), exclusive")
    bt.add_argument("--news", type=Path, help="historical calendar CSV (time,currency,impact)")
    bt.add_argument("--s10", type=Path, help="S10 quote cache CSV (fetched from OANDA if credentials are set)")
    bt.add_argument("--out", type=Path, default=Path("runtime/research"))
    bt.add_argument("--train-months", type=_positive, default=24)
    bt.add_argument("--test-months", type=_positive, default=6)
    bt.add_argument("--no-skill-runs", type=_positive, default=1000)
    bt.add_argument("--processes", type=_positive, default=None)
    bt.set_defaults(func=cmd_backtest)

    fd = sub.add_parser("fetch-data", help="download H1 and M15 bid/ask history from OANDA")
    fd.add_argument("--start", required=True, type=_date)
    fd.add_argument("--end", required=True, type=_date)
    fd.add_argument("--out", type=Path, default=Path("data"))
    fd.set_defaults(func=cmd_fetch_data)

    lv = sub.add_parser("live", help="run the trading bot (configured by environment variables)")
    lv.set_defaults(func=cmd_live)

    for name, text in (("status", "show bot state"), ("pause", "stop new entries"),
                       ("resume", "clear pause/halt (marks unconfirmed orders abandoned)")):
        p = sub.add_parser(name, help=f"{text} (only while the bot is stopped)")
        p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
        p.set_defaults(func=cmd_ledger)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -v`
Expected: all OK

- [ ] **Step 5: Commit**

```bash
git add fxbot/__main__.py tests/test_fx_cli.py
git commit -m "fxbot: add live, fetch-data, status, pause and resume commands"
```

---

### Task 7: Deployment, setup guide, integration suite and removal of the old stocks code

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `railway.json`, `docs/setup.md`, `tests/test_fx_oanda_integration.py`
- Modify: `README.md` (full file), `.gitignore` (add `data/`), `.github/workflows/tests.yml` (rename)
- Delete: `trading/`, `tests/test_core.py`, `tests/test_ibkr.py`, `tests/test_web.py`, `start.ps1`, `docs/strategy.md`, `docs/ibkr-setup.md`

**Interfaces:**
- Produces: the Railway image runs `python -m fxbot live` with `DATA_DIR=/data`; the practice-account suite runs only when `OANDA_PRACTICE_TOKEN` and `OANDA_PRACTICE_ACCOUNT` are set and refuses anything but the practice host.

- [ ] **Step 1: Write the integration suite (skipped without credentials)**

`tests/test_fx_oanda_integration.py`:
```python
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
        txns = b.transactions_since(start_txn)
        fills = [t for t in txns if t.kind == "FILL" and t.client_id == cid]
        closes = [t for t in txns if t.kind == "CLOSE" and t.trade_id == result.trade_id]
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(closes), 1)
        self.assertNotEqual(closes[0].amount, None)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it is skipped here**

Run: `python -m unittest discover -s tests -p "test_fx_oanda_integration.py" -v`
Expected: 2 tests skipped ("set OANDA_PRACTICE_TOKEN and OANDA_PRACTICE_ACCOUNT to run")

- [ ] **Step 3: Add deployment files and documents, remove the old package**

`Dockerfile`:
```dockerfile
# fxbot on Railway: one process, standard library only.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY fxbot ./fxbot
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "-m", "fxbot", "live"]
```

`.dockerignore`:
```text
.git
tests
docs
data
runtime
__pycache__
*.db
```

`railway.json`:
```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": { "numReplicas": 1, "restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 10 }
}
```

Replace the whole of `README.md`:
~~~~markdown
# fxbot

An autonomous forex trading bot for OANDA, built to run unattended on Railway.

**Status: research and demo stage.** The strategy is a hypothesis to test, not a proven way to make
money. The bot trades only on an OANDA practice account until the gates in the design spec are
passed and you explicitly enable real money.

## What it does

- Watches EUR/USD, GBP/USD, USD/JPY, AUD/USD and USD/CAD during the London and New York sessions.
- Enters on a fresh 20-candle 15-minute breakout in the direction of the 1-hour EMA(50/200) trend.
- Stop at 1.5 × ATR, target at 2R, optional exit after 2 hours; flat before every weekend.
- Risks 0.25% of equity per trade, at most 1% in total and 0.5% per US-dollar direction; stops new
  trades for the day after a 1.5% loss; skips trades that cost too much or fall near major news.
- Places every order with a stop attached at OANDA, never resends an order, and stops opening
  trades whenever anything is uncertain.
- Sends Telegram alerts and accepts `/status`, `/pause`, `/resume` and `/flatten` from your phone.

## Commands

```bash
python -m fxbot backtest --data data --news news.csv --start 2016-03-01 --end 2025-01-01
python -m fxbot fetch-data --start 2016-01-01 --end 2025-01-01 --out data
python -m fxbot live
python -m fxbot status
python -m unittest discover -s tests -v
```

Requires Python 3.12; no third-party packages.

## Documents

- [Setup checklist](docs/setup.md): OANDA, Telegram, healthchecks.io, Railway, the demo period.
- [Design spec](docs/superpowers/specs/2026-11-02-forex-bot-design.md): strategy, risk rules,
  safety behaviour and the gates to real money.
- [Implementation plans](docs/superpowers/plans/): how each part was built and reviewed.
~~~~

`docs/setup.md`:
~~~~markdown
# Setup checklist

Follow these steps in order. Everything here uses the OANDA **practice (demo) account**. Real money
is only switched on after Gate D (see the design spec, section 10). Never set
`LIVE_TRADING_CONFIRMED=yes` before then.

## 1. OANDA practice account

1. Open a free practice account at OANDA (UK). Choose **GBP** as the account currency.
2. In the OANDA hub, generate a personal **API access token** (Manage API Access).
3. Note your **v20 account ID** (looks like `101-004-1234567-001`).
4. Do not trade manually on this account while the bot runs. The bot pauses if it finds trades or
   orders it did not create.

## 2. Check the connection (from your computer)

Requires Python 3.12. In this folder:

```bash
OANDA_PRACTICE_TOKEN=your-token OANDA_PRACTICE_ACCOUNT=your-account-id python -m unittest tests.test_fx_oanda_integration -v
```

Run it while the market is open (Sunday 22:00 to Friday 22:00 UK time). It opens and closes one
1-unit EUR/USD trade. All tests must pass: they confirm that OANDA's real responses match what the
bot expects. If anything fails, stop and report it; do not deploy.

## 3. Backtest (Gate B)

1. Download several years of history (about 2 GB of CSV for 8 years):

   ```bash
   OANDA_API_TOKEN=your-token OANDA_ACCOUNT_ID=your-account-id python -m fxbot fetch-data --start 2016-01-01 --end 2025-01-01 --out data
   ```

   It prints a gap report (missing 15-minute candles per pair and month). A few gaps are normal.
2. Get a historical calendar of **scheduled high-impact** releases as `news.csv` with header
   `time,currency,impact` (UTC times, e.g. `2024-01-05T13:30:00+00:00,USD,High`).
3. Run the research report (a full run with 1,000 no-skill runs takes many CPU-hours; try
   `--no-skill-runs 50` first):

   ```bash
   python -m fxbot backtest --data data --news news.csv --s10 data/s10.csv --start 2016-03-01 --end 2025-01-01
   ```

4. Read `runtime/research/research.md`. Gate B passes only if every automated check passes **and**
   the per-year and per-pair tables look robust to you.

## 4. Telegram (alerts and phone controls)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the **bot token**.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `message.chat.id`.
3. Commands once running: `/status`, `/pause`, `/resume` (reply YES), `/flatten` (reply YES).

## 5. Dead-man's switch

1. Create a free check at healthchecks.io with **period 5 minutes** and **grace 10 minutes**.
2. Connect it to Telegram or email (Integrations) so you are told if the bot stops.
3. Copy the ping URL.

## 6. Railway

1. Create a project from this GitHub repository. Railway uses `Dockerfile` and `railway.json`.
2. Add a **volume** mounted at `/data` (the ledger lives there).
3. Confirm in the service settings: **1 replica**, restart policy **on failure**.
4. Variables:

   | Variable | Value |
   |---|---|
   | `OANDA_API_TOKEN` | practice token |
   | `OANDA_ACCOUNT_ID` | practice account ID |
   | `OANDA_ENV` | `practice` |
   | `TELEGRAM_BOT_TOKEN` | from BotFather |
   | `TELEGRAM_CHAT_ID` | your chat ID |
   | `HEALTHCHECK_URL` | healthchecks.io ping URL |
   | `TRADING_ENABLED` | `true` (or `false` to start paused) |

5. Deploy. You should receive "fxbot started on the OANDA practice account" in Telegram.

**First day:** a brand-new ledger on an account that already has history blocks new trades for
the rest of that UK day ("no reliable daily snapshot today"). This is expected; trading starts the
next day.

## 7. Demo period (Gate C)

Run for at least 8 weeks (target 12). During that time, deliberately test: `/pause` and `/resume`,
`/flatten`, a Railway restart, and watch at least two Friday flattens. Compare trade frequency and
real slippage with the backtest. There must be zero unresolved-order or missing-stop incidents.

## 8. Real money (Gate D)

Only after Gates A to C pass: work through the Gate D checklist in the spec, create a separate live
API token, and then you, not the bot, set `OANDA_ENV=live` and `LIVE_TRADING_CONFIRMED=yes`.
~~~~

```bash
printf 'data/\n' >> .gitignore
sed -i 's/name: Test paper-trading workbench/name: Test fxbot/' .github/workflows/tests.yml
git rm -r -q trading tests/test_core.py tests/test_ibkr.py tests/test_web.py start.ps1 docs/strategy.md docs/ibkr-setup.md
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m unittest discover -s tests -v`
Expected: all OK, 2 skipped (the practice-account suite).

- [ ] **Step 5: Commit**

```bash
git add -A Dockerfile .dockerignore railway.json README.md docs/setup.md .gitignore .github/workflows/tests.yml tests/test_fx_oanda_integration.py
git commit -m "Deploy fxbot on Railway, add setup guide and practice-account suite, remove the stocks workbench"
```

---

## Self-review (done while writing)

- **Spec coverage:** §2 OANDA/Railway/Telegram/healthchecks.io/news (Tasks 1–3, 5, 7); §4 credentials from env, `alerts.py`, CLI commands (Tasks 2, 5, 6); §5 BA candles, `homeConversions`, standalone pending orders, transaction paging, stop on fill, take-profit by price at instrument precision, unknown-outcome handling (Task 1); §7 Telegram commands with confirmation, halt on auth failure and unexpected errors, healthcheck on its own timer including weekends, Friday flatten loop, live-mode double flag, one replica and volume (Tasks 3, 5, 7); §8 history download and S10 fills (Task 4); §9 practice-account integration suite (Task 7); §10 setup steps for Gates A–D (Task 7).
- **Verification before publishing:** all code was run in a scratch copy (suite OK, 2 skipped). 18 deliberate mutations (live double flag, exception halt, auth halt, late-candle retry, Friday flatten, weekend market hours, chat filter, confirmation expiry, update offset persistence, stale-news block, alert de-duplication, SL/TP excluded from pending orders, transaction ID hold-back, incomplete candles dropped, loss conversion factor, 5xx not treated as rejection, auth flag, read-only S10 cache in workers) each make the suite fail. A real bug was caught on the way: the first `parse_time` swallowed the UTC offset and produced naive datetimes.
- **Placeholders:** none. **Type consistency:** interfaces above match the code blocks.
- **Cannot be verified here:** real OANDA, Telegram and Forex Factory responses; the Docker build; Railway's `railway.json` keys. The setup guide asks for these to be confirmed (integration suite, first deployment message, service settings).

## Amendments after the Plan 4 code review (implemented; code in the repo supersedes the blocks above)

No critical issues were found. Each important finding was reproduced with a failing test before
fixing; 22 new mutations each fail the suite.

- **Network glitches could crash the loop:** the transport now also wraps `http.client` protocol
  errors; `Telegram.send` and the healthcheck never raise; every non-trading part of
  `LiveRunner.step` (Telegram, healthcheck, notifications, daily summary) runs in `_safe` (log only),
  while cycles and flattening keep `_guard` (halt + critical alert).
- **Reads retried (spec §7):** GETs are attempted 3 times with 0.5 s / 1 s backoff on network errors
  and 5xx; POST/PUT are never retried.
- **No catch-up trading:** a cycle more than 90 s after its candle close runs with
  `engine.run_cycle(now, allow_entries=False)` — it reconciles and protects but blocks entries with
  `"late cycle: no catch-up entries"`.
- **Flatten could report success falsely:** `close_trade` is True only when OANDA returns an
  `orderFillTransaction`; a flatten request is cleared only after the broker's own trade list shows
  no bot trades. A confirmed `/flatten` also **pauses** new entries until `/resume`.
- **Silent stops:** the runner alerts when the same block reason persists for over an hour (user
  pauses and late cycles excluded) and when Telegram cannot be read for ~5 minutes.
- **OANDA formats:** cancels use `clientOrderID` (falling back to the in-memory map); close and fill
  prices come from `tradesClosed[].price` / `tradeOpened.price`; `tradeReduced` becomes a `REDUCE`
  transaction and the engine **halts** if it hits a bot trade; orders use `positionFill: OPEN_ONLY`;
  a trade already closed when read back after a fill gives an `unknown` result (reconciled next
  cycle, no false "no stop" halt); instrument info refreshes every 24 h.
- **Smaller fixes:** `TRADING_ENABLED` accepts only `true`/`false`; Telegram messages older than
  2 minutes (queued while offline) are ignored; `/status@BotName` works; `/status` shows open risk,
  NAV and day P&L; a news file whose events are all over a week old counts as stale; `live` waits up
  to 60 s for a previous instance to release the ledger; the setup guide covers the DST hour,
  `/flatten` pausing and `TRADING_ENABLED` semantics; the practice-account suite adds a price-bound
  cancel case.
- **Not changed (accepted):** the healthcheck pings from the main loop on purpose — a hung loop must
  stop the pings so the dead-man's switch fires; holiday early closes are not flattened (spec covers
  Fridays only); no `flatten` CLI command (the bot holds the ledger while running; use `/flatten`).

### Second review round (re-review of the fixes)
- **A transient read error while confirming a flatten no longer halts the bot:** the broker check
  treats `BrokerError` as "still unknown" and retries 60 s later.
- **Blocked-entries alert:** the timer runs while entries are blocked for any unexpected reason, even
  if the reason changes; it ignores late cycles; user/flatten/startup pauses and the daily loss
  limit reset it; it resets while the market is closed, so the weekend never triggers it.
- **Only chosen or expected blocks are excluded from that alert** (`EXPECTED_BLOCKS`: user pause,
  `/flatten` pause, `TRADING_ENABLED` pause, daily loss limit) — the engine's own
  "paused: untagged trade or order on the account" still alerts after an hour (third review round).
- **Entry deadline re-checked before each order** using the wall clock (`Engine.clock`, set by the
  live command), so slow reads cannot push an order past close + 90 s.
- **Telegram wording:** the `/flatten` prompt says it also pauses; commands ignored as too old get a
  "please resend" reply.
- Accepted: the Telegram-outage warning is itself sent over Telegram (it arrives when only polling
  fails, e.g. a webhook conflict); worst-case cycle latency with every read on its third attempt is
  ~9 minutes, inside the 10-minute healthcheck grace.

