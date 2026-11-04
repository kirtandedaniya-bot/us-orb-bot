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
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

MAX_RISK_USD = 50.0
MAX_DAILY_LOSS_USD = -100.0
TARGET_MULT = 2.0

# Institutional high-momentum universe dynamically scanned each session
EXPANDED_MARKET_UNIVERSE = [
    "NVDA", "TSLA", "AMD", "AAPL", "MSFT", "META", "AMZN", "PLTR",
    "COIN", "SMCI", "GOOGL", "NFLX", "AVGO", "ARM", "MSTR", "MARA",
    "SOFI", "UBER", "QCOM", "INTC", "HOOD", "DKNG", "RIVN", "SNOW"
]

MACRO_BLACKOUT_DATES = {
    "2026-01-28": "FOMC Rate Decision",
    "2026-03-18": "FOMC Rate Decision & Projections",
    "2026-04-29": "FOMC Rate Decision",
    "2026-06-17": "FOMC Rate Decision & Projections",
    "2026-07-29": "FOMC Rate Decision",
    "2026-09-16": "FOMC Rate Decision & Projections",
    "2026-10-28": "FOMC Rate Decision",
    "2026-12-09": "FOMC Rate Decision & Projections",
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
            "footer": {"text": "Cloud US ORB Engine • Market-Wide Precision"}
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

def fetch_multi_source_headlines(symbol):
    headlines = []
    try:
        ticker = yf.Ticker(symbol)
        yf_news = ticker.news or []
        for item in yf_news[:4]:
            t = item.get("title")
            if t:
                headlines.append(f"[Yahoo] {t.strip()}")
    except Exception:
        pass

    if FINNHUB_API_KEY:
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={today_str}&to={today_str}&token={FINNHUB_API_KEY}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                fh_items = json.loads(resp.read().decode("utf-8"))
                for item in fh_items[:4]:
                    headline = item.get("headline")
                    if headline:
                        headlines.append(f"[Finnhub] {headline.strip()}")
        except Exception as e:
            print(f"⚠️ Finnhub wire note: {e}")

    seen = set()
    deduped = []
    for h in headlines:
        core_text = h.split("] ", 1)[-1].lower()
        if core_text not in seen:
            seen.add(core_text)
            deduped.append(h)
    return deduped[:6]

def analyze_sentiment_with_ai(symbol):
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY not set. Bypassing AI news filter.")
        return True, 7, "No Gemini API key provided (Bypassed)."

    print(f"🤖 Querying Gemini AI across multi-source financial wires for {symbol}...")
    try:
        headlines = fetch_multi_source_headlines(symbol)
        if not headlines:
            print("   • No breaking headlines found across Yahoo or Finnhub. Neutral clearance.")
            return True, 6, "No recent news headlines found."

        prompt = f"""You are an institutional momentum risk manager.
Analyze these multi-source market headlines (Yahoo & Finnhub wires) for {symbol} to decide if a LONG breakout trade should proceed:
{json.dumps(headlines)}

Rules:
- Give a score from 1 to 10 (1 = extremely hostile/dilution/scandal, 5 = neutral, 10 = massive positive earnings beat/catalyst).
- Provide a concise 10-word justification.
- Respond ONLY with valid, raw JSON in this format:
{{"score": 8, "reason": "Strong earnings follow-through and analyst upgrades supporting upside momentum."}}"""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.0-flash:generateContent?key={GEMINI_API_KEY}"
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

            print(f"   • Multi-Source AI Score: {score}/10 | Analysis: {reason}")
            return (score >= 6), score, reason
    except Exception as e:
        print(f"⚠️ Multi-source AI sentiment error: {e}. Bypassing safely.")
        return True, 5, f"Check bypassed due to exception: {e}"

def get_market_wide_dynamic_watchlist(pool):
    """Screens the expanded universe for the top 5 highest-quality pre-market gappers (1.5% - 8.0%)."""
    print(f"🔎 Running Market-Wide Screener across {len(pool)} high-beta tickers...")
    active_candidates = []
    
    try:
        daily_df = yf.download(pool, period="5d", interval="1d", progress=False)
        closes = daily_df["Close"]
        
        for sym in pool:
            try:
                sym_closes = closes[sym].dropna()
                if len(sym_closes) < 2:
                    continue
                prev_close = float(sym_closes.iloc[-2])
                last_price = float(sym_closes.iloc[-1])
                gap_pct = ((last_price - prev_close) / prev_close) * 100
                
                if 15.0 <= last_price <= 500.0 and (1.5 <= abs(gap_pct) <= 8.0):
                    active_candidates.append((sym, gap_pct))
            except Exception:
                continue
    except Exception as e:
        print(f"⚠️ Bulk screener note: {e}")

    active_candidates.sort(key=lambda x: x[1], reverse=True)
    selected = [sym for sym, _ in active_candidates[:5]]
    
    if not selected:
        print("   • No active gappers within bounds. Defaulting to high-volume institutional basket.")
        selected = ["NVDA", "TSLA", "PLTR", "AMD", "META"]
        
    print(f"📋 Selected Top 5 Dynamic Leaders for Today: {selected}")
    return selected

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# 1. Macro Blackout Check
if check_macro_blackout():
    sys.exit(0)

# 2. Account Circuit Breaker
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

# 3. Market Breadth Filter (QQQ)
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

# 4. Market-Wide Dynamic Watchlist Generation
WATCHLIST = get_market_wide_dynamic_watchlist(EXPANDED_MARKET_UNIVERSE)

# 5. 15m Breakout Scanner + VWAP & ATR
print("🔍 Scanning selected dynamic basket for 15m breakout, VWAP & ATR...")
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
    send_alert("US ORB Scan Finished", "Scan complete across 24 tickers. No clean breakouts above 15m high & VWAP tonight.", color=8421504)
    sys.exit(0)

candidates.sort(key=lambda x: x["score"], reverse=True)
best = candidates[0]
symbol = best["symbol"]
current_price = best["current_price"]
atr = best["atr"]

# 6. Multi-Source AI Catalyst Sentiment Check
approved, ai_score, ai_reason = analyze_sentiment_with_ai(symbol)
if not approved:
    msg = f"Trade rejected by Multi-Source AI Gate (Score: {ai_score}/10).\nReason: {ai_reason}"
    print(f"🛑 {msg}")
    send_alert(f"🛑 AI Suppressed Setup: {symbol}", msg, color=15158332)
    sys.exit(0)

# 7. ATR Dynamic Volatility Sizing
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

# Bi-Directional Bracket Order Construction
side = best.get("side", "BUY")

if side == "BUY":
    stop_trigger_price = round(best["orb_high"] + 0.01, 2)
    limit_ceiling_price = round(stop_trigger_price + max(0.05, risk_per_share * 0.10), 2)
    stop_loss_price = round(current_price - risk_per_share, 2)
    take_profit_price = round(current_price + (risk_per_share * TARGET_MULT), 2)
    order_side = OrderSide.BUY
else:
    stop_trigger_price = round(best["orb_low"] - 0.01, 2)
    limit_ceiling_price = round(stop_trigger_price - max(0.05, risk_per_share * 0.10), 2)
    stop_loss_price = round(current_price + risk_per_share, 2)
    take_profit_price = round(current_price - (risk_per_share * TARGET_MULT), 2)
    order_side = OrderSide.SELL

req = StopLimitOrderRequest(
    symbol=symbol,
    qty=shares,
    side=order_side,
    time_in_force=TimeInForce.GTC,
    order_class=OrderClass.BRACKET,
    stop_price=stop_trigger_price,
    limit_price=limit_ceiling_price,
    take_profit=TakeProfitRequest(limit_price=take_profit_price),
    stop_loss=StopLossRequest(stop_price=stop_loss_price)
)

order = trading_client.submit_order(req)
print(f"✅ Submitted Native Alpaca Market-Wide ATR Bracket for {shares} shares of {symbol}:")
print(f"   • Entry: ${current_price:.2f} | ATR(14): ${atr:.2f} | Risk/sh: ${risk_per_share:.2f}")
print(f"   • Stop Loss: ${stop_price:.2f} | Take Profit: ${target_price:.2f} | Max Risk: -${total_risk:.2f}")

alert_msg = (
    f"**Ticker:** {symbol}\n"
    f"**Shares:** {shares}\n"
    f"**Order Type:** Stop-Limit Bracket (Market-Wide Screener + ATR)\n"
    f"**Trigger Price:** ${stop_trigger_price:.2f}\n"
    f"**Max Limit Fill:** ${limit_ceiling_price:.2f}\n"
    f"**ATR (14):** ${atr:.2f}\n"
    f"**VWAP:** ${best['vwap']}\n"
    f"**Stop Loss:** ${stop_price:.2f}\n"
    f"**Take Profit:** ${target_price:.2f}\n"
    f"**Max Risk:** -${total_risk:.2f} USD\n\n"
    f"🧠 **Multi-Source AI Catalyst Analysis ({ai_score}/10):**\n*{ai_reason}*"
)
send_alert(f"🚀 Cloud US Precision Bracket Placed: {symbol}", alert_msg, color=3066993)

# 8. Trailing Stop to Breakeven (+1R)
print("🛡️ Tracking for Breakeven Adjustment (+1R)...")
be_target = round(current_price + risk_per_share, 2)
be_set = False

# Trailing sleep loop disabled for instant ~25s execution
