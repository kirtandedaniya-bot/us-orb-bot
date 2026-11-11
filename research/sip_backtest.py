"""Stocks-in-play 5-min ORB (after Zarattini, Barbon & Aziz 2024) on the data-sip dataset.

Per selected symbol-day (top-20 RVOL, RVOL>=1, price>$5, avgvol>1M, ATR>$0.50):
  direction = colour of the 09:30 5-min candle (green long / red short / doji skip)
  entry     = stop order at that candle's high (long) / low (short), live from 09:35
  stop      = entry -/+ STOP_ATR x ATR14
  exit      = stop or 15:59 close (no profit target)
Costs: SLIP $/share/side + COMM $/share/side.  Usage: python research/sip_backtest.py SIP_DIR [stop_atr] [slip] [comm]
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.data import HOLDOUT_START  # noqa: E402

D = Path(sys.argv[1])
STOP_ATR = float(sys.argv[2]) if len(sys.argv) > 2 else 0.10
SLIP = float(sys.argv[3]) if len(sys.argv) > 3 else 0.01
COMM = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0035


def load_intraday() -> pd.DataFrame:
    df = pd.concat(pd.read_parquet(f) for f in sorted(D.glob("intraday_*.parquet")))
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df["date"] = ts.dt.date.astype(str)
    df["mins"] = ts.dt.hour * 60 + ts.dt.minute
    return df


def simulate(sel: pd.DataFrame, intr: pd.DataFrame, stop_atr=STOP_ATR, slip=SLIP, comm=COMM) -> pd.DataFrame:
    sel = sel.copy()
    sel["date"] = sel["date"].astype(str)
    groups = {k: g for k, g in intr.groupby(["symbol", "date"])}
    rows = []
    for r in sel.itertuples():
        if r.close == r.open:
            continue
        side = 1 if r.close > r.open else -1
        b = groups.get((r.symbol, r.date))
        if b is None:
            continue
        b = b[b.mins >= 575].sort_values("mins")          # from 09:35
        if b.empty:
            continue
        lvl = r.high if side == 1 else r.low
        hit = b[b.high >= lvl] if side == 1 else b[b.low <= lvl]
        if hit.empty:
            continue
        e = hit.iloc[0]
        fill = max(e.open, lvl) if side == 1 else min(e.open, lvl)
        risk = stop_atr * r.atr14
        stop = fill - side * risk
        after = b[b.mins >= e.mins]
        # stop can't be hit on the entry bar before entry unless bar range covers both; assume worst case
        hit_s = after[after.low <= stop] if side == 1 else after[after.high >= stop]
        if not hit_s.empty:
            x = hit_s.iloc[0]
            exit_px = min(x.open, stop) if side == 1 else max(x.open, stop)
            if x.mins == e.mins:
                exit_px = stop
            reason = "STOP"
        else:
            exit_px, reason = float(after.close.iloc[-1]), "EOD"
        gross = side * (exit_px - fill)
        cost = 2 * (slip + comm)
        rows.append(dict(date=r.date, symbol=r.symbol, side=side, rvol=r.rvol, price=fill, atr=r.atr14,
                         risk=risk, R=(gross - cost) / risk, R_gross=gross / risk, reason=reason,
                         cost_R=cost / risk))
    return pd.DataFrame(rows)


def report(t: pd.DataFrame, label: str) -> None:
    if t.empty:
        print(label, "n=0"); return
    bs = np.random.default_rng(0).choice(t.R.values, (2000, len(t))).mean(axis=1)
    daily = t.groupby("date").R.mean()
    print(f"{label:28s} n={len(t):5d} win={(t.R > 0).mean():.1%} E[R]={t.R.mean():+.3f} "
          f"CI=[{np.percentile(bs, 2.5):+.3f},{np.percentile(bs, 97.5):+.3f}] grossR={t.R_gross.mean():+.3f} "
          f"cost/trade={t.cost_R.mean():.3f}R daySharpe={daily.mean() / daily.std() * np.sqrt(252):.2f}")


if __name__ == "__main__":
    sel = pd.read_csv(D / "selected.csv")
    t = simulate(sel, load_intraday())
    t.to_csv(f"/tmp/sip_trades_{STOP_ATR}.csv", index=False)
    d = pd.to_datetime(t.date)
    print(f"stop={STOP_ATR}xATR slip=${SLIP} comm=${COMM}")
    report(t, "ALL")
    report(t[d < HOLDOUT_START], "TRAIN <2026-05")
    report(t[d >= HOLDOUT_START], "HOLDOUT 2026-05+")
    for y in sorted(d.dt.year.unique()):
        report(t[d.dt.year == y], str(y))
    report(t[t.side == 1], "longs"); report(t[t.side == -1], "shorts")
    for lo, hi in [(0, 10), (10, 30), (30, 100), (100, 1e9)]:
        report(t[(t.price >= lo) & (t.price < hi)], f"price {lo}-{hi}")
