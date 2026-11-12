"""RSI-2 mean reversion (Connors 2008) — the ONE place the rules live. Used by the bot and the parity test.

Long SPY when   close > SMA200  and  RSI(2) < 10
Exit when       close > SMA5
Decided on the official daily close; executed at the next open (market-on-open).
No stop, no target, no tuning — exactly the published rule that was tested.
"""
import pandas as pd

SMA_LONG, SMA_EXIT, RSI_LEN, RSI_ENTRY = 200, 5, 2, 10.0
MIN_BARS = 260


def rsi(close: pd.Series, n: int = RSI_LEN) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def indicators(close: pd.Series) -> dict:
    return {"close": float(close.iloc[-1]),
            "sma200": float(close.rolling(SMA_LONG).mean().iloc[-1]),
            "sma5": float(close.rolling(SMA_EXIT).mean().iloc[-1]),
            "rsi2": float(rsi(close).iloc[-1])}


def decide(close: pd.Series, held: bool) -> tuple[bool, dict]:
    """close: adjusted daily closes up to and including the signal day. Returns (hold_next, indicators)."""
    if len(close) < MIN_BARS or close.isna().any() or (close <= 0).any():
        raise ValueError(f"bad price history: {len(close)} bars")
    x = indicators(close)
    if not held:
        return (x["close"] > x["sma200"] and x["rsi2"] < RSI_ENTRY), x
    return (not x["close"] > x["sma5"]), x
