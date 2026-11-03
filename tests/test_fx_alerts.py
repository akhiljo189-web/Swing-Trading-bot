import unittest
from datetime import datetime, timedelta, timezone

from fx_telegram import FakeTelegram
from fxbot.alerts import Alerter, Healthcheck, Telegram
from fxbot.http import HttpError
from fxbot.news import NewsEvent
from fxbot.newsfeed import LiveNewsFeed, fetch_forex_factory, parse_feed

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 9, 15, 10, tzinfo=UTC)


class NewsFeedTests(unittest.TestCase):
    def test_parse_forex_factory_items(self):
        items = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
                  "impact": "High"},
                 {"title": "Bank Holiday", "country": "GBP", "date": "", "impact": "Holiday"}]
        self.assertEqual(parse_feed(items), [NewsEvent(datetime(2026, 11, 6, 13, 30, tzinfo=UTC), "USD", "High")])

    def test_fetch_rejects_bad_responses(self):
        with self.assertRaises(HttpError):
            fetch_forex_factory(lambda *a: (500, {}))
        self.assertEqual(fetch_forex_factory(lambda *a: (200, [])), [])

    def test_keeps_last_good_copy_then_reports_unavailable(self):
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) > 1:
                raise HttpError("down")
            return [NewsEvent(NOW, "USD", "High")]
        feed = LiveNewsFeed(fetch)
        self.assertEqual(len(feed(NOW)), 1)
        self.assertEqual(len(feed(NOW + timedelta(hours=2))), 1)          # refresh failed, copy still fresh
        self.assertIsNone(feed(NOW + timedelta(hours=3, minutes=1)))      # too old: block entries
        self.assertIsNone(LiveNewsFeed(lambda: (_ for _ in ()).throw(HttpError("x")))(NOW))

    def test_failed_refresh_is_not_retried_every_call(self):
        calls = []
        feed = LiveNewsFeed(lambda: calls.append(1) or (_ for _ in ()).throw(HttpError("x")))
        feed(NOW)
        feed(NOW + timedelta(minutes=1))
        feed(NOW + timedelta(minutes=6))
        self.assertEqual(len(calls), 2)


class AlertTests(unittest.TestCase):
    def test_repeated_warnings_are_muted_for_an_hour(self):
        tg, now = FakeTelegram(), [NOW]
        a = Alerter(tg, lambda: now[0], log=lambda s: None)
        a("warning", "paused: untagged trade")
        a("warning", "paused: untagged trade")
        now[0] = NOW + timedelta(minutes=61)
        a("warning", "paused: untagged trade")
        a("critical", "no stop-loss")
        self.assertEqual(tg.sent, ["Warning: paused: untagged trade", "Warning: paused: untagged trade",
                                   "CRITICAL: no stop-loss"])

    def test_telegram_request_shape(self):
        seen = []
        tg = Telegram("abc", "42", transport=lambda *a: seen.append(a) or (200, {"ok": True}))
        self.assertTrue(tg.send("hi"))
        method, url, _, body, _ = seen[0]
        self.assertEqual((method, url, body), ("POST", "https://api.telegram.org/botabc/sendMessage",
                                               {"chat_id": "42", "text": "hi"}))
        failing = Telegram("abc", "42", transport=lambda *a: (_ for _ in ()).throw(HttpError("down")))
        self.assertFalse(failing.send("hi"))

    def test_healthcheck_ping(self):
        self.assertTrue(Healthcheck("https://hc-ping.com/x", get_status=lambda url: 200).ping())
        self.assertFalse(Healthcheck("https://hc-ping.com/x", get_status=lambda url: 0).ping())


if __name__ == "__main__":
    unittest.main()
