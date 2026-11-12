"""RSI-2 mean reversion on SPY — live bot (Alpaca paper, its OWN account). Runs every evening via GitHub Actions.

Rules: rsi2_rules.py (Connors 2008; tested 1993-2026, parity-checked vs research 0 mismatches).
Flow (after each US close):
  1. find the last completed session (signal day) and the next session (execution day)
  2. pull official adjusted daily closes incl. the signal day; sanity-check them
  3. decide hold/flat for the next session from the current SPY position
  4. if it changes: submit a market-on-open (OPG) order for the next open, id rsi2-YYYYMMDD-buy|sell
Safety: idempotent client_order_id, kill switch, separate account, stale/short data refusal, buying-power
check, only ever touches SPY, never shorts, alerts on anything unexpected.

python rsi2_bot.py            -> normal evening run
python rsi2_bot.py --dry-run  -> show signal + what it would do, no orders
python rsi2_bot.py --status   -> post a status line to Discord (used Fridays)
"""
import logging
import math
import os
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest
from dotenv import load_dotenv

load_dotenv()

import rsi2_rules as R  # noqa: E402
from orb import notify  # noqa: E402
from orb.broker import retry  # noqa: E402

log = logging.getLogger("rsi2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
notify.ENGINE = "RSI2_SPY"

NY = ZoneInfo("America/New_York")
SYMBOL = "SPY"
INVEST_FRACTION = 0.97          # buffer for an opening gap up between the close and the open
OPG_OPEN = time(19, 0)          # Alpaca accepts OPG orders after 19:00 ET ...
OPG_CUTOFF = time(9, 28)        # ... and before 09:28 ET of the next session
CLOSE_GRACE = timedelta(minutes=20)
HISTORY_DAYS = 600              # ~410 sessions; parity verified identical to full history


def clients():
    key, sec = os.getenv("RSI2_ALPACA_API_KEY", ""), os.getenv("RSI2_ALPACA_SECRET_KEY", "")
    if not key or not sec:
        raise SystemExit("RSI2_ALPACA_API_KEY / RSI2_ALPACA_SECRET_KEY missing (separate paper account required)")
    return TradingClient(key, sec, paper=True), StockHistoricalDataClient(key, sec)


def sessions(tc, now: datetime) -> tuple[date, date, datetime, datetime]:
    """(signal_day, next_day, next_open, next_close) — signal_day = last session closed >= 20 min ago."""
    cal = retry(lambda: tc.get_calendar(GetCalendarRequest(start=now.date() - timedelta(days=12),
                                                           end=now.date() + timedelta(days=12))), "calendar")
    rows = []
    for c in cal:
        o = datetime.combine(c.date, c.open) if isinstance(c.open, time) else c.open
        cl = datetime.combine(c.date, c.close) if isinstance(c.close, time) else c.close
        o = o.replace(tzinfo=NY) if o.tzinfo is None else o.astimezone(NY)
        cl = cl.replace(tzinfo=NY) if cl.tzinfo is None else cl.astimezone(NY)
        rows.append((c.date, o, cl))
    done = [r for r in rows if r[2] + CLOSE_GRACE <= now]
    if not done:
        raise RuntimeError("no completed session in calendar window")
    sig = done[-1]
    nxt = [r for r in rows if r[0] > sig[0]][0]
    return sig[0], nxt[0], nxt[1], nxt[2]


def closes(data, signal_day: date) -> pd.Series:
    req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Day,
                           start=datetime.combine(signal_day - timedelta(days=HISTORY_DAYS), time(0), NY),
                           end=datetime.combine(signal_day + timedelta(days=1), time(0), NY),
                           feed=DataFeed.SIP, adjustment=Adjustment.ALL)
    df = retry(lambda: data.get_stock_bars(req).df, "daily bars").reset_index()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(NY).dt.date
    s = df.set_index("date")["close"].astype(float).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if s.empty or s.index[-1] != signal_day:
        raise RuntimeError(f"daily bar for {signal_day} not available yet (last {s.index[-1] if len(s) else None})")
    jump = (s / s.shift(1) - 1).abs().max()
    if jump > 0.25:   # SPY has never moved 25% in a day: bad data
        raise RuntimeError(f"price history failed sanity check (max daily move {jump:.0%})")
    return s


def spy_qty(tc) -> float:
    pos = retry(tc.get_all_positions, "positions")
    others = [p.symbol for p in pos if p.symbol != SYMBOL]
    if others:
        notify.event("UNEXPECTED_POSITIONS", f"RSI-2 account holds {others} — left untouched", alert=True,
                     color=notify.AMBER)
    q = sum(float(p.qty) for p in pos if p.symbol == SYMBOL)
    if q < 0:
        raise RuntimeError(f"short SPY position {q} — this bot never shorts; investigate")
    return q


