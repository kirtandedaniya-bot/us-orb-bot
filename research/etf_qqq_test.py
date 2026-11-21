"""Pre-registered one-shot test (2026-11-21): add QQQ to the ETF momentum universe; rules unchanged.
Pass: CAGR and Sharpe not worse, MaxDD not worse by >3 points, both 2007-03+ and OOS 2009+.
Usage: python research/etf_qqq_test.py ETF_CSV OHLC_DIR"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.argv = sys.argv[:1] + sys.argv[1:]
import etf_backtest as E  # noqa: E402

px = pd.read_csv(sys.argv[1], index_col=0, parse_dates=True).ffill().drop(columns=["BIL", "QQQ"], errors="ignore")
qqq = pd.read_csv(Path(sys.argv[2]) / "QQQ.csv", index_col=0, parse_dates=True)["Close"]
px["QQQ"] = qqq.reindex(px.index).ffill()
base = list(E.RISK)
px = px.dropna(subset=base + ["QQQ", E.CASH], how="any")
px = px[px.index < "2026-11-11"]
out = []
for name, uni in (("Current 10 ETFs", base), ("+ QQQ (11 ETFs)", base + ["QQQ"])):
    E.RISK = uni
    r = E.run(px, E.rotation, "2007-03-30")
    for per, st in (("2007+", "2007-03-30"), ("OOS 2009+", "2009-01-01"), ("last 5y", "2021-11-10")):
        x = r[r.index >= st]
        eq = (1 + x).cumprod(); yrs = len(x) / 252
        out.append([name, per, f"{eq.iloc[-1] ** (1 / yrs) - 1:+.1%}", f"{x.mean() / x.std() * np.sqrt(252):.2f}",
                    f"{(eq / eq.cummax() - 1).min():.1%}"])
print(pd.DataFrame(out, columns=["Universe", "Period", "CAGR", "Sharpe", "MaxDD"]).to_string(index=False))
