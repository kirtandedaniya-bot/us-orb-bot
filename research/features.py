"""Per (symbol, date) features known at 10:15 ET: ORB volume, RVOL vs prior 20 days, overnight gap."""
import sys
from datetime import time
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.data import load_dir  # noqa: E402

DATA = sys.argv[1]


def one(sym):
    d = load_dir(DATA, [sym])
    orb = d[d["t"] < time(10, 15)].groupby("date").agg(orb_vol=("volume", "sum"), open=("open", "first"),
                                                     orb_hi=("high", "max"), orb_lo=("low", "min"))
    day = d.groupby("date").agg(close=("close", "last"), dvol=("volume", "sum"))
    f = orb.join(day)
    f["rvol"] = f.orb_vol / f.orb_vol.rolling(20).mean().shift(1)
    f["gap"] = f.open / f.close.shift(1) - 1
    f["dollar_vol20"] = (f.close * f.dvol).rolling(20).mean().shift(1)
    f["symbol"] = sym
    return f.reset_index()[["symbol", "date", "rvol", "gap", "dollar_vol20"]]


if __name__ == "__main__":
    syms = sorted(p.stem for p in Path(DATA).glob("*.parquet"))
    with Pool(2) as p:
        out = pd.concat(p.map(one, syms))
    out.to_csv(sys.argv[2], index=False)
    print(len(out))
