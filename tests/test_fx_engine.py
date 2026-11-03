import unittest
from datetime import datetime, timedelta

from fx_fixtures import NOW, UTC, Harness, set_market
from fxbot.broker import PendingOrder, Price, Txn
from fxbot.config import Settings
from fxbot.config import PAIRS
from fxbot.engine import client_id
from fxbot.instruments import usd_direction
from fxbot.ledger import Ledger
from fxbot.news import NewsEvent
from fxbot.risk import uk_trading_day

LATER = NOW + timedelta(seconds=30)        # same candle, second look
SIGNAL_CANDLE = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
EUR_ID = client_id("EUR_USD", +1, SIGNAL_CANDLE)


class EntryTests(unittest.TestCase):
    def test_enters_on_signal_with_stop_and_target(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        rep = h.cycle()
        self.assertEqual(rep.filled, [EUR_ID])
        self.assertEqual(EUR_ID, "fxb-EUR_USD-20261103T0900-B")
        p = h.ledger.position(EUR_ID)
        trade = h.sim.trades[p.trade_id]
        stop_dist = p.fill_price - trade.stop_price
        self.assertGreater(stop_dist, 0)
        self.assertAlmostEqual(trade.take_profit_price, p.fill_price + 2 * stop_dist, places=10)
        self.assertAlmostEqual(p.tp_price, trade.take_profit_price, places=10)
        loss = h.sim.price_data["EUR_USD"].loss_factor
        self.assertAlmostEqual(p.risk_gbp, p.units * (stop_dist + p.cost) * loss, places=9)
        self.assertLessEqual(p.risk_gbp, 2.5)

    def test_no_signal_no_order(self):
        h = Harness(self)
        rep = h.cycle()
        self.assertEqual((rep.filled, rep.blocked, h.sim.orders_sent), ([], None, []))

    def test_same_signal_is_never_sent_twice(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        h.sim.close_at_broker(h.ledger.position(EUR_ID).trade_id, 1.1)   # stopped out quickly
        rep = h.cycle(LATER)
        self.assertIn(("EUR_USD", "duplicate signal"), rep.rejected)
        self.assertEqual(h.sim.orders_sent, [EUR_ID])

    def test_broker_rejection_marks_intent_failed(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["reject"]
        rep = h.cycle()
        self.assertEqual(h.ledger.position(EUR_ID).status, "failed")
        self.assertIn(("EUR_USD", "broker rejected: REJECTED"), rep.rejected)

    def test_outside_session_rejected(self):
        early = datetime(2026, 11, 3, 6, 30, 10, tzinfo=UTC)   # signal candle closes 06:30 London
        h = Harness(self, now=early)
        set_market(h.sim, "EUR_USD", now=early, breakout=+1)
        rep = h.cycle(early)
        self.assertIn(("EUR_USD", "outside session"), rep.rejected)
        self.assertEqual(h.sim.orders_sent, [])

    def test_wide_spread_rejected_by_cost_filter(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1, spread_pips=3.0)
        rep = h.cycle()
        self.assertEqual(rep.rejected[0][0], "EUR_USD")
        self.assertTrue(rep.rejected[0][1].startswith("cost "))

    def test_news_blackout_rejects_pair(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.news_events = [NewsEvent(NOW + timedelta(minutes=10), "USD", "High")]
        self.assertIn(("EUR_USD", "news blackout"), h.cycle().rejected)


class ConcurrencyTests(unittest.TestCase):
    def test_all_five_pairs_signal_at_once(self):
        h = Harness(self)
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        rep = h.cycle()
        self.assertEqual(len(rep.filled), 4)
        self.assertEqual(len(rep.rejected), 1)
        self.assertIn(rep.rejected[0][1], ("max positions", "max USD direction risk"))
        positions = h.ledger.open_positions()
        self.assertLessEqual(sum(p.risk_gbp for p in positions), 10.0)          # 1% of NAV
        for direction in (+1, -1):
            same = sum(p.risk_gbp for p in positions if usd_direction(p.pair, p.side) == direction)
            self.assertLessEqual(same, 5.0)                                     # 0.5% of NAV

    def test_timeout_blocks_remaining_candidates_then_reconciles(self):
        h = Harness(self)
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        h.sim.order_modes = ["timeout_filled"]
        rep = h.cycle()
        self.assertEqual(len(h.sim.orders_sent), 1)
        self.assertEqual(rep.filled, [])
        self.assertTrue(all(r.startswith("unresolved order") for _, r in rep.rejected))
        first = h.sim.orders_sent[0]
        h.cycle(LATER)                                   # transaction history resolves it
        self.assertEqual(h.ledger.position(first).status, "filled")
        self.assertIsNone(h.ledger.halt_reason())

    def test_lost_order_halts_and_is_never_resent(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["timeout_lost"]
        h.cycle()
        rep = h.cycle(LATER)
        self.assertEqual(rep.blocked, f"halted: unresolved order {EUR_ID}")
        self.assertIn("critical", [level for level, _ in h.alerts])
        self.assertEqual(h.ledger.resume(), [EUR_ID])
        rep = h.cycle(LATER + timedelta(seconds=5))
        self.assertIn(("EUR_USD", "duplicate signal"), rep.rejected)
        self.assertEqual(h.sim.orders_sent, [EUR_ID])

    def test_order_request_exception_is_treated_as_unknown(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.fail_next = {"market_order"}
        h.cycle()
        self.assertEqual(h.ledger.position(EUR_ID).status, "submitting")
        self.assertTrue(h.cycle(LATER).blocked.startswith("halted: unresolved order"))


class ProtectionTests(unittest.TestCase):
    def test_fill_without_stop_is_closed_and_halts(self):
        h = Harness(self)
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        h.sim.order_modes = ["no_stop"]
        h.cycle()
        self.assertEqual(h.sim.trades, {})                 # unprotected trade closed
        self.assertEqual(len(h.sim.orders_sent), 1)        # nothing else entered
        self.assertTrue(h.ledger.halt_reason().startswith("halted: trade"))
        self.assertEqual(h.alerts[-1][0], "critical")

    def test_existing_trade_losing_its_stop_is_closed_and_halts(self):
        h = Harness(self)
        h.sim.add_trade("EUR_USD", 1000, 1.1, "fxb-EUR_USD-20261103T0800-B", None)   # stop removed
        rep = h.cycle()
        self.assertEqual(h.sim.trades, {})
        self.assertTrue(rep.blocked.startswith("halted: trade"))
        self.assertEqual(h.alerts[-1][0], "critical")

    def test_take_profit_failure_is_retried(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.tp_failures = 1
        h.cycle()
        self.assertIsNone(h.ledger.position(EUR_ID).tp_price)
        self.assertEqual(h.alerts[-1][0], "warning")
        h.cycle(LATER)
        self.assertIsNotNone(h.ledger.position(EUR_ID).tp_price)

    def test_untagged_trade_pauses_entries(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.add_trade("GBP_USD", 1000, 1.32, None, 1.31)
        rep = h.cycle()
        self.assertEqual(rep.blocked, "paused: untagged trade or order on the account")
        self.assertEqual(h.sim.orders_sent, [])

    def test_alert_channel_failure_does_not_stop_safety_actions(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["no_stop"]
        h.engine._alert_fn = lambda level, text: 1 / 0
        h.cycle()
        self.assertEqual(h.sim.trades, {})
        self.assertIsNotNone(h.ledger.halt_reason())


class HealthTests(unittest.TestCase):
    def test_broker_unavailable_blocks_without_crashing(self):
        h = Harness(self)
        h.sim.fail_next = {"account"}
        self.assertEqual(h.cycle().blocked, "broker unavailable")

    def test_stale_candles_block(self):
        h = Harness(self)
        rep = h.cycle(NOW + timedelta(minutes=15))        # the next candle has not arrived
        self.assertEqual(rep.blocked, "stale candles EUR_USD")

    def test_stale_price_blocks(self):
        h = Harness(self)
        p = h.sim.price_data["GBP_USD"]
        h.sim.price_data["GBP_USD"] = Price(p.pair, NOW - timedelta(minutes=5), p.bid, p.ask,
                                            True, p.loss_factor, p.value_factor)
        self.assertEqual(h.cycle().blocked, "stale or untradeable price GBP_USD")

    def test_news_feed_down_blocks(self):
        h = Harness(self)
        h.news_events = None
        self.assertEqual(h.cycle().blocked, "news feed unavailable")


class DailyLossTests(unittest.TestCase):
    def test_breach_latches_until_next_uk_day(self):
        h = Harness(self)
        h.cycle()                                          # snapshot: start NAV 1000
        h.sim.nav = 984.0
        self.assertEqual(h.cycle(LATER).blocked, "daily loss limit reached")
        h.sim.nav = 1000.0                                 # equity recovers
        self.assertEqual(h.cycle(LATER).blocked, "daily loss limit reached")
        tomorrow = NOW + timedelta(days=1)
        for pair in PAIRS:
            set_market(h.sim, pair, now=tomorrow)
        self.assertIsNone(h.cycle(tomorrow).blocked)
        self.assertFalse(h.ledger.daily_loss_latched(uk_trading_day(tomorrow)))

    def test_deposit_cannot_hide_a_loss(self):
        h = Harness(self)
        h.cycle()
        h.sim.now = LATER
        h.sim.deposit(300.0)
        h.sim.nav -= 16.0
        self.assertEqual(h.cycle(LATER).blocked, "daily loss limit reached")

    def test_deposit_alone_is_not_profit_or_loss(self):
        h = Harness(self)
        h.cycle()
        h.sim.now = LATER
        h.sim.deposit(300.0)
        self.assertIsNone(h.cycle(LATER).blocked)


class LifecycleTests(unittest.TestCase):
    def test_broker_close_is_recorded_with_pl(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        p = h.ledger.position(EUR_ID)
        h.sim.close_at_broker(p.trade_id, p.tp_price)
        h.cycle(LATER)
        closed = h.ledger.position(EUR_ID)
        self.assertEqual(closed.status, "closed")
        self.assertGreater(closed.realized_pl, 0)

    def test_time_exit_after_120_minutes(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        trade_id = h.ledger.position(EUR_ID).trade_id
        rep = h.cycle(NOW + timedelta(minutes=120))
        self.assertEqual(rep.exits, [trade_id])
        self.assertNotIn(trade_id, h.sim.trades)

    def test_lost_database_adopts_bot_trades_and_blocks_the_day(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        h.ledger.close()
        h.engine.ledger = h.ledger = Ledger(f"{h.dir}/rebuilt.db")
        rep = h.cycle(LATER)
        self.assertEqual(h.ledger.position(EUR_ID).status, "filled")
        self.assertEqual(rep.blocked, "no reliable daily snapshot today")


class ReviewRegressionTests(unittest.TestCase):
    """Scenarios found by the Plan 2 code review."""

    def test_lost_database_after_closed_trade_never_resends_and_blocks_day(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        p = h.ledger.position(EUR_ID)
        h.sim.now = LATER
        h.sim.close_at_broker(p.trade_id, p.stop_price)        # stopped out, then the DB is lost
        h.ledger.close()
        h.engine.ledger = h.ledger = Ledger(f"{h.dir}/rebuilt.db")
        rep = h.cycle(LATER + timedelta(seconds=5))
        self.assertEqual(h.sim.orders_sent, [EUR_ID])
        self.assertEqual(h.ledger.position(EUR_ID).status, "closed")
        self.assertEqual(rep.blocked, "no reliable daily snapshot today")

    def test_overnight_close_counts_toward_new_day_without_blocking_it(self):
        h = Harness(self, settings=Settings(time_exit_minutes=None))
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        p = h.ledger.position(EUR_ID)
        h.sim.now = datetime(2026, 11, 4, 0, 5, tzinfo=UTC)    # target hit after UK midnight
        h.sim.close_at_broker(p.trade_id, p.tp_price)
        profit = h.sim.txns[-1].amount
        day2 = datetime(2026, 11, 4, 0, 15, 10, tzinfo=UTC)
        for pair in PAIRS:
            set_market(h.sim, pair, now=day2)
        self.assertIsNone(h.cycle(day2).blocked)
        self.assertAlmostEqual(h.ledger.day_snapshot()[1], h.sim.nav - profit, places=9)

    def test_fill_landing_between_fetches_is_not_lost(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["timeout_lost"]
        h.cycle()
        real = h.sim.transactions_since

        def racing(txn_id):
            seen = real(txn_id)
            h.sim.late_fill(EUR_ID, "EUR_USD", 1000, 0.0015)   # lands after transactions were read
            h.sim.transactions_since = real
            return seen
        h.sim.transactions_since = racing
        h.cycle(LATER)
        p = h.ledger.position(EUR_ID)
        self.assertEqual(p.status, "filled")
        self.assertIsNotNone(p.stop_price)
        self.assertIsNone(h.ledger.halt_reason())
        h.cycle(LATER + timedelta(seconds=5))
        self.assertEqual(h.ledger.position(EUR_ID).status, "filled")

    def test_bot_trade_in_unconfigured_pair_without_stop_is_still_closed(self):
        h = Harness(self)
        h.sim.price_data["EUR_GBP"] = Price("EUR_GBP", NOW - timedelta(seconds=5), 0.85, 0.85002,
                                            True, 1.0, 1.0)
        h.sim.add_trade("EUR_GBP", 1000, 0.85, "fxb-EUR_GBP-20261103T0800-B", None)
        rep = h.cycle()
        self.assertEqual(h.sim.trades, {})
        self.assertTrue(rep.blocked.startswith("halted: trade"))

    def test_bot_trade_in_unconfigured_pair_does_not_break_scanning(self):
        h = Harness(self)
        h.sim.price_data["EUR_GBP"] = Price("EUR_GBP", NOW - timedelta(seconds=5), 0.85, 0.85002,
                                            True, 1.0, 1.0)
        h.sim.add_trade("EUR_GBP", 1000, 0.85, "fxb-EUR_GBP-20261103T0800-B", 0.849)
        set_market(h.sim, "EUR_USD", breakout=+1)
        self.assertEqual(h.cycle().filled, [EUR_ID])

    def test_session_is_judged_on_candle_close_time(self):
        open_ = datetime(2026, 11, 3, 7, 0, 10, tzinfo=UTC)     # candle 06:45-07:00 closes at 07:00
        h = Harness(self, now=open_)
        set_market(h.sim, "EUR_USD", now=open_, breakout=+1)
        self.assertEqual(len(h.cycle(open_).filled), 1)
        close = datetime(2026, 11, 3, 20, 0, 10, tzinfo=UTC)    # candle 19:45-20:00 closes at 20:00
        h2 = Harness(self, now=close)
        set_market(h2.sim, "EUR_USD", now=close, breakout=+1)
        self.assertIn(("EUR_USD", "outside session"), h2.cycle(close).rejected)

    def test_news_window_is_measured_from_decision_time(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.news_events = [NewsEvent(NOW + timedelta(minutes=25), "USD", "High")]   # 40 min after candle open
        self.assertIn(("EUR_USD", "news blackout"), h.cycle().rejected)

    def test_untagged_pending_order_pauses_entries(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.pending = [PendingOrder("77", None)]
        self.assertEqual(h.cycle().blocked, "paused: untagged trade or order on the account")

    def test_reject_transaction_resolves_unknown_order(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["timeout_rejected"]
        h.cycle()
        h.cycle(LATER)
        self.assertEqual(h.ledger.position(EUR_ID).status, "failed")
        self.assertIsNone(h.ledger.halt_reason())

    def test_margin_is_reserved_within_a_cycle(self):
        h = Harness(self, settings=Settings(max_margin_fraction=0.12))
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        rep = h.cycle()
        self.assertEqual(len(rep.filled), 2)
        self.assertIn("max margin", [r for _, r in rep.rejected])

    def test_slot_freed_by_time_exit_is_usable_in_the_same_cycle(self):
        h = Harness(self)
        for pair in PAIRS:
            set_market(h.sim, pair, breakout=+1)
        waiting = h.cycle().rejected[0][0]
        later = NOW + timedelta(minutes=120)
        for pair in PAIRS:
            set_market(h.sim, pair, now=later, breakout=+1 if pair == waiting else 0)
        rep = h.cycle(later)
        self.assertEqual(len(rep.exits), 4)
        self.assertEqual([c.split("-")[1] for c in rep.filled], [waiting])

    def test_abandoned_intent_that_fills_later_is_tracked(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["timeout_lost"]
        h.cycle()
        h.cycle(LATER)
        h.ledger.resume()
        h.sim.late_fill(EUR_ID, "EUR_USD", 1000, 0.0015)
        h.cycle(LATER + timedelta(seconds=5))
        self.assertEqual([p.client_id for p in h.ledger.open_positions()], [EUR_ID])

    def test_close_landing_between_fetches_keeps_real_pl(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        p = h.ledger.position(EUR_ID)
        real = h.sim.transactions_since

        def racing(txn_id):
            seen = real(txn_id)
            h.sim.close_at_broker(p.trade_id, p.tp_price)       # closes after transactions were read
            h.sim.transactions_since = real
            return seen
        h.sim.transactions_since = racing
        h.cycle(LATER)
        self.assertEqual(h.ledger.position(EUR_ID).status, "filled")   # not written off yet
        h.cycle(LATER + timedelta(seconds=5))
        closed = h.ledger.position(EUR_ID)
        self.assertEqual(closed.status, "closed")
        self.assertAlmostEqual(closed.realized_pl, h.sim.txns[-1].amount, places=9)
        self.assertGreater(closed.realized_pl, 0)

    def test_reconciliation_error_halts_but_protection_still_runs(self):
        h = Harness(self)
        h.sim.add_trade("EUR_USD", 1000, 1.1, "fxb-EUR_USD-20261103T0800-B", None)   # no stop
        h.sim.txns.append(Txn(1, "FILL", NOW, client_id="fxb-bad", trade_id="x"))   # malformed
        rep = h.cycle()
        self.assertEqual(h.sim.trades, {})
        self.assertTrue(rep.blocked.startswith("halted: reconciliation error"))

    def overnight_stop_out(self, h):
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        p = h.ledger.position(EUR_ID)
        h.sim.now = datetime(2026, 11, 4, 0, 5, tzinfo=UTC)
        h.sim.close_at_broker(p.trade_id, p.stop_price)
        day2 = datetime(2026, 11, 4, 0, 15, 10, tzinfo=UTC)
        for pair in PAIRS:
            set_market(h.sim, pair, now=day2)
        return day2, h.sim.txns[-1].amount

    def test_no_daily_snapshot_until_reconciled(self):
        h = Harness(self, settings=Settings(time_exit_minutes=None))
        day2, loss = self.overnight_stop_out(h)
        h.sim.fail_next = {"prices"}
        self.assertEqual(h.cycle(day2).blocked, "broker unavailable")
        self.assertNotEqual(h.ledger.day_snapshot()[0], uk_trading_day(day2))
        h.cycle(day2 + timedelta(seconds=30))
        self.assertAlmostEqual(h.ledger.day_snapshot()[1], h.sim.nav - loss, places=9)

    def test_close_after_nav_read_is_not_subtracted_from_snapshot(self):
        h = Harness(self, settings=Settings(time_exit_minutes=None))
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        p = h.ledger.position(EUR_ID)
        day2 = datetime(2026, 11, 4, 0, 15, 10, tzinfo=UTC)
        for pair in PAIRS:
            set_market(h.sim, pair, now=day2)
        real = h.sim.account

        def racing():
            acct = real()
            h.sim.close_at_broker(p.trade_id, p.tp_price)      # closes after NAV was read
            h.sim.account = real
            return acct
        h.sim.account = racing
        nav_before = h.sim.nav
        h.cycle(day2)
        self.assertAlmostEqual(h.ledger.day_snapshot()[1], nav_before, places=9)

    def test_open_trade_without_a_price_halts_instead_of_recording_zero_risk(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.sim.order_modes = ["timeout_filled"]
        h.cycle()
        real = h.sim.prices
        h.sim.prices = lambda pairs: {k: v for k, v in real(pairs).items() if k != "EUR_USD"}
        rep = h.cycle(LATER)
        self.assertTrue(rep.blocked.startswith("halted: reconciliation error"))
        self.assertNotEqual(h.ledger.position(EUR_ID).risk_gbp, 0.0)

    def test_adopting_a_trade_without_a_price_halts_instead_of_zero_risk(self):
        h = Harness(self)
        h.sim.add_trade("EUR_USD", 1000, 1.1, "fxb-EUR_USD-20261103T0800-B", 1.099)
        real = h.sim.prices
        h.sim.prices = lambda pairs: {k: v for k, v in real(pairs).items() if k != "EUR_USD"}
        rep = h.cycle()
        self.assertTrue(rep.blocked.startswith("halted: reconciliation error"))
        self.assertIsNone(h.ledger.position("fxb-EUR_USD-20261103T0800-B"))

    def test_missing_price_for_configured_pair_is_unhealthy_not_a_crash(self):
        h = Harness(self)
        real = h.sim.prices
        h.sim.prices = lambda pairs: {k: v for k, v in real(pairs).items() if k != "GBP_USD"}
        self.assertEqual(h.cycle().blocked, "stale or untradeable price GBP_USD")

    def test_missing_marker_cleared_when_trade_reappears(self):
        h = Harness(self)
        set_market(h.sim, "EUR_USD", breakout=+1)
        h.cycle()
        tid = h.ledger.position(EUR_ID).trade_id
        real = h.sim.open_trades
        h.sim.open_trades = lambda: [t for t in real() if t.trade_id != tid]   # briefly omitted
        h.cycle(LATER)
        h.sim.open_trades = real
        h.cycle(LATER + timedelta(seconds=5))
        self.assertIsNone(h.ledger.get(f"missing:{tid}"))
        self.assertEqual(h.ledger.position(EUR_ID).status, "filled")

    def test_deposit_racing_the_snapshot_is_not_a_loss(self):
        h = Harness(self)
        h.cycle()                                             # ledger initialised on day 1
        day2 = datetime(2026, 11, 4, 0, 15, 10, tzinfo=UTC)
        for pair in PAIRS:
            set_market(h.sim, pair, now=day2)
        real = h.sim.account

        def racing():
            h.sim.now = day2 + timedelta(seconds=1)
            h.sim.deposit(300.0)                              # lands just before NAV is read
            h.sim.account = real
            return real()
        h.sim.account = racing
        self.assertIsNone(h.cycle(day2).blocked)


if __name__ == "__main__":
    unittest.main()
