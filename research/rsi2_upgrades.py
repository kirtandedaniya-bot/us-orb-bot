"""Pre-registered one-shot tests (2026-11-21) of two RSI-2 upgrades. Rules unchanged (rsi2_rules.py).
 A) idle cash in T-bills: days fully flat earn BIL's daily return (0 before BIL exists); 1 bp per side to switch.
 B) three equal sleeves SPY / QQQ / IWM, each running the same rule independently, idle cash in BIL.
Execution: decide on close t, act at open t+1 (what the live bot does). Costs 2 bp per side on ETF trades.
Pass rules (fixed before looking): A — higher CAGR and Sharpe than baseline, MaxDD no worse, in full (2008+) AND OOS
2009+. B — Sharpe higher than SPY-only(A) in both periods and MaxDD no worse by more than 5 points.
Usage: python research/rsi2_upgrades.py OHLC_DIR ETF_CSV
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rsi2_rules as R  # noqa: E402

OHLC, ETF_CSV = Path(sys.argv[1]), Path(sys.argv[2])
COST, BIL_COST = 0.0002, 0.0001


def load(t):
    d = pd.read_csv(OHLC / f"{t}.csv", index_col=0, parse_dates=True)
    return d[d.index < pd.Timestamp("2026-11-12")]


def positions(c: pd.Series) -> pd.Series:
    sma200, sma5, r = c.rolling(200).mean(), c.rolling(5).mean(), R.rsi(c)
    out, held = [], False
    for i in range(len(c)):
        if not held and c.iloc[i] > sma200.iloc[i] and r.iloc[i] < R.RSI_ENTRY:
            held = True
        elif held and c.iloc[i] > sma5.iloc[i]:
            held = False
        out.append(float(held))
    return pd.Series(out, index=c.index)


def sleeve(t: str, bil: pd.Series, use_bil: bool) -> pd.Series:
    d = load(t)
    pos = positions(d.Close)
    p1, p2 = pos.shift(1).fillna(0), pos.shift(2).fillna(0)     # p1: held during day t intraday, p2: overnight into t
    o, c = d.Open, d.Close
    r = p2 * (o / c.shift(1) - 1) + p1 * (c / o - 1) - (p1 - p2).abs() * COST
    if use_bil:
        b = bil.reindex(d.index).pct_change().fillna(0)
        flat = (p1 == 0) & (p2 == 0)
        r = r + flat * b - (p1 - p2).abs() * BIL_COST
    return r.fillna(0)


def stats(r: pd.Series) -> tuple:
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    return (eq.iloc[-1] ** (1 / yrs) - 1, r.mean() / r.std() * np.sqrt(252), (eq / eq.cummax() - 1).min())


def main():
    bil = pd.read_csv(ETF_CSV, index_col=0, parse_dates=True)["BIL"].dropna()
    spy = load("SPY").Close.pct_change().fillna(0)
    variants = {
        "SPY buy & hold": spy,
        "Baseline: RSI-2 SPY, cash earns 0": sleeve("SPY", bil, False),
        "A: RSI-2 SPY + cash in BIL": sleeve("SPY", bil, True),
    }
    sl = {t: sleeve(t, bil, True) for t in ("SPY", "QQQ", "IWM")}
    idx = sl["SPY"].index.intersection(sl["QQQ"].index).intersection(sl["IWM"].index)
    variants["B: SPY+QQQ+IWM sleeves + BIL"] = sum(sl[t].reindex(idx).fillna(0) for t in sl) / 3
    rows = []
    for name, r in variants.items():
        for per, start in (("2008+", "2008-01-01"), ("OOS 2009+", "2009-01-01"), ("last 5y", "2021-11-12")):
            x = r[r.index >= start]
            cg, sh, dd = stats(x)
            rows.append([name, per, f"{cg:+.1%}", f"{sh:.2f}", f"{dd:.1%}"])
    print(pd.DataFrame(rows, columns=["Variant", "Period", "CAGR", "Sharpe", "MaxDD"]).to_string(index=False))


if __name__ == "__main__":
    main()
