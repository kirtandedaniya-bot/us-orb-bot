"""V1 ORB rules. Pure functions — the ONLY place strategy logic lives.
Live bot and backtest both import this, so they cannot drift apart."""
from dataclasses import dataclass
from datetime import time
from typing import Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Params:
    orb_start: time = time(9, 30)
    orb_end: time = time(10, 15)          # ORB = bars in [orb_start, orb_end)
    entry_deadline: time = time(11, 30)   # unfilled entries cancelled after this
    flat_time: time = time(15, 55)        # backtest exit; live EOD job flattens ~15:35
    min_orb_bars: int = 40
    atr_lo: float = 25.0                  # ORB range as % of ATR14
    atr_hi: float = 60.0
    sweet_lo: float = 30.0
    sweet_hi: float = 45.0
    risk_sweet: float = 150.0             # $ risk inside sweet spot
    risk_base: float = 50.0               # $ risk otherwise
    min_risk_ps: float = 0.02
    target_r: float = 1.5               # robust 1.25–2.0R plateau; 1.5R chosen (see PR)
    max_notional: float = 50_000.0        # per-position cap ($)


@dataclass(frozen=True)
class Setup:
    symbol: str
    side: int          # +1 long, -1 short
    level: float       # entry trigger (ORB high / low)
    stop: float        # ORB-window VWAP
    target: float
    risk_ps: float
    shares: int
    atr_pct: float
    vwap: float
    trend: int

    @property
    def action(self) -> str:
        return "BUY" if self.side == 1 else "SELL"


def r2(x: float) -> float:
    return float(np.round(x + 1e-9, 2))


def daily_context(daily: pd.DataFrame) -> tuple[float, int]:
    """daily: PRIOR sessions only, cols high/low/close, oldest->newest. Returns (ATR14, trend)."""
    if len(daily) < 21:
        return float("nan"), 0
    pc = daily["close"].shift(1)
    tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(), (daily["low"] - pc).abs()],
                   axis=1).max(axis=1)
    atr = float(tr.iloc[-14:].mean())
    last, sma20 = float(daily["close"].iloc[-1]), float(daily["close"].iloc[-20:].mean())
    return atr, (1 if last > sma20 else -1)


def build_setup(symbol: str, orb: pd.DataFrame, daily: pd.DataFrame, p: Params = Params()) -> Optional[Setup]:
    """orb: 1-min bars in ORB window (cols high/low/close/volume). daily: prior sessions."""
    if len(orb) < p.min_orb_bars or orb["volume"].sum() <= 0:
        return None
    atr, trend = daily_context(daily)
    if trend == 0 or not np.isfinite(atr) or atr <= 0:
        return None

    tp = (orb["high"] + orb["low"] + orb["close"]) / 3
    vwap = float((tp * orb["volume"]).sum() / orb["volume"].sum())
    hi, lo, last = float(orb["high"].max()), float(orb["low"].min()), float(orb["close"].iloc[-1])
    atr_pct = (hi - lo) / atr * 100
    if not (p.atr_lo <= atr_pct <= p.atr_hi):
        return None

    if trend == 1 and last > vwap:
        side, level, stop = 1, r2(hi), r2(vwap)
    elif trend == -1 and last < vwap:
        side, level, stop = -1, r2(lo), r2(vwap)
    else:
        return None

    risk = side * (level - stop)
    if risk <= p.min_risk_ps:
        return None
    target = r2(level + side * risk * p.target_r)
    budget = p.risk_sweet if p.sweet_lo <= atr_pct <= p.sweet_hi else p.risk_base
    shares = min(int(budget / risk), int(p.max_notional / level))
    if shares < 1:
        return None
    return Setup(symbol, side, level, stop, target, r2(risk), shares, atr_pct, vwap, trend)


def simulate(s: Setup, post: pd.DataFrame, p: Params = Params(), slip: float = 0.01) -> Optional[dict]:
    """Bar-by-bar fill sim on 1-min bars from orb_end onward (index = timestamps, col 't' = time).
    Stop checked before target inside a bar (conservative)."""
    window = post[post["t"] < p.entry_deadline]
    hit = window[window["high"] >= s.level] if s.side == 1 else window[window["low"] <= s.level]
    if hit.empty:
        return None
    eb = hit.iloc[0]
    fill = (max(eb["open"], s.level) + slip) if s.side == 1 else (min(eb["open"], s.level) - slip)

    exit_px, reason = None, None
    for ts, x in post.loc[eb.name:].iterrows():
        if x["t"] >= p.flat_time:
            exit_px, reason = x["open"] - s.side * slip, "EOD"
            break
        if (s.side == 1 and x["low"] <= s.stop) or (s.side == -1 and x["high"] >= s.stop):
            px = min(x["open"], s.stop) if s.side == 1 else max(x["open"], s.stop)
            exit_px, reason = px - s.side * slip, "STOP"
            break
        if (s.side == 1 and x["high"] >= s.target) or (s.side == -1 and x["low"] <= s.target):
            exit_px, reason = s.target, "TARGET"
            break
    if exit_px is None:
        exit_px, reason = post["close"].iloc[-1] - s.side * slip, "EOD"
    pnl_ps = s.side * (exit_px - fill)
    return {"entry_time": eb.name, "fill": fill, "exit": exit_px, "reason": reason,
            "R": pnl_ps / s.risk_ps, "pnl": pnl_ps * s.shares}
