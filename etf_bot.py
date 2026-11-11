"""ETF momentum rotation — live bot (Alpaca paper). Runs monthly via GitHub Actions.

Rules (same as research/etf_backtest.py, strategy D, tested 2007-2026):
  universe : SPY IWM EFA EEM VNQ GLD DBC TLT IEF LQD ; cash = SHY
  score    : average of 3-, 6- and 12-month total returns (month-end closes)
  hold     : top 3 by score, 1/3 each — but a pick below its 10-month average goes to SHY instead
  rebalance: first trading day of each month, ~10:00 ET, using last month-end closes
Uses its OWN Alpaca paper account (ETF_ALPACA_API_KEY / ETF_ALPACA_SECRET_KEY) so the ORB bot's
end-of-day flatten can never touch these holdings.

python etf_bot.py           -> rebalance if today is the first trading day of the month (idempotent)
python etf_bot.py --dry-run -> print targets/orders only
python etf_bot.py --force   -> rebalance now (market must be open; still once per month)
"""
import logging
import math
import os
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest
from dotenv import load_dotenv

load_dotenv()

from orb import notify  # noqa: E402
from orb.broker import retry  # noqa: E402

log = logging.getLogger("etf")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
notify.ENGINE = "ETF_MOMENTUM"

NY = ZoneInfo("America/New_York")
RISK = ["SPY", "IWM", "EFA", "EEM", "VNQ", "GLD", "DBC", "TLT", "IEF", "LQD"]
CASH = "SHY"
TOP = 3
SMA_MONTHS = 10
INVEST_FRACTION = 0.98            # keep 2% cash buffer for price moves between sells and buys
MIN_TRADE_FRACTION = 0.02         # skip rebalancing trades smaller than 2% of equity
RUN_WINDOW = (time(9, 45), time(12, 0))


# ---------------- rules (pure) ----------------
def month_end_closes(daily: pd.DataFrame) -> pd.DataFrame:
    return daily.groupby(daily.index.to_period("M")).tail(1)


def target_weights(daily: pd.DataFrame) -> tuple[dict[str, float], pd.Series]:
    """daily: adjusted closes (rows=dates, cols=tickers) up to and including the last month-end."""
    m = month_end_closes(daily)
    if len(m) < 13:
        raise ValueError(f"need 13 month-ends, have {len(m)}")
    last, sma = m.iloc[-1], m.iloc[-SMA_MONTHS:].mean()
    score = sum(m.iloc[-1] / m.iloc[-1 - k] - 1 for k in (3, 6, 12))[RISK] / 3
    w: dict[str, float] = {}
    for t in score.nlargest(TOP).index:
        k = t if last[t] > sma[t] else CASH
        w[k] = w.get(k, 0.0) + 1 / TOP
    return w, score.sort_values(ascending=False)


# ---------------- broker ----------------
def clients():
    key, sec = os.getenv("ETF_ALPACA_API_KEY", ""), os.getenv("ETF_ALPACA_SECRET_KEY", "")
    if not key or not sec:
        raise SystemExit("ETF_ALPACA_API_KEY / ETF_ALPACA_SECRET_KEY missing (separate paper account required)")
    return TradingClient(key, sec, paper=True), StockHistoricalDataClient(key, sec)


def daily_closes(data, end: date) -> pd.DataFrame:
    req = StockBarsRequest(symbol_or_symbols=RISK + [CASH], timeframe=TimeFrame.Day,
                           start=datetime.combine(end - timedelta(days=430), time(0), NY),
                           end=datetime.combine(end, time(0), NY), feed=DataFeed.SIP, adjustment=Adjustment.ALL)
    df = retry(lambda: data.get_stock_bars(req).df, "daily bars").reset_index()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(NY).dt.tz_localize(None).dt.normalize()
    px = df.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
    return px[px.index.date < end]


def first_trading_day_of_month(tc, d: date) -> bool:
    cal = retry(lambda: tc.get_calendar(GetCalendarRequest(start=d.replace(day=1), end=d)), "calendar")
    days = [c.date for c in cal]
    return bool(days) and days[0] == d


