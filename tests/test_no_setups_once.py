"""NO_SETUPS goes to Discord once per NY day; the marker never decides whether orders are placed."""
import os
import sys

import pytest

os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bot  # noqa: E402


@pytest.fixture
def patched(monkeypatch):
    events = []
    monkeypatch.setattr(bot, "gates", lambda b, now: True)
    monkeypatch.setattr(bot, "own_positions", lambda b: [])
    monkeypatch.setattr(bot, "sleep_until", lambda t: None)
    monkeypatch.setattr(bot, "scan", lambda b, today, syms: [])
    monkeypatch.setattr(bot.notify, "event", lambda kind, *a, **k: events.append(kind))
    return events


def test_alert_once(tmp_path, monkeypatch, patched):
    marker = tmp_path / ".trade_marker"
    monkeypatch.setenv("TRADE_MARKER", str(marker))
    assert bot.mode_trade(None) == 0
    assert patched.count("NO_SETUPS") == 1 and marker.exists()
    assert bot.mode_trade(None) == 0            # second cron run same day
    assert patched.count("NO_SETUPS") == 1


def test_no_marker_env_still_alerts(monkeypatch, patched):
    monkeypatch.delenv("TRADE_MARKER", raising=False)
    bot.mode_trade(None); bot.mode_trade(None)
    assert patched.count("NO_SETUPS") == 2


def test_marker_does_not_block_orders(tmp_path, monkeypatch, patched):
    marker = tmp_path / ".trade_marker"
    marker.write_text("x")
    monkeypatch.setenv("TRADE_MARKER", str(marker))
    s = bot.Setup("AAPL", 1, 100.0, 99.0, 101.5, 1.0, 10, 35.0, 99.0, 1)
    submitted = []

    class B:
        def last_price(self, syms): return {x: 99.5 for x in syms}

    monkeypatch.setattr(bot, "scan", lambda b, today, syms: [s])
    monkeypatch.setattr(bot, "submit", lambda b, s_, cid_, market: submitted.append(cid_) or "ok")
    monkeypatch.setattr(bot.notify, "trade", lambda **k: None)
    monkeypatch.setattr(bot.notify, "discord", lambda *a, **k: None)
    bot.mode_trade(B())
    assert len(submitted) == 1
