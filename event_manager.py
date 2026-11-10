"""Tier-1 macro blackout check via Finnhub economic calendar."""
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

TIER_1_KEYWORDS = ["Fed Interest Rate Decision", "FOMC", "CPI", "Nonfarm Payrolls",
                   "RBA Interest Rate Decision", "Employment Change"]


def is_macro_blackout_active(market: str = "US") -> Optional[str]:
    """Returns event name if a high-impact tier-1 event is today, else None. Raises on API failure."""
    if not FINNHUB_API_KEY:
        return None
    tz = ZoneInfo("America/New_York" if market == "US" else "Australia/Sydney")
    today = datetime.now(tz).strftime("%Y-%m-%d")
    r = requests.get("https://finnhub.io/api/v1/calendar/economic",
                     params={"from": today, "to": today, "token": FINNHUB_API_KEY}, timeout=10)
    r.raise_for_status()
    country = "US" if market == "US" else "AU"
    for ev in r.json().get("economicCalendar", []):
        name = ev.get("event", "")
        if ev.get("country") == country and ev.get("impact") == "high" \
                and any(k.lower() in name.lower() for k in TIER_1_KEYWORDS):
            return name
    return None
