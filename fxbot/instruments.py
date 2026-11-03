"""Static facts about traded pairs."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    name: str
    pip: float


INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", 0.0001),
    "GBP_USD": Instrument("GBP_USD", 0.0001),
    "USD_JPY": Instrument("USD_JPY", 0.01),
    "AUD_USD": Instrument("AUD_USD", 0.0001),
    "USD_CAD": Instrument("USD_CAD", 0.0001),
}


def currencies(pair: str) -> tuple[str, str]:
    base, quote = pair.split("_")
    return base, quote


def usd_direction(pair: str, side: int) -> int:
    """+1 if the position is long USD, -1 if short USD, 0 if the pair has no USD leg."""
    if side not in (1, -1):
        raise ValueError("side must be +1 (buy) or -1 (sell)")
    base, quote = currencies(pair)
    return side if base == "USD" else -side if quote == "USD" else 0
