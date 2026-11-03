"""Live scheduler for Railway (spec §5, §7): one process, a loop that wakes every second.

- Trading cycle 10 s after each M15 close while the market is open; while candles are late
  ("stale candles"), retried every 10 s for up to 60 s. A cycle more than 90 s after its close
  (restart, slow step) still reconciles and protects but never enters: no catch-up trading.
- Friday from 19:30 London, and after a confirmed /flatten: flatten every 60 s until flat.
- Telegram commands every 10 s; healthcheck ping every 5 minutes, weekends included.
- Any unexpected exception from a cycle or flatten halts new entries and alerts; failures in
  Telegram, the healthcheck or notifications are only logged. Nothing stops the loop.
- Alerts when new entries have been blocked for over an hour, and when Telegram commands
  cannot be read for 5 minutes.
"""
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fxbot.alerts import Alerter, Healthcheck
from fxbot.broker import BrokerError, is_bot
from fxbot.config import Settings
from fxbot.control import Control
from fxbot.engine import Engine, weekend_phase
from fxbot.ledger import Ledger
from fxbot.risk import uk_trading_day

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
M15 = timedelta(minutes=15)
DELAY = timedelta(seconds=10)
ENTRY_DEADLINE = timedelta(seconds=90)      # later than this after a close: no new entries
BLOCKED_ALERT_AFTER = timedelta(hours=1)
TELEGRAM_FAILURES_ALERT = 30                # consecutive failed polls (about 5 minutes)
EXPECTED_BLOCKS = ("paused by user", "paused: /flatten requested", "paused: TRADING_ENABLED",
                   "daily loss limit reached")   # chosen or expected: no "silent stop" alert


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class LiveConfig:
    token: str
    account_id: str
    environment: str
    data_dir: str
    trading_enabled: bool
    telegram_token: str | None
    telegram_chat_id: str | None
    healthcheck_url: str | None


