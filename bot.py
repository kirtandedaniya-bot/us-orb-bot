import os
import sys
import time
import json
import urllib.request
import warnings
from datetime import datetime
import pytz
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

warnings.filterwarnings("ignore")

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
MAX_RISK_USD = 50.0
MAX_DAILY_LOSS_USD = -100.0
TARGET_MULT = 1.5

# High-liquidity universe to screen dynamically each morning
SCREENER_POOL = [
    "NVDA", "TSLA", "AMD", "AAPL", "MSFT", "META", "AMZN", "PLTR",
    "COIN", "SMCI", "GOOGL", "NFLX", "ARM", "AVGO", "INTC", "MU"
]

def send_alert(title, message, color=3066993):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "footer": {"text": "Cloud US ORB Engine"}
        }]
    }
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    try:
        urllib.request.urlopen(req)
    except Exception:
        pass

def calculate_vwap(df):
    try:
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
        cum_vol_price = (typical_price * df["Volume"]).cumsum()
        cum_volume = df["Volume"].cumsum()
        vwap_series = cum_vol_price / cum_volume
        return float(vwap_series.iloc[-1])
    except Exception:
        return None

def get_dynamic_watchlist(pool):
    """Screens universe for active pre-market gappers (1.5% - 8.0%)."""
    print("🔎 Running Dynamic Watchlist Screener (Pre-Market Gap & RVOL)...")
    active_candidates = []
    
    for sym in pool:
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="5d", interval="1d")
            if len(hist) < 2:
                continue
            prev_close = float(hist["Close"].iloc[-2])
            
            # Intraday data
            intra = ticker.history(period="1d", interval="5m", prepost=True)
            if intra.empty:
                continue
            current_price = float(intra["Close"].iloc[-1])
            gap_pct = ((current_price - prev_close) / prev_close) * 100
            
            # Filter: Gapping up between 1.5% and 8%
            if 1.5 <= gap_pct <= 8.0:
                active_candidates.append((sym, gap_pct))
                print(f"   • {sym}: Active Gap +{gap_pct:.2f}% ✅")
        except Exception:
            continue
            
    active_candidates.sort(key=lambda x: x[1], reverse=True)
    selected = [sym for sym, _ in active_candidates[:5]]
    
    # Fallback to liquid leaders if no stocks are gapping within bounds
    if not selected:
        print("   • No active gappers found within 1.5%-8.0%. Defaulting to core liquid basket.")
        selected = ["NVDA", "TSLA", "PLTR", "AMD"]
        
    print(f"📋 Final Dynamic Watchlist for Today: {selected}")
    return selected

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# 1. Circuit Breaker
print("🛡️ Checking daily account P&L circuit breaker...")
try:
    account = trading_client.get_account()
    equity_now = float(account.equity)
    equity_open = float(account.last_equity)
    daily_pl = equity_now - equity_open
    print(f"   • Today's Account P&L: ${daily_pl:+.2f} USD")
    
    if daily_pl <= MAX_DAILY_LOSS_USD:
        msg = f"Account daily loss threshold breached (${daily_pl:.2f} <= ${MAX_DAILY_LOSS_USD:.2f}). Trading halted for the day."
        print(f"🛑 CIRCUIT BREAKER TRIPPED: {msg}")
        send_alert("🚨 US Account Circuit Breaker Triggered", msg, color=15158332)
        sys.exit(0)
except Exception as e:
    print(f"⚠️ Circuit breaker check warning: {e}. Proceeding.")

# 2. Market Breadth Filter (QQQ)
print("🔍 Checking US Market Sentiment (QQQ)...")
try:
    qqq = yf.Ticker("QQQ").history(period="2d", interval="5m")
    if len(qqq) >= 2:
        qqq_open = qqq["Open"].iloc[0]
        qqq_last = qqq["Close"].iloc[-1]
        qqq_pct = ((qqq_last - qqq_open) / qqq_open) * 100
        if qqq_pct < -0.5:
            msg = f"Broad market weakness: QQQ down {qqq_pct:.2f}%. Suppressing long breakout entries."
            print(f"🛑 {msg}")
            send_alert("⚠️ US Market Filter Triggered", msg, color=15158332)
            sys.exit(0)
        print(f"   • QQQ Sentiment: Neutral/Bullish ({qqq_pct:+.2f}%) ✅")
except Exception as e:
    print(f"⚠️ Index check warning: {e}. Proceeding.")

# 3. Dynamic Watchlist Generation
WATCHLIST = get_dynamic_watchlist(SCREENER_POOL)

