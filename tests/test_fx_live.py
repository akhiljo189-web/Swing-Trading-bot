import unittest
from datetime import datetime, timedelta, timezone

from fx_fixtures import NOW, Harness, set_market
from fxbot.alerts import Alerter
from fxbot.broker import Price
from fxbot.config import PAIRS, Settings
from fxbot.live import ConfigError, LiveRunner, load_config, market_open, status_text

UTC = timezone.utc
S = Settings()
ENV = {"OANDA_API_TOKEN": "t", "OANDA_ACCOUNT_ID": "001"}


class ConfigTests(unittest.TestCase):
    def test_defaults_to_practice(self):
        cfg = load_config(ENV)
        self.assertEqual((cfg.environment, cfg.data_dir, cfg.trading_enabled), ("practice", "/data", True))

    def test_live_needs_explicit_confirmation(self):
        with self.assertRaises(ConfigError):
            load_config({**ENV, "OANDA_ENV": "live"})
        self.assertEqual(load_config({**ENV, "OANDA_ENV": "live", "LIVE_TRADING_CONFIRMED": "yes"}).environment, "live")

    def test_invalid_configs(self):
        for env in ({}, {**ENV, "OANDA_ENV": "demo"}, {**ENV, "TELEGRAM_BOT_TOKEN": "x"},
                    {**ENV, "TRADING_ENABLED": "no"}):
            with self.assertRaises(ConfigError, msg=env):
                load_config(env)
        self.assertFalse(load_config({**ENV, "TRADING_ENABLED": "false"}).trading_enabled)


class MarketHoursTests(unittest.TestCase):
    def test_market_hours(self):
        self.assertTrue(market_open(NOW))                                              # Tuesday
        self.assertFalse(market_open(datetime(2026, 11, 7, 12, tzinfo=UTC)))           # Saturday
        self.assertFalse(market_open(datetime(2026, 11, 6, 22, 30, tzinfo=UTC)))       # Fri 17:30 New York
        self.assertTrue(market_open(datetime(2026, 11, 6, 21, 30, tzinfo=UTC)))
        self.assertTrue(market_open(datetime(2026, 11, 8, 22, 30, tzinfo=UTC)))        # Sun 17:30 New York
        self.assertFalse(market_open(datetime(2026, 11, 8, 21, 30, tzinfo=UTC)))