def existing_order(tc, tag: str):
    orders = retry(lambda: tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[SYMBOL],
                                                          after=datetime.now(NY) - timedelta(days=5),
                                                          limit=100)), "orders")
    return next((o for o in orders if (o.client_order_id or "").startswith(tag)), None)


def last_rsi2_order(tc):
    orders = retry(lambda: tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[SYMBOL],
                                                          after=datetime.now(NY) - timedelta(days=60),
                                                          limit=200)), "orders")
    mine = [o for o in orders if (o.client_order_id or "").startswith("rsi2-")]
    return max(mine, key=lambda o: o.submitted_at) if mine else None


def describe(x: dict, held: bool) -> str:
    return (f"SPY close ${x['close']:.2f} | RSI(2) {x['rsi2']:.1f} (buy <10) | SMA200 ${x['sma200']:.2f} "
            f"({'above' if x['close'] > x['sma200'] else 'BELOW'}) | SMA5 ${x['sma5']:.2f} | "
            f"now {'HOLDING' if held else 'flat'}")


def run(dry: bool = False, status: bool = False) -> int:
    now = datetime.now(NY)
    tc, data = clients()
    signal_day, next_day, next_open, next_close = sessions(tc, now)
    px = closes(data, signal_day)
    qty = spy_qty(tc)
    held = qty > 0
    hold_next, x = R.decide(px, held)
    info = describe(x, held)
    log.info("signal %s → next %s | %s | hold_next=%s", signal_day, next_day, info, hold_next)

    if status:
        acct = retry(tc.get_account, "account")
        last = last_rsi2_order(tc)
        lo = f"last order {last.client_order_id} ({last.status.value})" if last else "no orders yet"
        notify.discord("RSI-2 weekly status", f"{info}\nEquity ${float(acct.equity):,.2f} | {lo}", notify.GREY)
        return 0
    if hold_next == held:
        log.info("No change — nothing to do"); return 0

    side = OrderSide.BUY if hold_next else OrderSide.SELL
    tag = f"rsi2-{next_day:%Y%m%d}-{side.value}"
    if dry:
        print(f"DRY RUN: would {side.value} SPY for the {next_day} open | {info}"); return 0
    if os.getenv("RSI2_TRADING_ENABLED", "true").lower() == "false":
        notify.event("KILL_SWITCH", f"RSI2_TRADING_ENABLED=false — skipped {side.value} | {info}", alert=True,
                     color=notify.AMBER)
        return 0
    prev = existing_order(tc, tag)
    if prev:
        st = prev.status.value
        if st in ("rejected", "canceled", "expired"):
            notify.event("ORDER_PROBLEM", f"{tag} is {st} — not resubmitting automatically | {info}", alert=True,
                         color=notify.RED)
        else:
            log.info("%s already submitted (%s) — exit", tag, st)
        return 0

    # timing: OPG only 19:00 → 09:28 ET; if the next session is already open we are late
    if next_open <= now < next_close:
        tif, late = TimeInForce.DAY, True
    elif (now.time() >= OPG_OPEN and (next_day - now.date()).days == 1) or \
            (now.date() == next_day and now.time() < OPG_CUTOFF):
        tif, late = TimeInForce.OPG, False      # evening before the next session, or its pre-open morning
    else:
        log.info("Not an OPG window (daytime / weekend / holiday gap) — a later run will submit"); return 0

    if side == OrderSide.BUY:
        acct = retry(tc.get_account, "account")
        cash = min(float(acct.equity) * INVEST_FRACTION, float(acct.buying_power))
        q = math.floor(cash / x["close"])
        if q <= 0:
            raise RuntimeError(f"not enough buying power for 1 share (bp ${float(acct.buying_power):,.2f})")
    else:
        q = int(qty) if float(qty).is_integer() else qty
    try:
        o = retry(lambda: tc.submit_order(MarketOrderRequest(symbol=SYMBOL, qty=q, side=side, time_in_force=tif,
                                                              client_order_id=tag)), f"{side.value} SPY")
    except APIError as e:
        if "client_order_id" in str(e).lower():
            log.info("duplicate client_order_id — another run already submitted"); return 0
        notify.event("ORDER_REJECTED", f"RSI-2 {side.value} SPY x{q}: {e}", alert=True, color=notify.RED)
        return 1
    when = "at market NOW (late run)" if late else f"at the {next_day:%a %d %b} open"
    title = f"RSI-2: {'BUY' if side == OrderSide.BUY else 'SELL'} {q} SPY {when}"
    notify.discord(title, f"{info}\nOrder {o.id} ({tag})", notify.ORANGE if late else notify.GREEN)
    notify.event("ORDER", f"{title} | {info}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run(dry="--dry-run" in sys.argv, status="--status" in sys.argv))
    except SystemExit:
        raise
    except Exception as e:
        log.exception("fatal")
        notify.event("FATAL", f"rsi2_bot: {e!r}", alert=True, color=notify.RED)
        sys.exit(1)
