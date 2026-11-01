import os
import sys
import time
import json
import urllib.request
import warnings
from datetime import datetime
import pytz
import numpy as np
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopLimitOrderRequest, TakeProfitRequest, StopLossRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

warnings.filterwarnings("ignore")

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

MAX_RISK_USD = 50.0
MAX_DAILY_LOSS_USD = -100.0
TARGET_MULT = 1.5

SCREENER_POOL = [
    "NVDA", "TSLA", "AMD", "AAPL", "MSFT", "META", "AMZN", "PLTR",
    "COIN", "SMCI", "GOOGL", "NFLX", "ARM", "AVGO", "INTC", "MU"
]

# High-impact US macro event dates (FOMC Rate Decisions, CPI Releases, NFP)
MACRO_BLACKOUT_DATES = {
    # 2026 FOMC Decision Days (2:00 PM ET)
    "2026-01-28": "FOMC Rate Decision",
    "2026-03-18": "FOMC Rate Decision & Projections",
    "2026-04-29": "FOMC Rate Decision",
    "2026-06-17": "FOMC Rate Decision & Projections",
    "2026-07-29": "FOMC Rate Decision",
    "2026-09-16": "FOMC Rate Decision & Projections",
    "2026-10-28": "FOMC Rate Decision",
    "2026-12-09": "FOMC Rate Decision & Projections",
    # 2026 Key CPI Release Days (8:30 AM ET)
    "2026-11-10": "US CPI Inflation Release",
    "2026-12-10": "US CPI Inflation Release",
    "2027-01-13": "US CPI Inflation Release"
}

def send_alert(title, message, color=3066993):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "footer": {"text": "Cloud US ORB Engine • Macro Risk Blackout"}
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

def check_macro_blackout():
    """Checks if today is a scheduled high-impact macro risk day."""
    eastern = pytz.timezone("US/Eastern")
    today_et = datetime.now(eastern).strftime("%Y-%m-%d")
    if today_et in MACRO_BLACKOUT_DATES:
        event_name = MACRO_BLACKOUT_DATES[today_et]
        msg = f"Trading suppressed: Today ({today_et}) is a scheduled high-impact macro event ({event_name}). Capital preserved."
        print(f"🛑 MACRO RISK BLACKOUT: {msg}")
        send_alert("⚠️ Macro Event Blackout Activated", msg, color=15844367)
        return True
    return False

def calculate_vwap(df):
    try:
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
        cum_vol_price = (typical_price * df["Volume"]).cumsum()
        cum_volume = df["Volume"].cumsum()
        vwap_series = cum_vol_price / cum_volume
        return float(vwap_series.iloc[-1])
    except Exception:
        return None

def calculate_atr(df, period=14):
    try:
        high = df["High"]
        low = df["Low"]
        close = df["Close"].shift(1)
        tr1 = high - low
        tr2 = (high - close).abs()
        tr3 = (low - close).abs()
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        atr = tr.rolling(window=period).mean()
        val = float(atr.iloc[-1])
        return val if not np.isnan(val) else None
    except Exception:
        return None

def analyze_sentiment_with_ai(symbol):
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY not set. Bypassing AI news filter.")
        return True, 7, "No Gemini API key provided (Bypassed)."

    print(f"🤖 Querying Gemini AI for catalyst sentiment on {symbol}...")
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        headlines = [n.get("title", "") for n in news[:5] if "title" in n]

        if not headlines:
            print("   • No breaking headlines found. Neutral clearance.")
            return True, 6, "No recent news headlines found."

        prompt = f"""You are an institutional momentum risk manager.
Analyze these recent market headlines for {symbol} to decide if a LONG breakout trade should proceed:
{json.dumps(headlines)}

Rules:
- Give a score from 1 to 10 (1 = extremely hostile/dilution/scandal, 5 = neutral, 10 = massive positive earnings beat/catalyst).
- Provide a concise 10-word justification.
- Respond ONLY with valid, raw JSON in this format:
{{"score": 8, "reason": "Strong earnings follow-through and analyst upgrades supporting upside momentum."}}"""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        req_body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"response_mime_type": "application/json"}
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(req_body).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            raw_text = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            parsed = json.loads(raw_text)
            
            score = int(parsed.get("score", 5))
            reason = str(parsed.get("reason", "No reason provided."))

            print(f"   • AI Score: {score}/10 | Analysis: {reason}")
            return (score >= 6), score, reason
    except Exception as e:
        print(f"⚠️ AI sentiment check error: {e}. Bypassing safely.")
        return True, 5, f"Check bypassed due to exception: {e}"

def get_dynamic_watchlist(pool):
    print("🔎 Running Dynamic Watchlist Screener (Pre-Market Gap & RVOL)...")
    active_candidates = []
    
    for sym in pool:
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="5d", interval="1d")
            if len(hist) < 2:
                continue
            prev_close = float(hist["Close"].iloc[-2])
            
            intra = ticker.history(period="1d", interval="5m", prepost=True)
            if intra.empty:
                continue
            current_price = float(intra["Close"].iloc[-1])
            gap_pct = ((current_price - prev_close) / prev_close) * 100
            
            if 1.5 <= gap_pct <= 8.0:
                active_candidates.append((sym, gap_pct))
                print(f"   • {sym}: Active Gap +{gap_pct:.2f}% ✅")
        except Exception:
            continue
            
    active_candidates.sort(key=lambda x: x[1], reverse=True)
    selected = [sym for sym, _ in active_candidates[:5]]
    
    if not selected:
        print("   • No active gappers found within 1.5%-8.0%. Defaulting to core liquid basket.")
        selected = ["NVDA", "TSLA", "PLTR", "AMD"]
        
    print(f"📋 Final Dynamic Watchlist: {selected}")
    return selected

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# ==============================================================
# UPGRADE 3: MACRO ECONOMIC BLACKOUT GATE
# ==============================================================
if check_macro_blackout():
    sys.exit(0)

