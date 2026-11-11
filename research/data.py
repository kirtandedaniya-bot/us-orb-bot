"""Shared research data helpers.

HOLDOUT: everything from HOLDOUT_START on is the 'final exam' set. Research decisions are made on
data BEFORE it; the holdout is evaluated once per finished idea, never used to pick parameters.
(Note: the 1.5R target chosen on 2026-11-11 had already seen this period — recorded honestly.)
"""
from datetime import time
from pathlib import Path

import pandas as pd

HOLDOUT_START = pd.Timestamp("2026-05-01")


def load_dir(path: str, symbols: list[str] | None = None) -> pd.DataFrame:
    """Parquet files from the `data` branch -> RTH frame in backtest.run() format."""
    parts = []
    for f in sorted(Path(path).glob("*.parquet")):
        if symbols and f.stem not in symbols:
            continue
        d = pd.read_parquet(f).astype("float64")
        d["symbol"] = f.stem
        parts.append(d)
    df = pd.concat(parts)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    df.index.name = "timestamp"
    df["date"] = df.index.date
    df["t"] = df.index.time
    return df[(df["t"] >= time(9, 30)) & (df["t"] < time(16, 0))]


def split(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = pd.to_datetime(trades["date"])
    return trades[d < HOLDOUT_START], trades[d >= HOLDOUT_START]
