"""OANDA v20 REST adapter implementing the Broker interface (spec §2, §5).

Everything OANDA-specific is normalised here; the engine never sees OANDA JSON.
The response formats below follow OANDA's v20 documentation as understood when this was written;
`tests/test_fx_oanda_integration.py` verifies them against a real practice account.
"""
import time
from datetime import datetime, timedelta, timezone

from fxbot.broker import (Account, BrokerError, InstrumentInfo, OrderResult, PendingOrder, Price,
                          Trade, Txn)
from fxbot.history import HistCandle
from fxbot.http import HttpError, Transport, urllib_transport
from fxbot.instruments import currencies
from fxbot.strategy import Candle

HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}
MAX_CANDLES = 5000
READ_ATTEMPTS = 3                 # spec §7: reads retried with backoff; orders never are
INFO_MAX_AGE = timedelta(hours=24)


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
                 transport: Transport = urllib_transport, timeout: float = 10.0, sleep=time.sleep,
                 clock=lambda: datetime.now(timezone.utc)):
        if environment not in HOSTS:
            raise ValueError(f"environment must be one of {sorted(HOSTS)}")
        self.base, self.account_id, self.timeout, self._send = HOSTS[environment], account_id, timeout, transport
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "Accept-Datetime-Format": "RFC3339"}
        self._sleep, self._clock = sleep, clock
        self._info: dict[str, tuple[datetime, dict]] = {}
        self._order_client: dict[str, str] = {}   # order ID -> client ID (to map cancels)
        self.auth_failed = False

    # --- plumbing --------------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None,
                 ok: tuple[int, ...] = (200,)) -> tuple[int, dict]:
        attempts = READ_ATTEMPTS if method == "GET" else 1   # an order is never sent twice
        for attempt in range(attempts):
            if attempt:
                self._sleep(0.5 * 2 ** (attempt - 1))
            try:
                status, data = self._send(method, self.base + path, self._headers, body, self.timeout)
            except HttpError as exc:
                if attempt == attempts - 1:
                    raise BrokerError(f"OANDA unreachable: {exc}") from exc
                continue
            if status < 500 or attempt == attempts - 1:
                break
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
        cached = self._info.get(pair)
        if cached is None or self._clock() - cached[0] > INFO_MAX_AGE:   # margin rates can change
            found = self._get(self._acct(f"/instruments?instruments={pair}"))["instruments"]
            if not found:
                raise BrokerError(f"unknown instrument {pair}")
            cached = self._info[pair] = (self._clock(), found[0])
        return cached[1]

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
                                 units=int(float(closed["units"])), price=float(closed.get("price", t.get("price", 0))),
                                 amount=float(closed["realizedPL"])))
            reduced = t.get("tradeReduced")
            if reduced:   # a partial close (e.g. margin closeout): the engine halts on it
                found.append(Txn(tid, "REDUCE", at, trade_id=reduced["tradeID"], pair=t["instrument"],
                                 units=int(float(reduced["units"])), price=float(reduced.get("price", t.get("price", 0))),
                                 amount=float(reduced.get("realizedPL", 0))))
            opened = t.get("tradeOpened")
            if opened:
                cid = (opened.get("clientExtensions") or {}).get("id") or self._order_client.get(t.get("orderID"))
                found.append(Txn(tid, "FILL", at, client_id=cid, trade_id=opened["tradeID"], pair=t["instrument"],
                                 units=int(float(opened["units"])), price=float(opened.get("price", t.get("price", 0)))))
        elif kind == "MARKET_ORDER_REJECT":
            found.append(Txn(tid, "REJECT", at, client_id=(t.get("clientExtensions") or {}).get("id"),
                             pair=t.get("instrument")))
        elif kind == "ORDER_CANCEL":
            cid = t.get("clientOrderID") or self._order_client.get(t.get("orderID"))
            if cid:
                found.append(Txn(tid, "REJECT", at, client_id=cid))
        elif kind == "TRANSFER_FUNDS":
            found.append(Txn(tid, "TRANSFER", at, amount=float(t["amount"])))
        return [Txn(tid - 1 if i < len(found) - 1 else tid, x.kind, x.time, x.client_id, x.trade_id,
                    x.pair, x.units, x.price, x.amount) for i, x in enumerate(found)]

    # --- Broker interface: writes ------------------------------------------------------
    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult:
        ext = {"id": client_id, "tag": "fxbot"}
        body = {"order": {"type": "MARKET", "instrument": pair, "units": str(units), "timeInForce": "FOK",
                          "positionFill": "OPEN_ONLY", "priceBound": self._fmt(pair, price_bound),
                          "stopLossOnFill": {"distance": self._fmt(pair, stop_distance), "timeInForce": "GTC"},
                          "clientExtensions": ext, "tradeClientExtensions": ext}}
        status, data = self._request("POST", self._acct("/orders"), body, ok=(201, 400, 404))
        if "orderFillTransaction" in data and data["orderFillTransaction"].get("tradeOpened"):
            opened = data["orderFillTransaction"]["tradeOpened"]
            trade = self._get(self._acct(f"/trades/{opened['tradeID']}"))["trade"]
            if trade.get("state", "OPEN") != "OPEN":   # already stopped out: let reconciliation record it
                return OrderResult("unknown", reason="trade closed immediately after fill")
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
        """True only if a closing fill happened (OANDA can answer 200 with a cancellation)."""
        status, data = self._request("PUT", self._acct(f"/trades/{trade_id}/close"), {"units": "ALL"},
                                     ok=(200, 400, 404))
        return status == 200 and "orderFillTransaction" in data

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
