"""Static facts about traded pairs."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    name: str
    pip: float
    usd_is_base: bool


INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", 0.0001, False),
    "GBP_USD": Instrument("GBP_USD", 0.0001, False),
    "USD_JPY": Instrument("USD_JPY", 0.01, True),
    "AUD_USD": Instrument("AUD_USD", 0.0001, False),
    "USD_CAD": Instrument("USD_CAD", 0.0001, True),
}


def currencies(pair: str) -> tuple[str, str]:
    base, quote = pair.split("_")
    return base, quote


def usd_direction(pair: str, side: int) -> int:
    """+1 if the position is long USD, -1 if short USD."""
    if side not in (1, -1):
        raise ValueError("side must be +1 (buy) or -1 (sell)")
    return side if INSTRUMENTS[pair].usd_is_base else -side
