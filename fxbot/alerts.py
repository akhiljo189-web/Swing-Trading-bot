"""Telegram messages, de-duplicated warnings, and the healthchecks.io ping (spec §7)."""
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from fxbot.http import HttpError, Transport, urllib_transport


class Telegram:
    def __init__(self, token: str, chat_id: str, transport: Transport = urllib_transport):
        self.base, self.chat_id, self._send = f"https://api.telegram.org/bot{token}", str(chat_id), transport

    def send(self, text: str) -> bool:
        try:
            status, _ = self._send("POST", f"{self.base}/sendMessage", {"Content-Type": "application/json"},
                                   {"chat_id": self.chat_id, "text": text[:4000]}, 10)
        except Exception:   # an alert channel failure must never stop the bot
            return False
        return status == 200

    def updates(self, offset: int) -> list[dict]:
        status, data = self._send("GET", f"{self.base}/getUpdates?offset={offset}&timeout=0", {}, None, 10)
        if status != 200 or not data.get("ok"):
            raise HttpError(f"telegram getUpdates returned {status}")
        return data.get("result", [])


class Alerter:
    """The engine's alert callback. Repeats of the same warning are muted for `dedupe`."""

    def __init__(self, telegram: Telegram | None, clock, dedupe: timedelta = timedelta(hours=1), log=print):
        self.telegram, self.clock, self.dedupe, self.log = telegram, clock, dedupe, log
        self._last: dict[tuple[str, str], datetime] = {}

    def __call__(self, level: str, text: str) -> None:
        now = self.clock()
        self.log(f"{now.isoformat()} {level.upper()} {text}")
        key = (level, text)
        if key in self._last and now - self._last[key] < self.dedupe:
            return
        self._last[key] = now
        if self.telegram:
            self.telegram.send(f"{'CRITICAL' if level == 'critical' else 'Warning'}: {text}")

    def info(self, text: str) -> None:
        self.log(f"{self.clock().isoformat()} INFO {text}")
        if self.telegram:
            self.telegram.send(text)


def http_status(url: str) -> int:
    """GET a URL and return only its status code (healthchecks.io replies with plain text)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fxbot"}),
                                    timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


class Healthcheck:
    """Dead-man's switch: if these pings stop, healthchecks.io alerts the user."""

    def __init__(self, url: str, get_status=http_status):
        self.url, self._get = url, get_status

    def ping(self) -> bool:
        return self._get(self.url) == 200
