"""US ORB v2 — live engine. Rules come from orb/strategy.py (same code the backtest runs).

Modes (each is idempotent and checks its own time window, so double cron runs are harmless):
  python bot.py trade    ~09:45 ET  wait to 10:15, place stop-entry brackets
  python bot.py cancel   ~11:31 ET  cancel unfilled entries
  python bot.py eod      ~15:35 ET  flatten everything, verify, recap
"""
import logging
import os
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, MarketOrderRequest, StopLossRequest,
                                     StopOrderRequest, TakeProfitRequest)
from dotenv import load_dotenv

load_dotenv()

from orb import notify  # noqa: E402
from orb.broker import Broker, retry  # noqa: E402
from orb.strategy import Params, Setup, build_setup  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orb")

NY = ZoneInfo("America/New_York")
UNIVERSE = os.getenv("UNIVERSE", "AAPL,MSFT,NVDA,QQQ").split(",")
P = Params()
CID = "orb"                       # client_order_id prefix: orb-YYYYMMDD-SYM
TRADE_WINDOW = (time(9, 25), time(10, 40))  # cron fires ~09:30 ET; GitHub delays tolerated
CANCEL_WINDOW = (time(11, 30), time(12, 29))
MAX_CHASE_R = 0.5                 # skip if price already > 0.5R past the trigger
OPEN_STATES = {OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW, OrderStatus.HELD}


def now_ny() -> datetime:
    return datetime.now(NY)


def cid(d: date, sym: str) -> str:
    return f"{CID}-{d:%Y%m%d}-{sym}"


def in_window(t: time, w: tuple[time, time]) -> bool:
    return w[0] <= t <= w[1]


def sleep_until(target: datetime) -> None:
    while (left := (target - now_ny()).total_seconds()) > 0:
        _time.sleep(min(left, 30))


def bracket(s: Setup, cid_: str, market: bool) -> MarketOrderRequest | StopOrderRequest:
    kw = dict(symbol=s.symbol, qty=s.shares, side=OrderSide.BUY if s.side == 1 else OrderSide.SELL,
              time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET, client_order_id=cid_,
              take_profit=TakeProfitRequest(limit_price=s.target), stop_loss=StopLossRequest(stop_price=s.stop))
    return MarketOrderRequest(**kw) if market else StopOrderRequest(stop_price=s.level, **kw)


def fmt(s: Setup) -> str:
    return (f"**{s.symbol}** {'LONG' if s.side == 1 else 'SHORT'} x{s.shares} | trigger ${s.level:.2f} | "
            f"stop ${s.stop:.2f} | target ${s.target:.2f} | risk ${s.risk_ps * s.shares:.0f} | ATR% {s.atr_pct:.0f}")


# ======================= TRADE =======================
def gates(b: Broker, now: datetime) -> bool:
    if os.getenv("TRADING_ENABLED", "true").lower() == "false":
        notify.event("KILL_SWITCH", "TRADING_ENABLED=false — no trades today", alert=True, color=notify.AMBER)
        return False
    sess = b.session(now.date())
    if sess is None:
        log.info("Market closed today"); return False
    if sess[1].time() != time(16, 0):
        notify.event("EARLY_CLOSE", f"Half day (close {sess[1]:%H:%M}) — skipping", alert=True, color=notify.AMBER)
        return False
    if not in_window(now.time(), TRADE_WINDOW):
        log.info("Outside trade window %s (now %s ET) — exit", TRADE_WINDOW, now.strftime("%H:%M")); return False
    try:
        import event_manager
        ev = event_manager.is_macro_blackout_active("US")
    except Exception as e:
        ev = None
        notify.event("CALENDAR_ERROR", f"{e} — not blocking", alert=True, color=notify.AMBER)
    if ev:
        notify.event("BLACKOUT", f"Tier-1 macro event: {ev}", alert=True, color=notify.AMBER)
        return False
    return True


def scan(b: Broker, today: date, symbols: list[str]) -> list[Setup]:
    daily = b.daily_bars(symbols, today)
    start, end = datetime.combine(today, P.orb_start, NY), datetime.combine(today, P.orb_end, NY)
    orb = b.orb_bars(symbols, start, end)
    out = []
    for sym in symbols:
        o, d = orb.get(sym), daily.get(sym)
        if o is None or d is None or o.empty:
            notify.event("DATA_MISSING", f"{sym}: no bars"); continue
        if o.index[-1] < end - timedelta(minutes=5):
            notify.event("DATA_STALE", f"{sym}: last ORB bar {o.index[-1]:%H:%M}"); continue
        s = build_setup(sym, o, d, P)
        log.info("%s -> %s", sym, fmt(s) if s else "no setup")
        if s:
            out.append(s)
    return out


