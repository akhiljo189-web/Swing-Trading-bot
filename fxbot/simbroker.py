"""In-memory broker for tests (and the Plan 3 backtest). Scriptable failures included."""
from datetime import datetime

from fxbot.broker import (Account, BrokerError, InstrumentInfo, OrderResult, PendingOrder,
                          Price, Trade, Txn)
from fxbot.strategy import Candle


class SimBroker:
    def __init__(self, nav: float, now: datetime):
        self.nav = nav
        self.now = now
        self.candle_data: dict[tuple[str, str], list[Candle]] = {}
        self.price_data: dict[str, Price] = {}
        self.info = InstrumentInfo(margin_rate=1 / 30, max_units=100_000_000)
        self.trades: dict[str, Trade] = {}
        self.pending: list[PendingOrder] = []
        self.txns: list[Txn] = []
        # scripted behaviour
        self.fail_next: set[str] = set()     # method names that raise BrokerError once
        self.order_modes: list[str] = []     # fill | reject | no_stop | timeout_filled | timeout_lost
        self.tp_failures = 0                 # next N set_take_profit calls fail
        self.close_failures = 0              # next N close_trade calls fail
        self.orders_sent: list[str] = []     # client IDs received, in order
        self._next_trade = 1

    # --- helpers for tests -------------------------------------------------
    def _check(self, name: str) -> None:
        if name in self.fail_next:
            self.fail_next.discard(name)
            raise BrokerError(f"simulated {name} failure")

    def _txn(self, kind: str, **fields) -> Txn:
        txn = Txn(len(self.txns) + 1, kind, self.now, **fields)
        self.txns.append(txn)
        return txn

    def deposit(self, amount: float) -> None:
        self.nav += amount
        self._txn("TRANSFER", amount=amount)

    def add_trade(self, pair: str, units: int, price: float, client_id: str | None,
                  stop_price: float | None = None) -> Trade:
        trade = Trade(str(self._next_trade), pair, units, price, self.now, client_id,
                      stop_price, None)
        self._next_trade += 1
        self.trades[trade.trade_id] = trade
        return trade

    def close_at_broker(self, trade_id: str, price: float) -> None:
        """Simulate the broker closing a trade (stop or target hit)."""
        trade = self.trades.pop(trade_id)
        pl = trade.units * (price - trade.open_price) * self.price_data[trade.pair].loss_factor
        self.nav += pl
        self._txn("CLOSE", trade_id=trade_id, pair=trade.pair, units=-trade.units,
                  price=price, amount=pl)

    # --- Broker interface --------------------------------------------------
    def account(self) -> Account:
        self._check("account")
        margin = sum(abs(t.units) * t.open_price * self.price_data[t.pair].value_factor
                     * self.info.margin_rate for t in self.trades.values())
        return Account(self.nav, margin, len(self.txns))

    def candles(self, pair: str, granularity: str, count: int) -> list[Candle]:
        self._check("candles")
        return self.candle_data[(pair, granularity)][-count:]

    def prices(self, pairs: tuple[str, ...]) -> dict[str, Price]:
        self._check("prices")
        return {p: self.price_data[p] for p in pairs}

    def instrument(self, pair: str) -> InstrumentInfo:
        self._check("instrument")
        return self.info

    def open_trades(self) -> list[Trade]:
        self._check("open_trades")
        return list(self.trades.values())

    def pending_orders(self) -> list[PendingOrder]:
        self._check("pending_orders")
        return list(self.pending)

    def transactions_since(self, txn_id: int) -> list[Txn]:
        self._check("transactions_since")
        return [t for t in self.txns if t.txn_id > txn_id]

    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult:
        self._check("market_order")
        self.orders_sent.append(client_id)
        mode = self.order_modes.pop(0) if self.order_modes else "fill"
        if mode == "timeout_lost":
            return OrderResult("unknown", reason="timeout")
        if any(t.client_id == client_id for t in self.trades.values()):
            self._txn("REJECT", client_id=client_id, pair=pair)
            return OrderResult("rejected", reason="CLIENT_ORDER_ID_ALREADY_EXISTS")
        p = self.price_data[pair]
        fill = p.ask if units > 0 else p.bid
        if mode == "reject" or (units > 0 and fill > price_bound) or (units < 0 and fill < price_bound):
            self._txn("REJECT", client_id=client_id, pair=pair)
            return OrderResult("rejected", reason="BOUNDS_VIOLATION" if mode != "reject" else "REJECTED")
        stop = None if mode == "no_stop" else (fill - stop_distance if units > 0 else fill + stop_distance)
        trade = self.add_trade(pair, units, fill, client_id, stop)
        self._txn("FILL", client_id=client_id, trade_id=trade.trade_id, pair=pair,
                  units=units, price=fill)
        if mode == "timeout_filled":
            return OrderResult("unknown", reason="timeout")
        return OrderResult("filled", trade.trade_id, fill, units, stop)

    def set_take_profit(self, trade_id: str, price: float) -> bool:
        self._check("set_take_profit")
        if self.tp_failures:
            self.tp_failures -= 1
            return False
        t = self.trades[trade_id]
        self.trades[trade_id] = Trade(t.trade_id, t.pair, t.units, t.open_price, t.open_time,
                                      t.client_id, t.stop_price, price)
        return True

    def close_trade(self, trade_id: str) -> bool:
        self._check("close_trade")
        if self.close_failures:
            self.close_failures -= 1
            return False
        p = self.price_data[self.trades[trade_id].pair]
        self.close_at_broker(trade_id, p.bid if self.trades[trade_id].units > 0 else p.ask)
        return True
