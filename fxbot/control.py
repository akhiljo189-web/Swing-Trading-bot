"""Phone controls over Telegram (spec §7): /status, /pause, /resume, /flatten.

Only messages from the configured chat are accepted. /resume and /flatten need a "YES" reply
within two minutes. Commands, pending confirmations and the update offset live in the ledger,
so a restart neither loses nor replays them.
"""
from collections.abc import Callable
from datetime import datetime, timedelta

from fxbot.alerts import Telegram
from fxbot.http import HttpError
from fxbot.ledger import Ledger

CONFIRM_WINDOW = timedelta(minutes=2)
HELP = "Commands: /status, /pause, /resume, /flatten"


class Control:
    def __init__(self, telegram: Telegram, ledger: Ledger, status: Callable[[], str]):
        self.telegram, self.ledger, self.status = telegram, ledger, status

    def _reply(self, text: str) -> None:
        self.telegram.send(text)

    def poll(self, now: datetime) -> None:
        offset = int(self.ledger.get("telegram_offset", "0"))
        try:
            updates = self.telegram.updates(offset)
        except HttpError:
            return
        for update in updates:
            self.ledger.set("telegram_offset", str(update["update_id"] + 1))
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id"))
            text = (message.get("text") or "").strip()
            if chat != self.telegram.chat_id:
                self.ledger.record(now, None, "command", f"ignored message from chat {chat}")
                continue
            self.ledger.record(now, None, "command", text[:100])
            self.handle(text, now)

    def handle(self, text: str, now: datetime) -> None:
        command = text.split()[0].lower() if text else ""
        if command == "/status":
            self._reply(self.status())
        elif command == "/pause":
            self.ledger.pause("paused by user (Telegram)")
            self._reply("Paused: no new trades. Open trades stay protected. Send /resume to continue.")
        elif command in ("/resume", "/flatten"):
            self.ledger.set("pending_command", command)
            self.ledger.set("pending_until", (now + CONFIRM_WINDOW).isoformat())
            action = "resume trading" if command == "/resume" else "close ALL bot trades"
            self._reply(f"Reply YES within 2 minutes to {action}.")
        elif command == "yes":
            pending, until = self.ledger.get("pending_command"), self.ledger.get("pending_until")
            self.ledger.set("pending_command", None)
            self.ledger.set("pending_until", None)
            if not pending or now > datetime.fromisoformat(until):
                self._reply("Nothing to confirm (or it expired). " + HELP)
            elif pending == "/resume":
                abandoned = self.ledger.resume()
                note = f" Unconfirmed orders marked abandoned: {', '.join(abandoned)}." if abandoned else ""
                self._reply("Resumed." + note)
            else:
                self.ledger.set("flatten_requested", now.isoformat())
                self._reply("Closing all bot trades now. You will get a message when done.")
        else:
            self._reply(HELP)
