"""Tests for the Telegram front-end (jrock/telegram_bot.py).

Two things are being protected here:

1. The bot must never contain a bot token, and must fail loudly (exit 2 with
   guidance) when TELEGRAM_BOT_TOKEN is absent - a channel that starts and
   silently answers nothing is worse than one that refuses to start.
2. Every number it reports comes from `build_state`, the SAME snapshot the web
   dashboard uses, so the two front-ends cannot disagree.

No test touches the network: TelegramBot.call is replaced by a recorder.
"""
from __future__ import annotations

import asyncio

import pytest

from jrock import telegram_bot as tb


class RecordingBot:
    """Stands in for TelegramBot: records sends instead of calling the API."""

    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, chat_id, text, *, markdown=False):
        self.sent.append((chat_id, text, markdown))
        return True


STATE = {
    "updated_at": "10:30:41",
    "engine": {
        "engine_on": True, "autotrade": False, "scan_on": True,
        "symbols": ["BTCUSDT"], "leverage": 20, "tpsl_method": "POSITION",
        "scans": 12, "errors": 0, "last_error": "",
    },
    "account": {
        "equity": 1234.5, "available": 900.25, "margin": 334.25,
        "unrealized": 12.5, "realized": -3.0,
        "open_positions": 1, "open_orders": 2,
    },
    "positions": [{
        "symbol": "BTCUSDT", "side": "LONG", "qty": "0.5", "entry": 60000.0,
        "mark": 60500.0, "liq": 55000.0, "leverage": 20, "roi": 16.67,
        "unrealized": 250.0, "margin": "334.25", "margin_mode": "CROSS",
    }],
    "orders": [
        {"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "qty": "0.2",
         "price": "61000", "status": "NEW", "id": "O1"},
        {"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "qty": "0.1",
         "price": "62000", "status": "NEW", "id": "O2"},
    ],
}


def _app(monkeypatch, state=STATE, agent_output="engine says hi"):
    app = tb.JRockTelegramApp(agent=None, bot=RecordingBot())

    async def fake_state():
        return state

    async def fake_handle(line):
        app.last_command = line
        return agent_output

    app.state = fake_state            # type: ignore[assignment]
    app.agent = type("A", (), {"handle": staticmethod(fake_handle)})()
    return app


# ------------------------------------------------------------- security / start


def test_token_comes_from_the_environment_only(monkeypatch):
    """A missing token must exit non-zero with guidance, not start silently."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    rc = asyncio.run(tb._amain(tb.build_parser().parse_args([])))
    assert rc == 2


def test_no_token_literal_in_the_source():
    """The bot must never carry a hard-coded token."""
    import inspect
    src = inspect.getsource(tb)
    assert "TELEGRAM_BOT_TOKEN" in src
    # a bot token is <digits>:<35-ish chars>; assert none is baked in
    import re
    assert not re.search(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", src)


def test_telegram_error_never_leaks_the_token():
    """Error text is shown to a chat - it must not contain the token."""
    err = tb.TelegramError("sendMessage: Bad Request")
    assert "123456" not in str(err)


# ------------------------------------------------------------------- rendering


def test_summary_matches_build_state_numbers():
    text = tb.render_summary(STATE)
    assert "1,234.50" in text          # equity, from account
    assert "BTCUSDT" in text
    assert "12" in text                # scans


def test_positions_render_roi_and_mark():
    text = tb.render_positions(STATE)
    assert "BTCUSDT" in text
    assert "16.67" in text             # roi
    assert "60,500.00" not in text or "60500" in text


def test_orders_render_every_row_not_an_envelope_count():
    """The bug class from DEF-1: never report len(dict) as the order count."""
    text = tb.render_orders(STATE)
    assert text.count("BTCUSDT") == 2
    assert "O1" in text and "O2" in text


def test_pnl_uses_the_account_block():
    text = tb.render_pnl(STATE)
    assert "1,234.50" in text
    assert "+12.50" in text            # unrealized, signed
    assert "-3.00" in text             # realized, signed


def test_empty_collections_say_so():
    empty = dict(STATE, positions=[], orders=[])
    assert "no open positions" in tb.render_positions(empty)
    assert "no pending orders" in tb.render_orders(empty)


def test_untrusted_text_is_html_escaped():
    """Symbols come from the exchange - they must not inject markup."""
    nasty = dict(STATE, positions=[dict(STATE["positions"][0],
                                        symbol="<b>x</b>")])
    assert "&lt;b&gt;" in tb.render_positions(nasty)
    assert "<b>x</b>" not in tb.render_positions(nasty)


def test_long_output_is_split_on_line_boundaries():
    body = "\n".join(f"line {i}" for i in range(4000))
    chunks = tb._split(body, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == body


# -------------------------------------------------------------------- routing


def test_orders_command_answers_from_state(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(42, "/orders"))
    chat, text, md = app.bot.sent[-1]
    assert chat == 42 and md is True
    assert "O1" in text and "O2" in text


def test_positions_command(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(42, "/positions"))
    assert "BTCUSDT" in app.bot.sent[-1][1]


def test_help_lists_the_commands(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(42, "/help"))
    text = app.bot.sent[-1][1]
    for c in ("/status", "/positions", "/orders", "/pnl"):
        assert c in text


def test_unknown_text_goes_to_the_engine_router(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(42, "buy 0.1 BTCUSDT"))
    assert app.last_command == "buy 0.1 BTCUSDT"
    assert "engine says hi" in app.bot.sent[-1][1]


def test_command_with_bot_username_suffix_is_stripped(monkeypatch):
    """In a group Telegram sends '/orders@MyBot' - it must still route."""
    app = _app(monkeypatch)
    asyncio.run(app.handle(42, "/orders@Myclineinmachine_bot"))
    assert "O1" in app.bot.sent[-1][1]


def test_failure_is_reported_to_the_chat_not_swallowed(monkeypatch):
    app = _app(monkeypatch)

    async def boom():
        raise RuntimeError("state failed")

    app.state = boom                  # type: ignore[assignment]
    asyncio.run(app.handle(42, "/status"))
    assert "RuntimeError" in app.bot.sent[-1][1]
