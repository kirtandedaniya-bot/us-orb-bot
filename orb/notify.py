"""Discord + Supabase side effects. Never raise — logging must not kill trading."""
import json
import logging
import os
import urllib.request

log = logging.getLogger("orb")
ENGINE = "US_ALPACA"
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")

GREEN, RED, AMBER, ORANGE, GREY = 3066993, 15158332, 15844367, 16753920, 9807270


def discord(title: str, msg: str, color: int = GREEN) -> None:
    if not WEBHOOK:
        return
    body = json.dumps({"embeds": [{"title": title, "description": msg[:4000], "color": color,
                                   "footer": {"text": {"US_ALPACA": "US ORB v2", "ETF_MOMENTUM": "ETF momentum", "RSI2_SPY": "RSI-2 SPY"}.get(ENGINE, ENGINE)}}]}).encode()
    req = urllib.request.Request(WEBHOOK, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning("discord failed: %s", e)


# Serious events also ring the owner's phone (Telegram voice call via CallMeBot, free).
CALL_KINDS = {"FATAL", "DRAWDOWN_BREAKER", "EDGE_DEGRADED", "FLATTEN_FAILED", "LEFTOVER_POSITIONS",
              "UNEXPECTED_POSITIONS", "ORDER_PROBLEM", "CRASH_WATCH"}
BOT_NAME = {"US_ALPACA": "O R B bot", "ETF_MOMENTUM": "E T F bot", "RSI2_SPY": "R S I 2 bot"}


def phone_call(text: str, attempts: int = 4) -> bool:
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
                                                              headers={"User-Agent": "Mozilla/5.0"}), timeout=90)
            body = " ".join(re.sub(r"<[^>]+>", " ", r.read().decode("utf-8", "replace")).split())
            busy = any(w in body.upper() for w in ("FLOOD", "BUSY", "SKIPPED", "ERROR"))
            log.info("phone call attempt %d: %s", i + 1, body[body.find("Starting"):][:200] or body[:200])
            if not busy:
                return True
        except Exception as e:
            log.warning("phone call attempt %d failed: %s", i + 1, e)
        time.sleep(60 * (i + 1))
    log.warning("phone call gave up after %d attempts", attempts)
    return False


def event(kind: str, msg: str, alert: bool = False, color: int = GREY) -> None:
    log.info("[%s] %s", kind, msg)
    try:
        import db_manager
        db_manager.log_system_event(ENGINE, kind, msg)
    except Exception as e:
        log.warning("db event failed: %s", e)
    if alert:
        discord(kind, msg, color)
    if kind in CALL_KINDS:
        phone_call(f"Alert from your {BOT_NAME.get(ENGINE, 'trading bot')}. {kind.replace('_', ' ').lower()}. "
                   f"{msg}. Check Discord.")


def trade(**kw) -> None:
    try:
        import db_manager
        db_manager.log_trade(engine=ENGINE, **kw)
    except Exception as e:
        log.warning("db trade failed: %s", e)
