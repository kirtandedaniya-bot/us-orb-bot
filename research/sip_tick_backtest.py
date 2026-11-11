"""Stocks-in-play ORB with tick-resolved entry minute. Usage: python research/sip_tick_backtest.py SIP_DIR TICKS_CSV"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.data import HOLDOUT_START  # noqa: E402

SIP, TICKS = Path(sys.argv[1]), Path(sys.argv[2])
SLIP, COMM = 0.01, 0.0035


def load():
    df = pd.concat(pd.read_parquet(f) for f in sorted(SIP.glob("intraday_*.parquet")))
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df["date"] = ts.dt.date.astype(str)
    df["mins"] = ts.dt.hour * 60 + ts.dt.minute
    return {k: g.sort_values("mins") for k, g in df.groupby(["symbol", "date"])}


def run(e, groups, k):
    rows = []
    for r in e.itertuples():
        if np.isnan(r.fill):
            continue
        risk = k * r.atr
        stop = r.fill - r.side * risk
        if getattr(r, f"hit_{int(k*100):03d}") == 1:
            bar = groups[(r.symbol, r.date)]
            bar = bar[bar.mins == r.mins].iloc[0]                 # clamp to consolidated bar range (bad prints)
            ex, reason = float(np.clip(getattr(r, f"px_{int(k*100):03d}"), bar.low, bar.high)), "STOP0"
        else:
            b = groups[(r.symbol, r.date)]
            b = b[b.mins > r.mins]
            hs = b[b.low <= stop] if r.side == 1 else b[b.high >= stop]
            if hs.empty:
                ex, reason = (float(b.close.iloc[-1]) if len(b) else r.fill), "EOD"
            else:
                x = hs.iloc[0]
                ex, reason = (min(x.open, stop) if r.side == 1 else max(x.open, stop)), "STOP"
        g = r.side * (ex - r.fill)
        c = 2 * (SLIP + COMM)
        rows.append(dict(date=r.date, side=r.side, price=r.fill, R=(g - c) / risk, Rg=g / risk, reason=reason))
    return pd.DataFrame(rows)


def rep(t, label):
    bs = np.random.default_rng(0).choice(t.R.values, (2000, len(t))).mean(axis=1)
    print(f"  {label:18s} n={len(t):5d} win={(t.R > 0).mean():5.1%} E[R]={t.R.mean():+.3f} "
          f"CI=[{np.percentile(bs, 2.5):+.3f},{np.percentile(bs, 97.5):+.3f}] gross={t.Rg.mean():+.3f} "
          f"entry-min stops={(t.reason == 'STOP0').mean():.0%}")


if __name__ == "__main__":
    e = pd.read_csv(Path(__file__).with_name("entry_minutes.csv"))[["symbol", "date", "mins", "side", "lvl", "atr"]]
    tk = pd.read_csv(TICKS)
    tk.columns = [c.replace("0.05", "005").replace("0.1", "010").replace("0.2", "020") for c in tk.columns]
    e = e.merge(tk, on=["symbol", "date"], how="left")
    print("ticks missing:", int(e.fill.isna().sum()), "of", len(e))
    # ticks are raw, bars split-adjusted: drop entries where the two disagree by >3% (later splits)
    bad = (e.fill / e.lvl - 1).abs() > 0.03
    print("split-mismatch dropped:", int(bad.sum()))
    e.loc[bad, "fill"] = np.nan
    groups = load()
    for k in (0.05, 0.10, 0.20):
        t = run(e, groups, k)
        d = pd.to_datetime(t.date)
        print(f"stop {k} x ATR")
        rep(t[d < HOLDOUT_START], "TRAIN <2026-05")
        rep(t[d >= HOLDOUT_START], "HOLDOUT 2026-05+")
        for y in sorted(d.dt.year.unique()):
            rep(t[d.dt.year == y], str(y))
        rep(t[t.price >= 30], "price>=30")
