"""Robustness suite for the US ORB rules (orb/strategy.py).
Usage: python research/robustness.py candle_cache.pkl [outdir]

A. target-R sweep          -> per-trade CSVs (input for walk-forward)
B. ATR-band sensitivity    -> is the 25-60% band a lucky pick?
C. price-noise test        -> does the edge survive small random price perturbations?
D. Monte Carlo             -> drawdown / losing-streak / losing-month distributions
E. walk-forward            -> pick target-R on trailing 12 months, trade next quarter
"""
import json
import sys
from dataclasses import replace
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import load, run  # noqa: E402
from orb.strategy import Params  # noqa: E402

SLIP = 0.01
BASE = Params()
RTH: pd.DataFrame = None  # set in worker init


def _init(path):
    global RTH
    RTH = load(path)


def noisy(df: pd.DataFrame, seed: int, bp: float = 2.0) -> pd.DataFrame:
    """Gaussian noise of `bp` basis points on every price; keep bars internally consistent."""
    rng = np.random.default_rng(seed)
    d = df.copy()
    for c in ("open", "high", "low", "close"):
        d[c] = d[c] * (1 + rng.normal(0, bp / 1e4, len(d)))
    d["high"] = d[["open", "high", "low", "close"]].max(axis=1)
    d["low"] = d[["open", "high", "low", "close"]].min(axis=1)
    return d


def job(spec):
    name, p, seed = spec
    data = RTH if seed is None else noisy(RTH, seed)
    t = run(data, p, SLIP)
    t["exp"] = name
    return name, t


def summary(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0}
    eq = t.pnl.cumsum()
    return {"n": len(t), "win": round((t.R > 0).mean(), 3), "E_R": round(t.R.mean(), 3),
            "net": round(t.pnl.sum()), "maxDD": round((eq.cummax() - eq).max())}


def monte_carlo(t: pd.DataFrame, sims: int = 10_000, seed: int = 0) -> dict:
    """Bootstrap whole trading days (keeps same-day correlation between symbols)."""
    rng = np.random.default_rng(seed)
    daily = t.groupby("date").pnl.sum().values
    n = len(daily)
    idx = rng.integers(0, n, (sims, n))
    paths = daily[idx].cumsum(axis=1)
    dd = (np.maximum.accumulate(paths, axis=1) - paths).max(axis=1)
    finals = paths[:, -1]
    # worst rolling ~21-trading-day (1 month of trade days) and losing streaks on trade level
    tr = t.R.values
    streaks = []
    for _ in range(2000):
        s = rng.choice(tr, len(tr))
        run_, best = 0, 0
        for r in s:
            run_ = run_ + 1 if r <= 0 else 0
            best = max(best, run_)
        streaks.append(best)
    months = t.assign(m=pd.to_datetime(t.date).dt.to_period("M")).groupby("m").pnl.sum()
    return {"maxDD_p50": round(np.percentile(dd, 50)), "maxDD_p95": round(np.percentile(dd, 95)),
            "maxDD_p99": round(np.percentile(dd, 99)), "final_p5": round(np.percentile(finals, 5)),
            "final_p50": round(np.percentile(finals, 50)), "prob_loss_full_period": round((finals < 0).mean(), 4),
            "loss_streak_p50": int(np.percentile(streaks, 50)), "loss_streak_p95": int(np.percentile(streaks, 95)),
            "losing_months_hist": f"{(months < 0).sum()}/{len(months)}",
            "worst_month_hist": round(months.min())}


def walk_forward(sweep: dict[float, pd.DataFrame], train_m: int = 12, test_m: int = 3) -> tuple[pd.DataFrame, list]:
    allt = {k: v.assign(d=pd.to_datetime(v.date)) for k, v in sweep.items()}
    start = min(v.d.min() for v in allt.values()).to_period("M").to_timestamp()
    end = max(v.d.max() for v in allt.values())
    out, picks = [], []
    t0 = start + pd.DateOffset(months=train_m)
    while t0 <= end:
        t1 = t0 + pd.DateOffset(months=test_m)
        tr_lo = t0 - pd.DateOffset(months=train_m)
        score = {k: v[(v.d >= tr_lo) & (v.d < t0)].R.sum() for k, v in allt.items()}
        best = max(score, key=score.get)
        picks.append((str(t0.date()), best))
        out.append(allt[best][(allt[best].d >= t0) & (allt[best].d < t1)])
        t0 = t1
    return pd.concat(out), picks


if __name__ == "__main__":
    cache = sys.argv[1]
    outdir = Path(sys.argv[2] if len(sys.argv) > 2 else "research/out")
    outdir.mkdir(parents=True, exist_ok=True)

    targets = [1.0, 1.25, 1.5, 1.75, 2.0]
    bands = [(20, 60), (30, 60), (25, 55), (25, 65)]
    specs = [(f"R{r}", replace(BASE, target_r=r), None) for r in targets]
    specs += [(f"band{lo}-{hi}", replace(BASE, atr_lo=lo, atr_hi=hi), None) for lo, hi in bands]
    specs += [(f"noise{s}", BASE, s) for s in (1, 2, 3)]

    with Pool(2, initializer=_init, initargs=(cache,)) as pool:
        res = dict(pool.map(job, specs))
    for k, v in res.items():
        v.to_csv(outdir / f"{k}.csv", index=False)

    base = res[f"R{BASE.target_r}"]
    report = {
        "A_target_sweep": {k: summary(res[f"R{r}"]) for k, r in ((f"{r}R", r) for r in targets)},
        "B_atr_band": {"25-60 (live)": summary(base), **{k[4:]: summary(res[k]) for k in res if k.startswith("band")}},
        "C_noise_2bp": {k: summary(res[k]) for k in res if k.startswith("noise")},
        "D_monte_carlo_live": monte_carlo(base),
    }
    wf, picks = walk_forward({r: res[f"R{r}"] for r in targets})
    fixed = base[pd.to_datetime(base.date) >= pd.to_datetime(picks[0][0])]
    report["E_walk_forward"] = {"wf": summary(wf), "fixed_1.5R_same_period": summary(fixed), "picks": picks}
    (outdir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
