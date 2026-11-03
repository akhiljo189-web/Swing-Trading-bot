"""Phone controls over Telegram (spec §7): /status, /pause, /resume, /flatten.

Only messages from the configured chat are accepted. /resume and /flatten need a "YES" reply
within two minutes. Commands, pending confirmations and the update offset live in the ledger,
so a restart neither loses nor replays them.
"""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fxbot.alerts import Telegram
from fxbot.ledger import Ledger

CONFIRM_WINDOW = timedelta(minutes=2)
HELP = "Commands: /status, /pause, /resume, /flatten"


class Control:
    def __init__(self, telegram: Telegram, ledger: Ledger, status: Callable[[], str]):
        self.telegram, self.ledger, self.status = telegram, ledger, status

    def _reply(self, text: str) -> None:
        self.telegram.send(text)

    def poll(self, now: datetime) -> bool:
        """Process new messages; False if Telegram could not be read."""
        offset = int(self.ledger.get("telegram_offset", "0"))
        try:
            updates = self.telegram.updates(offset)
        except Exception:
            return False
        for update in updates:
            self.ledger.set("telegram_offset", str(update["update_id"] + 1))
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id"))
            text = (message.get("text") or "").strip()
            if chat != self.telegram.chat_id:
                self.ledger.record(now, None, "command", f"ignored message from chat {chat}")
                continue
            sent = message.get("date")
            if sent is not None and now - datetime.fromtimestamp(sent, timezone.utc) > CONFIRM_WINDOW:
                self.ledger.record(now, None, "command", f"ignored old message: {text[:60]}")
                self._reply(f"Ignored '{text[:40]}': sent more than 2 minutes ago. Please resend it.")
                continue   # never act on commands queued while the bot was down
            self.ledger.record(now, None, "command", text[:100])
            self.handle(text, now)
        return True

    def handle(self, text: str, now: datetime) -> None:
        command = text.split()[0].split("@")[0].lower() if text else ""
        if command == "/status":
            self._reply(self.status())
        elif command == "/pause":
            self.ledger.pause("paused by user (Telegram)")
            self._reply("Paused: no new trades. Open trades stay protected. Send /resume to continue.")
        elif command in ("/resume", "/flatten"):
            self.ledger.set("pending_command", command)
            self.ledger.set("pending_until", (now + CONFIRM_WINDOW).isoformat())
            action = "resume trading" if command == "/resume" else "close ALL bot trades and pause new trades"
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
                self.ledger.pause("paused: /flatten requested")
                self._reply("Closing all bot trades now and pausing new trades. "
                            "You will get a message when done; send /resume to trade again.")
        else:
            self._reply(HELP)
