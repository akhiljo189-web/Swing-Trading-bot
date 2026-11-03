"""Command line: backtest, fetch-data, live, status, pause, resume.

`status`, `pause` and `resume` open the ledger directly, so they only work while the bot is
stopped (the running bot holds the ledger lock). While it runs, use the Telegram commands.
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from fxbot.config import Settings
from fxbot.history import History
from fxbot.ledger import Ledger, LedgerError, LedgerLocked
from fxbot.news import load_calendar
from fxbot.research import research, write_report


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _oanda_from_env(required: bool):
    from fxbot.live import ConfigError, load_config
    from fxbot.oanda import OandaBroker
    try:
        cfg = load_config()
    except ConfigError as exc:
        if required:
            raise
        return None
    return OandaBroker(cfg.token, cfg.account_id, cfg.environment)


def cmd_backtest(args) -> int:
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
    s10 = None
    if args.s10:
        from fxbot.datafetch import S10Cache
        broker = _oanda_from_env(required=False)
        s10 = S10Cache(args.s10, broker.s10_quote if broker else None)
        if broker is None:
            print("note: no OANDA credentials; using cached S10 quotes only", file=sys.stderr)
    try:
        report = research(history, settings, args.start, args.end, news,
                          train_months=args.train_months, test_months=args.test_months,
                          no_skill_runs=args.no_skill_runs, processes=args.processes, s10=s10)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    json_path, md_path = write_report(report, args.out)
    print(f"Gate B automated checks: {'PASSED' if report['gate_b']['passed'] else 'NOT PASSED'}")
    print(f"Report: {md_path}  Data: {json_path}")
    return 0


def cmd_fetch_data(args) -> int:
    from fxbot.datafetch import fetch_all, gap_report
    from fxbot.live import ConfigError
    try:
        broker = _oanda_from_env(required=True)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pairs = Settings().pairs
    fetch_all(broker, pairs, args.start, args.end, args.out)
    gaps = gap_report(History.from_dir(args.out, pairs), pairs)
    print("\n".join(gaps) if gaps else "No missing M15 candles relative to the reference pair.")
    return 0


def cmd_live(args) -> int:
    from fxbot.alerts import Alerter, Healthcheck, Telegram
    from fxbot.control import Control
    from fxbot.engine import Engine
    from fxbot.live import ConfigError, LiveRunner, load_config, status_text
    from fxbot.newsfeed import LiveNewsFeed
    from fxbot.oanda import OandaBroker
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    try:
        ledger = Ledger(str(Path(cfg.data_dir) / "ledger.db"))
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    clock = lambda: datetime.now(timezone.utc)   # noqa: E731
    telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id) if cfg.telegram_token else None
    alerter = Alerter(telegram, clock)
    if not cfg.trading_enabled and not ledger.pause_reason():
        ledger.pause("paused: TRADING_ENABLED=false at startup")
    broker = OandaBroker(cfg.token, cfg.account_id, cfg.environment)
    settings = Settings()
    engine = Engine(broker, ledger, settings, LiveNewsFeed(), alerter)
    control = Control(telegram, ledger, lambda: status_text(ledger)) if telegram else None
    health = Healthcheck(cfg.healthcheck_url) if cfg.healthcheck_url else None
    alerter.info(f"fxbot started on the OANDA {cfg.environment} account.\n{status_text(ledger)}")
    LiveRunner(engine, ledger, settings, alerter, control, health, broker).run_forever()
    return 0


def _open_ledger(args) -> Ledger | None:
    if not Path(args.data_dir).is_dir():
        print(f"error: data directory {args.data_dir} does not exist (set --data-dir or DATA_DIR)",
              file=sys.stderr)
        return None
    try:
        return Ledger(str(Path(args.data_dir) / "ledger.db"))
    except LedgerLocked:
        print("The bot is running and holds the ledger. Use the Telegram commands instead.", file=sys.stderr)
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
    return None


def cmd_ledger(args) -> int:
    from fxbot.live import status_text
    ledger = _open_ledger(args)
    if ledger is None:
        return 2
    try:
        if args.command == "pause":
            ledger.pause("paused by user (command line)")
        elif args.command == "resume":
            abandoned = ledger.resume()
            if abandoned:
                print(f"Unconfirmed orders marked abandoned: {', '.join(abandoned)}")
        print(status_text(ledger))
    finally:
        ledger.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fxbot")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="walk-forward research report and Gate B check")
    bt.add_argument("--data", required=True, type=Path, help="folder of <PAIR>_<H1|M15>.csv files")
    bt.add_argument("--start", required=True, type=_date, help="YYYY-MM-DD (UTC)")
    bt.add_argument("--end", required=True, type=_date, help="YYYY-MM-DD (UTC), exclusive")
    bt.add_argument("--news", type=Path, help="historical calendar CSV (time,currency,impact)")
    bt.add_argument("--s10", type=Path, help="S10 quote cache CSV (fetched from OANDA if credentials are set)")
    bt.add_argument("--out", type=Path, default=Path("runtime/research"))
    bt.add_argument("--train-months", type=_positive, default=24)
    bt.add_argument("--test-months", type=_positive, default=6)
    bt.add_argument("--no-skill-runs", type=_positive, default=1000)
    bt.add_argument("--processes", type=_positive, default=None)
    bt.set_defaults(func=cmd_backtest)

    fd = sub.add_parser("fetch-data", help="download H1 and M15 bid/ask history from OANDA")
    fd.add_argument("--start", required=True, type=_date)
    fd.add_argument("--end", required=True, type=_date)
    fd.add_argument("--out", type=Path, default=Path("data"))
    fd.set_defaults(func=cmd_fetch_data)

    lv = sub.add_parser("live", help="run the trading bot (configured by environment variables)")
    lv.set_defaults(func=cmd_live)

    for name, text in (("status", "show bot state"), ("pause", "stop new entries"),
                       ("resume", "clear pause/halt (marks unconfirmed orders abandoned)")):
        p = sub.add_parser(name, help=f"{text} (only while the bot is stopped)")
        p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
        p.set_defaults(func=cmd_ledger)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
