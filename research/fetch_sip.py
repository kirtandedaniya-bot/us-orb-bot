"""Full-market 'stocks in play' dataset (Alpaca free SIP history, split-adjusted). Runs in GitHub Actions.

Stage 1  assets      : active, tradable US equities on NYSE/NASDAQ/AMEX/ARCA/BATS
Stage 2  daily       : daily bars for all of them -> ATR14, 14d avg volume, close (known pre-open via shift)
Stage 3  first5      : 09:30 5-min bar for every symbol that is ever a candidate (price>$5, avgvol>1M, ATR>$0.50)
Stage 4  selection   : RVOL = first5 volume / 14-day avg first5 volume; top 20 per day with RVOL >= 1
Stage 5  intraday    : 1-min bars 09:30-16:00 for the selected symbol-days

Caveat: universe = stocks active today (survivorship bias: delisted names missing).
"""
import os
import sys
import threading
import time as _t
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

NY = ZoneInfo("America/New_York")
START = datetime(2023, 11, 1, tzinfo=timezone.utc)   # warm-up for 14-day stats before 2024
END = datetime.now(timezone.utc) - timedelta(hours=1)
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "sip")
KEY, SEC = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
data = StockHistoricalDataClient(KEY, SEC)
_last = [0.0]
_lock = threading.Lock()


def bars(syms, tf, start, end) -> pd.DataFrame:
    for attempt in range(5):
        with _lock:                                   # global pacing: <=180 req/min across threads
            wait = 0.34 - (_t.time() - _last[0])
            if wait > 0:
                _t.sleep(wait)
            _last[0] = _t.time()
        try:
            df = data.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=tf, start=start, end=end,
                                                      feed=DataFeed.SIP, adjustment=Adjustment.SPLIT)).df
            return df.reset_index() if not df.empty else pd.DataFrame()
        except Exception as e:
            print("retry", attempt, str(e)[:120], flush=True)
            _t.sleep(5 * (attempt + 1))
    return pd.DataFrame()


def chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tc = TradingClient(KEY, SEC, paper=True)
    assets = tc.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))
    syms = sorted(a.symbol for a in assets if a.tradable and str(a.exchange.value) in
                  ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS") and "/" not in a.symbol)
    print("assets", len(syms), flush=True)

    # Stage 2: daily (resumable)
    if (OUT / "daily.parquet").exists():
        d = pd.read_parquet(OUT / "daily.parquet"); print("resume daily", flush=True)
    else:
        d = None
    if d is None:
        d = _daily(syms)
    cand = d[(d.prev_close > 5) & (d.avgvol14 > 1e6) & (d.atr14 > 0.5)]
    csyms = sorted(cand.symbol.unique())
    days = sorted(cand.date.unique())
    print("daily rows", len(d), "candidate symbols", len(csyms), "days", len(days), flush=True)
    return _rest(d, csyms, days)


def _daily(syms) -> pd.DataFrame:
    parts = [bars(c, TimeFrame.Day, START, END) for c in chunks(syms, 200)]
    d = pd.concat([p for p in parts if not p.empty])
    d["date"] = pd.to_datetime(d["timestamp"]).dt.tz_convert(NY).dt.date
    d = d.sort_values(["symbol", "date"])
    g = d.groupby("symbol")
    pc = g["close"].shift(1)
    tr = np.maximum(d["high"] - d["low"], np.maximum((d["high"] - pc).abs(), (d["low"] - pc).abs()))
    d["atr14"] = tr.groupby(d["symbol"]).transform(lambda x: x.rolling(14).mean().shift(1))
    d["avgvol14"] = g["volume"].transform(lambda x: x.rolling(14).mean().shift(1))
    d["prev_close"] = pc
    d = d[["symbol", "date", "open", "high", "low", "close", "volume", "prev_close", "atr14", "avgvol14"]]
    d.to_parquet(OUT / "daily.parquet", compression="zstd")
    return d


def _first5(csyms, days) -> pd.DataFrame:
    """Per-month checkpoints in OUT/f5/ so a timed-out run resumes where it stopped."""
    ck = OUT / "f5"; ck.mkdir(exist_ok=True)
    tf5 = TimeFrame(5, TimeFrameUnit.Minute)
    months = sorted({(dy.year, dy.month) for dy in days})
    for (y, mo) in months:
        f = ck / f"{y}-{mo:02d}.parquet"
        if f.exists():
            continue
        jobs = [(datetime.combine(day, time(9, 30), NY), c)
                for day in days if (day.year, day.month) == (y, mo) for c in chunks(csyms, 400)]
        with ThreadPoolExecutor(8) as ex:             # requests are latency-bound (~3s), not rate-bound
            res = list(ex.map(lambda j: bars(j[1], tf5, j[0], j[0] + timedelta(minutes=4)), jobs))
        part = [b[["symbol", "timestamp", "open", "high", "low", "close", "volume"]] for b in res if not b.empty]
        if part:
            pd.concat(part).to_parquet(f, compression="zstd")
        print("first5 month", y, mo, flush=True)
    return pd.concat(pd.read_parquet(f) for f in sorted(ck.glob("*.parquet")))


def _rest(d, csyms, days) -> int:
    # Stage 3: first 5-min bar per day for all candidate symbols
    f5 = _first5(csyms, days)
    f5["date"] = pd.to_datetime(f5["timestamp"]).dt.tz_convert(NY).dt.date
    f5 = f5[pd.to_datetime(f5["timestamp"]).dt.tz_convert(NY).dt.time == time(9, 30)]
    f5 = f5.drop(columns="timestamp").sort_values(["symbol", "date"])
    f5["avg_f5vol14"] = f5.groupby("symbol")["volume"].transform(lambda x: x.rolling(14).mean().shift(1))
    f5["rvol"] = f5["volume"] / f5["avg_f5vol14"]
    f5.to_parquet(OUT / "first5.parquet", compression="zstd")

    # Stage 4: selection (filters use only pre-open info + the first 5 minutes)
    m = f5.merge(d[["symbol", "date", "prev_close", "atr14", "avgvol14"]], on=["symbol", "date"])
    m = m[(m.prev_close > 5) & (m.avgvol14 > 1e6) & (m.atr14 > 0.5) & (m.rvol >= 1)]
    m["rank"] = m.groupby("date")["rvol"].rank(ascending=False, method="first")
    sel = m[m["rank"] <= 20].sort_values(["date", "rank"])
    sel.to_csv(OUT / "selected.csv", index=False)
    print("selected symbol-days", len(sel), flush=True)

    # Stage 5: 1-min bars for selected symbol-days, one checkpoint file per month
    sel["date"] = pd.to_datetime(sel["date"]).dt.date
    sel["ym"] = sel["date"].map(lambda x: f"{x.year}-{x.month:02d}")
    n = 0
    for ym, mgrp in sel.groupby("ym"):
        f = OUT / f"intraday_{ym}.parquet"
        if f.exists():
            continue
        jobs = [(day, list(grp.symbol)) for day, grp in mgrp.groupby("date")]
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda j: bars(j[1], TimeFrame.Minute, datetime.combine(j[0], time(9, 30), NY),
                                             datetime.combine(j[0], time(15, 59), NY)), jobs))
        out = [b[["symbol", "timestamp", "open", "high", "low", "close", "volume"]] for b in res if not b.empty]
        if out:
            intr = pd.concat(out)
            intr["timestamp"] = pd.to_datetime(intr["timestamp"])
            for c in ("open", "high", "low", "close", "volume"):
                intr[c] = intr[c].astype("float32")
            intr.to_parquet(f, compression="zstd"); n += len(intr)
        print("intraday month", ym, flush=True)
    print("intraday rows (this run)", n, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
