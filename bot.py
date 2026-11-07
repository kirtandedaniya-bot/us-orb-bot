import os
import sys
import time
import json
import urllib.request
import warnings
import pandas as pd
from datetime import datetime
import pytz
import openpyxl
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

warnings.filterwarnings("ignore")

# 1. SECURE CONFIGURATION
PAPER_API_KEY = os.getenv("ALPACA_API_KEY", "")
PAPER_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TARGET_MULT = 1.0
EXCEL_PATH = "Trading Journal.xlsx"
WATCHLIST = ["AAPL", "MSFT", "NVDA", "QQQ"]
NY_TZ = pytz.timezone('America/New_York')

def send_alert(title, message, color=3066993):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD" in DISCORD_WEBHOOK_URL: return
    payload = {"embeds": [{"title": title, "description": message, "color": color, "footer": {"text": "Bi-Directional US Quant Engine"}}]}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try: urllib.request.urlopen(req)
    except Exception: pass

if not PAPER_API_KEY or not PAPER_SECRET_KEY:
    print("API Keys missing! Halting execution.")
    sys.exit(1)

trading_client = TradingClient(PAPER_API_KEY, PAPER_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(PAPER_API_KEY, PAPER_SECRET_KEY)

# 2. DAILY CIRCUIT BREAKER
print("\nChecking Daily Circuit Breaker...")
try:
    today_est = datetime.now(NY_TZ).date()
    closed_orders = trading_client.get_orders(status='closed', limit=50)
    today_stops = [o for o in closed_orders if o.filled_at and o.filled_at.date() == today_est and o.order_type.name in ('STOP', 'TRAILING_STOP')]
    if len(today_stops) >= 2:
        msg = "2 stop-outs hit today. Market is choppy. Halting to protect capital."
        print(f"CIRCUIT BREAKER: {msg}")
        send_alert("Circuit Breaker Tripped", msg, color=15158332)
        sys.exit(0)
    print("Circuit Breaker: Clear")
except Exception as e:
    print(f"Circuit breaker check warning: {e}")

# 3. BI-DIRECTIONAL QUANTITATIVE ORB SCANNER
print(f"\nScanning mega-cap universe for bi-directional ORB setups...")
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
        
        daily_close = daily_data['close']
        sma_20 = daily_data['sma_20']
        market_trend = 1 if daily_close > sma_20 else -1
        
        # Intraday ORB Data
        today = datetime.now(NY_TZ).date()
        start_time = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=30, tzinfo=NY_TZ)
        end_time = datetime.combine(today, datetime.min.time()).replace(hour=10, minute=15, tzinfo=NY_TZ)
        
        req_intra = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start_time, end=end_time, feed=DataFeed.IEX)
        intraday_bars = data_client.get_stock_bars(req_intra).df
        if intraday_bars.empty: continue
        
        intraday_bars['typical_price'] = (intraday_bars['high'] + intraday_bars['low'] + intraday_bars['close']) / 3.0
        intraday_bars['vp'] = intraday_bars['typical_price'] * intraday_bars['volume']
        vwap_1015 = intraday_bars['vp'].sum() / intraday_bars['volume'].sum()
        
        orb_high = intraday_bars['high'].max()
        orb_low = intraday_bars['low'].min()
        orb_range = orb_high - orb_low
        orb_close = intraday_bars['close'].iloc[-1]
        
        atr_14 = daily_close * 0.02
        atr_pct = (orb_range / atr_14) * 100
        
        if not (25 <= atr_pct <= 60):
            continue
            
        signal = None
        risk_per_share = 0.0
        entry_price = 0.0
        stop_price = 0.0
        target_price = 0.0
        
        # LONG LOGIC: Bull Market, Price > VWAP, Breaks ORB High
        if market_trend == 1 and orb_close > vwap_1015 and orb_close >= orb_high:
            signal = OrderSide.BUY
            risk_per_share = orb_high - vwap_1015
            if risk_per_share <= 0.02: continue
            entry_price = orb_high
            stop_price = vwap_1015
            target_price = entry_price + (risk_per_share * TARGET_MULT)
            
        # SHORT LOGIC: Bear Market, Price < VWAP, Breaks ORB Low
        elif market_trend == -1 and orb_close < vwap_1015 and orb_close <= orb_low:
            signal = OrderSide.SELL
            risk_per_share = vwap_1015 - orb_low
            if risk_per_share <= 0.02: continue
            entry_price = orb_low
            stop_price = vwap_1015
            target_price = entry_price - (risk_per_share * TARGET_MULT)
            
        if not signal: continue
        
        # Dynamic Kelly Risk Sizing
        capital_at_risk = 150.0 if 30 <= atr_pct <= 45 else 50.0
        qty = max(1, int(capital_at_risk / risk_per_share))
        
        candidates.append({
            "symbol": symbol,
            "signal": signal,
            "entry_price": round(entry_price, 2),
            "stop_price": round(stop_price, 2),
            "target_price": round(target_price, 2),
            "shares": qty,
            "risk_per_share": risk_per_share,
            "total_risk": capital_at_risk,
            "atr_pct": atr_pct
        })
    except Exception as e:
        print(f"Error processing {symbol}: {e}")
        pass

