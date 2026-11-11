"""Daily total-return (dividend/split adjusted) closes for the ETF rotation universe via yfinance."""
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

ETFS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "SHY", "LQD", "GLD", "DBC", "BIL"]
out = Path(sys.argv[1] if len(sys.argv) > 1 else "etf")
out.mkdir(parents=True, exist_ok=True)
df = yf.download(ETFS, start="1993-01-01", auto_adjust=True, progress=False)["Close"]
df.to_csv(out / "etf_daily_adjclose.csv")
print(df.apply(lambda s: s.first_valid_index()).to_string())
print(df.tail(2).to_string())
