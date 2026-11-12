"""Daily adjusted OHLC for strategy research (runs in GitHub Actions; yfinance)."""
import sys
import time
from pathlib import Path

import yfinance as yf

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "ohlc")
TICKERS = ["SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "EFA", "^VIX"]

OUT.mkdir(parents=True, exist_ok=True)
for t in TICKERS:
    for attempt in range(5):
        try:
            d = yf.Ticker(t).history(start="1993-01-01", auto_adjust=True)
            if len(d) > 1000:
                d.index = d.index.tz_localize(None)
                d[["Open", "High", "Low", "Close", "Volume"]].to_csv(OUT / f"{t.replace('^', '')}.csv")
                print(t, len(d), d.index[0].date(), d.index[-1].date(), flush=True)
                break
        except Exception as e:
            print("retry", t, e, flush=True)
        time.sleep(5 * (attempt + 1))
    else:
        print("FAILED", t, flush=True)
