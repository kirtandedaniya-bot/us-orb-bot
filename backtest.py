"""Backtest V1 ORB on 1-min cache using the SAME rules as the live bot (orb/strategy.py).
Usage: python backtest.py candle_cache.pkl [--slip 0.01] [--split 2025-07-01] [--csv trades.csv]"""
import argparse
from datetime import time

import numpy as np
import pandas as pd

from orb.strategy import Params, build_setup, simulate


def load(path: str) -> pd.DataFrame:
    df = pd.read_pickle(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    df["timestamp"] = df["timestamp"].dt.tz_convert("America/New_York")
    df["date"] = df["timestamp"].dt.date
    df["t"] = df["timestamp"].dt.time
    return df[(df["t"] >= time(9, 30)) & (df["t"] < time(16, 0))].set_index("timestamp")


def run(rth: pd.DataFrame, p: Params, slip: float) -> pd.DataFrame:
    daily = rth.groupby(["symbol", "date"]).agg(high=("high", "max"), low=("low", "min"),
                                                close=("close", "last")).reset_index()
    rows = []
    for sym, bars in rth.groupby("symbol"):
        dsym = daily[daily["symbol"] == sym].reset_index(drop=True)
        for i, (day, b) in enumerate(bars.groupby("date")):
            prior = dsym[dsym["date"] < day].tail(260)
            orb = b[(b["t"] >= p.orb_start) & (b["t"] < p.orb_end)]
            s = build_setup(sym, orb, prior, p)
            if s is None:
                continue
            res = simulate(s, b[b["t"] >= p.orb_end], p, slip)
            if res:
                rows.append({"date": day, "symbol": sym, "side": s.side, "atr_pct": s.atr_pct,
                             "risk_ps": s.risk_ps, "shares": s.shares, **res})
    return pd.DataFrame(rows).sort_values("entry_time").reset_index(drop=True)


def report(t: pd.DataFrame, label: str) -> None:
    if t.empty:
        print(f"{label:<10} n=0"); return
    w, l = t[t.R > 0], t[t.R <= 0]
    eq = t.pnl.cumsum()
    pf = w.pnl.sum() / -l.pnl.sum() if l.pnl.sum() < 0 else np.inf
    bs = np.random.default_rng(0).choice(t.R.values, (2000, len(t))).mean(axis=1)
    daily_pnl = t.groupby("date").pnl.sum()
    sharpe = daily_pnl.mean() / daily_pnl.std() * np.sqrt(252) if len(daily_pnl) > 1 else np.nan
    print(f"{label:<10} n={len(t):4d} win={len(w)/len(t):5.1%} E[R]={t.R.mean():+.3f} "
          f"CI95=[{np.percentile(bs, 2.5):+.3f},{np.percentile(bs, 97.5):+.3f}] PF={pf:4.2f} "
          f"net=${t.pnl.sum():>8,.0f} maxDD=${(eq.cummax() - eq).max():>6,.0f} Sharpe(d)={sharpe:4.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cache")
    ap.add_argument("--slip", type=float, default=0.01)
    ap.add_argument("--split", default=None, help="YYYY-MM-DD: report in-sample vs out-of-sample")
    ap.add_argument("--target-r", type=float, default=Params().target_r)
    ap.add_argument("--csv", default="trades.csv")
    a = ap.parse_args()

    p = Params(target_r=a.target_r)
    t = run(load(a.cache), p, a.slip)
    t.to_csv(a.csv, index=False)
    print(f"slip=${a.slip}/sh/side target={a.target_r}R")
    report(t, "ALL")
    report(t[t.side == 1], "longs")
    report(t[t.side == -1], "shorts")
    yrs = pd.to_datetime(t.date).dt.year
    for y in sorted(yrs.unique()):
        report(t[yrs == y], str(y))
    for s in sorted(t.symbol.unique()):
        report(t[t.symbol == s], s)
    if a.split:
        d = pd.to_datetime(t.date)
        report(t[d < a.split], "IN-SAMPLE")
        report(t[d >= a.split], "OUT-SAMPLE")
    print("exits:", t.reason.value_counts().to_dict())
