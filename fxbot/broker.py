"""Broker interface shared by OandaBroker (Plan 4) and SimBroker (spec §4).

All broker-specific formats are normalised into these types, so the engine never sees
OANDA JSON. Units are signed: positive = long, negative = short.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fxbot.strategy import Candle

BOT_TAG = "fxb-"   # client IDs created by this bot start with this prefix


class BrokerError(Exception):
    """A broker request failed or returned something unusable."""


@dataclass(frozen=True)
class Account:
    nav: float
    margin_used: float
    last_txn_id: int


@dataclass(frozen=True)
class Price:
    pair: str
    time: datetime
    bid: float
    ask: float
    tradeable: bool
    loss_factor: float    # homeConversions.accountLoss: GBP per unit of quote currency
    value_factor: float   # homeConversions.positionValue


@dataclass(frozen=True)
class InstrumentInfo:
    margin_rate: float
    max_units: int


@dataclass(frozen=True)
class Trade:
    trade_id: str
    pair: str
    units: int
    open_price: float
    open_time: datetime
    client_id: str | None
    stop_price: float | None
    take_profit_price: float | None


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    client_id: str | None


@dataclass(frozen=True)
class Txn:
    """Normalised transaction. kind is one of FILL, CLOSE, REJECT, TRANSFER."""
    txn_id: int
    kind: str
    time: datetime
    client_id: str | None = None
    trade_id: str | None = None
    pair: str | None = None
    units: int = 0
    price: float = 0.0
    amount: float = 0.0   # realised P&L (CLOSE) or funds moved (TRANSFER), GBP


@dataclass(frozen=True)
class OrderResult:
    """status: 'filled', 'rejected', or 'unknown' (request outcome not known, e.g. timeout)."""
    status: str
    trade_id: str | None = None
    fill_price: float = 0.0
    units: int = 0
    stop_price: float | None = None
    reason: str = ""


class Broker(Protocol):
    def account(self) -> Account: ...
    def candles(self, pair: str, granularity: str, count: int) -> list[Candle]: ...
    def prices(self, pairs: tuple[str, ...]) -> dict[str, Price]: ...
    def instrument(self, pair: str) -> InstrumentInfo: ...
    def open_trades(self) -> list[Trade]: ...
    def pending_orders(self) -> list[PendingOrder]: ...
    def transactions_since(self, txn_id: int) -> list[Txn]: ...
    def market_order(self, pair: str, units: int, client_id: str, stop_distance: float,
                     price_bound: float) -> OrderResult: ...
    def set_take_profit(self, trade_id: str, price: float) -> bool: ...
    def close_trade(self, trade_id: str) -> bool: ...


def is_bot(client_id: str | None) -> bool:
    return client_id is not None and client_id.startswith(BOT_TAG)
