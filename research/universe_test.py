"""V1 rules (live params) on every symbol in the data dir. Usage: python research/universe_test.py DATA_DIR OUT_CSV"""
import sys
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import run  # noqa: E402
from orb.strategy import Params  # noqa: E402
from research.data import load_dir  # noqa: E402

DATA = sys.argv[1]


def one(sym: str) -> pd.DataFrame:
    return run(load_dir(DATA, [sym]), Params(), 0.01)


if __name__ == "__main__":
    syms = sorted(p.stem for p in Path(DATA).glob("*.parquet"))
    with Pool(2) as pool:
        t = pd.concat(pool.map(one, syms), ignore_index=True)
    t.to_csv(sys.argv[2], index=False)
    print(len(t), "trades")
