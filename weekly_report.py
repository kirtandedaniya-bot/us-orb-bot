"""Sunday weekly report of all three paper bots → Discord (detailed + plain English). Read-only, never trades."""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest
from dotenv import load_dotenv

load_dotenv()
from orb import notify  # noqa: E402
from orb.broker import retry  # noqa: E402

log = logging.getLogger("report")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
notify.ENGINE = "REPORT"

# name, key env, secret env, real starting equity, shown-as starting balance
BOTS = [("ORB (day trades)", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", 99905.0, 10000.0),
        ("ETF Momentum (monthly)", "ETF_ALPACA_API_KEY", "ETF_ALPACA_SECRET_KEY", 10000.0, 10000.0),
        ("RSI-2 (dip buyer)", "RSI2_ALPACA_API_KEY", "RSI2_ALPACA_SECRET_KEY", 10000.0, 10000.0)]


def week_change(tc) -> tuple[float, float]:
    """(equity now, equity one week ago)."""
    h = retry(lambda: tc.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D")), "history")
    eq = [(t, float(e)) for t, e in zip(h.timestamp, h.equity) if e]
    now = float(retry(tc.get_account, "account").equity)
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    before = [e for t, e in eq if t <= cutoff]
    return now, (before[-1] if before else (eq[0][1] if eq else now))


def spy_week(key, sec) -> float:
    data = StockHistoricalDataClient(key, sec)
    df = retry(lambda: data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=12),
        end=datetime.now(timezone.utc) - timedelta(minutes=16))).df, "spy")
    c = df["close"].to_numpy()
    return c[-1] / c[-6] - 1 if len(c) >= 6 else 0.0


def main() -> int:
    rows, simple, tot_week, tot_all = [], [], 0.0, 0.0
    spy = None
    for name, ke, se, real_start, shown_start in BOTS:
        k, s = os.getenv(ke), os.getenv(se)
        if not k or not s:
            rows.append(f"**{name}**: keys missing"); continue
        tc = TradingClient(k, s, paper=True)
        now, wk = week_change(tc)
        dw, dall = now - wk, now - real_start
        bal = shown_start + dall
        tot_week += dw; tot_all += dall
        rows.append(f"**{name}**: balance ${bal:,.2f} | this week {dw:+,.2f} | since start {dall:+,.2f} "
                    f"({dall / shown_start:+.1%})")
        simple.append(f"• {name}: {'up' if dw >= 0 else 'down'} ${abs(dw):,.0f} this week, "
                      f"{'up' if dall >= 0 else 'down'} ${abs(dall):,.0f} since starting.")
        if spy is None:
            try:
                spy = spy_week(k, s)
            except Exception as e:
                log.warning("spy: %s", e)
    body = "\n".join(rows) + f"\n\nAll bots this week: {tot_week:+,.2f} | since start: {tot_all:+,.2f}"
    if spy is not None:
        body += f"\nS&P 500 (SPY) this week: {spy:+.1%}"
    plain = ("Weekly check-in:\n" + "\n".join(simple) +
             f"\n\nTogether: {'up' if tot_week >= 0 else 'down'} ${abs(tot_week):,.0f} this week "
             f"({'up' if tot_all >= 0 else 'down'} ${abs(tot_all):,.0f} overall)."
             + (f" The overall US market {'rose' if spy >= 0 else 'fell'} {abs(spy):.1%} this week." if spy is not None else "")
             + "\nRemember: judge the bots over months, not weeks. All of this is paper money.")
    log.info(body.replace("\n", " | "))
    notify.discord(f"Weekly report — {datetime.now(timezone.utc):%d %b %Y}", body, notify.GREY, plain=plain)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.exception("fatal")
        notify.event("FATAL", f"weekly_report: {e!r}", alert=True, color=notify.RED)
        sys.exit(1)
