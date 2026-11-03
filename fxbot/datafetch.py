"""Download OANDA history for backtests, report gaps, and cache S10 entry quotes (spec §8)."""
import csv
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fxbot.history import History, save

S10Fetch = Callable[[str, datetime], tuple[float, float] | None]


def fetch_all(broker, pairs: tuple[str, ...], start: datetime, end: datetime, out_dir: Path,
              granularities: tuple[str, ...] = ("H1", "M15"), log=print) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for pair in pairs:
        for g in granularities:
            candles = broker.fetch_history(pair, g, start, end)
            save(out_dir / f"{pair}_{g}.csv", candles)
            log(f"{pair} {g}: {len(candles)} candles")


def gap_report(history: History, pairs: tuple[str, ...], reference: str | None = None) -> list[str]:
    """M15 candles present for the reference pair but missing for another pair, per month."""
    reference = reference or pairs[0]
    ref = set(history.get(reference, "M15").times)
    lines = []
    for pair in pairs:
        if pair == reference:
            continue
        missing = ref - set(history.get(pair, "M15").times)
        for month, n in sorted(Counter(t.strftime("%Y-%m") for t in missing).items()):
            lines.append(f"{pair} {month}: {n} M15 candle(s) missing (relative to {reference})")
    return lines


class S10Cache:
    """(pair, time) -> (bid, ask) or None, stored in a CSV so each quote is fetched once."""

    def __init__(self, path: Path, fetch: S10Fetch | None = None):
        self.path, self.fetch = Path(path), fetch
        self.quotes: dict[tuple[str, str], tuple[float, float] | None] = {}
        if self.path.exists():
            with open(self.path, newline="") as f:
                for row in csv.DictReader(f):
                    self.quotes[(row["pair"], row["time"])] = (
                        (float(row["bid"]), float(row["ask"])) if row["bid"] else None)

    def read_only(self) -> "S10Cache":
        """A copy that never fetches (for parallel worker processes)."""
        copy = S10Cache.__new__(S10Cache)
        copy.path, copy.fetch, copy.quotes = self.path, None, dict(self.quotes)
        return copy

    def __call__(self, pair: str, at: datetime) -> tuple[float, float] | None:
        key = (pair, at.isoformat())
        if key in self.quotes or self.fetch is None:
            return self.quotes.get(key)
        try:
            quote = self.fetch(pair, at)
        except Exception:   # not cached: a later run may succeed
            return None
        self.quotes[key] = quote
        new = not self.path.exists()
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["pair", "time", "bid", "ask"])
            w.writerow([pair, at.isoformat(), *(quote if quote else ("", ""))])
        return quote
