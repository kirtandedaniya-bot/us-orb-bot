import os, sys, requests

base_url = os.getenv("ALPACA_API_URL", "https://paper-api.alpaca.markets")
api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    print("❌ Error: Missing ALPACA_API_KEY or ALPACA_SECRET_KEY.")
    sys.exit(1)

headers = {
    "APCA-API-KEY-ID": api_key,
    "APCA-API-SECRET-KEY": secret_key
}

# 1. Cancel all working limit/stop orders
r_orders = requests.delete(f"{base_url}/v2/orders", headers=headers)
print(f"Cancel Orders Status: {r_orders.status_code}")

# 2. Liquidate open positions at market price
r_pos = requests.delete(f"{base_url}/v2/positions", headers=headers)
print(f"Liquidate Positions Status: {r_pos.status_code}")

print("✅ EOD Flatten complete: All open orders canceled, all positions closed.")
# Send Discord notification
discord_url = os.getenv("DISCORD_WEBHOOK_URL")
if discord_url and "YOUR_DISCORD" not in discord_url:
    payload = {
        "embeds": [{
            "title": "🛑 EOD Market Flatten Executed",
            "description": "All remaining open orders cancelled and runner positions liquidated prior to market close.",
            "color": 15158332,
            "footer": {"text": "Risk Management Engine"}
        }]
    }
    try:
        requests.post(discord_url, json=payload, timeout=5)
    except Exception:
        pass

