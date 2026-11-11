"""Tier-1 US macro blackout from a static official calendar.

Finnhub's economic-calendar endpoint is premium-only (403 on free keys), so dates are
hardcoded from bls.gov/schedule (CPI, Employment Situation) and federalreserve.gov (FOMC
decision day). Extend CALENDAR when BLS publishes the next year's schedule.
"""
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

CALENDAR = {
    # 2026 remaining (source: BLS 2026 schedule, FOMC 2026 calendar)
    "2026-12-04": "Nonfarm Payrolls (Nov)",
    "2026-12-09": "FOMC rate decision",
    "2026-12-10": "CPI (Nov)",
    # 2027 FOMC decision days (tentative Fed calendar); add CPI/NFP when BLS posts 2027
    "2027-01-27": "FOMC rate decision",
    "2027-03-17": "FOMC rate decision",
    "2027-04-28": "FOMC rate decision",
    "2027-06-09": "FOMC rate decision",
}
LAST_COVERED = date(2026, 12, 31)  # CPI/NFP coverage ends here


def is_macro_blackout_active(market: str = "US") -> Optional[str]:
    """Event name if today (New York) is a tier-1 day, else None. Raises when coverage expired."""
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if today > LAST_COVERED:
        raise RuntimeError("macro calendar out of date — add CPI/NFP dates to event_manager.CALENDAR")
    return CALENDAR.get(today.isoformat())
