import os
import requests
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

TIER_1_KEYWORDS = ["Fed Interest Rate Decision", "Core CPI", "Nonfarm Payrolls", "RBA Interest Rate Decision", "Employment Change"]

def is_macro_blackout_active(market="US"):
    if not FINNHUB_API_KEY:
        print("Finnhub API key missing. Defaulting to NO BLACKOUT.")
        return False
        
    tz = pytz.timezone('America/New_York') if market == "US" else pytz.timezone('Australia/Sydney')
    today_str = datetime.now(tz).strftime('%Y-%m-%d')
    
    url = f"https://finnhub.io/api/v1/calendar/economic?from={today_str}&to={today_str}&token={FINNHUB_API_KEY}"
    
    try:
        response = requests.get(url, timeout=5)
        events = response.json().get("economicCalendar", [])
        target_country = "US" if market == "US" else "AU"
        
        for event in events:
            country = event.get("country", "")
            impact = event.get("impact", "")
            event_name = event.get("event", "")
            
            if country == target_country and impact == "high":
                for keyword in TIER_1_KEYWORDS:
                    if keyword.lower() in event_name.lower():
                        return event_name
                        
        return False
    except Exception as e:
        print(f"Calendar API Error: {e}")
        return False