# 4. 15m Candle Breakout Scanner + VWAP
print("🔍 Scanning dynamic basket for 15m breakout & VWAP alignment...")
try:
    data = yf.download(WATCHLIST, period="2d", interval="5m", progress=False, group_by="ticker")
except Exception as e:
    print(f"❌ Failed to download scanner data: {e}")
    sys.exit(1)

candidates = []
for symbol in WATCHLIST:
    try:
        df = data[symbol].dropna() if len(WATCHLIST) > 1 else data.dropna()
        if len(df) < 4:
            continue
        orb_high = float(df["High"].iloc[:3].max())
        orb_low = float(df["Low"].iloc[:3].min())
        current_price = float(df["Close"].iloc[-1])
        vol_opening = float(df["Volume"].iloc[:3].sum())
        
        vwap_val = calculate_vwap(df)

        if current_price > orb_high:
            if vwap_val and current_price < vwap_val:
                print(f"   ⚠️ {symbol} broke ORB High (${orb_high:.2f}) but is BELOW VWAP (${vwap_val:.2f}). Dropped.")
                continue

            pct_gain = ((current_price - orb_low) / orb_low) * 100
            score = (vol_opening / 100000.0) * pct_gain
            candidates.append({
                "symbol": symbol,
                "current_price": round(current_price, 2),
                "orb_high": round(orb_high, 2),
                "orb_low": round(orb_low, 2),
                "vwap": round(vwap_val, 2) if vwap_val else "N/A",
                "pct": pct_gain,
                "score": score
            })
    except Exception:
        continue

if not candidates:
    print("⚠️ No valid breakouts meeting 15m + VWAP criteria. Skipping execution.")
    sys.exit(0)

candidates.sort(key=lambda x: x["score"], reverse=True)
best = candidates[0]
symbol = best["symbol"]
current_price = best["current_price"]
stop_price = round(best["orb_low"], 2)

risk_per_share = round(current_price - stop_price, 2)
if risk_per_share <= 0.20 or risk_per_share > (current_price * 0.04):
    risk_per_share = round(current_price * 0.02, 2)
    stop_price = round(current_price - risk_per_share, 2)

shares = int(MAX_RISK_USD // risk_per_share)
if shares < 1:
    shares = 1

target_price = round(current_price + (risk_per_share * TARGET_MULT), 2)
total_risk = round(shares * risk_per_share, 2)

# 5. Submit Bracket Order
req = MarketOrderRequest(
    symbol=symbol,
    qty=shares,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.GTC,
    order_class=OrderClass.BRACKET,
    take_profit=TakeProfitRequest(limit_price=target_price),
    stop_loss=StopLossRequest(stop_price=stop_price)
)

order = trading_client.submit_order(req)
print(f"✅ Submitted Native Alpaca Bracket for {shares} shares of {symbol}:")
print(f"   • Entry: ${current_price:.2f} | VWAP: ${best['vwap']} | SL: ${stop_price:.2f} | TP: ${target_price:.2f} | Risk: -${total_risk:.2f}")

alert_msg = f"**Ticker:** {symbol}\n**Shares:** {shares}\n**Fill:** ${current_price:.2f}\n**VWAP:** ${best['vwap']}\n**Stop Loss:** ${stop_price:.2f}\n**Take Profit:** ${target_price:.2f}\n**Max Risk:** -${total_risk:.2f} USD"
send_alert(f"🚀 Cloud US Trade Executed: {symbol}", alert_msg, color=3066993)

# 6. Trailing Stop to Breakeven (+1R)
print("🛡️ Tracking for Breakeven Adjustment (+1R)...")
be_target = round(current_price + risk_per_share, 2)
be_set = False

for _ in range(30):
    time.sleep(15)
    try:
        quote = yf.Ticker(symbol).history(period="1d", interval="1m")["Close"].iloc[-1]
        if quote >= be_target and not be_set:
            orders = trading_client.get_orders()
            sl_order = next((o for o in orders if o.symbol == symbol and o.side.name == "SELL" and o.order_type.name == "STOP"), None)
            if sl_order:
                trading_client.replace_order_by_id(sl_order.id, ReplaceOrderRequest(stop_price=current_price))
                print(f"🔒 Breakeven Triggered: Stop moved to ${current_price:.2f}.")
                send_alert(f"🛡️ {symbol} Breakeven Activated", f"Price hit ${quote:.2f} (+1R).\nStop Loss raised to ${current_price:.2f}.", color=15844367)
                be_set = True
                break
    except Exception:
        pass

print("🏁 Session complete.")
