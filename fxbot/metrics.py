"""Performance metrics for backtest results (spec §8, Gate B)."""
import random
from collections import Counter, defaultdict
from datetime import date

from fxbot.backtest import BacktestResult, TradeRecord
from fxbot.risk import uk_trading_day


def max_drawdown(marks: list[float], start_nav: float) -> float:
    """Largest peak-to-trough fall of NAV across every mark, starting from `start_nav`."""
    peak, worst = start_nav, 0.0
    for nav in marks:
        peak = max(peak, nav)
        worst = max(worst, (peak - nav) / peak)
    return worst


def losing_streak(trades: list[TradeRecord]) -> int:
    streak = worst = 0
    for t in sorted(trades, key=lambda t: t.close_time):
        streak = streak + 1 if t.pl_gbp < 0 else 0
        worst = max(worst, streak)
    return worst


def max_simultaneous(trades: list[TradeRecord]) -> int:
    events = sorted([(t.open_time, 1) for t in trades] + [(t.close_time, -1) for t in trades],
                    key=lambda e: (e[0], e[1]))   # closes before opens at the same instant
    current = best = 0
    for _, step in events:
        current += step
        best = max(best, current)
    return best


def bootstrap_ci(trades: list[TradeRecord], runs: int = 2000, seed: int = 0,
                 level: float = 0.95) -> tuple[float, float] | None:
    """Confidence interval for expectancy (R per trade), resampling whole UK trading days.

    Trades on the same day are not independent, so days - not trades - are resampled.
    """
    days: dict[date, list[float]] = defaultdict(list)
    for t in trades:
        days[uk_trading_day(t.open_time)].append(t.r)
    groups = list(days.values())
    if not groups or runs <= 0:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(runs):
        sample = [rng.choice(groups) for _ in groups]
        n = sum(len(g) for g in sample)
        stats.append(sum(sum(g) for g in sample) / n)
    stats.sort()
    lo = stats[int((1 - level) / 2 * runs)]
    hi = stats[min(runs - 1, int((1 + level) / 2 * runs))]
    return lo, hi


def _group(trades: list[TradeRecord]) -> dict:
    n = len(trades)
    return {"trades": n, "net_gbp": sum(t.pl_gbp for t in trades),
            "expectancy_r": sum(t.r for t in trades) / n if n else 0.0}


def summarize(result: BacktestResult, bootstrap_runs: int = 2000, seed: int = 0) -> dict:
    trades = result.trades
    n = len(trades)
    wins = [t.pl_gbp for t in trades if t.pl_gbp > 0]
    losses = [-t.pl_gbp for t in trades if t.pl_gbp < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    by_pair, by_year, by_day = defaultdict(list), defaultdict(list), defaultdict(list)
    for t in trades:
        by_pair[t.pair].append(t)
        by_year[t.open_time.year].append(t)
        by_day[uk_trading_day(t.open_time)].append(t)
    multi = [d for d in by_day.values() if len(d) >= 2]
    return {
        "trades": n,
        "start_nav": result.start_nav,
        "end_nav": result.end_nav,
        "net_gbp": sum(t.pl_gbp for t in trades),
        "expectancy_gbp": sum(t.pl_gbp for t in trades) / n if n else 0.0,
        "expectancy_r": sum(t.r for t in trades) / n if n else 0.0,
        "win_rate": len(wins) / n if n else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else None,   # None = no losses
        "max_drawdown": max_drawdown(result.nav_marks, result.start_nav),
        "worst_losing_streak": losing_streak(trades),
        "expectancy_r_ci95": bootstrap_ci(trades, bootstrap_runs, seed),
        "per_pair": {p: _group(ts) for p, ts in sorted(by_pair.items())},
        "per_year": {y: _group(ts) for y, ts in sorted(by_year.items())},
        "exits": dict(Counter(t.reason for t in trades)),
        "rejections": dict(result.rejections),
        "notional_cap_hits": result.rejections.get("max gross notional", 0),
        "blocked_cycles": dict(result.blocked),   # only cycles that had a signal or an open trade
        "max_simultaneous_positions": max_simultaneous(trades),
        "days_with_multiple_trades": len(multi),
        "multi_trade_days_all_lost": (sum(all(t.pl_gbp < 0 for t in d) for d in multi) / len(multi)
                                      if multi else None),
        # what that share would be if trades on the same day were independent
        "multi_trade_days_all_lost_if_independent": (
            sum((len(losses) / n) ** len(d) for d in multi) / len(multi) if multi else None),
        "open_at_end": result.open_at_end,
        "halted": result.halted,
    }
