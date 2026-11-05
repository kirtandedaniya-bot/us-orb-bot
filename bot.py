import os
import sys
import time
import json
import urllib.request
import warnings
from datetime import datetime
import pytz
import openpyxl

warnings.filterwarnings("ignore")

# --- USER CONFIGURATION ---
API_KEY = os.getenv("ALPACA_API_KEY", "PKQNGYFU3ZIK30TDCTDC6UROV7")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "GUHA9f4JbcRR9Wrw5JWLe3q6QsxZTuR3EMmrKp9gVWw1")
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1566306841485377536/1ICmUjCfb8J2kGSobqtPacClaDe-k_PpCEHT5HYo-BaxohN-3ZtKkQ6g_CrEoTHvqb_q"
MAX_RISK_USD = 50.0
TARGET_MULT = 1.5
EXCEL_PATH = os.path.expanduser("~/Desktop/Trading Journal.xlsx")
WATCHLIST = ["NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AMD", "QQQ", "UBER"]

def send_alert(title, message, color=3066993):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD" in DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "footer": {"text": "US ORB Momentum Bot"}
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

# --- 1. TIME GATE ENFORCEMENT ---
awst = pytz.timezone("Australia/Perth")
now_awst = datetime.now(awst)
is_dst_active = (now_awst < datetime(2026, 11, 2, tzinfo=awst))
start_hour, start_min = (22, 15) if is_dst_active else (23, 15)
end_hour, end_min = (23, 45) if is_dst_active else (24, 45)

current_minutes = now_awst.hour * 60 + now_awst.minute
window_start = start_hour * 60 + start_min
window_end = end_hour * 60 + end_min

if not (window_start <= current_minutes <= window_end):
    print("\n🛑 TIME GATE HALT: US Market Window is CLOSED.")
    print(f"   • Current AWST Time : {now_awst.strftime('%I:%M:%S %p AWST')}")
    print(f"   • Allowed US Window : {start_hour:02d}:{start_min:02d} - {end_hour%24:02d}:{end_min:02d} AWST")
    sys.exit(0)

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest, ReplaceOrderRequest, TrailingStopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
import yfinance as yf

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# --- 2. MARKET BREADTH FILTER (QQQ) ---
print("\n🔍 Checking US Broad Market Sentiment (QQQ)...")
try:
    qqq = yf.Ticker("QQQ").history(period="2d", interval="5m")
    if len(qqq) >= 2:
        qqq_open = qqq["Open"].iloc[0]
        qqq_last = qqq["Close"].iloc[-1]
        qqq_pct = ((qqq_last - qqq_open) / qqq_open) * 100
        if qqq_pct < -0.5:
            msg = f"QQQ is selling off ({qqq_pct:.2f}%). Long breakouts suppressed to protect capital."
            print(f"🛑 MARKET FILTER HALT: {msg}")
            send_alert("⚠️ US Market Filter Triggered", msg, color=15158332)
            sys.exit(0)
        print(f"   • QQQ Sentiment: Bullish/Neutral ({qqq_pct:+.2f}%) ✅")
except Exception as e:
    print(f"⚠️ Index check warning: {e}. Proceeding.")