class Recorder:
    def __init__(self):
        self.messages = []

    def __call__(self, text):
        self.messages.append(text)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.log = Recorder()
        self.alerter = Alerter(None, lambda: self.now, log=self.log)
        self.now = NOW
        self.cycles = []
        real = self.h.engine.run_cycle
        self.h.engine.run_cycle = lambda now, **kw: self.cycles.append(now) or real(now, **kw)
        self.runner = LiveRunner(self.h.engine, self.h.ledger, S, self.alerter, broker=self.h.sim)

    def step(self, at):
        self.now = at
        self.h.sim.now = at
        self.runner.step(at)

    def test_one_cycle_per_candle_close(self):
        self.step(NOW - timedelta(seconds=5))          # 09:15:05: too early (needs close + 10 s)
        self.step(NOW)
        self.step(NOW + timedelta(seconds=30))
        self.assertEqual(self.cycles, [NOW])

    def test_late_candles_are_retried_for_up_to_a_minute(self):
        later = NOW + timedelta(minutes=15)            # 09:30:10, but data still ends at 09:15
        for s in range(0, 90, 5):
            self.step(later + timedelta(seconds=s))
        self.assertEqual(len(self.cycles), 7)          # +0, +10 ... +60 s, then gives up until next close
        self.assertEqual(self.cycles[-1], later + timedelta(seconds=60))

    def test_unexpected_error_halts_but_loop_survives(self):
        self.h.engine.run_cycle = lambda now, **kw: 1 / 0
        self.step(NOW)
        self.assertTrue(self.h.ledger.halt_reason().startswith("halted: unexpected error"))
        self.assertIn("CRITICAL", self.log.messages[-1])
        self.step(NOW + timedelta(minutes=15))         # still stepping

    def test_fill_and_close_notifications(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        self.step(NOW)
        self.assertTrue(any("Opened EUR_USD long" in m for m in self.log.messages))
        p = self.h.ledger.open_positions()[0]
        self.h.sim.close_at_broker(p.trade_id, p.tp_price)
        for pair in PAIRS:
            set_market(self.h.sim, pair, now=NOW + timedelta(minutes=15))
        self.step(NOW + timedelta(minutes=15))
        self.assertTrue(any("Closed EUR_USD: P&L" in m for m in self.log.messages))

    def test_confirmed_flatten_closes_and_clears_request(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        self.step(NOW)
        self.h.ledger.set("flatten_requested", NOW.isoformat())
        self.step(NOW + timedelta(seconds=20))
        self.assertEqual(self.h.sim.trades, {})
        self.assertIsNone(self.h.ledger.get("flatten_requested"))
        self.assertTrue(any("Flatten complete" in m for m in self.log.messages))

    def test_friday_flatten_window(self):
        friday = datetime(2026, 11, 6, 19, 30, 10, tzinfo=UTC)
        self.h.sim.now = friday - timedelta(minutes=30)          # opened 30 min earlier: no time exit yet
        self.h.sim.add_trade("EUR_USD", 1000, 1.1, "fxb-x", 1.099)
        self.step(friday - timedelta(minutes=5))                 # 19:25: before the flatten window
        self.assertEqual(len(self.h.sim.trades), 1)
        self.step(friday)
        self.assertEqual(self.h.sim.trades, {})

    def test_auth_failure_halts(self):
        self.h.sim.auth_failed = True
        self.step(NOW)
        self.assertEqual(self.h.ledger.halt_reason(), "halted: OANDA rejected the credentials")

    def test_daily_summary_on_new_uk_day(self):
        self.step(NOW)
        self.step(NOW + timedelta(days=1))
        self.assertTrue(any(m.endswith("Open now: 0.") and "Daily summary 2026-11-03" in m for m in self.log.messages))

    def test_healthcheck_every_five_minutes_even_at_weekends(self):
        pings = []

        class HC:
            def ping(self):
                pings.append(1)
        self.runner.health = HC()
        saturday = datetime(2026, 11, 7, 12, tzinfo=UTC)
        for minute in range(0, 16):
            self.step(saturday + timedelta(minutes=minute))
        self.assertEqual(len(pings), 4)                # 0, 5, 10, 15
        self.assertEqual(self.cycles, [])              # no trading while closed

    def test_restart_late_in_the_candle_does_not_enter(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        late = NOW + timedelta(minutes=14)               # 09:29:10: the 09:15 signal is 14 min old
        for pair, p in list(self.h.sim.price_data.items()):   # prices themselves are fresh
            self.h.sim.price_data[pair] = Price(pair, late - timedelta(seconds=5), p.bid, p.ask, True,
                                                p.loss_factor, p.value_factor)
        self.step(late)
        self.assertEqual(len(self.cycles), 1)
        self.assertEqual(self.h.sim.orders_sent, [])

    def test_flatten_complete_only_when_broker_confirms(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        self.step(NOW)
        self.h.ledger.set("flatten_requested", NOW.isoformat())
        real = self.h.engine.flatten
        self.h.engine.flatten = lambda now: 0            # broker claims success, trade still open
        self.step(NOW + timedelta(seconds=20))
        self.assertIsNotNone(self.h.ledger.get("flatten_requested"))
        self.h.engine.flatten = real
        self.step(NOW + timedelta(seconds=90))
        self.assertIsNone(self.h.ledger.get("flatten_requested"))

    def test_entries_blocked_for_an_hour_raise_an_alert(self):
        self.h.news_events = None                       # news feed down
        for minutes in range(0, 76, 15):
            for pair in PAIRS:
                set_market(self.h.sim, pair, now=NOW + timedelta(minutes=minutes))
            self.step(NOW + timedelta(minutes=minutes))
        self.assertTrue(any("No new trades for over an hour" in m and "news feed unavailable" in m
                            for m in self.log.messages))

    def test_transient_read_error_during_flatten_does_not_halt(self):
        friday = datetime(2026, 11, 6, 19, 45, 10, tzinfo=UTC)
        self.h.sim.now = friday
        self.h.sim.add_trade("EUR_USD", 1000, 1.1, "fxb-x", 1.099)
        self.h.ledger.set("flatten_requested", friday.isoformat())
        self.h.engine.flatten = lambda now: 0
        self.h.sim.fail_next = {"open_trades"}
        self.runner._flatten(friday, True)
        self.assertIsNone(self.h.ledger.halt_reason())
        self.assertIsNotNone(self.h.ledger.get("flatten_requested"))       # retried next time

    def test_chosen_pauses_and_daily_limit_do_not_nag(self):
        for reason in ("paused: /flatten requested", "paused: TRADING_ENABLED=false at startup",
                       "daily loss limit reached"):
            self.runner._watch_blocks(reason, NOW)
            self.runner._watch_blocks(reason, NOW + timedelta(hours=2))
        self.assertFalse(any("No new trades" in m for m in self.log.messages))

    def test_untagged_trade_pause_still_alerts(self):
        reason = "paused: untagged trade or order on the account"
        self.runner._watch_blocks(reason, NOW)
        self.runner._watch_blocks(reason, NOW + timedelta(hours=1))
        self.assertTrue(any("No new trades for over an hour" in m for m in self.log.messages))

    def test_changing_block_reasons_still_alert(self):
        reasons = ["stale candles EUR_USD", "broker unavailable"] * 4
        for i, reason in enumerate(reasons):
            self.runner._watch_blocks(reason, NOW + timedelta(minutes=15 * i))
        self.assertTrue(any("No new trades for over an hour" in m for m in self.log.messages))

    def test_block_timer_restarts_after_the_weekend(self):
        self.runner._watch_blocks("news feed unavailable", datetime(2026, 11, 6, 21, 30, tzinfo=UTC))
        self.step(datetime(2026, 11, 7, 12, tzinfo=UTC))                    # market closed
        self.runner._watch_blocks("news feed unavailable", datetime(2026, 11, 8, 22, 15, tzinfo=UTC))
        self.assertFalse(any("No new trades" in m for m in self.log.messages))

    def test_failing_parts_never_stop_the_loop(self):
        class Boom:
            def poll(self, now):
                raise ValueError("telegram garbled")

            def ping(self):
                raise ValueError("ping garbled")
        self.runner.control, self.runner.health = Boom(), Boom()
        self.step(NOW)
        self.assertEqual(self.cycles, [NOW])
        self.assertIsNone(self.h.ledger.halt_reason())

    def test_telegram_failures_are_reported(self):
        class Down:
            def poll(self, now):
                return False
        self.runner.control = Down()
        for s in range(0, 400, 10):
            self.step(NOW + timedelta(seconds=s))
        self.assertTrue(any("Telegram commands are not being received" in m for m in self.log.messages))

    def test_status_text(self):
        set_market(self.h.sim, "EUR_USD", breakout=+1)
        self.step(NOW)
        text = status_text(self.h.ledger)
        self.assertIn("Open bot trades: 1", text)
        self.assertIn("EUR_USD long", text)
        self.assertIn("Open risk £", text)
        self.assertIn("Day P&L £", status_text(self.h.ledger, nav=1005.0))


if __name__ == "__main__":
    unittest.main()
