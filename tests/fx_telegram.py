"""Fake Telegram client for control and alert tests (not a test module itself)."""
from fxbot.alerts import Telegram


class FakeTelegram(Telegram):
    """Records sent messages; serves queued updates."""

    def __init__(self, chat_id="42"):
        super().__init__("t", chat_id, transport=None)
        self.sent, self.inbox = [], []

    def send(self, text):
        self.sent.append(text)
        return True

    def updates(self, offset):
        return [u for u in self.inbox if u["update_id"] >= offset]


def msg(update_id, text, chat=42, date=None):
    m = {"chat": {"id": chat}, "text": text}
    if date is not None:
        m["date"] = date
    return {"update_id": update_id, "message": m}
