import os
import sys
import time
import json
import urllib.request
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import warnings
from datetime import datetime
import pytz
import numpy as np
import openpyxl

warnings.filterwarnings("ignore")

# --- UPGRADED MODULAR IMPORTS ---
from market_regime import check_market_regime
from volume_filter import check_rvol_and_spread
from split_bracket import build_two_tranche_brackets
from be_manager import BreakEvenManager

PORT = 7497
CLIENT_ID = 20
MAX_RISK_AUD = 50.0
MAX_DAILY_LOSS_AUD = -100.0
TARGET_MULT = 1.5
DISCORD_WEBHOOK_URL = "REDACTED"
EXCEL_PATH = os.path.expanduser("~/Desktop/Trading Journal.xlsx")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

RBA_BLACKOUT_DATES = {
    "2026-02-03": "RBA Rate Decision",
    "2026-03-17": "RBA Rate Decision",
    "2026-05-05": "RBA Rate Decision",
    "2026-06-16": "RBA Rate Decision",
    "2026-08-04": "RBA Rate Decision",
}

def send_alert(title, message, color=3066993):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "footer": {"text": "ASX ORB Engine • Synchronized"}
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

def check_rba_blackout():
    sydney = pytz.timezone("Australia/Sydney")
    today_syd = datetime.now(sydney).strftime("%Y-%m-%d")
    if today_syd in RBA_BLACKOUT_DATES:
        event = RBA_BLACKOUT_DATES[today_syd]
        msg = f"Trading suppressed: Today is an RBA event ({event}). Capital preserved."
        print(f"🛑 RBA MACRO BLACKOUT: {msg}")
        send_alert("⚠️ ASX RBA Event Blackout Activated", msg, color=15844367)
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
        for item in yf_news[:5]:
            t = item.get("title")
            if t:
                headlines.append(f"[Yahoo ASX] {t.strip()}")
    except Exception:
        pass

    clean_sym = symbol.replace(".AX", "")
    if FINNHUB_API_KEY:
        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
            url = f'https://finnhub.io/api/v1/company-news?symbol={clean_sym}&from={today_str}&to={today_str}&token={FINNHUB_API_KEY}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as resp:
                fh_items = json.loads(resp.read().decode('utf-8'))
                for item in fh_items[:3]:
                    headline = item.get('headline')
                    if headline:
                        headlines.append(f'[Finnhub Wire] {headline.strip()}')
        except Exception:
            pass
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
        return True, 7, "GEMINI_API_KEY not set locally (Bypassed)."
    print(f"🔮 Querying Gemini AI across multi-source feeds for {symbol}...")
    
    headlines = fetch_multi_source_headlines(symbol)
    if not headlines:
        return True, 6, "No recent news headlines found."

    prompt = (
        f"Analyze these recent market headlines for {symbol} to decide if a LONG breakout trade should proceed:\n"
        f"{json.dumps(headlines)}\n\n"
        "Rules:\n"
        "- Score 1 to 10 (1 = adverse/dilution/scandal, 10 = massive positive catalyst).\n"
        "- Provide a 10-word summary.\n"
        "- Respond ONLY with raw JSON: {\"score\": 8, \"reason\": \"Strong sector tailwinds and positive momentum.\"}"
    )

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash-lite-tts:generateContent?key={GEMINI_API_KEY}"
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
            parsed = json.loads(res_data["candidates"][0]["content"]["parts"][0]["text"].strip())
            score = int(parsed.get("score", 5))
            reason = str(parsed.get("reason", "Neutral news sentiment."))
            return (score >= 6), score, reason
    except Exception as e:
        return True, 5, f"Check bypassed due to exception: {e}"

