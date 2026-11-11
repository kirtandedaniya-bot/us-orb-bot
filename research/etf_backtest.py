"""Monthly ETF trend / momentum strategies (pre-registered, published rules — no parameter search).

A  SPY buy & hold                 benchmark
B  60/40 SPY/IEF                  benchmark
C  GTAA (Faber): SPY EFA IEF VNQ DBC, 20% each, held only if close > 10-month SMA else cash (SHY)
D  Momentum rotation: top 3 of 11 ETFs by avg 3/6/12-month return, each only if > 10-month SMA else cash
E  Dual momentum (GEM): SPY vs EFA 12m return; if winner's 12m return < cash -> IEF
Signals at month-end close, positions held from the next trading day. Cost 5 bp per side on turnover.
Usage: python research/etf_backtest.py etf_daily_adjclose.csv
"""
import sys

import numpy as np
import pandas as pd

COST = 0.0005
CASH = "SHY"
RISK = ["SPY", "IWM", "EFA", "EEM", "VNQ", "GLD", "DBC", "TLT", "IEF", "LQD"]


def month_ends(px: pd.DataFrame) -> pd.DatetimeIndex:
    return px.groupby(px.index.to_period("M")).tail(1).index


def run(px: pd.DataFrame, weights_fn, start: str) -> pd.Series:
    """weights_fn(px_upto_month_end) -> dict ticker->weight. Returns daily strategy returns."""
    rets = px.pct_change().fillna(0)
    me = [d for d in month_ends(px) if d >= pd.Timestamp(start)]
    w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    prev = pd.Series(0.0, index=px.columns)
    costs = pd.Series(0.0, index=px.index)
    for i, d in enumerate(me):
        tgt = pd.Series(weights_fn(px.loc[:d]), dtype=float).reindex(px.columns).fillna(0.0)
        nxt = me[i + 1] if i + 1 < len(me) else px.index[-1]
        span = px.index[(px.index > d) & (px.index <= nxt)]
        if len(span) == 0:
            continue
        w.loc[span] = tgt.values
        costs.loc[span[0]] = COST * (tgt - prev).abs().sum()
        prev = tgt
    r = (w * rets).sum(axis=1) - costs
    return r[r.index > pd.Timestamp(start)]


def sma10(p: pd.DataFrame) -> pd.Series:
    m = p.groupby(p.index.to_period("M")).tail(1)
    return m.iloc[-10:].mean()


def mret(p: pd.DataFrame, months: int) -> pd.Series:
    m = p.groupby(p.index.to_period("M")).tail(1)
    return m.iloc[-1] / m.iloc[-1 - months] - 1


def spy(p):
    return {"SPY": 1.0}


def sixty40(p):
    return {"SPY": 0.6, "IEF": 0.4}


def gtaa(p):
    s, last = sma10(p), p.iloc[-1]
    w = {}
    for t in ["SPY", "EFA", "IEF", "VNQ", "DBC"]:
        k = t if last[t] > s[t] else CASH
        w[k] = w.get(k, 0) + 0.2
    return w


def rotation(p, top=3):
    s, last = sma10(p), p.iloc[-1]
    score = (mret(p, 3) + mret(p, 6) + mret(p, 12))[RISK] / 3
    w = {}
    for t in score.nlargest(top).index:
        k = t if last[t] > s[t] else CASH
        w[k] = w.get(k, 0) + 1 / top
    return w


def gem(p):
    r12 = mret(p, 12)
    best = "SPY" if r12["SPY"] >= r12["EFA"] else "EFA"
    return {best: 1.0} if r12[best] > r12[CASH] else {"IEF": 1.0}


def stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    dd = (eq / eq.cummax() - 1).min()
    yearly = (1 + r).groupby(r.index.year).prod() - 1
    monthly = (1 + r).groupby(r.index.to_period("M")).prod() - 1
    return {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1, "Vol": r.std() * np.sqrt(252),
            "Sharpe": r.mean() / r.std() * np.sqrt(252), "MaxDD": dd, "WorstYr": yearly.min(),
            "PosMonths": (monthly > 0).mean(), "2008": yearly.get(2008, np.nan), "2022": yearly.get(2022, np.nan)}


if __name__ == "__main__":
    px = pd.read_csv(sys.argv[1], index_col=0, parse_dates=True).ffill()
    start = "2007-03-30"
    px = px.loc[:, [c for c in px.columns if c != "BIL"]].dropna(subset=RISK + [CASH], how="any")
    strategies = {"A SPY buy&hold": spy, "B 60/40": sixty40, "C GTAA (Faber)": gtaa,
                  "D Momentum top-3": rotation, "E Dual momentum": gem}
    rows, rets = {}, {}
    for name, fn in strategies.items():
        r = run(px, fn, start)
        rets[name] = r
        rows[name] = stats(r)
        rows[name + " | last 3y"] = stats(r[r.index >= r.index[-1] - pd.DateOffset(years=3)])
    out = pd.DataFrame(rows).T
    pct = ["CAGR", "Vol", "MaxDD", "WorstYr", "PosMonths", "2008", "2022"]
    out[pct] = (out[pct] * 100).round(1)
    out["Sharpe"] = out["Sharpe"].astype(float).round(2)
    print(f"Period {start} .. {px.index[-1].date()} (5bp/side costs)")
    print(out.to_string())
    pd.DataFrame(rets).to_csv("/tmp/etf_strategy_returns.csv")
    print("current D weights:", rotation(px)); print("current E:", gem(px)); print("current C:", gtaa(px))
