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
MAX_DAILY_LOSS_USD = -100.0  # Daily account circuit breaker
TARGET_MULT = 1.5
WATCHLIST = ["NVDA", "TSLA", "AMD", "AAPL", "MSFT", "META", "AMZN", "PLTR", "COIN", "SMCI"]

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

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# ==========================================
# UPGRADE 1: ACCOUNT CIRCUIT BREAKER
# ==========================================
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

# 1. Market Breadth Filter (QQQ)
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

# 2. Candle Confirmation Scanner (15m Opening Range)
print("🔍 Scanning tech basket for confirmed 15m breakout...")
try:
    data = yf.download(WATCHLIST, period="2d", interval="5m", progress=False, group_by="ticker")
except Exception as e:
    print(f"❌ Failed to download scanner data: {e}")
    sys.exit(1)

candidates = []
for symbol in WATCHLIST:
    try:
        df = data[symbol].dropna()
        if len(df) < 4:
            continue
        orb_high = float(df["High"].iloc[:3].max())
        orb_low = float(df["Low"].iloc[:3].min())
        current_price = float(df["Close"].iloc[-1])
        vol_opening = float(df["Volume"].iloc[:3].sum())

        if current_price > orb_high:
            pct_gain = ((current_price - orb_low) / orb_low) * 100
            score = (vol_opening / 100000.0) * pct_gain
            candidates.append({
                "symbol": symbol,
                "current_price": round(current_price, 2),
                "orb_high": round(orb_high, 2),
                "orb_low": round(orb_low, 2),
                "pct": pct_gain,
                "score": score
            })
    except Exception:
        continue

if not candidates:
    print("⚠️ No valid 15m candle breakouts confirmed. Skipping execution.")
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

# 3. Submit Native Broker Bracket
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
print(f"   • Entry: ${current_price:.2f} | SL: ${stop_price:.2f} | TP: ${target_price:.2f} | Risk: -${total_risk:.2f}")

alert_msg = f"**Ticker:** {symbol}\n**Shares:** {shares}\n**Fill:** ${current_price:.2f}\n**Stop Loss:** ${stop_price:.2f}\n**Take Profit:** ${target_price:.2f}\n**Max Risk:** -${total_risk:.2f} USD"
send_alert(f"🚀 Cloud US Trade Executed: {symbol}", alert_msg, color=3066993)

# 4. Trailing Stop to Breakeven (+1R)
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
