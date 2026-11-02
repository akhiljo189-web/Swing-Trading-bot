"""Indicator definitions shared by live trading and backtests (spec §3)."""
from collections.abc import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int) -> list[float | None]:
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("highs, lows and closes must have equal length")
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(highs)
    tr = []
    for i in range(n):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    out: list[float | None] = [None] * n
    if n < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out
