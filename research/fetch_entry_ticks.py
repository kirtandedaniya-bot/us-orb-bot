"""Resolve the entry-minute ambiguity in sip_backtest with SIP trade ticks (runs in GitHub Actions).

For each candidate entry (research/entry_minutes.csv) fetch all trades in the entry minute, find the first
trade through the trigger, then whether a later trade in that same minute crosses the stop for each stop size.
Output: OUT/entry_ticks.csv  (fill, and for k in STOPS: hit_k, px_k = first crossing trade price or NaN)
"""
import os
import sys
import threading
import time as _t
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockTradesRequest

NY = ZoneInfo("America/New_York")
STOPS = (0.05, 0.10, 0.20)
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "ticks")
data = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
_lock, _last = threading.Lock(), [0.0]


def trades(sym, start, end) -> pd.DataFrame:
    for attempt in range(5):
        with _lock:
            w = 0.34 - (_t.time() - _last[0])
            if w > 0:
                _t.sleep(w)
            _last[0] = _t.time()
        try:
            df = data.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=start, end=end,
                                                          feed=DataFeed.SIP)).df
            return df.reset_index() if not df.empty else pd.DataFrame()
        except Exception as e:
            print("retry", attempt, sym, str(e)[:100], flush=True)
            _t.sleep(5 * (attempt + 1))
    return pd.DataFrame()


def resolve(r) -> dict:
    s = datetime.fromisoformat(r.date).replace(tzinfo=NY) + timedelta(minutes=int(r.mins))
    t = trades(r.symbol, s, s + timedelta(minutes=1))
    out = dict(symbol=r.symbol, date=r.date, n=len(t), fill=np.nan)
    if t.empty:
        return out
    t = t.sort_values("timestamp")
    p = t["price"].to_numpy()
    trig = np.nonzero(p >= r.lvl)[0] if r.side == 1 else np.nonzero(p <= r.lvl)[0]
    if len(trig) == 0:
        return out
    i = trig[0]
    fill = max(p[i], r.lvl) if r.side == 1 else min(p[i], r.lvl)
    out["fill"] = fill
    later = p[i + 1:]
    for k in STOPS:
        stop = fill - r.side * k * r.atr
        x = np.nonzero(later <= stop)[0] if r.side == 1 else np.nonzero(later >= stop)[0]
        out[f"hit_{k}"] = int(len(x) > 0)
        out[f"px_{k}"] = float(later[x[0]]) if len(x) else np.nan
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    e = pd.read_csv(Path(__file__).with_name("entry_minutes.csv"))
    rows = list(e.itertuples())
    res = []
    with ThreadPoolExecutor(8) as ex:
        for i, o in enumerate(ex.map(resolve, rows)):
            res.append(o)
            if i % 500 == 0:
                print("done", i, "/", len(rows), flush=True)
    pd.DataFrame(res).to_csv(OUT / "entry_ticks.csv", index=False)
    print("rows", len(res), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