def already_done(tc, d: date) -> bool:
    after = datetime.combine(d.replace(day=1), time(0), NY)
    orders = retry(lambda: tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, limit=100)),
                   "orders")
    return any((o.client_order_id or "").startswith(f"etf-{d:%Y%m}-") for o in orders)


def wait_filled(tc, ids: list, timeout: int = 120) -> None:
    end = _time.time() + timeout
    while ids and _time.time() < end:
        ids = [i for i in ids if retry(lambda i=i: tc.get_order_by_id(i), "order").status.value
               not in ("filled", "canceled", "rejected", "expired")]
        if ids:
            _time.sleep(3)


# ---------------- main ----------------
def rebalance(dry: bool = False, force: bool = False) -> int:
    now = datetime.now(NY)
    today = now.date()
    tc, data = clients()
    if os.getenv("ETF_TRADING_ENABLED", "true").lower() == "false":
        notify.event("KILL_SWITCH", "ETF_TRADING_ENABLED=false — no rebalance", alert=True, color=notify.AMBER)
        return 0
    if force and not dry:
        if not retry(tc.get_clock, "clock").is_open:
            log.info("--force needs the market open — exit"); return 0
        if already_done(tc, today):
            log.info("Already rebalanced this month — exit"); return 0
    elif not dry:
        if not first_trading_day_of_month(tc, today):
            log.info("Not the first trading day of the month — exit"); return 0
        if not (RUN_WINDOW[0] <= now.time() <= RUN_WINDOW[1]):
            log.info("Outside run window — exit"); return 0
        if already_done(tc, today):
            log.info("Already rebalanced this month — exit"); return 0

    px = daily_closes(data, today)
    weights, score = target_weights(px)
    acct = retry(tc.get_account, "account")
    equity = float(acct.equity)
    held = {p.symbol: float(p.qty) for p in retry(tc.get_all_positions, "positions")}
    syms = sorted(set(held) | set(weights))
    last = retry(lambda: data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=syms)), "prices")
    price = {s: float(t.price) for s, t in last.items()}

    target = {s: math.floor(weights.get(s, 0) * equity * INVEST_FRACTION / price[s]) for s in syms}
    delta = {s: target[s] - held.get(s, 0) for s in syms}
    trades = {s: q for s, q in delta.items()
              if q != 0 and (target[s] == 0 or abs(q) * price[s] >= MIN_TRADE_FRACTION * equity)}

    lines = [f"{s}: {held.get(s, 0):.0f} → {target[s]} ({'+' if q > 0 else ''}{q:.0f})" for s, q in trades.items()]
    rank = ", ".join(f"{t} {v:+.1%}" for t, v in score.head(5).items())
    msg = (f"Targets: " + ", ".join(f"{k} {v:.0%}" for k, v in weights.items()) +
           f"\nTop scores: {rank}\nEquity ${equity:,.0f}\n" + ("\n".join(lines) or "No trades needed"))
    log.info(msg)
    if dry:
        print(msg); return 0

    tag = f"etf-{today:%Y%m}"
    sells = [retry(lambda s=s, q=q: tc.submit_order(MarketOrderRequest(
        symbol=s, qty=-q, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        client_order_id=f"{tag}-{s}-sell")), f"sell {s}").id for s, q in trades.items() if q < 0]
    wait_filled(tc, sells)
    for s, q in trades.items():
        if q > 0:
            try:
                retry(lambda s=s, q=q: tc.submit_order(MarketOrderRequest(
                    symbol=s, qty=q, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                    client_order_id=f"{tag}-{s}-buy")), f"buy {s}")
            except APIError as e:
                notify.event("ORDER_REJECTED", f"buy {s} x{q}: {e}", alert=True, color=notify.RED)
    if not trades:   # still mark the month as done
        log.info("No trades; nothing submitted")
    notify.discord(f"ETF momentum rebalance {today:%b %Y}", msg, notify.GREEN)
    notify.event("REBALANCE", msg.replace("\n", " | "))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(rebalance(dry="--dry-run" in sys.argv, force="--force" in sys.argv))
    except SystemExit:
        raise
    except Exception as e:
        log.exception("fatal")
        notify.event("FATAL", f"etf_bot: {e!r}", alert=True, color=notify.RED)
        sys.exit(1)
