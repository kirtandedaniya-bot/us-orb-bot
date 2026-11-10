"""Claude pre-trade analyst. Returns a structured verdict per setup.

AI_MODE=off     -> not called
AI_MODE=shadow  -> called and logged, NEVER blocks a trade (default: gather evidence first)
AI_MODE=gate    -> trade skipped if decision == "skip" or conviction < AI_MIN_CONVICTION
Any API/news failure -> verdict=None and the trade proceeds (fail-open), logged.
"""
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Optional

import requests

from orb.strategy import Setup

log = logging.getLogger("orb")

AI_MODE = os.getenv("AI_MODE", "shadow").lower()
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MIN_CONVICTION = int(os.getenv("AI_MIN_CONVICTION", "60"))
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
TIMEOUT_S = 30

SYSTEM = (
    "You are a risk reviewer for a mechanical opening-range-breakout (ORB) day-trading system. "
    "The setup has ALREADY passed every quantitative filter. Your only job: flag concrete, "
    "information-based reasons this specific trade should be skipped today (e.g. earnings or "
    "guidance today/after close, trading halt, M&A, FDA/legal ruling, major single-stock news "
    "that reverses the setup direction). Do not second-guess the technical rules. Absent a "
    "concrete reason, decide 'take'. Be terse."
)

VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record the pre-trade verdict.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["take", "skip"]},
            "conviction": {"type": "integer", "minimum": 0, "maximum": 100,
                           "description": "Confidence the trade should be taken."},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string", "description": "One sentence."},
        },
        "required": ["decision", "conviction", "risk_flags", "reason"],
    },
}


@dataclass
class Verdict:
    decision: str
    conviction: int
    risk_flags: list
    reason: str
    model: str

    @property
    def blocks(self) -> bool:
        return self.decision == "skip" or self.conviction < MIN_CONVICTION


def enabled() -> bool:
    return AI_MODE in ("shadow", "gate") and bool(os.getenv("ANTHROPIC_API_KEY"))


def headlines(symbol: str, today: date, n: int = 10) -> list[str]:
    if not FINNHUB_KEY:
        return []
    try:
        r = requests.get("https://finnhub.io/api/v1/company-news",
                         params={"symbol": symbol, "from": str(today - timedelta(days=2)), "to": str(today),
                                 "token": FINNHUB_KEY}, timeout=10)
        r.raise_for_status()
        items = sorted(r.json(), key=lambda x: x.get("datetime", 0), reverse=True)[:n]
        return [f"- {i.get('headline', '')} ({i.get('source', '')})" for i in items]
    except Exception as e:
        log.warning("news %s failed: %s", symbol, e)
        return []


def build_prompt(s: Setup, today: date, prev_close: Optional[float], news: list[str]) -> str:
    gap = f"{(s.level / prev_close - 1) * 100:+.2f}% vs prior close" if prev_close else "n/a"
    return (
        f"Date (New York): {today}\n"
        f"Symbol: {s.symbol}\n"
        f"Direction: {'LONG' if s.side == 1 else 'SHORT'} (20-day trend {'up' if s.trend == 1 else 'down'})\n"
        f"Entry trigger: {s.level:.2f} | Stop (ORB VWAP): {s.stop:.2f} | Target (1R): {s.target:.2f}\n"
        f"Opening range = {s.atr_pct:.0f}% of 14-day ATR | trigger {gap}\n"
        f"Recent headlines (last 48h, newest first):\n" + ("\n".join(news) or "- none found")
    )


def analyse(s: Setup, today: date, prev_close: Optional[float] = None) -> Optional[Verdict]:
    if not enabled():
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=2)
        msg = client.messages.create(
            model=MODEL, max_tokens=400, system=SYSTEM, tools=[VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "record_verdict"},
            messages=[{"role": "user", "content": build_prompt(s, today, prev_close, headlines(s.symbol, today))}],
        )
        block = next(b for b in msg.content if b.type == "tool_use")
        v = block.input
        return Verdict(v["decision"], int(v["conviction"]), list(v.get("risk_flags", [])),
                       str(v.get("reason", ""))[:300], MODEL)
    except Exception as e:
        log.warning("AI analyst failed for %s: %s", s.symbol, e)
        return None


def to_json(v: Optional[Verdict]) -> str:
    return json.dumps(asdict(v)) if v else "null"