# --- 3. CANDLE BREAKOUT & RVOL SCANNER ---
# --- 2.5 DAILY CIRCUIT BREAKER ---
print("
🛡️ Checking Daily Circuit Breaker...")
try:
    today_est = datetime.now(pytz.timezone('US/Eastern')).date()
    closed_orders = trading_client.get_orders(status='closed', limit=50)
    today_stops = [o for o in closed_orders if o.filled_at and o.filled_at.date() == today_est and o.order_type.name in ('STOP', 'TRAILING_STOP')]
    if len(today_stops) >= 2:
        msg = "2 stop-outs hit today. Market is choppy. Halting to protect capital."
        print(f"🛑 CIRCUIT BREAKER: {msg}")
        send_alert("🛑 Circuit Breaker Tripped", msg, color=15158332)
        sys.exit(0)
    print("   • Circuit Breaker: Clear ✅")
except Exception as e:
    print(f"⚠️ Circuit breaker check warning: {e}")

print("🔍 Scanning liquid tech watchlist for opening range breakout...")
try:
    # Fetch Watchlist plus SPY benchmark for Relative Strength scoring
    dl_tickers = list(set(WATCHLIST + ["SPY"]))
    data = yf.download(dl_tickers, period="2d", interval="5m", progress=False, group_by="ticker")
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

        # --- ATR EXHAUSTION & DEAD RANGE FILTER ---
        orb_range = orb_high - orb_low
        try:
            daily_df = yf.Ticker(symbol).history(period="20d", interval="1d")
            if len(daily_df) >= 14:
                daily_atr = (daily_df["High"] - daily_df["Low"]).tail(14).mean()
                if not (0.25 * daily_atr <= orb_range <= 0.60 * daily_atr):
                    continue  # Range is too dead (<25%) or exhausted (>60%)
        except Exception:
            pass

        # --- CONFIRMED CANDLE CLOSE FILTER ---
        completed_candle_close = float(df["Close"].iloc[-2])
        if completed_candle_close > orb_high:
                # --- RELATIVE STRENGTH ENGINE (vs SPY) ---
            spy_pct = 0.0
            try:
                spy_df = data["SPY"].dropna() if "SPY" in data else None
                if spy_df is not None and len(spy_df) >= 3:
                    spy_open = float(spy_df["Open"].iloc[0])  # Day open
                    spy_curr = float(spy_df["Close"].iloc[-1])
                    spy_pct = ((spy_curr - spy_open) / spy_open) * 100
            except Exception:
                spy_pct = 0.0

            stock_pct = ((current_price - orb_low) / orb_low) * 100
            rs_score = stock_pct - spy_pct

            # Only select leaders displaying true Relative Strength over SPY
            if rs_score <= 0:
                continue

            score = rs_score * (vol_opening / 100000.0)
            candidates.append({
                "symbol": symbol,
                "current_price": round(current_price, 2),
                "orb_high": round(orb_high, 2),
                "orb_low": round(orb_low, 2),
                "pct": stock_pct,
            "rs_score": round(rs_score, 2),
                "score": score
            })
    except Exception:
        continue

if not candidates:
    print("⚠️ No valid candle breakouts confirmed above 15m high right now.")
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

# --- 4. SUBMIT TWO-TRANCHE SPLIT BRACKET (Target 1 + Trailing Runner) ---
q2 = max(1, shares // 2)
q1 = max(1, shares - q2)
trail_dollar_dist = round(abs(current_price - stop_price) * 2, 2)

# Tranche 1: Hard 1.0R Target Bracket
req_t1 = MarketOrderRequest(
    symbol=symbol,
    qty=q1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.GTC,
    order_class=OrderClass.BRACKET,
    take_profit=TakeProfitRequest(limit_price=target_price),
    stop_loss=StopLossRequest(stop_price=stop_price)
)
order1 = trading_client.submit_order(req_t1)

# Tranche 2: Uncapped Runner with Native 2x ATR Trailing Stop
req_t2 = TrailingStopOrderRequest(
    symbol=symbol,
    qty=q2,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.GTC,
    trail_price=trail_dollar_dist
)
order2 = trading_client.submit_order(req_t2)

print(f"[92m✔ Submitted 2-Tranche Order for {symbol}:[0m")
print(f"   • Leg 1 ({q1} shs): Target ${target_price:.2f} | SL: ${stop_price:.2f}")
print(f"   • Leg 2 ({q2} shs): Native Trailing Runner (Trail Dist: ${trail_dollar_dist:.2f})")

alert_msg = f"**Ticker:** {symbol}\n**Shares:** {shares}\n**Fill:** ${current_price}\n**Stop Loss:** ${stop_price}\n**Take Profit:** ${target_price}\n**Max Risk:** -${total_risk} USD"
send_alert(f"🚀 US Trade Executed: {symbol}", alert_msg, color=3066993)

wb = openpyxl.load_workbook(EXCEL_PATH)
ws = wb.active
target_row = next((r for r in range(2, 200) if not ws.cell(row=r, column=2).value), ws.max_row + 1)
now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

ws.cell(row=target_row, column=2, value=now_str)
ws.cell(row=target_row, column=3, value=symbol)
ws.cell(row=target_row, column=4, value="BUY")
ws.cell(row=target_row, column=5, value=shares)
ws.cell(row=target_row, column=6, value=float(current_price))
ws.cell(row=target_row, column=7, value=float(stop_price))
ws.cell(row=target_row, column=9, value=f'=IF(H{target_row}="", 0, (H{target_row}-F{target_row})*E{target_row})')
ws.cell(row=target_row, column=10, value=f'=IF(H{target_row}="", "OPEN", IF(I{target_row}>0, "WIN", IF(I{target_row}<0, "LOSS", "BE")))')
ws.cell(row=target_row, column=12, value=f"ORB 15m Breakout | +{best['pct']:.1f}%")
ws.cell(row=target_row, column=13, value=f"TP: ${target_price} | SL: ${stop_price} | Risk: -${total_risk} USD")
wb.save(EXCEL_PATH)

# --- 5. AUTOMATIC BREAKEVEN MONITOR ---
print("\n🛡️ Monitoring for Breakeven Adjustment (+1R)...")
breakeven_threshold = round(current_price + risk_per_share, 2)
be_set = False

for _ in range(30):
    time.sleep(15)
    try:
        last_quote = yf.Ticker(symbol).history(period="1d", interval="1m")["Close"].iloc[-1]
        if last_quote >= breakeven_threshold and not be_set:
            orders = trading_client.get_orders()
            sl_order = next((o for o in orders if o.symbol == symbol and o.side.name == "SELL" and o.order_type.name == "STOP"), None)
            if sl_order:
                trading_client.replace_order_by_id(sl_order.id, ReplaceOrderRequest(stop_price=current_price))
                print(f"🔒 Breakeven Triggered: Stop Loss updated to Entry (${current_price}). Position is risk-free!")
                send_alert(f"🛡️ {symbol} Moved to Breakeven", f"Price hit ${last_quote:.2f} (+1R). Stop Loss raised to ${current_price:.2f}.", color=15844367)
                be_set = True
                break
    except Exception:
        pass

print("🏁 Session complete.")