if not candidates:
    print("No valid setups passed the ORB filters right now.")
    send_alert("NO SETUPS", "All US symbols blocked by trend, ATR, or VWAP filters.", color=15548997)
    sys.exit(0)

# Sort by tightest ATR profile
candidates.sort(key=lambda x: abs(x["atr_pct"] - 37.5))

# 4. EXECUTION & EXCEL LOGGING
try:
    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb.active
except Exception:
    wb = None

for best in candidates:
    symbol = best["symbol"]
    signal = best["signal"]
    entry_price = best["entry_price"]
    stop_price = best["stop_price"]
    target_price = best["target_price"]
    shares = best["shares"]
    total_risk = round(best["total_risk"], 2)
    
    req = MarketOrderRequest(
        symbol=symbol,
        qty=shares,
        side=signal,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=target_price),
        stop_loss=StopLossRequest(stop_price=stop_price)
    )
    
    try:
        trading_client.submit_order(req)
        action_str = "LONG" if signal == OrderSide.BUY else "SHORT"
        print(f"\nSubmitted 1.0R {action_str} Bracket Order for {symbol}")
        print(f"Entry: ${entry_price:.2f} | TP: ${target_price:.2f} | SL: ${stop_price:.2f}")
        
        alert_msg = f"**Ticker:** {symbol}\n**Action:** {action_str} (Bi-Directional Active)\n**Shares:** {shares}\n**Fill:** ${entry_price:.2f}\n**Stop Loss (VWAP):** ${stop_price:.2f}\n**Take Profit (+1.0R):** ${target_price:.2f}\n**Max Risk:** ${total_risk} USD"
        send_alert(f"US Trade Executed: {symbol}", alert_msg, color=16711680 if signal == OrderSide.SELL else 3066993)
        
        if wb:
            target_row = next((r for r in range(2, 200) if not ws.cell(row=r, column=2).value), ws.max_row + 1)
            ws.cell(row=target_row, column=2, value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            ws.cell(row=target_row, column=3, value=symbol)
            ws.cell(row=target_row, column=4, value="BUY" if signal == OrderSide.BUY else "SELL")
            ws.cell(row=target_row, column=5, value=shares)
            ws.cell(row=target_row, column=6, value=float(entry_price))
            ws.cell(row=target_row, column=7, value=float(stop_price))
            
            # P&L tracking (inverted for short-selling)
            if signal == OrderSide.BUY:
                ws.cell(row=target_row, column=9, value=f'=IF(H{target_row}="", 0, (H{target_row}-F{target_row})*E{target_row})')
            else:
                ws.cell(row=target_row, column=9, value=f'=IF(H{target_row}="", 0, (F{target_row}-H{target_row})*E{target_row})')
                
            ws.cell(row=target_row, column=10, value=f'=IF(H{target_row}="", "OPEN", IF(I{target_row}>0, "WIN", IF(I{target_row}<0, "LOSS", "BE")))')
            ws.cell(row=target_row, column=12, value=f"ORB 10:15 Breakout | ATR: {best['atr_pct']:.1f}%")
            ws.cell(row=target_row, column=13, value=f"TP: ${target_price} | SL: ${stop_price} | Risk: ${total_risk}")
            wb.save(EXCEL_PATH)
            
    except Exception as e:
        print(f"Order Failed for {symbol}: {e}")

# 5. AUTOMATIC BREAKEVEN MONITOR
print("\nMonitoring active executions for Breakeven Adjustment (+1R)...")
be_status = {c['symbol']: False for c in candidates}
for _ in range(30):
    time.sleep(15)
    for best in candidates:
        symbol = best["symbol"]
        if be_status[symbol]: continue
        
        signal = best["signal"]
        risk_per_share = best["risk_per_share"]
        entry_price = best["entry_price"]
        
        if signal == OrderSide.BUY:
            breakeven_threshold = round(entry_price + risk_per_share, 2)
        else:
            breakeven_threshold = round(entry_price - risk_per_share, 2)
            
        try:
            # Using yfinance for real-time unthrottled quotes in paper trading
            last_quote = yf.Ticker(symbol).history(period="1d", interval="1m")["Close"].iloc[-1]
            
            trigger_be = False
            if signal == OrderSide.BUY and last_quote >= breakeven_threshold:
                trigger_be = True
            elif signal == OrderSide.SELL and last_quote <= breakeven_threshold:
                trigger_be = True
                
            if trigger_be:
                orders = trading_client.get_orders()
                sl_side = "SELL" if signal == OrderSide.BUY else "BUY"
                sl_order = next((o for o in orders if o.symbol == symbol and o.side.name == sl_side and o.order_type.name == "STOP"), None)
                if sl_order:
                    trading_client.replace_order_by_id(sl_order.id, ReplaceOrderRequest(stop_price=entry_price))
                    print(f"Breakeven Triggered: {symbol} Stop Loss updated to Entry (${entry_price}). Position is risk-free!")
                    send_alert(f"{symbol} Moved to Breakeven", f"Price hit target threshold. Stop Loss secured at ${entry_price:.2f}.", color=15844367)
                    be_status[symbol] = True
        except Exception:
            pass

print("Session complete.")
