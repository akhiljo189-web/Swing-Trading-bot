import tempfile
import unittest
from datetime import timedelta

from fx_fixtures import NOW, Harness, set_market
from fx_history import START, make_history
from fxbot.config import Settings
from fxbot.indicators import atr
from fxbot.ledger import Ledger
from fxbot.strategy import Signal, _breakout, evaluate, trend_direction

S = Settings()


def reference_evaluate(pair, h1, m15, s):
    """Plan 1's original order (trend first, then breakout) - the optimised version must match."""
    if len(h1) < s.h1_window or len(m15) < s.m15_window:
        return None
    h1, m15 = h1[-s.h1_window:], m15[-s.m15_window:]
    if h1[-1].time + timedelta(hours=1) > m15[-1].time + timedelta(minutes=15):
        return None
    trend = trend_direction(h1, s)
    if trend == 0 or _breakout(m15, s.breakout_lookback) != trend:
        return None
    a = atr([c.high for c in m15], [c.low for c in m15], [c.close for c in m15], s.atr_period)[-1]
    return Signal(pair, trend, m15[-1].time, s.stop_atr_mult * a)


class OptimisedStrategyTests(unittest.TestCase):
    def test_breakout_first_matches_reference_on_synthetic_history(self):
        h = make_history(60)
        checked = signals = 0
        for pair in ("EUR_USD", "USD_JPY"):
            h1s, m15s = h.get(pair, "H1"), h.get(pair, "M15")
            for t in m15s.times[3000:4500]:
                now = t + timedelta(minutes=15, seconds=10)
                h1 = h1s.mids[:h1s.completed_count(now)]
                m15 = m15s.mids[:m15s.completed_count(now)]
                expected = reference_evaluate(pair, h1, m15, S)
                self.assertEqual(evaluate(pair, h1, m15, S), expected)
                checked += 1
                signals += expected is not None
        self.assertGreater(checked, 2000)
        self.assertGreater(signals, 10)            # the comparison covered real signals


class EngineSignalHookTests(unittest.TestCase):
    def test_engine_uses_injected_signal_function(self):
        h = Harness(self)
        calls = []

        def never(pair, h1, m15, s):
            calls.append(pair)
            return None
        h.engine.signal_fn = never
        set_market(h.sim, "EUR_USD", breakout=+1)
        self.assertEqual(h.cycle().filled, [])
        self.assertEqual(len(calls), 5)


class NonDurableLedgerTests(unittest.TestCase):
    def test_non_durable_ledger_works_and_is_still_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(f"{tmp}/l.db", durable=False)
            ledger.set_last_txn_id(7)
            self.assertEqual(ledger.last_txn_id, 7)
            with self.assertRaises(Exception):
                Ledger(f"{tmp}/l.db")
            ledger.close()


if __name__ == "__main__":
    unittest.main()