# Time gate
    awst = pytz.timezone("Australia/Perth")
    now_awst = datetime.now(awst)
    current_minutes = now_awst.hour * 60 + now_awst.minute

    if not ((7 * 60 + 10) <= current_minutes <= (8 * 60 + 30)):
        print("\n🛑 TIME GATE HALT: ASX Market Window is CLOSED.")
        print(f"   • Current AWST Time : {now_awst.strftime('%I:%M:%S %p AWST')}")
        print(f"   • Allowed ASX Window: 07:10 AM - 08:30 AM AWST")
        sys.exit(0)

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


    # 1. Circuit Breaker
    ib = IB()
    try:
        ib.connect("127.0.0.1", PORT, clientId=CLIENT_ID, timeout=5)
        ib.reqMarketDataType(3)
        account_values = ib.accountValues()
        realized_pnl = 0.0
        for v in account_values:
            if v.tag == "RealizedPnL" and v.currency == "AUD":
                realized_pnl = float(v.value)
                break
        print(f"🛡️ Checking ASX Circuit Breaker: Realized P&L = ${realized_pnl:+.2f} AUD")
        if realized_pnl <= MAX_DAILY_LOSS_AUD:
            msg = f"ASX daily realized loss limit hit (${realized_pnl:.2f} <= ${MAX_DAILY_LOSS_AUD:.2f}). Trading halted."
            print(f"🛑 CIRCUIT BREAKER TRIPPED: {msg}")
            send_alert("🚨 ASX Account Circuit Breaker Triggered", msg, color=15158332)
            ib.disconnect()
            sys.exit(0)
    except Exception as e:
        print(f"⚠️ IBKR account check warning: {e}. Proceeding.")

    # 2. Market Breadth Filter (^AXJO)
    print("\n🔍 Checking ASX Broad Market Sentiment (^AXJO)...")
    try:
        asx200 = yf.Ticker("^AXJO").history(period="2d", interval="5m")
        if len(asx200) >= 2:
            asx_pct = ((asx200["Close"].iloc[-1] - asx200["Open"].iloc[0]) / asx200["Open"].iloc[0]) * 100
            if asx_pct < -0.4:
                msg = f"ASX 200 down {asx_pct:.2f}%. Suppressing long breakout entries."
                print(f"🛑 {msg}")
                send_alert("⚠️ ASX Market Filter Triggered", msg, color=15158332)
                if ib.isConnected():
                    ib.disconnect()
                sys.exit(0)
            print(f"   • ASX 200 Sentiment: Bullish/Neutral ({asx_pct:+.2f}%) ✅")
    except Exception as e:
        print(f"⚠️ Index check warning: {e}. Proceeding.")

    # 3. Market-Wide ASX Dynamic Screener
    print(f"🔍 Dynamically screening {len(EXPANDED_ASX_UNIVERSE)} ASX leaders for pre-market momentum...")
    active_asx = []
    try:
        daily_data = yf.download(EXPANDED_ASX_UNIVERSE, period="5d", interval="1d", progress=False)
        for ticker in EXPANDED_ASX_UNIVERSE:
            try:
                closes = daily_data["Close"][ticker].dropna()
                if len(closes) >= 2:
                    gap = ((closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2]) * 100
                    if abs(gap) >= 1.0:
                        active_asx.append(ticker)
            except Exception:
                continue
    except Exception:
        pass

    WATCHLIST = active_asx[:8] if len(active_asx) >= 3 else EXPANDED_ASX_UNIVERSE[:8]
    print(f"📋 Selected ASX Dynamic Watchlist: {WATCHLIST}")

    # 4. 15m Breakout + VWAP & ATR

    print("🔍 Scanning dynamic basket with 15m Confirmation, VWAP & ATR...")
    try:
        # Fetch Watchlist plus ASX 200 benchmark (^AXJO) for RS scoring
        dl_tickers = list(set(WATCHLIST + ["^AXJO"]))
        data = yf.download(dl_tickers, period="5d", interval="5m", progress=False, group_by="ticker")
    except Exception as e:
        print(f"❌ Failed to download ASX market data: {e}")
        if ib.isConnected():
            ib.disconnect()
        sys.exit(1)

    candidates = []
    for ticker in WATCHLIST:
        try:
            df = data[ticker].dropna() if len(WATCHLIST) > 1 else data.dropna()
            if len(df) < 15:
                continue
            orb_high = float(df["High"].iloc[-4:-1].max())
            orb_low = float(df["Low"].iloc[-4:-1].min())
            current_price = float(df["Close"].iloc[-1])
            vol_opening = float(df["Volume"].iloc[-4:-1].sum())
        
            vwap_val = calculate_vwap(df)
            atr_val = calculate_atr(df, period=14)

            if current_price > orb_high and current_price >= 1.0:
                if vwap_val and current_price < vwap_val:
                    print(f"   ⚠️ {ticker} above ORB High but BELOW VWAP (${vwap_val:.2f}). Dropped.")
                    continue

                # --- RELATIVE STRENGTH ENGINE ---
            index_pct = 0.0
            try:
                axjo_df = data["^AXJO"].dropna() if "^AXJO" in data else None
                if axjo_df is not None and len(axjo_df) >= 2:
                    index_open = float(axjo_df["Open"].iloc[-4])  # 15m open
                    index_curr = float(axjo_df["Close"].iloc[-1])
                    index_pct = ((index_curr - index_open) / index_open) * 100
            except Exception:
                index_pct = 0.0

            stock_pct = ((current_price - orb_low) / orb_low) * 100
            rs_score = stock_pct - index_pct
            score = rs_score * (vol_opening / 10000.0)

            candidates.append({
                    "symbol": ticker.replace(".AX", ""),
                    "full_symbol": ticker,
                    "price": round(current_price, 2),
                    "orb_high": round(orb_high, 2),
                    "orb_low": round(orb_low, 2),
                    "vwap": round(vwap_val, 2) if vwap_val else "N/A",
                    "atr": round(atr_val, 2) if atr_val else round(current_price * 0.015, 2),
                    "pct": stock_pct,
                "rs_score": round(rs_score, 2),
                    "score": score
                })
        except Exception:
            continue

