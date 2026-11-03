"""All strategy and risk parameters. Values are initial testing assumptions (spec §3, §6)."""
from dataclasses import dataclass
from datetime import time

PAIRS = ("EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD")


@dataclass(frozen=True)
class Settings:
    pairs: tuple = PAIRS
    # indicator windows and parameters
    h1_window: int = 600
    m15_window: int = 100
    ema_fast: int = 50
    ema_slow: int = 200
    breakout_lookback: int = 20
    atr_period: int = 14
    # exits
    stop_atr_mult: float = 1.5
    reward_r: float = 2.0
    time_exit_minutes: int | None = 120
    # sessions (Europe/London)
    session_start: time = time(7, 0)
    session_end: time = time(20, 0)
    friday_entry_cutoff: time = time(18, 0)
    friday_flatten_start: time = time(19, 30)
    friday_flatten_deadline: time = time(20, 0)
    # costs
    slippage_pips_per_side: float = 0.3
    commission_pips_round_trip: float = 0.0
    max_cost_to_stop: float = 0.15
    # news
    news_window_minutes: int = 30
    # risk
    risk_per_trade: float = 0.0025
    max_open_risk: float = 0.01
    max_usd_direction_risk: float = 0.005
    max_positions: int = 4
    max_entries_per_pair_per_day: int = 2
    daily_loss_limit: float = 0.015
    max_margin_fraction: float = 0.5
    max_gross_notional_multiple: float = 10.0
    # execution safety
    price_bound_pips: float = 1.0        # max entry slippage accepted by the broker
    price_max_age_seconds: int = 60      # older prices count as stale

    def __post_init__(self):
        if self.ema_slow > self.h1_window or self.ema_fast > self.h1_window:
            raise ValueError("EMA periods must fit inside h1_window")
        if self.atr_period > self.m15_window:
            raise ValueError("atr_period must fit inside m15_window")
        if self.breakout_lookback + 2 > self.m15_window:
            raise ValueError("breakout_lookback + 2 must fit inside m15_window")
