import os
import sys
import time
import json
import urllib.request
import warnings
from datetime import datetime
import pytz
import openpyxl
import pandas as pd
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

warnings.filterwarnings("ignore")

# --- USER CONFIGURATION ---
API_KEY = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1566306841485377536/1ICmUjCfb8J2kGSobqtPacClaDe-k_PpCEHT5HYo-BaxohN-3ZtKkQ6g_CrEoTHvqb_q"
TARGET_MULT = 1.0
EXCEL_PATH = os.path.expanduser("~/Desktop/Trading Journal.xlsx")
WATCHLIST = ["AAPL", "MSFT", "NVDA", "QQQ"]

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
    sys.exit(0)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

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

# --- 2.5 DAILY CIRCUIT BREAKER ---
print("\n🛡️ Checking Daily Circuit Breaker...")
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

# --- 3. QUANTITATIVE ORB SCANNER (VWAP, SMA, ATR CLAMP) ---
print("\n🔍 Scanning mega-cap universe for quantitative ORB setups...")
NY_TZ = pytz.timezone('America/New_York')
candidates = []

for symbol in WATCHLIST:
    try:
        # Macro Trend (20 SMA)
        end_date = datetime.now(NY_TZ) - pd.Timedelta(days=1)
        start_date = end_date - pd.Timedelta(days=40)
        req_daily = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start_date, end=end_date, feed=DataFeed.IEX)
        daily_bars = data_client.get_stock_bars(req_daily).df
        if daily_bars.empty: continue
        daily_bars['sma_20'] = daily_bars['close'].rolling(20).mean()
        daily_data = daily_bars.iloc[-1]
        
        if daily_data['close'] <= daily_data['sma_20']:
            continue
            
        # Intraday ORB Data
        today = datetime.now(NY_TZ).date()
        start_time = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=30, tzinfo=NY_TZ)
        end_time = datetime.combine(today, datetime.min.time()).replace(hour=10, minute=15, tzinfo=NY_TZ)
        req_intra = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start_time, end=end_time, feed=DataFeed.IEX)
        intraday_bars = data_client.get_stock_bars(req_intra).df
        if intraday_bars.empty: continue
            
        intraday_bars['typical_price'] = (intraday_bars['high'] + intraday_bars['low'] + intraday_bars['close']) / 3
        intraday_bars['vp'] = intraday_bars['typical_price'] * intraday_bars['volume']
        
        vwap_1015 = intraday_bars['vp'].sum() / intraday_bars['volume'].sum()
        orb_high = intraday_bars['high'].max()
        orb_low = intraday_bars['low'].min()
        orb_range = orb_high - orb_low
        orb_close = intraday_bars['close'].iloc[-1]
        
        atr_14 = daily_data['close'] * 0.02 
        atr_pct = (orb_range / atr_14) * 100
        
        if not (25 <= atr_pct <= 60) or orb_close <= vwap_1015:
            continue
            
        # Dynamic Risk Sizing
        capital_at_risk = 150 if 30 <= atr_pct <= 45 else 50
        risk_per_share = orb_high - vwap_1015
        if risk_per_share <= 0: continue
        
        qty = max(1, int(capital_at_risk / risk_per_share))
        target_price = orb_high + (risk_per_share * TARGET_MULT)
        
        candidates.append({
            "symbol": symbol,
            "current_price": orb_high,
            "stop_price": vwap_1015,
            "target_price": target_price,
            "shares": qty,
            "risk_per_share": risk_per_share,
            "total_risk": capital_at_risk,
            "atr_pct": atr_pct
        })
    except Exception as e:
        pass

if not candidates:
    print("⚠️ No valid setups passed the ORB filters right now.")
    sys.exit(0)

# --- 4. EXECUTION & EXCEL LOGGING ---
wb = openpyxl.load_workbook(EXCEL_PATH)
ws = wb.active

for best in candidates:
    symbol = best["symbol"]
    current_price = round(best["current_price"], 2)
    stop_price = round(best["stop_price"], 2)
    target_price = round(best["target_price"], 2)
    shares = best["shares"]
    total_risk = round(best["total_risk"], 2)

    req = MarketOrderRequest(
        symbol=symbol,
        qty=shares,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=target_price),
        stop_loss=StopLossRequest(stop_price=stop_price)
    )
    trading_client.submit_order(req)

    print(f"\n[92m✔ Submitted 1.0R Bracket Order for {symbol}:[0m")
    print(f"   • Entry: ${current_price:.2f} | TP: ${target_price:.2f} | SL: ${stop_price:.2f}")

    alert_msg = f"**Ticker:** {symbol}\n**Shares:** {shares}\n**Fill:** ${current_price}\n**Stop Loss:** ${stop_price}\n**Take Profit:** ${target_price}\n**Max Risk:** -${total_risk} USD"
    send_alert(f"🚀 US Trade Executed: {symbol}", alert_msg, color=3066993)

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
    ws.cell(row=target_row, column=12, value=f"ORB 10:15 Breakout | ATR: {best['atr_pct']:.1f}%")
    ws.cell(row=target_row, column=13, value=f"TP: ${target_price} | SL: ${stop_price} | Risk: -${total_risk} USD")

wb.save(EXCEL_PATH)

# --- 5. AUTOMATIC BREAKEVEN MONITOR ---
print("\n🛡️ Monitoring active executions for Breakeven Adjustment (+1R)...")
be_status = {c['symbol']: False for c in candidates}

for _ in range(30):
    time.sleep(15)
    for best in candidates:
        symbol = best["symbol"]
        if be_status[symbol]: continue
        
        breakeven_threshold = round(best["current_price"] + best["risk_per_share"], 2)
        try:
            last_quote = yf.Ticker(symbol).history(period="1d", interval="1m")["Close"].iloc[-1]
            if last_quote >= breakeven_threshold:
                orders = trading_client.get_orders()
                sl_order = next((o for o in orders if o.symbol == symbol and o.side.name == "SELL" and o.order_type.name == "STOP"), None)
                if sl_order:
                    trading_client.replace_order_by_id(sl_order.id, ReplaceOrderRequest(stop_price=best["current_price"]))
                    print(f"🔒 Breakeven Triggered: {symbol} Stop Loss updated to Entry (${best['current_price']}).")
                    send_alert(f"🛡️ {symbol} Moved to Breakeven", f"Price hit ${last_quote:.2f} (+1R). Stop Loss raised to ${best['current_price']:.2f}.", color=15844367)
                    be_status[symbol] = True
        except Exception:
            pass

print("🏁 Session complete.")
