"""One-change-at-a-time variant tests vs the live rules, with in-sample / out-of-sample split.
Usage: python research/variants.py candle_cache.pkl [outdir]
A variant is only worth adopting if it beats baseline in BOTH halves and is not a knife-edge setting."""
import json
import sys
from dataclasses import replace
from datetime import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import load, run  # noqa: E402
from orb.strategy import Params  # noqa: E402

SPLIT = "2025-07-01"
SLIP = 0.01
B = Params()
VARIANTS = {
    "baseline": B,
    "orb15": replace(B, orb_end=time(9, 45), min_orb_bars=13),
    "orb30": replace(B, orb_end=time(10, 0), min_orb_bars=27),
    "orb60": replace(B, orb_end=time(10, 30), min_orb_bars=55),
    "stop_mid": replace(B, stop_mode="mid"),
    "entry_close": replace(B, entry_mode="close"),
    "breakeven_1R": replace(B, be_at_r=1.0),
    "no_trend": replace(B, trend_len=0),
    "trend50": replace(B, trend_len=50),
    "trend200": replace(B, trend_len=200),
}
RTH = None


def _init(path):
    global RTH
    RTH = load(path)


def job(name):
    return name, run(RTH, VARIANTS[name], SLIP)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0}
    bs = np.random.default_rng(0).choice(t.R.values, (2000, len(t))).mean(axis=1)
    eq = t.pnl.cumsum()
    return {"n": len(t), "win": round((t.R > 0).mean(), 3), "E_R": round(t.R.mean(), 3),
            "CI95": [round(np.percentile(bs, 2.5), 3), round(np.percentile(bs, 97.5), 3)],
            "net": round(t.pnl.sum()), "maxDD": round((eq.cummax() - eq).max())}


if __name__ == "__main__":
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "research/out")
    out.mkdir(parents=True, exist_ok=True)
    with Pool(2, initializer=_init, initargs=(sys.argv[1],)) as pool:
        res = dict(pool.map(job, list(VARIANTS)))
    rep = {}
    for k, t in res.items():
        t.to_csv(out / f"var_{k}.csv", index=False)
        d = pd.to_datetime(t.date)
        rep[k] = {"all": stats(t), "in_sample": stats(t[d < SPLIT]), "out_sample": stats(t[d >= SPLIT])}
    (out / "variants_report.json").write_text(json.dumps(rep, indent=1))
    for k, v in rep.items():
        a, i, o = v["all"], v["in_sample"], v["out_sample"]
        print(f"{k:13s} n={a['n']:4d} win={a['win']:.0%} E[R]={a['E_R']:+.3f} net=${a['net']:>6,} DD=${a['maxDD']:>5,} "
              f"| IS {i['E_R']:+.3f} (n={i['n']}) | OOS {o['E_R']:+.3f} (n={o['n']}) CI{o['CI95']}")
