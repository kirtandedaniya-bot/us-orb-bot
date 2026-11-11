"""Download RTH 1-min bars (SIP, free for data >15 min old) for the research universe.
Writes data/<SYMBOL>.parquet (timestamp UTC, OHLCV, float32). Run in GitHub Actions (has keys)."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

UNIVERSE = os.getenv("RESEARCH_UNIVERSE", (
    "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AMD,AVGO,NFLX,JPM,BAC,XOM,CVX,UNH,LLY,V,"
    "MA,COST,WMT,HD,CRM,ORCL,ADBE,INTC,MU,QCOM,PLTR,COIN,UBER,BA,DIS,PFE,KO,PEP,NKE,SMCI")).split(",")
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "data")


def main() -> int:
    c = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    end = datetime.now(timezone.utc) - timedelta(minutes=30)
    OUT.mkdir(parents=True, exist_ok=True)
    for sym in UNIVERSE:
        parts, s = [], START
        while s < end:
            e = min(s + timedelta(days=120), end)
            df = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                                   start=s, end=e, feed=DataFeed.SIP)).df
            if not df.empty:
                parts.append(df.xs(sym, level=0))
            s = e
        df = pd.concat(parts)
        ny = df.index.tz_convert("America/New_York")
        t = ny.hour * 60 + ny.minute
        df = df[(t >= 570) & (t < 960)][["open", "high", "low", "close", "volume"]].astype("float32")
        df.index.name = "timestamp"
        df.to_parquet(OUT / f"{sym}.parquet", compression="zstd")
        print(f"{sym}: {len(df):,} bars {df.index[0].date()}..{df.index[-1].date()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
