"""One-shot tests of published daily strategies. Rules fixed from the original sources; no tuning.
OOS = after publication (2009+ for all three). Costs: 2 bp per side. Flat periods earn 0 (conservative).
Usage: python research/strategy_tests.py OHLC_DIR
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

D = Path(sys.argv[1])
COST = 0.0002
OOS = "2009-01-01"


def load(t):
    d = pd.read_csv(D / f"{t}.csv", index_col=0, parse_dates=True)
    return d[d.index < pd.Timestamp.today().normalize()]          # drop today's partial bar


def rsi2(c):
    x = c.diff()
    up = x.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    dn = (-x.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def pos_to_ret(d, pos):
    """pos[t] = position held from close t to close t+1 (decided at close t)."""
    r = d.Close.pct_change().shift(-1).fillna(0)
    trades = pos.diff().abs().fillna(pos.iloc[0])
    return (pos * r - trades * COST).shift(1).fillna(0)


def s_rsi2(d, next_open=False):
    """Connors (2008): Close>SMA200 & RSI2<10 -> long; exit when Close>SMA5."""
    c = d.Close
    sma200, sma5, r = c.rolling(200).mean(), c.rolling(5).mean(), rsi2(c)
    pos, held = [], 0
    for i in range(len(d)):
        if held == 0 and c.iloc[i] > sma200.iloc[i] and r.iloc[i] < 10:
            held = 1
        elif held == 1 and c.iloc[i] > sma5.iloc[i]:
            held = 0
        pos.append(held)
    pos = pd.Series(pos, index=d.index, dtype=float)
    if not next_open:
        return pos_to_ret(d, pos)
    # conservative: act at next open instead of the signal close
    o, cl = d.Open, d.Close
    p_prev = pos.shift(1).fillna(0)          # decided at close t-1, executed at open t
    p_prev2 = pos.shift(2).fillna(0)
    ret = pd.Series(0.0, index=d.index)
    # day t: overnight gap belongs to p_prev2 (old position), intraday open->close to p_prev (new)
    ret += p_prev2 * (o / cl.shift(1) - 1) + p_prev * (cl / o - 1)
    ret -= (p_prev - p_prev2).abs() * COST
    return ret.fillna(0)


def s_tom(d):
    """Turn of month (Lakonishok-Smidt 1988, McConnell-Xu 2008): hold from close of day -2 to close of day +3."""
    m = d.index.to_period("M")
    rank_end = d.groupby(m).cumcount(ascending=False)          # 0 = last day of month
    rank_start = d.groupby(m).cumcount()                       # 0 = first day
    pos = ((rank_end <= 1) | (rank_start <= 1)).astype(float)  # held close->close over days -1..+3
    pos = pd.Series(pos.values, index=d.index)
    return pos_to_ret(d, pos)


def s_overnight(d):
    """Buy close, sell next open, every day (Cliff-Cooper-Gulen 2008)."""
    r = d.Open / d.Close.shift(1) - 1
    return (r - 2 * COST).fillna(0)


def s_tsmom(d):
    """Time-series momentum (Moskowitz-Ooi-Pedersen 2012): long if 12-month return > 0, checked monthly."""
    c = d.Close
    me = c.groupby(c.index.to_period("M")).transform("last") == c
    sig = (c / c.shift(252) - 1 > 0).astype(float).where(me).ffill().fillna(0)
    return pos_to_ret(d, sig)


def stats(r, pos_days=None):
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    sh = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    expo = (r != 0).mean()
    return cagr, sh, dd, expo


def main():
    rows = []
    for t in ["SPY", "QQQ", "IWM", "DIA"]:
        d = load(t)
        strats = {"Buy & hold": d.Close.pct_change().fillna(0),
                  "1 RSI-2 (at close)": s_rsi2(d), "1 RSI-2 (next open)": s_rsi2(d, True),
                  "2 Turn of month": s_tom(d), "3 Overnight": s_overnight(d), "4 Trend 12m": s_tsmom(d)}
        for name, r in strats.items():
            a = stats(r); o = stats(r[r.index >= OOS]); l5 = stats(r[r.index >= r.index[-1] - pd.DateOffset(years=5)])
            rows.append([t, name, *a, *o, l5[0], l5[1]])
    cols = ["ETF", "Strategy", "CAGR", "Sharpe", "MaxDD", "Exposure", "OOS CAGR", "OOS Sharpe", "OOS MaxDD",
            "OOS Exp", "5y CAGR", "5y Sharpe"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["CAGR", "MaxDD", "Exposure", "OOS CAGR", "OOS MaxDD", "OOS Exp", "5y CAGR"]:
        df[c] = (df[c] * 100).round(1)
    for c in ["Sharpe", "OOS Sharpe", "5y Sharpe"]:
        df[c] = df[c].round(2)
    print(df.drop(columns=["OOS Exp"]).to_string(index=False))
    df.to_csv("/tmp/strategy_tests.csv", index=False)


if __name__ == "__main__":
    main()
