import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fx_telegram import FakeTelegram, msg
from fxbot.control import Control
from fxbot.ledger import Ledger

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)


class ControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Ledger(f"{tmp.name}/l.db")
        self.addCleanup(self.ledger.close)
        self.tg = FakeTelegram()
        self.control = Control(self.tg, self.ledger, lambda: "STATUS")

    def say(self, *texts, chat=42, at=NOW, sent=None):
        base = int(self.ledger.get("telegram_offset", "0"))
        when = int((sent or at).timestamp())
        self.tg.inbox += [msg(base + i, t, chat, when) for i, t in enumerate(texts)]
        return self.control.poll(at)

    def test_status_and_pause(self):
        self.say("/status", "/pause")
        self.assertEqual(self.tg.sent[0], "STATUS")
        self.assertEqual(self.ledger.pause_reason(), "paused by user (Telegram)")

    def test_other_chats_are_ignored(self):
        self.say("/pause", chat=999)
        self.assertIsNone(self.ledger.pause_reason())
        self.assertEqual(self.tg.sent, [])

    def test_resume_needs_yes_within_two_minutes(self):
        self.ledger.halt("halted: x")
        self.say("/resume")
        self.assertEqual(self.ledger.halt_reason(), "halted: x")
        self.say("YES", at=NOW + timedelta(minutes=3))                   # too late
        self.assertEqual(self.ledger.halt_reason(), "halted: x")
        self.say("/resume", "yes")
        self.assertIsNone(self.ledger.halt_reason())

    def test_flatten_sets_request_after_confirmation(self):
        self.say("/flatten")
        self.assertIn("pause", self.tg.sent[-1])
        self.assertIsNone(self.ledger.get("flatten_requested"))
        self.say("YES")
        self.assertIsNotNone(self.ledger.get("flatten_requested"))

    def test_updates_are_processed_once_across_restarts(self):
        self.say("/pause")
        self.ledger.resume()
        Control(self.tg, self.ledger, lambda: "S").poll(NOW)           # same inbox, new instance
        self.assertIsNone(self.ledger.pause_reason())

    def test_old_messages_are_never_replayed(self):
        self.say("/pause", sent=NOW - timedelta(hours=5))
        self.assertIsNone(self.ledger.pause_reason())
        self.assertIn("resend", self.tg.sent[-1])

    def test_command_with_bot_name(self):
        self.say("/status@fxbot_helper_bot")
        self.assertEqual(self.tg.sent[0], "STATUS")

    def test_confirmed_flatten_also_pauses_entries(self):
        self.say("/flatten", "yes")
        self.assertIsNotNone(self.ledger.pause_reason())
        self.assertIn("/resume", self.tg.sent[-1])

    def test_poll_reports_failures(self):
        def broken(offset):
            raise ValueError("garbled")
        self.tg.updates = broken
        self.assertFalse(self.control.poll(NOW))

    def test_unknown_text_gets_help(self):
        self.say("hello")
        self.assertIn("/status", self.tg.sent[-1])


if __name__ == "__main__":
    unittest.main()
