"""Live-vs-backtest early warning (Wald sequential probability ratio test on per-trade R).

H0: live trades average EXPECTED_R (what the backtest promised)
H1: live trades average 0R (edge gone)
After every trade the log-likelihood ratio is updated; crossing the upper bound = 'DEGRADED'
(evidence the edge is gone), crossing the lower bound = 'ON TRACK'. Usually decides in 30-150 trades,
much earlier than waiting for a drawdown limit.
"""
import math
import os
from datetime import datetime, timedelta, timezone

EXPECTED_R = float(os.getenv("EXPECTED_R", "0.10"))   # conservative: below the 4-symbol backtest
SIGMA_R = float(os.getenv("SIGMA_R", "1.2"))           # per-trade R std from backtests
ALPHA, BETA = 0.05, 0.20                               # false alarm 5%, miss 20%


def sprt(rs: list[float], mu0: float = EXPECTED_R, mu1: float = 0.0, sigma: float = SIGMA_R) -> dict:
    llr = sum(((x - mu0) ** 2 - (x - mu1) ** 2) / (2 * sigma ** 2) for x in rs)
    upper, lower = math.log((1 - BETA) / ALPHA), math.log(BETA / (1 - ALPHA))
    state = "DEGRADED" if llr >= upper else "ON TRACK" if llr <= lower else "INCONCLUSIVE"
    return {"n": len(rs), "avg_R": (sum(rs) / len(rs)) if rs else 0.0, "llr": llr,
            "upper": upper, "lower": lower, "state": state}


def live_r(trading, days: int = 365) -> list[float]:
    """Per-trade R of closed bot trades (client_order_id orb-*) from Alpaca, oldest first."""
    from alpaca.trading.enums import OrderSide, QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    after = datetime.now(timezone.utc) - timedelta(days=days)
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, nested=True, after=after, limit=500))
    filled = lambda o: float(o.filled_qty or 0) > 0  # noqa: E731
    others = [o for o in orders if not (o.client_order_id or "").startswith("orb-") and filled(o)]
    out = []
    for p in orders:
        if not (p.client_order_id or "").startswith("orb-") or not filled(p) or not p.stop_price:
            continue
        legs = p.legs or []
        sl = next((l for l in legs if l.type.value in ("stop", "stop_limit")), None)
        if sl is None or not sl.stop_price:
            continue
        risk = abs(float(p.stop_price) - float(sl.stop_price))
        hit = next((l for l in legs if filled(l)), None)
        if hit is not None:
            exit_px = float(hit.filled_avg_price)
        else:
            flat = next((o for o in others if o.symbol == p.symbol and o.side != p.side and o.filled_at
                         and o.filled_at > p.filled_at), None)
            if flat is None:
                continue
            exit_px = float(flat.filled_avg_price)
        side = 1 if p.side == OrderSide.BUY else -1
        if risk > 0:
            out.append((p.filled_at, side * (exit_px - float(p.filled_avg_price)) / risk))
    return [r for _, r in sorted(out)]


def summary_line(s: dict) -> str:
    icon = {"DEGRADED": "🔴", "ON TRACK": "🟢", "INCONCLUSIVE": "⚪"}[s["state"]]
    return (f"{icon} Edge check: {s['state']} — {s['n']} live trades, avg {s['avg_R']:+.2f}R "
            f"(evidence {s['llr']:+.2f}; alarm at {s['upper']:+.2f}, all-clear at {s['lower']:+.2f})")
