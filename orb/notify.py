"""Discord + Supabase side effects. Never raise — logging must not kill trading."""
import json
import logging
import os
import urllib.request

log = logging.getLogger("orb")
ENGINE = "US_ALPACA"
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")

GREEN, RED, AMBER, ORANGE, GREY = 3066993, 15158332, 15844367, 16753920, 9807270


# Simple-English explanation shown under every alert of that kind (owner asked for plain language).
PLAIN = {
    "FATAL": "The bot hit an error and stopped this run. No new trades were placed. Someone needs to look at it.",
    "KILL_SWITCH": "The bot is switched off on purpose, so it skipped trading.",
    "EARLY_CLOSE": "The US market closes early today, so the bot sits this day out.",
    "DRAWDOWN_BREAKER": "The account has lost more than the safety limit, so the bot paused itself. "
                        "No new trades until it's reviewed.",
    "CALENDAR_ERROR": "The bot couldn't check the big-news calendar. It's still trading normally.",
    "BLACKOUT": "Big US economic news today (inflation, jobs or the Fed). Prices jump around randomly on "
                "these days, so the bot skips trading.",
    "ORDER_REJECTED": "The broker refused an order, so that trade did NOT happen.",
    "ORDER_ERROR": "Something went wrong sending an order, so that trade did NOT happen.",
    "ENTRY_FIRED": "The price had already passed the entry level, so the bot jumped in straight away.",
    "LEFTOVER_POSITIONS": "A trade from an earlier day is still open, which shouldn't happen. Check Alpaca.",
    "UNEXPECTED_POSITIONS": "The account holds something this bot didn't buy. The bot leaves it alone - check Alpaca.",
    "NO_SETUPS": "None of the stocks gave a valid setup today, so no trades. That's normal.",
    "ENTRIES_EXPIRED": "Orders that never reached their entry price got cancelled. Nothing bought, nothing lost.",
    "EDGE_DEGRADED": "Live results are now clearly worse than the strategy should do. Time to think about "
                     "switching this bot off.",
    "FLATTEN_FAILED": "The bot tried to close everything before the market closed, but something is STILL open. "
                      "Close it in Alpaca!",
    "SYSTEM_FLATTEN": "The bot closed its open trades before the market closed, like it does every day.",
    "CRASH_WATCH": "The market or your ETF account dropped a lot. Don't panic - this is normal sometimes. "
                   "The bot only changes holdings on the first trading day of each month.",
    "GOOD_NEWS": "A good day! Nothing to do - the plan stays the same.",
    "ORDER_PROBLEM": "An earlier order didn't go through, so the bot is trying again.",
}


def _post(emb: dict) -> None:
    req = urllib.request.Request(WEBHOOK, data=json.dumps({"embeds": [emb]}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning("discord failed: %s", e)


def discord(title: str, msg: str, color: int = GREEN, plain: str | None = None) -> None:
    """Two messages: the normal (detailed) one, then — when there is one — a simple-English version."""
    if not WEBHOOK:
        return
    footer = {"text": {"US_ALPACA": "US ORB v2", "ETF_MOMENTUM": "ETF momentum", "RSI2_SPY": "RSI-2 SPY",
                       "REPORT": "Weekly report"}.get(ENGINE, ENGINE)}
    _post({"title": title, "description": msg[:4000], "color": color, "footer": footer})
    plain = plain or PLAIN.get(title)
    if plain:
        _post({"title": f"🗣️ In plain English — {title}"[:256], "description": plain[:4000], "color": color,
               "footer": footer})


# Serious events also ring the owner's phone (Telegram voice call via CallMeBot, free).
CALL_KINDS = {"FATAL", "DRAWDOWN_BREAKER", "EDGE_DEGRADED", "FLATTEN_FAILED", "LEFTOVER_POSITIONS",
              "UNEXPECTED_POSITIONS", "ORDER_PROBLEM", "CRASH_WATCH"}
BOT_NAME = {"US_ALPACA": "O R B bot", "ETF_MOMENTUM": "E T F bot", "RSI2_SPY": "R S I 2 bot"}


def phone_call(text: str, attempts: int = 2) -> bool:
    """Telegram voice call that reads `text` aloud (CallMeBot, free). No-op unless CALLMEBOT_USER is set.
    The free service sometimes answers FLOOD/busy: retry with back-off. Returns True if the call started."""
    user = os.getenv("CALLMEBOT_USER", "").strip()
    if not user:
        return False
    import re
    import time
    import urllib.parse
    q = urllib.parse.urlencode({"user": user, "text": text[:240], "lang": "en-GB-Standard-B"})
    for i in range(attempts):
        try:
            r = urllib.request.urlopen(urllib.request.Request(f"https://api.callmebot.com/start.php?{q}",
                                                              headers={"User-Agent": "Mozilla/5.0"}), timeout=45)
            body = " ".join(re.sub(r"<[^>]+>", " ", r.read().decode("utf-8", "replace")).split())
            up = body.upper()
            # "Line is busy (call queued)" is a success: CallMeBot rings a few seconds later
            busy = "QUEUED" not in up and any(w in up for w in ("FLOOD", "BUSY", "SKIPPED", "ERROR"))
            log.info("phone call attempt %d: %s", i + 1, body[body.find("Starting"):][:200] or body[:200])
            if not busy:
                return True
        except Exception as e:
            log.warning("phone call attempt %d failed: %s", i + 1, e)
        if i + 1 < attempts:
            time.sleep(20)
    log.warning("phone call gave up after %d attempts", attempts)
    return False


def event(kind: str, msg: str, alert: bool = False, color: int = GREY, plain: str | None = None) -> None:
    log.info("[%s] %s", kind, msg)
    try:
        import db_manager
        db_manager.log_system_event(ENGINE, kind, msg)
    except Exception as e:
        log.warning("db event failed: %s", e)
    if alert:
        discord(kind, msg, color, plain)
    if kind in CALL_KINDS:
        phone_call(f"Alert from your {BOT_NAME.get(ENGINE, 'trading bot')}. {kind.replace('_', ' ').lower()}. "
                   f"{msg}. Check Discord.")


def trade(**kw) -> None:
    try:
        import db_manager
        db_manager.log_trade(engine=ENGINE, **kw)
    except Exception as e:
        log.warning("db trade failed: %s", e)
