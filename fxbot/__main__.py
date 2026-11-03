"""Command line. Plan 3 adds `backtest`; Plan 4 adds live-trading commands."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from fxbot.config import Settings
from fxbot.history import History
from fxbot.news import load_calendar
from fxbot.research import research, write_report


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fxbot")
    sub = parser.add_subparsers(dest="command", required=True)
    bt = sub.add_parser("backtest", help="walk-forward research report and Gate B check")
    bt.add_argument("--data", required=True, type=Path, help="folder of <PAIR>_<H1|M15>.csv files")
    bt.add_argument("--start", required=True, type=_date, help="YYYY-MM-DD (UTC)")
    bt.add_argument("--end", required=True, type=_date, help="YYYY-MM-DD (UTC), exclusive")
    bt.add_argument("--news", type=Path, help="historical calendar CSV (time,currency,impact)")
    bt.add_argument("--out", type=Path, default=Path("runtime/research"))
    bt.add_argument("--train-months", type=_positive, default=24)
    bt.add_argument("--test-months", type=_positive, default=6)
    bt.add_argument("--no-skill-runs", type=_positive, default=1000)
    bt.add_argument("--processes", type=_positive, default=None)
    args = parser.parse_args(argv)

    settings = Settings()
    try:
        history = History.from_dir(args.data, settings.pairs)
        news = load_calendar(args.news) if args.news else None
    except (OSError, ValueError) as exc:
        print(f"error: cannot load data: {exc}", file=sys.stderr)
        return 2
    problems = history.coverage_problems(settings.pairs, args.start, args.end)
    if problems:
        print("error: historical data does not cover the requested period:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    if news is None:
        print("warning: no --news calendar given; the news filter is off in this report", file=sys.stderr)
    try:
        report = research(history, settings, args.start, args.end, news,
                          train_months=args.train_months, test_months=args.test_months,
                          no_skill_runs=args.no_skill_runs, processes=args.processes)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    json_path, md_path = write_report(report, args.out)
    print(f"Gate B automated checks: {'PASSED' if report['gate_b']['passed'] else 'NOT PASSED'}")
    print(f"Report: {md_path}  Data: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
