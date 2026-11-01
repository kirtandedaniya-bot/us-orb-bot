import os
import sys
import json
import urllib.request
from datetime import datetime
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

def send_alert(title, fields, color=3066993):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": "US Market Close Recap • Cloud Bot"}
        }]
    }
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Failed to post to Discord: {e}")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

try:
    account = client.get_account()
    equity_now = float(account.equity)
    equity_open = float(account.last_equity)
    daily_pl = equity_now - equity_open
    daily_pct = (daily_pl / equity_open) * 100 if equity_open > 0 else 0.0

    req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=20)
    closed_orders = client.get_orders(req)
    positions = client.get_all_positions()

    color = 3066993 if daily_pl >= 0 else 15158332
    emoji = "🟢" if daily_pl >= 0 else "🔴"

    fields = [
        {"name": "Daily P&L", "value": f"{emoji} **${daily_pl:+.2f} USD** ({daily_pct:+.2f}%)", "inline": True},
        {"name": "Closing Equity", "value": f"${equity_now:,.2f}", "inline": True},
        {"name": "Buying Power", "value": f"${float(account.buying_power):,.2f}", "inline": True},
        {"name": "Orders Filled", "value": str(len(closed_orders)), "inline": True},
        {"name": "Open Positions", "value": str(len(positions)), "inline": True}
    ]

    awst_time = datetime.now(pytz.timezone("Australia/Perth")).strftime("%d %b %Y")
    send_alert(f"📊 US Session EOD Summary ({awst_time})", fields, color)
    print("✅ EOD recap sent to Discord.")
except Exception as e:
    print(f"❌ Error compiling recap: {e}")
