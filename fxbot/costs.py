"""The single cost model used by filter, sizing, simulator and reports (spec §5)."""
from fxbot.config import Settings
from fxbot.instruments import INSTRUMENTS


def round_trip_cost(pair: str, spread: float, settings: Settings) -> float:
    pip = INSTRUMENTS[pair].pip
    return (spread
            + 2 * settings.slippage_pips_per_side * pip
            + settings.commission_pips_round_trip * pip)


def passes_cost_filter(cost: float, stop_distance: float, settings: Settings) -> bool:
    return cost <= settings.max_cost_to_stop * stop_distance