# 1. Circuit Breaker
print("🛡️ Checking daily account P&L circuit breaker...")
try:
    account = trading_client.get_account()
    equity_now = float(account.equity)
    equity_open = float(account.last_equity)
    daily_pl = equity_now - equity_open
    print(f"   • Today's Account P&L: ${daily_pl:+.2f} USD")
    
    if daily_pl <= MAX_DAILY_LOSS_USD:
        msg = f"Account daily loss threshold breached (${daily_pl:.2f} <=${MAX_DAILY_LOSS_USD:.2f}). Trading halted for the day."
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

# 4. 15m Breakout Scanner + VWAP & ATR
print("🔍 Scanning dynamic basket for 15m breakout, VWAP & ATR calculation...")
try:
    data = yf.download(WATCHLIST, period="5d", interval="5m", progress=False, group_by="ticker")
except Exception as e:
    print(f"❌ Failed to download scanner data: {e}")
    sys.exit(1)

candidates = []
for symbol in WATCHLIST:
    try:
        df = data[symbol].dropna() if len(WATCHLIST) > 1 else data.dropna()
        if len(df) < 15:
            continue
        orb_high = float(df["High"].iloc[-4:-1].max())
        orb_low = float(df["Low"].iloc[-4:-1].min())
        current_price = float(df["Close"].iloc[-1])
        vol_opening = float(df["Volume"].iloc[-4:-1].sum())
        
        vwap_val = calculate_vwap(df)
        atr_val = calculate_atr(df, period=14)

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
                "atr": round(atr_val, 2) if atr_val else round(current_price * 0.015, 2),
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
atr = best["atr"]

# 5. Gemini AI Catalyst Sentiment Check
approved, ai_score, ai_reason = analyze_sentiment_with_ai(symbol)
if not approved:
    msg = f"Trade rejected by AI News Gate (Score: {ai_score}/10).\nReason: {ai_reason}"
    print(f"🛑 {msg}")
    send_alert(f"🛑 AI Suppressed Setup: {symbol}", msg, color=15158332)
    sys.exit(0)

# ATR Dynamic Volatility Sizing
candle_risk = round(current_price - best["orb_low"], 2)
atr_risk = round(1.5 * atr, 2)
risk_per_share = round(max(candle_risk, atr_risk), 2)

min_bound = round(current_price * 0.003, 2)
max_bound = round(current_price * 0.035, 2)
risk_per_share = max(min_bound, min(risk_per_share, max_bound))

stop_price = round(current_price - risk_per_share, 2)
shares = int(MAX_RISK_USD // risk_per_share)
if shares < 1:
    shares = 1

target_price = round(current_price + (risk_per_share * TARGET_MULT), 2)
total_risk = round(shares * risk_per_share, 2)

# Stop-Limit Bracket with Slippage Guard
stop_trigger_price = round(best["orb_high"] + 0.01, 2)
limit_ceiling_price = round(stop_trigger_price + max(0.05, risk_per_share * 0.10), 2)

req = StopLimitOrderRequest(
    symbol=symbol,
    qty=shares,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.GTC,
    order_class=OrderClass.BRACKET,
    stop_price=stop_trigger_price,
    limit_price=limit_ceiling_price,
    take_profit=TakeProfitRequest(limit_price=target_price),
    stop_loss=StopLossRequest(stop_price=stop_price)
)

order = trading_client.submit_order(req)
print(f"✅ Submitted Native Alpaca ATR-Volatility Bracket for {shares} shares of {symbol}:")
print(f"   • Entry: ${current_price:.2f} | ATR(14): ${atr:.2f} \vert{} Risk/sh:${risk_per_share:.2f}")
print(f"   • Stop Loss: ${stop_price:.2f} | Take Profit: ${target_price:.2f} \vert{} Max Risk: -${total_risk:.2f}")

alert_msg = (
    f"**Ticker:** {symbol}\n"
    f"**Shares:** {shares}\n"
    f"**Order Type:** Stop-Limit Bracket (ATR & Macro Guarded)\n"
    f"**Trigger Price:** ${stop_trigger_price:.2f}\n"
    f"**Max Limit Fill:** ${limit_ceiling_price:.2f}\n"
    f"**ATR (14):** ${atr:.2f}\n"
    f"**VWAP:** ${best['vwap']}\n"
    f"**Stop Loss:** ${stop_price:.2f}\n"
    f"**Take Profit:** ${target_price:.2f}\n"
    f"**Max Risk:** -${total_risk:.2f} USD\n\n"
    f"🧠 **AI Catalyst Analysis ({ai_score}/10):**\n*{ai_reason}*"
)
send_alert(f"🚀 Cloud US ATR Bracket Placed: {symbol}", alert_msg, color=3066993)

# Trailing Stop to Breakeven (+1R)
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
                send_alert(f"🛡️ {symbol} Breakeven Activated", f"Price hit ${quote:.2f} (+1R).\nStop Loss raised to${current_price:.2f}.", color=15844367)
                be_set = True
                break
    except Exception:
        pass

print("🏁 Session complete.")