def load_config(env=os.environ) -> LiveConfig:
    environment = env.get("OANDA_ENV", "practice")
    if environment not in ("practice", "live"):
        raise ConfigError("OANDA_ENV must be 'practice' or 'live'")
    if environment == "live" and env.get("LIVE_TRADING_CONFIRMED") != "yes":
        raise ConfigError("OANDA_ENV=live also needs LIVE_TRADING_CONFIRMED=yes (set only after Gate D)")
    missing = [k for k in ("OANDA_API_TOKEN", "OANDA_ACCOUNT_ID") if not env.get(k)]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    if bool(env.get("TELEGRAM_BOT_TOKEN")) != bool(env.get("TELEGRAM_CHAT_ID")):
        raise ConfigError("set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither")
    enabled = env.get("TRADING_ENABLED", "true").lower()
    if enabled not in ("true", "false"):
        raise ConfigError("TRADING_ENABLED must be 'true' or 'false'")
    return LiveConfig(env["OANDA_API_TOKEN"], env["OANDA_ACCOUNT_ID"], environment,
                      env.get("DATA_DIR", "/data"), enabled == "true",
                      env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID"), env.get("HEALTHCHECK_URL"))


def market_open(now: datetime) -> bool:
    """FX trades from Sunday 17:00 to Friday 17:00 New York time."""
    ny = now.astimezone(NEW_YORK)
    if ny.weekday() == 5:
        return False
    if ny.weekday() == 4 and ny.hour >= 17:
        return False
    if ny.weekday() == 6 and ny.hour < 17:
        return False
    return True


def status_text(ledger: Ledger, nav: float | None = None) -> str:
    lines = [f"Halted: {ledger.halt_reason()}" if ledger.halt_reason() else "Not halted",
             f"Paused: {ledger.pause_reason()}" if ledger.pause_reason() else "Not paused"]
    positions = ledger.open_positions()
    lines.append(f"Open bot trades: {len(positions)}; Open risk £{sum(p.risk_gbp or 0 for p in positions):.2f}")
    for p in positions:
        lines.append(f"  {p.pair} {'long' if p.side > 0 else 'short'} {p.units} @ {p.fill_price} "
                     f"stop {p.stop_price} target {p.tp_price} risk £{p.risk_gbp or 0:.2f}")
    snap = ledger.day_snapshot()
    if snap:
        lines.append(f"Day {snap[0]}: start NAV £{snap[1]:.2f}"
                     + (" (daily loss limit reached)" if ledger.daily_loss_latched(snap[0]) else ""))
        if nav is not None:
            deposits, withdrawals = ledger.contributions_since(snap[2])
            lines.append(f"NAV £{nav:.2f}; Day P&L £{nav - snap[1] - deposits + withdrawals:.2f}")
    return "\n".join(lines)


class LiveRunner:
    def __init__(self, engine: Engine, ledger: Ledger, settings: Settings, alerter: Alerter,
                 control: Control | None = None, health: Healthcheck | None = None, broker=None):
        self.engine, self.ledger, self.s, self.alerter = engine, ledger, settings, alerter
        self.control, self.health, self.broker = control, health, broker
        self.done_close: datetime | None = None       # M15 close already handled
        self.retry_at: datetime | None = None
        self.last_poll = self.last_ping = self.last_flatten = None
        self.reported_closed = {p.client_id for p in ledger.all_positions() if p.status == "closed"}
        self.summary_day = None
        self.block_since: datetime | None = None
        self.telegram_failures = 0

    @staticmethod
    def _close_time(now: datetime) -> datetime:
        return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)

    def _every(self, last: datetime | None, now: datetime, period: timedelta) -> bool:
        return last is None or now - last >= period

    def step(self, now: datetime) -> None:
        if self.control and self._every(self.last_poll, now, timedelta(seconds=10)):
            self.last_poll = now
            self._safe(lambda: self._poll(now))
        if self.health and self._every(self.last_ping, now, timedelta(minutes=5)):
            self.last_ping = now
            self._safe(self.health.ping)
        self._safe(lambda: self._daily_summary(now))
        if not market_open(now):
            self.block_since = None   # the weekend is not a silent stop
            return
        flatten_requested = self.ledger.get("flatten_requested") is not None
        if (flatten_requested or weekend_phase(now, self.s) != "normal") and \
                self._every(self.last_flatten, now, timedelta(seconds=60)):
            self.last_flatten = now
            self._guard(lambda: self._flatten(now, flatten_requested), now)
        close = self._close_time(now)
        if now >= close + DELAY and self.done_close != close and (self.retry_at is None or now >= self.retry_at):
            on_time = now - close <= ENTRY_DEADLINE
            report = self._guard(lambda: self.engine.run_cycle(now, allow_entries=on_time,
                                                               entry_deadline=close + ENTRY_DEADLINE), now)
            late = report is not None and (report.blocked or "").startswith("stale candles")
            if late and now < close + DELAY + timedelta(seconds=60):
                self.retry_at = now + timedelta(seconds=10)
            else:
                self.done_close, self.retry_at = close, None
            if report is not None:
                self._safe(lambda: self._notify(report))
                late = any(r.startswith("late cycle") for _, r in report.rejected)
                self._watch_blocks(report.blocked or ("late cycle" if late else None), now)
        if self.broker is not None and getattr(self.broker, "auth_failed", False):
            if not self.ledger.halt_reason():
                self.ledger.halt("halted: OANDA rejected the credentials")
                self.alerter("critical", "OANDA rejected the API credentials; bot halted")

    def _daily_summary(self, now: datetime) -> None:
        day = uk_trading_day(now)
        if self.summary_day is None:
            self.summary_day = day
        elif day != self.summary_day:
            closed = [p for p in self.ledger.all_positions()
                      if p.closed_at and uk_trading_day(p.closed_at) == self.summary_day]
            pl = sum(p.realized_pl or 0.0 for p in closed)
            self.alerter.info(f"Daily summary {self.summary_day}: {len(closed)} trade(s) closed, P&L £{pl:.2f}. "
                              f"Open now: {len(self.ledger.open_positions())}.")
            self.summary_day = day

    def _poll(self, now: datetime) -> None:
        if self.control.poll(now) is False:
            self.telegram_failures += 1
            if self.telegram_failures >= TELEGRAM_FAILURES_ALERT:
                self.alerter("warning", "Telegram commands are not being received (check the bot token "
                             "and that no other program polls it); use the OANDA app if needed")
        else:
            self.telegram_failures = 0

    def _watch_blocks(self, reason: str | None, now: datetime) -> None:
        """Alert if new entries stay blocked for over an hour (a silent stop is the worst failure).

        The timer runs while entries are blocked for any unexpected reason, even if the reason
        changes. Pauses the user chose and the daily loss limit are expected, so they reset it.
        """
        if reason is not None and reason.startswith("late cycle"):
            return
        if reason is None or reason.startswith(EXPECTED_BLOCKS):
            self.block_since = None
            return
        if self.block_since is None:
            self.block_since = now
        elif now - self.block_since >= BLOCKED_ALERT_AFTER:
            self.alerter("warning", f"No new trades for over an hour: {reason}")

    def _flatten(self, now: datetime, requested: bool) -> None:
        remaining = self.engine.flatten(now)
        if remaining == 0 and self.broker is not None:   # trust the broker's list, not the replies
            try:
                remaining = sum(1 for t in self.broker.open_trades() if is_bot(t.client_id))
            except BrokerError:
                remaining = 1                              # unknown: check again in 60 s, no halt
        if requested and remaining == 0:
            self.ledger.set("flatten_requested", None)
            self.alerter.info("Flatten complete: no bot trades open.")

    def _safe(self, action):
        """For non-trading parts (Telegram, healthcheck, messages): log failures, never halt."""
        try:
            return action()
        except Exception as exc:
            self.alerter.log(f"{type(exc).__name__} in background task: {exc}")
            return None

    def _guard(self, action, now: datetime):
        try:
            return action()
        except Exception as exc:   # keep the loop (and protection) alive; stop new entries
            self.ledger.halt(f"halted: unexpected error: {exc}")
            self.alerter("critical", f"unexpected error ({type(exc).__name__}: {exc}); bot halted")
            return None

    def _notify(self, report) -> None:
        for cid in report.filled:
            p = self.ledger.position(cid)
            self.alerter.info(f"Opened {p.pair} {'long' if p.side > 0 else 'short'} {p.units} @ {p.fill_price}, "
                              f"stop {p.stop_price}, risk £{p.risk_gbp:.2f}")
        for p in self.ledger.all_positions():
            if p.status == "closed" and p.client_id not in self.reported_closed:
                self.reported_closed.add(p.client_id)
                self.alerter.info(f"Closed {p.pair}: P&L £{p.realized_pl:.2f}")

    def run_forever(self, clock=lambda: datetime.now(UTC), sleep=time.sleep) -> None:
        while True:
            self.step(clock())
            sleep(1)
