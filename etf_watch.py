"""Daily ETF crash watch (read-only — never trades). Pings Discord when:
  * SPY falls >= 3% in a day, or
  * the ETF account crosses a drawdown level (5/10/15/20% below its peak) today.
The strategy itself still only acts on the monthly rebalance; this is information, not a sell signal.
"""
import logging
import os
import sys

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()
from orb import notify  # noqa: E402
from orb.broker import retry  # noqa: E402

log = logging.getLogger("etf_watch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
notify.ENGINE = "ETF_MOMENTUM"
LEVELS = (0.05, 0.10, 0.15, 0.20)
SPY_DROP = -0.03
NOTE = ("No action needed: the bot only changes holdings on the monthly rebalance "
        "(downtrending funds rotate into SHY then). Backtest worst drop was −18.9%.")


def dd_series(eq):
    peak, out = 0.0, []
    for e in eq:
        peak = max(peak, e)
        out.append(1 - e / peak)
    return out


def main() -> int:
    k, s = os.environ["ETF_ALPACA_API_KEY"], os.environ["ETF_ALPACA_SECRET_KEY"]
    tc = TradingClient(k, s, paper=True)
    h = retry(lambda: tc.get_portfolio_history(GetPortfolioHistoryRequest(period="1A", timeframe="1D")), "history")
    eq = [float(e) for e in (h.equity or []) if e is not None and float(e) > 0]
    eq.append(float(retry(tc.get_account, "account").equity))
    dd = dd_series(eq)
    today, prev = dd[-1], (dd[-2] if len(dd) > 1 else 0.0)
    crossed = [lv for lv in LEVELS if prev < lv <= today]
    msgs = []
    if crossed:
        msgs.append(f"ETF account is {today:.1%} below its peak (${max(eq):,.0f} → ${eq[-1]:,.0f}).")

    data = StockHistoricalDataClient(k, s)
    bars = retry(lambda: data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=10))).df, "spy")
    c = bars["close"].to_numpy()
    if len(c) >= 2:
        r = c[-1] / c[-2] - 1
        log.info(f"SPY day {r:+.2%} | account dd {today:.2%} | equity ${eq[-1]:,.0f}")
        if r <= SPY_DROP:
            msgs.append(f"Market drop: SPY {r:+.1%} today.")
    if msgs:
        notify.event("CRASH_WATCH", " ".join(msgs) + " " + NOTE, alert=True, color=notify.RED)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.exception("fatal")
        notify.event("FATAL", f"etf_watch: {e!r}", alert=True, color=notify.RED)
        sys.exit(1)