def shortable(b: Broker, sym: str) -> bool:
    a = retry(lambda: b.trading.get_asset(sym), f"asset {sym}")
    return bool(a.shortable and a.easy_to_borrow)


def submit(b: Broker, s: Setup, cid_: str, market: bool) -> str:
    """Returns 'ok' | 'dup' | 'unsupported' | 'error'."""
    try:
        retry(lambda: b.trading.submit_order(bracket(s, cid_, market)), f"submit {s.symbol}")
        return "ok"
    except APIError as e:
        m = str(e).lower()
        if "client_order_id" in m and "unique" in m:
            return "dup"
        if not market and ("bracket" in m or "order type" in m or "not supported" in m):
            return "unsupported"
        notify.event("ORDER_REJECTED", f"{s.symbol}: {e}", alert=True, color=notify.RED)
        return "error"
    except Exception as e:
        notify.event("ORDER_ERROR", f"{s.symbol}: {e}", alert=True, color=notify.RED)
        return "error"


def watch_and_fire(b: Broker, pending: list[Setup], today: date) -> None:
    """Fallback if Alpaca rejects stop-entry brackets: poll and fire market brackets on the cross."""
    deadline = datetime.combine(today, P.entry_deadline, NY)
    notify.event("WATCH_MODE", f"Watching {[s.symbol for s in pending]} until {deadline:%H:%M} ET")
    while pending and now_ny() < deadline:
        try:
            px = b.last_price([s.symbol for s in pending])
        except Exception as e:
            log.warning("price poll failed: %s", e); _time.sleep(10); continue
        for s in list(pending):
            p = px.get(s.symbol)
            if p is None or not ((s.side == 1 and p >= s.level) or (s.side == -1 and p <= s.level)):
                continue
            pending.remove(s)
            inside = (s.stop < p < s.target) if s.side == 1 else (s.target < p < s.stop)
            if not inside or s.side * (p - s.level) > MAX_CHASE_R * s.risk_ps:
                notify.event("SKIP_CHASE", f"{s.symbol} crossed but price {p} too far / outside bracket")
                continue
            if submit(b, s, cid(today, s.symbol), market=True) == "ok":
                notify.event("ENTRY_FIRED", f"{s.symbol} market bracket @~{p}", alert=True)
        _time.sleep(5)
    if pending:
        notify.event("NO_TRIGGER", f"Not triggered by deadline: {[s.symbol for s in pending]}")


def mode_trade(b: Broker) -> int:
    now = now_ny()
    if not gates(b, now):
        return 0
    today = now.date()

    held = {p.symbol for p in retry(b.trading.get_all_positions, "positions")}
    if held:
        notify.event("LEFTOVER_POSITIONS", f"Open before entry: {held} — excluded. Check EOD job!",
                     alert=True, color=notify.RED)
    symbols = [s for s in UNIVERSE if s not in held]

    sleep_until(datetime.combine(today, time(10, 15, 5), NY))
    setups = scan(b, today, symbols)
    if not setups:
        notify.event("NO_SETUPS", "No symbols passed V1 filters", alert=True, color=notify.GREY)
        return 0

    px = b.last_price([s.symbol for s in setups])
    placed, watch = [], []
    for s in setups:
        p = px.get(s.symbol)
        if p is not None and s.side * (p - s.level) > MAX_CHASE_R * s.risk_ps:
            notify.event("SKIP_CHASE", f"{s.symbol} already {p} vs trigger {s.level}"); continue
        if s.side == -1 and not shortable(b, s.symbol):
            notify.event("NOT_SHORTABLE", s.symbol); continue
        r = submit(b, s, cid(today, s.symbol), market=False)
        if r == "ok":
            placed.append(s)
            notify.trade(symbol=s.symbol, direction="LONG" if s.side == 1 else "SHORT", regime=s.trend,
                         atr=round(s.atr_pct, 2), vwap=round(s.vwap, 4), entry=s.level, stop=s.stop,
                         target=s.target, shares=s.shares, risk=s.risk_ps,
                         total_risk=round(s.risk_ps * s.shares, 2), status="PENDING_ENTRY")
        elif r == "dup":
            log.info("%s already submitted today", s.symbol)
        elif r == "unsupported":
            watch.append(s)

    if placed:
        notify.discord("US ORB: entries armed", "\n".join(fmt(s) for s in placed), notify.GREEN)
    if watch:
        watch_and_fire(b, watch, today)
    return 0


