import tempfile
import unittest
from datetime import date, datetime, timezone

from fxbot.ledger import Ledger, LedgerError, LedgerLocked

UTC = timezone.utc
T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = f"{tmp.name}/ledger.db"
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.path)

    def fill(self, cid="fxb-a", pair="EUR_USD", at=T0):
        self.ledger.create_intent(cid, pair, 1, T0, at)
        self.ledger.mark_filled(cid, "T" + cid, 1000, 1.1, 1.099, 0.0001, 1.0, 0.1, 846.0, at)

    def test_second_process_is_refused(self):
        with self.assertRaises(LedgerLocked):
            Ledger(self.path)

    def test_corrupt_file_is_refused(self):
        bad = self.path + ".bad"
        with open(bad, "wb") as f:
            f.write(b"this is not a database" * 100)
        with self.assertRaises(LedgerError):
            Ledger(bad)

    def test_client_id_can_never_be_reused(self):
        self.fill()
        self.ledger.mark_closed("Tfxb-a", T0, -2.5)
        with self.assertRaises(LedgerError):
            self.ledger.create_intent("fxb-a", "EUR_USD", 1, T0, T0)

    def test_fill_records_split_risk(self):
        self.fill()
        p = self.ledger.position("fxb-a")
        self.assertEqual((p.status, p.units, p.trade_id), ("filled", 1000, "Tfxb-a"))
        self.assertAlmostEqual(p.risk_gbp, 1.1)
        self.assertEqual((p.stop_risk_gbp, p.cost_risk_gbp), (1.0, 0.1))

    def test_resume_abandons_unresolved_intents(self):
        self.ledger.create_intent("fxb-x", "EUR_USD", 1, T0, T0)
        self.ledger.halt("halted: unresolved order fxb-x")
        self.ledger.halt("halted: something later")          # first reason is kept
        self.assertEqual(self.ledger.halt_reason(), "halted: unresolved order fxb-x")
        self.assertEqual(self.ledger.resume(), ["fxb-x"])
        self.assertIsNone(self.ledger.halt_reason())
        self.assertEqual(self.ledger.position("fxb-x").status, "abandoned")
        self.assertTrue(self.ledger.has_client_id("fxb-x"))

    def test_entries_counted_by_uk_day(self):
        self.fill("fxb-1", at=datetime(2026, 7, 7, 22, 30, tzinfo=UTC))   # 23:30 BST, 7 July
        self.fill("fxb-2", at=datetime(2026, 7, 7, 23, 30, tzinfo=UTC))   # 00:30 BST, 8 July
        self.assertEqual(self.ledger.entries_today(date(2026, 7, 8)), {"EUR_USD": 1})

    def test_contributions_after_snapshot_only(self):
        self.ledger.add_contribution(1, datetime(2026, 11, 3, 8, 0, tzinfo=UTC), 100.0)
        self.ledger.add_contribution(2, datetime(2026, 11, 3, 10, 0, tzinfo=UTC), 300.0)
        self.ledger.add_contribution(3, datetime(2026, 11, 3, 11, 0, tzinfo=UTC), -50.0)
        self.ledger.add_contribution(2, datetime(2026, 11, 3, 10, 0, tzinfo=UTC), 300.0)  # replay
        self.assertEqual(self.ledger.contributions_since(T0), (300.0, 50.0))

    def test_state_survives_restart(self):
        self.ledger.set_last_txn_id(42)
        self.ledger.set_day_snapshot(date(2026, 11, 3), 1000.0, T0)
        self.ledger.latch_daily_loss(date(2026, 11, 3))
        self.ledger.pause("paused by user")
        self.reopen()
        self.assertEqual(self.ledger.last_txn_id, 42)
        self.assertEqual(self.ledger.day_snapshot(), (date(2026, 11, 3), 1000.0, T0))
        self.assertTrue(self.ledger.daily_loss_latched(date(2026, 11, 3)))
        self.assertFalse(self.ledger.daily_loss_latched(date(2026, 11, 4)))
        self.assertEqual(self.ledger.pause_reason(), "paused by user")


if __name__ == "__main__":
    unittest.main()