if 'candidates' not in locals() and 'candidates' not in globals():
    candidates = []
if not candidates:
    print("⚠️ No ASX candidates met 15m breakout + VWAP criteria.")
    try:
        if "ib" in globals() and ib.isConnected():
            ib.disconnect()
    except Exception:
        pass
    print("Scan complete: No breakout setups active right now.")
    os._exit(0)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]
    symbol = best["symbol"]
    full_symbol = best["full_symbol"]
    entry_price = best["price"]
    atr = best["atr"]

    # 5. Multi-Source AI Validation Gate
    approved, ai_score, ai_reason = analyze_sentiment_with_ai(full_symbol)
    if not approved:
        msg = f"ASX trade for {symbol}.AX suppressed by Multi-Source AI Gate (Score: {ai_score}/10).\nReason: {ai_reason}"
        print(f"🛑 {msg}")
        send_alert(f"🛑 AI Suppressed Setup: {symbol}.AX", msg, color=15158332)
        if ib.isConnected():
            ib.disconnect()
        sys.exit(0)

    # ATR Dynamic Volatility Sizing
    candle_risk = round(entry_price - best["orb_low"], 2)
    atr_risk = round(1.5 * atr, 2)
    risk_per_share = round(max(candle_risk, atr_risk), 2)

    min_bound = round(entry_price * 0.005, 2)
    max_bound = round(entry_price * 0.035, 2)
    risk_per_share = max(min_bound, min(risk_per_share, max_bound))

    stop_price = round(entry_price - risk_per_share, 2)
    shares = int(MAX_RISK_AUD // risk_per_share)
    if shares < 1:
        shares = 1

side = best.get("side", "BUY")

if side == "BUY":
    stop_trigger_price = round(best["orb_high"] + 0.01, 2)
    limit_ceiling_price = round(stop_trigger_price + max(0.05, risk_per_share * 0.30), 2)
    stop_price = round(entry_price - risk_per_share, 2)
    target_1r = round(entry_price + risk_per_share, 2)
    target_2r = round(entry_price + (risk_per_share * 2.0), 2)
    order_action = "BUY"
else:
    stop_trigger_price = round(best["orb_low"] - 0.01, 2)
    limit_ceiling_price = round(stop_trigger_price - max(0.02, risk_per_share * 0.10), 2)
    stop_price = round(entry_price + risk_per_share, 2)
    target_1r = round(entry_price - risk_per_share, 2)
    target_2r = round(entry_price - (risk_per_share * 2.0), 2)
    order_action = "SELL"

total_risk = round(shares * risk_per_share, 2)

# --- PRE-TRADE INSTITUTIONAL FILTERS ---
regime_ok, regime_msg = check_market_regime("ASX")
if not regime_ok:
    print(f"⛔ Market Regime Blocked: {regime_msg}")
    send_alert(f"⚠️ ASX Trade Blocked: {symbol}", f"Regime filter blocked breakout: {regime_msg}", color=15158332)
    sys.exit(0)

vol_ok, rvol_val, spread_val, vol_msg = check_rvol_and_spread(symbol, "ASX")
if not vol_ok:
    print(f"⛔ Liquidity/Volume Blocked: {vol_msg}")
    send_alert(f"⚠️ ASX Trade Blocked: {symbol}", f"Liquidity filter failed: {vol_msg}", color=15158332)
    sys.exit(0)

if not ib.isConnected():
    try:
        ib.connect("127.0.0.1", PORT, clientId=CLIENT_ID, timeout=5)
    except Exception as e:
        print(f"❌ Could not reconnect to TWS: {e}")
        sys.exit(1)

contract = Stock(symbol, "SMART", "AUD", primaryExchange="ASX")
ib.qualifyContracts(contract)

next_id = ib.client.getReqId()
parent_order, child_orders = build_two_tranche_brackets(
    parent_order_id=next_id,
    action=order_action,
    total_quantity=shares,
    entry_price=entry_price,
    stop_price=stop_price,
    target_1r=target_1r,
    target_2r=target_2r,
    market="ASX"
)

ib.placeOrder(contract, parent_order)
placed_children = [ib.placeOrder(contract, o) for o in child_orders]
ib.sleep(1)

be_mgr = BreakEvenManager(ib, "ASX")
tp1_order_id = child_orders[0].orderId
sl2_order = child_orders[2]
be_mgr.register_trade(symbol, entry_price, tp1_order_id, sl2_order)
ib.orderStatusEvent += be_mgr.on_order_status

print(f"Verified: Two-Tranche Atomic Bracket Active for {symbol}.AX")
print(f" • Entry: {entry_price:.2f} | Risk/sh: {risk_per_share:.2f} | Total Risk: -${total_risk:.2f} AUD")
print(f" • Leg 1 (50%): TP @ {target_1r:.2f} (+1.0R) | SL @ {stop_price:.2f}")
print(f" • Leg 2 (50% Runner): TP @ {target_2r:.2f} (+2.0R) | SL @ {stop_price:.2f} -> BE on Leg 1 fill")

alert_msg = (
    f"**Ticker:** {symbol}.AX\n"
    f"**Shares:** {shares} (Two 50% Tranches)\n"
    f"**Leg 1 Target (+1.0R):** ${target_1r:.2f}\n"
    f"**Leg 2 Target (+2.0R Runner):** ${target_2r:.2f}\n"
    f"**Stop Loss:** ${stop_price:.2f}\n"
    f"**Total Max Risk:** -${total_risk:.2f} AUD\n"
    f"**RVOL:** {rvol_val:.2f}x | **Spread:** {spread_val:.2f}%\n"
    f"**Regime:** {regime_msg}"
)
send_alert(f"🚀 ASX Split-Bracket Placed: {symbol}.AX", alert_msg, color=3066993)

try:
    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb.active
    target_row = next((r for r in range(2, 200) if not ws.cell(row=r, column=2).value), ws.max_row + 1)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.cell(row=target_row, column=2, value=now_str)
    ws.cell(row=target_row, column=3, value=f"{symbol}.AX")
    ws.cell(row=target_row, column=4, value="BUY (SPLIT-BRACKET)")
    ws.cell(row=target_row, column=5, value=shares)
    ws.cell(row=target_row, column=6, value=float(entry_price))
    ws.cell(row=target_row, column=7, value=float(stop_price))
    ws.cell(row=target_row, column=10, value="OPEN")
    ws.cell(row=target_row, column=13, value=f"TP1:{target_1r} | TP2:{target_2r}")
    wb.save(EXCEL_PATH)
except Exception as e:
    print(f"⚠️ Excel log warning: {e}")

# Auto-sync logger hook once positions are closed
