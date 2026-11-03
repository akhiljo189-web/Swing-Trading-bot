"""One 15-minute trading cycle, shared by live trading and backtests (spec §5-§7).

Safety rule: when anything is uncertain, new entries stop; existing trades stay protected.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fxbot.broker import BOT_TAG, Broker, BrokerError, Price, Trade, is_bot
from fxbot.config import Settings
from fxbot.costs import passes_cost_filter, round_trip_cost
from fxbot.instruments import INSTRUMENTS
from fxbot.ledger import Ledger
from fxbot.news import NewsEvent, news_blocked
from fxbot.risk import (AccountState, Candidate, OpenPosition, daily_loss_breached,
                        evaluate as approve, uk_trading_day)
from fxbot.strategy import LONDON, entry_window_open, evaluate as signal_for

NewsSource = Callable[[datetime], list[NewsEvent] | None]   # None = feed unavailable
Alert = Callable[[str, str], None]                           # (level, text)

M15 = timedelta(minutes=15)


@dataclass
class CycleReport:
    at: datetime
    blocked: str | None = None
    filled: list[str] = field(default_factory=list)            # client IDs
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (pair, reason)
    exits: list[str] = field(default_factory=list)             # trade IDs closed by time exit


def client_id(pair: str, side: int, candle_time: datetime) -> str:
    return f"{BOT_TAG}{pair}-{candle_time:%Y%m%dT%H%M}-{'B' if side > 0 else 'S'}"


class Engine:
    def __init__(self, broker: Broker, ledger: Ledger, settings: Settings,
                 news: NewsSource, alert: Alert):
        self.broker, self.ledger, self.s, self.news, self._alert_fn = broker, ledger, settings, news, alert

    # --- helpers -----------------------------------------------------------
    def alert(self, level: str, text: str) -> None:
        try:
            self._alert_fn(level, text)
        except Exception:   # a broken alert channel must never stop the engine
            pass

    def _fill(self, cid: str, side: int, trade_id: str, units: int, fill: float,
              stop: float | None, cost: float, price: Price, at: datetime) -> None:
        n = abs(units)
        stop_dist = abs(fill - stop) if stop is not None else 0.0
        self.ledger.mark_filled(cid, trade_id, n, fill, stop, cost,
                                n * stop_dist * price.loss_factor, n * cost * price.loss_factor,
                                n * fill * price.value_factor, at)

    def _set_take_profit(self, cid: str, trade_id: str, side: int, fill: float, stop: float) -> None:
        tp = fill + side * self.s.reward_r * abs(fill - stop)
        try:
            ok = self.broker.set_take_profit(trade_id, tp)
        except BrokerError:
            ok = False
        if ok:
            self.ledger.set_take_profit(cid, tp)
        else:
            self.alert("warning", f"take-profit not set on trade {trade_id}; will retry")

    def _close_unprotected(self, trade_id: str, now: datetime) -> None:
        try:
            closed = self.broker.close_trade(trade_id)
        except BrokerError:
            closed = False
        self.ledger.halt(f"halted: trade {trade_id} had no stop-loss")
        self.alert("critical", f"trade {trade_id} had no stop-loss; "
                   f"{'closed' if closed else 'CLOSE FAILED - close it in the OANDA app'}; bot halted")

    # --- reconciliation (spec §5 step 3) ----------------------------------
    def _reconcile(self, now: datetime, trades: list[Trade], prices: dict[str, Price]) -> None:
        by_id = {t.trade_id: t for t in trades}
        for txn in self.broker.transactions_since(self.ledger.last_txn_id):
            if txn.kind == "FILL" and is_bot(txn.client_id):
                pos = self.ledger.position(txn.client_id)
                if pos is not None and pos.status in ("submitting", "abandoned"):
                    t = by_id.get(txn.trade_id)
                    stop = t.stop_price if t else None
                    cost = round_trip_cost(txn.pair, prices[txn.pair].ask - prices[txn.pair].bid, self.s)
                    self._fill(txn.client_id, pos.side, txn.trade_id, txn.units, txn.price, stop,
                               cost, prices[txn.pair], txn.time)
                    self.ledger.record(now, txn.pair, "reconciled", f"fill confirmed {txn.client_id}")
            elif txn.kind == "CLOSE":
                if self.ledger.mark_closed(txn.trade_id, txn.time, txn.amount):
                    self.ledger.record(now, txn.pair, "closed", f"trade {txn.trade_id} P&L {txn.amount:.2f}")
            elif txn.kind == "REJECT" and is_bot(txn.client_id):
                pos = self.ledger.position(txn.client_id)
                if pos is not None and pos.status == "submitting":
                    self.ledger.mark_failed(txn.client_id, "rejected by broker")
            elif txn.kind == "TRANSFER":
                self.ledger.add_contribution(txn.txn_id, txn.time, txn.amount)
            self.ledger.set_last_txn_id(txn.txn_id)

        known = {p.trade_id for p in self.ledger.open_positions()}
        for t in trades:   # bot trades the ledger lost track of (e.g. database rebuilt)
            if is_bot(t.client_id) and t.trade_id not in known:
                side = 1 if t.units > 0 else -1
                if self.ledger.position(t.client_id) is None:
                    self.ledger.create_intent(t.client_id, t.pair, side, t.open_time, now)
                cost = round_trip_cost(t.pair, prices[t.pair].ask - prices[t.pair].bid, self.s)
                self._fill(t.client_id, side, t.trade_id, t.units, t.open_price, t.stop_price,
                           cost, prices[t.pair], t.open_time)
                if t.take_profit_price is not None:
                    self.ledger.set_take_profit(t.client_id, t.take_profit_price)
                self.ledger.record(now, t.pair, "adopted", f"bot trade {t.trade_id} restored from broker")
        for p in self.ledger.open_positions():
            if p.trade_id not in by_id:   # closed at broker but no CLOSE transaction seen
                self.ledger.mark_closed(p.trade_id, now, 0.0)
                self.alert("warning", f"trade {p.trade_id} closed without a transaction; P&L unknown")

    # --- the cycle ---------------------------------------------------------
    def run_cycle(self, now: datetime) -> CycleReport:
        rep, s, ledger = CycleReport(now), self.s, self.ledger
        blocks: list[str] = []

        def block(reason: str) -> None:
            if reason not in blocks:
                blocks.append(reason)

        for reason in (ledger.halt_reason(), ledger.pause_reason()):
            if reason:
                block(reason)
        try:
            account = self.broker.account()
            trades = self.broker.open_trades()
            pending = self.broker.pending_orders()
            prices = self.broker.prices(s.pairs)
            self._reconcile(now, trades, prices)
        except BrokerError as exc:
            self.alert("warning", f"broker unavailable: {exc}")
            ledger.record(now, None, "blocked", "broker unavailable")
            rep.blocked = "broker unavailable"
            return rep

        # account hygiene
        if any(not is_bot(t.client_id) for t in trades) or any(not is_bot(o.client_id) for o in pending):
            block("paused: untagged trade or order on the account")
        unresolved = ledger.submitting()
        if unresolved:
            ledger.halt(f"halted: unresolved order {unresolved[0]}")
            self.alert("critical", f"order {unresolved[0]} could not be confirmed; bot halted")
            block(ledger.halt_reason())
        for t in trades:
            if is_bot(t.client_id) and t.stop_price is None:
                self._close_unprotected(t.trade_id, now)
                block(ledger.halt_reason())

        # take-profit retries and time exits
        broker_trades = {t.trade_id: t for t in trades}
        closed_now: set[str] = set()
        try:
            for p in ledger.open_positions():
                t = broker_trades.get(p.trade_id)
                if t is None or t.stop_price is None:
                    continue
                if p.tp_price is None:
                    if t.take_profit_price is not None:
                        ledger.set_take_profit(p.client_id, t.take_profit_price)
                    else:
                        self._set_take_profit(p.client_id, p.trade_id, p.side, p.fill_price, t.stop_price)
                if s.time_exit_minutes and now - p.opened_at >= timedelta(minutes=s.time_exit_minutes):
                    if self.broker.close_trade(p.trade_id):
                        closed_now.add(p.trade_id)
                        rep.exits.append(p.trade_id)
                        ledger.record(now, p.pair, "exit", f"time exit trade {p.trade_id}")
                    else:
                        self.alert("warning", f"time exit failed for trade {p.trade_id}; will retry")
        except BrokerError as exc:
            self.alert("warning", f"broker error managing trades: {exc}")
            block("broker unavailable")

        # daily loss (spec §6)
        day = uk_trading_day(now)
        snap = ledger.day_snapshot()
        if snap is None or snap[0] != day:
            activity = any((p.opened_at and uk_trading_day(p.opened_at) == day) or
                           (p.closed_at and uk_trading_day(p.closed_at) == day)
                           for p in ledger.all_positions())
            ledger.set_day_snapshot(day, account.nav, now)
            if activity:   # trading already happened today: start-of-day NAV is unknown
                ledger.set("daily_snapshot_unreliable", day.isoformat())
            snap = ledger.day_snapshot()
        if ledger.get("daily_snapshot_unreliable") == day.isoformat():
            block("no reliable daily snapshot today")
        deposits, withdrawals = ledger.contributions_since(snap[2])
        if ledger.daily_loss_latched(day) or daily_loss_breached(snap[1], account.nav, deposits, withdrawals, s):
            if not ledger.daily_loss_latched(day):
                ledger.latch_daily_loss(day)
                self.alert("warning", "daily loss limit reached; no new entries until tomorrow")
            block("daily loss limit reached")

        events = self.news(now)
        if events is None:
            block("news feed unavailable")

        if blocks:
            rep.blocked = blocks[0]
            ledger.record(now, None, "blocked", blocks[0])
            return rep
        try:
            self._scan_and_enter(now, day, account, prices, events, closed_now, blocks, rep)
        except BrokerError as exc:
            self.alert("warning", f"broker error during scan: {exc}")
            ledger.record(now, None, "blocked", "broker unavailable")
            rep.blocked = "broker unavailable"
        return rep

    def _healthy(self, now: datetime, prices: dict[str, Price]) -> tuple[dict, dict, str | None]:
        s = self.s
        last_m15 = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0) - M15
        last_h1 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        h1, m15 = {}, {}
        for pair in s.pairs:
            h1[pair] = self.broker.candles(pair, "H1", s.h1_window)
            m15[pair] = self.broker.candles(pair, "M15", s.m15_window)
            if not h1[pair] or not m15[pair] or h1[pair][-1].time != last_h1 or m15[pair][-1].time != last_m15:
                return h1, m15, f"stale candles {pair}"
            p = prices[pair]
            if (not p.tradeable or p.loss_factor <= 0 or p.value_factor <= 0
                    or now - p.time > timedelta(seconds=s.price_max_age_seconds)):
                return h1, m15, f"stale or untradeable price {pair}"
        return h1, m15, None

    def _scan_and_enter(self, now, day, account, prices, events, closed_now, blocks, rep) -> None:
        s, ledger = self.s, self.ledger
        h1, m15, unhealthy = self._healthy(now, prices)
        if unhealthy:
            rep.blocked = unhealthy
            ledger.record(now, None, "blocked", unhealthy)
            return

        candidates = []
        for pair in s.pairs:
            sig = signal_for(pair, h1[pair], m15[pair], s)
            if sig is None:
                continue
            price = prices[pair]
            cost = round_trip_cost(pair, price.ask - price.bid, s)
            if not entry_window_open(sig.candle_time + M15, s):
                reason = "outside session"
            elif not passes_cost_filter(cost, sig.stop_distance, s):
                reason = f"cost {cost / sig.stop_distance:.0%} of stop"
            elif news_blocked(pair, now, events, s):
                reason = "news blackout"
            else:
                info = self.broker.instrument(pair)
                entry = price.ask if sig.side > 0 else price.bid
                candidates.append(Candidate(sig, cost, entry, price.loss_factor, price.value_factor,
                                            info.margin_rate, info.max_units))
                continue
            rep.rejected.append((pair, reason))
            ledger.record(now, pair, "rejected", reason)
        candidates.sort(key=lambda c: c.cost / c.signal.stop_distance)

        positions = [OpenPosition(p.pair, p.side, p.units, p.risk_gbp, p.notional_gbp)
                     for p in ledger.open_positions() if p.trade_id not in closed_now]
        entries = ledger.entries_today(day)
        margin_used = account.margin_used
        for c in candidates:
            sig = c.signal
            state = AccountState(account.nav, margin_used, tuple(positions), entries,
                                 blocks[0] if blocks else None)
            decision = approve(c, state, s)
            cid = client_id(sig.pair, sig.side, sig.candle_time)
            if decision.approved and ledger.has_client_id(cid):
                decision = type(decision)(False, "duplicate signal")
            if not decision.approved:
                rep.rejected.append((sig.pair, decision.reason))
                ledger.record(now, sig.pair, "rejected", decision.reason)
                continue

            ledger.create_intent(cid, sig.pair, sig.side, sig.candle_time, now)   # before sending
            bound = c.price + sig.side * s.price_bound_pips * INSTRUMENTS[sig.pair].pip
            # a BrokerError here propagates: the intent stays 'submitting' and the next
            # cycle's reconciliation resolves it or halts (never resends)
            result = self.broker.market_order(sig.pair, sig.side * decision.units, cid,
                                              sig.stop_distance, bound)
            if result.status == "rejected":
                ledger.mark_failed(cid, result.reason)
                rep.rejected.append((sig.pair, f"broker rejected: {result.reason}"))
                ledger.record(now, sig.pair, "rejected", f"broker rejected: {result.reason}")
                continue
            if result.status != "filled":
                blocks.append(f"unresolved order {cid}")   # exposure unknown: stop entering
                self.alert("warning", f"order {cid} outcome unknown; checking next cycle")
                ledger.record(now, sig.pair, "unknown", f"order {cid} outcome unknown")
                continue

            price = prices[sig.pair]
            self._fill(cid, sig.side, result.trade_id, result.units, result.fill_price,
                       result.stop_price, c.cost, price, now)
            rep.filled.append(cid)
            ledger.record(now, sig.pair, "filled", f"{cid} {result.units} @ {result.fill_price}")
            if result.stop_price is None:
                self._close_unprotected(result.trade_id, now)
                blocks.append(ledger.halt_reason())
                continue
            self._set_take_profit(cid, result.trade_id, sig.side, result.fill_price, result.stop_price)
            filled = ledger.position(cid)   # risk recomputed from the actual fill
            positions.append(OpenPosition(sig.pair, sig.side, filled.units, filled.risk_gbp,
                                          filled.notional_gbp))
            entries = {**entries, sig.pair: entries.get(sig.pair, 0) + 1}
            margin_used += filled.notional_gbp * c.margin_rate

    # --- Friday flatten and /flatten (spec §7) ------------------------------
    def flatten(self, now: datetime) -> int:
        """Close every bot trade. Returns how many remain open (call again until 0)."""
        try:
            bot_trades = [t for t in self.broker.open_trades() if is_bot(t.client_id)]
        except BrokerError as exc:
            self.alert("warning", f"flatten: broker unavailable: {exc}")
            remaining = len(self.ledger.open_positions())
        else:
            remaining = 0
            for t in bot_trades:
                try:
                    ok = self.broker.close_trade(t.trade_id)
                except BrokerError:
                    ok = False
                if not ok:
                    remaining += 1
            self.ledger.record(now, None, "flatten", f"{len(bot_trades) - remaining} closed, {remaining} open")
        if remaining and weekend_phase(now, self.s) == "deadline":
            self.alert("critical", f"{remaining} bot trade(s) still open after Friday 20:00 - "
                       "close them in the OANDA app")
        return remaining


def weekend_phase(now: datetime, settings: Settings) -> str:
    """'normal', 'flatten' (Friday from 19:30 London) or 'deadline' (Friday from 20:00)."""
    local = now.astimezone(LONDON)
    if local.weekday() != 4 or local.time() < settings.friday_flatten_start:
        return "normal"
    return "deadline" if local.time() >= settings.friday_flatten_deadline else "flatten"
