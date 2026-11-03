import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from fxbot.__main__ import main
from fxbot.ledger import Ledger

NO_CREDS = {k: v for k, v in os.environ.items() if not k.startswith(("OANDA_", "TELEGRAM_"))}


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_pause_resume_status_while_stopped(self):
        code, out, _ = run("pause", "--data-dir", self.dir)
        self.assertEqual(code, 0)
        self.assertIn("Paused: paused by user (command line)", out)
        code, out, _ = run("resume", "--data-dir", self.dir)
        self.assertIn("Not paused", out)

    def test_status_with_missing_data_dir_is_a_clean_error(self):
        code, _, err = run("status", "--data-dir", f"{self.dir}/does-not-exist")
        self.assertEqual(code, 2)
        self.assertIn("does-not-exist", err)

    def test_status_refuses_while_bot_holds_the_ledger(self):
        held = Ledger(f"{self.dir}/ledger.db")
        self.addCleanup(held.close)
        code, _, err = run("status", "--data-dir", self.dir)
        self.assertEqual(code, 2)
        self.assertIn("Telegram", err)

    def test_live_and_fetch_data_need_credentials(self):
        with mock.patch.dict(os.environ, NO_CREDS, clear=True):
            self.assertEqual(run("live")[0], 2)
            code, _, err = run("fetch-data", "--start", "2024-01-01", "--end", "2024-02-01", "--out", self.dir)
            self.assertEqual(code, 2)
            self.assertIn("OANDA_API_TOKEN", err)

    def test_live_refuses_real_money_without_confirmation(self):
        env = {**NO_CREDS, "OANDA_API_TOKEN": "t", "OANDA_ACCOUNT_ID": "1", "OANDA_ENV": "live"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, _, err = run("live")
        self.assertEqual(code, 2)
        self.assertIn("LIVE_TRADING_CONFIRMED", err)


if __name__ == "__main__":
    unittest.main()
