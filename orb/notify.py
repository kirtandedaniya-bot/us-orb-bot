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
                                   "footer": {"text": "US ORB v2"}}]}).encode()
    req = urllib.request.Request(WEBHOOK, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning("discord failed: %s", e)


def event(kind: str, msg: str, alert: bool = False, color: int = GREY) -> None:
    log.info("[%s] %s", kind, msg)
    try:
        import db_manager
        db_manager.log_system_event(ENGINE, kind, msg)
    except Exception as e:
        log.warning("db event failed: %s", e)
    if alert:
        discord(kind, msg, color)


def trade(**kw) -> None:
    try:
        import db_manager
        db_manager.log_trade(engine=ENGINE, **kw)
    except Exception as e:
        log.warning("db trade failed: %s", e)
