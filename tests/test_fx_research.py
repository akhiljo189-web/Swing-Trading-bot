import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fx_history import START, make_history
from fxbot import research
from fxbot.config import Settings
from fxbot.history import save
from fxbot.research import (add_months, gate_b, random_direction, trend_baseline, windows,
                            write_report)
from fxbot.strategy import LONDON, Signal

UTC = timezone.utc
S = Settings()
TINY_GRID = {"stop_atr_mult": (1.5, 2.0)}


class BaselineTests(unittest.TestCase):
    def test_trend_baseline_only_at_session_opens(self):
        h = make_history(40)
        h1s, m15s = h.get("EUR_USD", "H1"), h.get("EUR_USD", "M15")
        seen = set()
        for t in m15s.times[2600:]:
            now = t + timedelta(minutes=15, seconds=10)
            sig = trend_baseline("EUR_USD", h1s.mids[:h1s.completed_count(now)],
                                 m15s.mids[:m15s.completed_count(now)], S)
            if sig:
                seen.add((sig.candle_time + timedelta(minutes=15)).astimezone(LONDON).strftime("%H:%M"))
        self.assertTrue(seen)
        self.assertLessEqual(seen, {"07:00", "13:00"})

    def test_random_direction_keeps_times_and_is_seeded(self):
        t = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)
        base = {("EUR_USD", t): Signal("EUR_USD", 1, t, 0.001), ("EUR_USD", t + timedelta(minutes=15)): None}

        class C:
            time = t
        sides = [random_direction(base, seed)("EUR_USD", [], [C], S).side for seed in range(40)]
        self.assertEqual(set(sides), {1, -1})
        self.assertEqual(random_direction(base, 7)("EUR_USD", [], [C], S),
                         random_direction(base, 7)("EUR_USD", [], [C], S))


class WalkForwardTests(unittest.TestCase):
    def test_add_months(self):
        self.assertEqual(add_months(datetime(2025, 11, 1, tzinfo=UTC), 3), datetime(2026, 2, 1, tzinfo=UTC))

    def test_windows_do_not_overlap_and_test_after_train(self):
        w = windows(datetime(2020, 1, 1, tzinfo=UTC), datetime(2023, 1, 1, tzinfo=UTC), 24, 6)
        self.assertEqual(len(w), 2)
        for tr_s, tr_e, te_s, te_e in w:
            self.assertEqual(tr_e, te_s)
            self.assertLessEqual(te_e, datetime(2023, 1, 1, tzinfo=UTC))
        self.assertEqual(w[0][3], w[1][2])


class GridSelectionTests(unittest.TestCase):
    def test_walk_forward_picks_best_training_expectancy(self):
        from fxbot.backtest import BacktestResult, TradeRecord
        t0 = datetime(2026, 1, 6, 9, tzinfo=UTC)

        def fake_run(history, settings, start, end, **kw):
            r = 1.0 if settings.stop_atr_mult == 2.0 else 0.1     # 2.0 is clearly better
            trades = [TradeRecord("EUR_USD", 1, 1, t0, t0, 1, 1, r * 2.5, 2.5, "target")] * 40
            return BacktestResult(1000, 1000, trades, {})
        original = research.run
        research.run = fake_run
        try:
            plan = research.walk_forward(None, S, datetime(2020, 1, 1, tzinfo=UTC),
                                         datetime(2021, 1, 1, tzinfo=UTC), None, 6, 3,
                                         {"stop_atr_mult": (1.5, 2.0, 1.0)})
        finally:
            research.run = original
        self.assertEqual([s.stop_atr_mult for s, _, _ in plan], [2.0, 2.0])


def fake_report(**over):
    good = {"expectancy_r": 0.2, "net_gbp": 50.0, "trades": 150, "max_drawdown": 0.05, "halted": None}
    report = {"walk_forward": dict(good), "walk_forward_slippage_0.6": dict(good),
              "trend_baseline": {"expectancy_r": 0.05}, "no_skill": {"expectancy_r": [0.0, 0.05, 0.1]}}
    for key, value in over.items():
        section, field = key.split("__")
        report[section][field] = value
    return report


class GateBTests(unittest.TestCase):
    def test_pass(self):
        g = gate_b(fake_report())
        self.assertTrue(g["passed"])
        self.assertFalse(g["checks"]["oos_trades_preferred_200"])   # preferred, not required

    def test_each_requirement_can_fail(self):
        for over in ({"walk_forward__expectancy_r": -0.01}, {"walk_forward__trades": 99},
                     {"walk_forward_slippage_0.6__net_gbp": -1.0}, {"trend_baseline__expectancy_r": 0.3},
                     {"walk_forward__max_drawdown": 0.10}, {"walk_forward__halted": "halted: x"}):
            self.assertFalse(gate_b(fake_report(**over))["passed"], over)
        r = fake_report()
        r["no_skill"]["expectancy_r"] = [0.0, 0.3, 0.3]
        self.assertFalse(gate_b(r)["passed"])


class ResearchEndToEndTests(unittest.TestCase):
    def test_full_report_and_cli(self):
        h = make_history(80)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            for (pair, g), s in h.series.items():
                save(data / f"{pair}_{g}.csv", s.candles)
            original = research.GRID
            research.GRID = TINY_GRID
            try:
                from fxbot.__main__ import main
                code = main(["backtest", "--data", str(data), "--start", "2026-02-02", "--end", "2026-03-23",
                             "--train-months", "1", "--test-months", "1", "--no-skill-runs", "3",
                             "--processes", "1", "--out", f"{tmp}/out"])
            finally:
                research.GRID = original
            self.assertEqual(code, 0)
            report = json.loads((Path(tmp) / "out" / "research.json").read_text())
            self.assertEqual(set(report["slippage_sensitivity"]), {"0.0", "0.3", "0.6", "1.0"})
            self.assertEqual(len(report["no_skill"]["expectancy_r"]), 3)
            self.assertIn("passed", report["gate_b"])
            self.assertGreater(report["walk_forward"]["trades"], 0)
            self.assertIn("Gate B", (Path(tmp) / "out" / "research.md").read_text())

    def test_parallel_no_skill_matches_serial(self):
        h = make_history(50)
        s, e = START + timedelta(days=40), START + timedelta(days=47)
        base = research.run(h, S, s, e).signals
        serial = research.no_skill(h, S, s, e, None, base, 4, processes=1)
        parallel = research.no_skill(h, S, s, e, None, base, 4, processes=2)
        self.assertEqual(serial, parallel)


if __name__ == "__main__":
    unittest.main()
