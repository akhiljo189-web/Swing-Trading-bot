"""Central risk manager: sizing and portfolio approval (spec §6). Pure functions only."""
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from fxbot.config import Settings
from fxbot.instruments import usd_direction
from fxbot.strategy import LONDON, Signal, require_aware


@dataclass(frozen=True)
class OpenPosition:
    """risk_gbp is the trade's full planned loss: stop risk + cost risk (spec §6 rule 2)."""
    pair: str
    side: int
    units: int
    risk_gbp: float
    notional_gbp: float


@dataclass(frozen=True)
class AccountState:
    nav: float
    margin_used: float
    positions: tuple[OpenPosition, ...]
    entries_today: Mapping[str, int]
    block_reason: str | None


@dataclass(frozen=True)
class Candidate:
    signal: Signal
    cost: float          # round-trip cost in price units (fxbot.costs.round_trip_cost)
    price: float         # expected entry price
    loss_factor: float   # homeConversions.accountLoss: GBP per unit of quote currency
    value_factor: float  # homeConversions.positionValue
    margin_rate: float
    max_units: int


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str
    units: int = 0
    risk_gbp: float = 0.0
    stop_risk_gbp: float = 0.0
    cost_risk_gbp: float = 0.0
    notional_gbp: float = 0.0
    margin_gbp: float = 0.0


def size_units(nav: float, stop_distance: float, cost: float, loss_factor: float,
               risk_fraction: float) -> int:
    loss_per_unit = (stop_distance + cost) * loss_factor
    return math.floor(risk_fraction * nav / loss_per_unit + 1e-9)


def evaluate(candidate: Candidate, account: AccountState, settings: Settings) -> Decision:
    s, c, a, sig = settings, candidate, account, candidate.signal
    if a.block_reason is not None:
        return Decision(False, a.block_reason)
    if sig.stop_distance <= 0 or c.loss_factor <= 0 or c.price <= 0:
        return Decision(False, "invalid price data")
    units = size_units(a.nav, sig.stop_distance, c.cost, c.loss_factor, s.risk_per_trade)
    if units < 1:
        return Decision(False, "position below minimum size")
    if units > c.max_units:
        return Decision(False, "above instrument maximum units")
    if len(a.positions) >= s.max_positions:
        return Decision(False, "max positions")
    if any(p.pair == sig.pair for p in a.positions):
        return Decision(False, "position already open in pair")
    if a.entries_today.get(sig.pair, 0) >= s.max_entries_per_pair_per_day:
        return Decision(False, "max entries for pair today")

    stop_risk = units * sig.stop_distance * c.loss_factor
    cost_risk = units * c.cost * c.loss_factor
    risk = stop_risk + cost_risk
    if sum(p.risk_gbp for p in a.positions) + risk > s.max_open_risk * a.nav:
        return Decision(False, "max open risk")
    direction = usd_direction(sig.pair, sig.side)
    same_dir = sum(p.risk_gbp for p in a.positions if usd_direction(p.pair, p.side) == direction)
    if same_dir + risk > s.max_usd_direction_risk * a.nav:
        return Decision(False, "max USD direction risk")
    notional = units * c.price * c.value_factor
    if sum(p.notional_gbp for p in a.positions) + notional > s.max_gross_notional_multiple * a.nav:
        return Decision(False, "max gross notional")
    margin = notional * c.margin_rate
    if a.margin_used + margin > s.max_margin_fraction * a.nav:
        return Decision(False, "max margin")
    return Decision(True, "approved", units, risk, stop_risk, cost_risk, notional, margin)


def uk_trading_day(at_utc: datetime) -> date:
    return require_aware(at_utc).astimezone(LONDON).date()


def daily_pnl(start_nav: float, current_nav: float, deposits: float, withdrawals: float) -> float:
    return current_nav - start_nav - deposits + withdrawals


def daily_loss_breached(start_nav: float, current_nav: float, deposits: float,
                        withdrawals: float, settings: Settings) -> bool:
    pnl = daily_pnl(start_nav, current_nav, deposits, withdrawals)
    return pnl <= -settings.daily_loss_limit * start_nav
