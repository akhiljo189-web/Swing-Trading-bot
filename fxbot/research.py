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


def chain(history, plan: list[tuple], news, nav: float = 1000.0, signal_fn=None) -> BacktestResult:
    """Run consecutive (settings, start, end) windows, carrying NAV forward; merge results."""
    merged = BacktestResult(nav, nav, [], [])
    for settings, start, end in plan:
        extra = {"signal_fn": signal_fn} if signal_fn is not None else {}
        r = run(history, settings, start, end, nav=merged.end_nav, news=news, **extra)
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
                 grid=GRID, min_trades=30) -> list[tuple[Settings, datetime, datetime]]:
    """Pick the best grid settings on each training window (expectancy R, at least `min_trades`)."""
    plan = []
    for tr_s, tr_e, te_s, te_e in windows(start, end, train_months, test_months):
        best, best_score = settings, float("-inf")
        for candidate in grid_settings(settings, grid):
            m = summarize(run(history, candidate, tr_s, tr_e, news=news), bootstrap_runs=0)
            score = m["expectancy_r"] if m["trades"] >= min_trades else float("-inf")
            if score > best_score:
                best, best_score = candidate, score
        plan.append((best, te_s, te_e))
    return plan


# --- no-skill distribution (parallel) ------------------------------------------
_WORKER: dict = {}


def _init_worker(history, plan, news, base):
    _WORKER.update(history=history, plan=plan, news=news, base=base)


def _no_skill_run(seed: int) -> float:
    w = _WORKER
    r = chain(w["history"], w["plan"], w["news"], signal_fn=random_direction(w["base"], seed))
    return summarize(r, bootstrap_runs=0)["expectancy_r"]


def no_skill(history, plan, news, base, runs: int, processes: int | None) -> list[float]:
    """Expectancy (R) of `runs` seeded random-direction runs over the walk-forward windows."""
    if processes == 1:
        _init_worker(history, plan, news, base)
        return [_no_skill_run(seed) for seed in range(runs)]
    methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in methods else None)
    gc.freeze()   # keep forked workers from copying the shared history pages
    try:
        with ProcessPoolExecutor(processes, mp_context=context, initializer=_init_worker,
                                 initargs=(history, plan, news, base)) as pool:
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
             no_skill_runs=1000, processes: int | None = None) -> dict:
    """Every comparison in Gate B uses the same walk-forward windows and chosen settings."""
    plan = walk_forward(history, settings, start, end, news, train_months, test_months, grid)
    if not plan:
        raise ValueError("period too short for one training plus one test window")
    oos_start = plan[0][1]

    def variant(**changes) -> BacktestResult:
        return chain(history, [(replace(s, **changes), a, b) for s, a, b in plan], news)

    wf = chain(history, plan, news)
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
        "news_filter_off": summarize(chain(history, plan, None), bootstrap_runs=0),
        "trend_baseline": summarize(chain(history, plan, news, signal_fn=trend_baseline), bootstrap_runs=0),
        "no_skill": {"runs": no_skill_runs,
                     "expectancy_r": no_skill(history, plan, news, wf.signals, no_skill_runs, processes)},
        "fixed_params": summarize(run(history, settings, oos_start, end, news=news), bootstrap_runs=0),
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
