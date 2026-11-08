import os
import sys
import json
import urllib.request
from alpaca.trading.client import TradingClient
import db_manager

PAPER_API_KEY = os.getenv("ALPACA_API_KEY", "")
PAPER_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

def send_alert(title, message, color=3066993):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD" in DISCORD_WEBHOOK_URL: return
    payload = {"embeds": [{"title": title, "description": message, "color": color, "footer": {"text": "US EOD Watchdog"}}]}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try: urllib.request.urlopen(req)
    except Exception: pass

if not PAPER_API_KEY or not PAPER_SECRET_KEY:
    print("API Keys missing! Halting.")
    sys.exit(1)

trading_client = TradingClient(PAPER_API_KEY, PAPER_SECRET_KEY, paper=True)

print("Executing US EOD Flatten Protocol...")
try:
    # 1. Cancel all pending bracket orders
    trading_client.cancel_orders()
    
    # 2. Market-sell all open positions
    trading_client.close_all_positions(cancel_orders=True)
    
    msg = "EOD Protocol Executed: Canceled pending brackets and flattened open positions. No overnight risk."
    print(msg)
    
    send_alert("US Market Flattened", msg, color=16753920)
    db_manager.log_system_event("US_ALPACA", "SYSTEM_FLATTEN", msg)
    
except Exception as e:
    err_msg = f"CRITICAL: Failed to execute EOD flatten: {e}"
    print(err_msg)
    send_alert("US EOD FLATTEN ERROR", err_msg, color=15158332)
    db_manager.log_system_event("US_ALPACA", "ERROR", err_msg)