# ======================= CANCEL =======================
def mode_cancel(b: Broker) -> int:
    now = now_ny()
    if not in_window(now.time(), CANCEL_WINDOW):
        log.info("Outside cancel window — exit"); return 0
    orders = retry(lambda: b.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)),
                   "open orders")
    n = 0
    for o in orders:
        if (o.client_order_id or "").startswith(f"{CID}-{now:%Y%m%d}") and o.status in OPEN_STATES \
                and float(o.filled_qty or 0) == 0:
            retry(lambda: b.trading.cancel_order_by_id(o.id), f"cancel {o.symbol}")
            n += 1
    notify.event("ENTRIES_EXPIRED", f"Cancelled {n} untriggered entries", alert=n > 0, color=notify.GREY)
    return 0


# ======================= EOD =======================
def flatten(b: Broker) -> list:
    for attempt in range(1, 4):
        retry(lambda: b.trading.close_all_positions(cancel_orders=True), "close_all")
        for _ in range(12):
            _time.sleep(5)
            left = retry(b.trading.get_all_positions, "positions")
            if not left:
                return []
        log.warning("flatten round %d: %d positions remain", attempt, len(left))
    return left


def recap(b: Broker, today: date) -> None:
    acct = retry(b.trading.get_account, "account")
    pnl = float(acct.equity) - float(acct.last_equity)
    after = datetime.combine(today, time(0, 0), NY)
    orders = retry(lambda: b.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, nested=True,
                                                                 after=after, limit=200)), "orders")
    lines = []
    for o in orders:
        if not (o.client_order_id or "").startswith(f"{CID}-{today:%Y%m%d}") or float(o.filled_qty or 0) == 0:
            continue
        entry, qty = float(o.filled_avg_price), float(o.filled_qty)
        side = 1 if o.side == OrderSide.BUY else -1
        leg = next((l for l in (o.legs or []) if float(l.filled_qty or 0) > 0), None)
        if leg:
            ex = float(leg.filled_avg_price)
            reason = "TARGET" if leg.type.value == "limit" else "STOP"
            tp = side * (ex - entry) * qty
            lines.append(f"{o.symbol}: {reason} {entry:.2f}->{ex:.2f} = ${tp:+.2f}")
            notify.trade(symbol=o.symbol, direction="LONG" if side == 1 else "SHORT", regime=0, atr=0, vwap=0,
                         entry=entry, stop=0, target=0, shares=int(qty), risk=0, total_risk=0,
                         status="CLOSED", pnl=round(tp, 2), exit_reason=reason)
        else:
            lines.append(f"{o.symbol}: filled {entry:.2f}, closed by EOD flatten")
    body = f"Day P&L: **${pnl:+.2f}** | Equity ${float(acct.equity):,.2f}\n" + ("\n".join(lines) or "No fills")
    notify.discord(f"US ORB recap {today}", body, notify.GREEN if pnl >= 0 else notify.RED)


def mode_eod(b: Broker) -> int:
    now = now_ny()
    sess = b.session(now.date())
    positions = retry(b.trading.get_all_positions, "positions")
    close = sess[1] if sess else datetime.combine(now.date(), time(16, 0), NY)
    flat_at = close - timedelta(minutes=10)
    # GitHub cron can start 40-60 min late, so the job is scheduled ~45 min early and waits here
    if now < flat_at - timedelta(minutes=75):
        log.info("Too early for EOD (close %s) — exit", close.strftime("%H:%M")); return 0
    if now < flat_at:
        log.info("Waiting until %s ET to flatten", flat_at.strftime("%H:%M"))
        sleep_until(flat_at)
        now = now_ny()
        positions = retry(b.trading.get_all_positions, "positions")
    late = now >= close  # after the bell: only act if something is still open (no duplicate recap)
    if late and not positions:
        log.info("Late duplicate run, nothing open — exit"); return 0

    left = flatten(b) if positions else []
    if left:
        notify.event("FLATTEN_FAILED", f"STILL OPEN: {[(p.symbol, p.qty) for p in left]} — CLOSE MANUALLY",
                     alert=True, color=notify.RED)
        return 2
    if positions:
        notify.event("SYSTEM_FLATTEN", f"Flattened {len(positions)} positions" + (" (AFTER CLOSE)" if late else ""),
                     alert=True, color=notify.RED if late else notify.ORANGE)
    if not late and sess:
        recap(b, now.date())
    return 0


MODES = {"trade": mode_trade, "cancel": mode_cancel, "eod": mode_eod}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in MODES:
        sys.exit(f"usage: python bot.py [{'|'.join(MODES)}]")
    try:
        sys.exit(MODES[mode](Broker()))
    except Exception as e:
        log.exception("fatal")
        notify.event("FATAL", f"{mode}: {e!r}", alert=True, color=notify.RED)
        sys.exit(1)
