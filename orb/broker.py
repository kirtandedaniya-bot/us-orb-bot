"""Alpaca wrapper: retries on transient failures, market-calendar helpers, data fetch."""
import logging
import os
import time as _time
from datetime import date, datetime, timedelta
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

log = logging.getLogger("orb")
NY = ZoneInfo("America/New_York")
T = TypeVar("T")

INTRADAY_FEED = DataFeed.SIP if os.getenv("INTRADAY_FEED", "iex").lower() == "sip" else DataFeed.IEX


def retry(fn: Callable[[], T], what: str, tries: int = 4) -> T:
    """Retry network/5xx/429; re-raise 4xx business errors immediately."""
    for i in range(1, tries + 1):
        try:
            return fn()
        except APIError as e:
            code = getattr(e, "status_code", None)
            if code is not None and code < 500 and code != 429:
                raise
            err = e
        except Exception as e:  # connection reset, timeout, DNS
            err = e
        log.warning("%s failed (%d/%d): %s", what, i, tries, err)
        if i < tries:
            _time.sleep(min(2 ** i, 20))
    raise RuntimeError(f"{what} failed after {tries} tries: {err}")


class Broker:
    def __init__(self) -> None:
        key, sec = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not sec:
            raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
        paper = os.getenv("ALPACA_PAPER", "true").lower() != "false"
        self.trading = TradingClient(key, sec, paper=paper)
        self.data = StockHistoricalDataClient(key, sec)

    # ---------- calendar ----------
    def session(self, d: date) -> tuple[datetime, datetime] | None:
        cal = retry(lambda: self.trading.get_calendar(GetCalendarRequest(start=d, end=d)), "calendar")
        if not cal or cal[0].date != d:
            return None

        def aware(x):
            if isinstance(x, datetime):
                return x if x.tzinfo else x.replace(tzinfo=NY)
            return datetime.combine(d, x, NY)
        return aware(cal[0].open), aware(cal[0].close)

    # ---------- data ----------
    def daily_bars(self, symbols: list[str], today: date, lookback: int = 90) -> dict[str, pd.DataFrame]:
        start = datetime.combine(today - timedelta(days=lookback), datetime.min.time(), NY)
        end = datetime.combine(today, datetime.min.time(), NY)  # excludes today
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                               start=start, end=end, feed=DataFeed.SIP)
        df = retry(lambda: self.data.get_stock_bars(req).df, "daily bars")
        out = {}
        for s in symbols:
            if s in df.index.get_level_values(0):
                d = df.xs(s, level=0)
                d = d[d.index.tz_convert(NY).date < today]
                out[s] = d[["high", "low", "close"]]
        return out

    def orb_bars(self, symbols: list[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
                               start=start, end=end, feed=INTRADAY_FEED)
        df = retry(lambda: self.data.get_stock_bars(req).df, "minute bars")
        out = {}
        for s in symbols:
            if s in df.index.get_level_values(0):
                d = df.xs(s, level=0)
                d.index = d.index.tz_convert(NY)
                out[s] = d[(d.index >= start) & (d.index < end)]
        return out

    def last_price(self, symbols: list[str]) -> dict[str, float]:
        req = StockLatestTradeRequest(symbol_or_symbols=symbols, feed=INTRADAY_FEED)
        res = retry(lambda: self.data.get_stock_latest_trade(req), "latest trade")
        return {s: float(t.price) for s, t in res.items()}
