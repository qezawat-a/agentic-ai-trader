"""Tests for the auto-shown command menu (bottom-left corner of the chat).

The operator's complaint was that the bot's commands were invisible until they
typed ``/help``. Two mechanisms fix that, and both are bottom-left in Telegram:

1. ``setMyCommands`` - the command list behind the **Menu** button. Published
   once at startup, cached by Telegram, so it is there on the next visit.
2. A persistent **reply keyboard** - the same commands one tap away, attached
   to every reply.

These tests pin both, plus the invariant that the Menu list and the ``/help``
text are generated from ONE source, so they cannot drift apart.

No test touches the network: ``TelegramBot.call`` is replaced by a recorder.
"""
from __future__ import annotations

import asyncio

import pytest

from jrock import telegram_bot as tb


class RecordingBot:
    """Stands in for TelegramBot: records calls instead of hitting the API."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.commands: list[tuple[str, str]] | None = None

    async def send(self, chat_id, text, *, markdown=False):
        self.sent.append((chat_id, text, markdown))
        return True

    async def set_my_commands(self, commands):
        self.commands = list(commands)
        return True


class RecordingCallBot(tb.TelegramBot):
    """A real TelegramBot whose ``call`` is recorded rather than sent."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, **params):  # type: ignore[override]
        self.calls.append((method, params))
        return True


STATE = {
    "updated_at": "11:35:56",
    "engine": {
        "engine_on": False, "autotrade": False, "scan_on": True,
        "symbols": ["INIUSDT"], "leverage": 25, "tpsl_method": "POSITION",
        "partial_tp_ladder": "40@1,35@2,25@3",
        "scans": 0, "errors": 0, "last_error": "",
    },
    "account": {
        "equity": 0.68, "available": 0.68, "margin": 0.0,
        "unrealized": 0.0, "realized": 0.0,
        "open_positions": 0, "open_orders": 0,
    },
    "positions": [],
    "orders": [],
}


def _app(monkeypatch, state=STATE):
    app = tb.JRockTelegramApp(agent=object(), bot=RecordingBot())

    async def fake_state():
        return state

    monkeypatch.setattr(app, "state", fake_state)
    return app


# --------------------------------------------------------------- the Menu list
def test_commands_cover_every_documented_command():
    """Every command the bot routes must be published to the Menu."""
    names = [c for c, _ in tb.COMMANDS]
    assert names == ["status", "positions", "orders", "pnl", "report",
                     "tpsl", "balance", "price", "engine", "help"]


def test_commands_are_valid_telegram_command_names():
    """Telegram rejects a command that is not 1-32 chars of [a-z0-9_]."""
    for name, desc in tb.COMMANDS:
        assert 1 <= len(name) <= 32, name
        assert name == name.lower(), name
        assert all(ch.isalnum() or ch == "_" for ch in name), name
        assert 1 <= len(desc) <= 256, desc


def test_help_text_is_generated_from_the_same_list():
    """The /help body and the Menu cannot disagree - one source, two views."""
    text = tb.render_help()
    for name, desc in tb.COMMANDS:
        assert f"/{name}" in text, name
        assert desc in text, desc


def test_help_constant_matches_render_help():
    assert tb.HELP == tb.render_help()


# ------------------------------------------------------- setMyCommands at boot
def test_set_my_commands_sends_the_whole_list():
    bot = RecordingCallBot()
    asyncio.run(bot.set_my_commands(tb.COMMANDS))

    method, params = bot.calls[0]
    assert method == "setMyCommands"
    assert params["commands"] == [
        {"command": c, "description": d} for c, d in tb.COMMANDS]


def test_run_publishes_the_menu_before_polling(monkeypatch):
    """Startup must publish the Menu, and must do it before the first poll."""
    order: list[str] = []

    class Bot(RecordingBot):
        async def get_me(self):
            order.append("get_me")
            return {"username": "Myclineinmachine_bot"}

        async def set_my_commands(self, commands):
            order.append("set_my_commands")
            self.commands = list(commands)
            return True

        async def poll(self, offset, timeout=30):
            order.append("poll")
            return []

    app = tb.JRockTelegramApp(agent=object(), bot=Bot())
    asyncio.run(app.run(once=True))

    assert order[:3] == ["get_me", "set_my_commands", "poll"]
    assert app.bot.commands == tb.COMMANDS


def test_a_failed_menu_publish_does_not_stop_the_bot(monkeypatch):
    """A rejected setMyCommands is a warning, not a crash - commands still work."""
    polled: list[bool] = []

    class Bot(RecordingBot):
        async def get_me(self):
            return {"username": "Myclineinmachine_bot"}

        async def set_my_commands(self, commands):
            raise tb.TelegramError("setMyCommands: Bad Request (code 400)")

        async def poll(self, offset, timeout=30):
            polled.append(True)
            return []

    app = tb.JRockTelegramApp(agent=object(), bot=Bot())
    asyncio.run(app.run(once=True))
    assert polled == [True]


# ------------------------------------------------------- the reply keyboard
def test_reply_keyboard_is_persistent_and_bottom_left():
    kb = tb.REPLY_KEYBOARD
    assert kb["is_persistent"] is True
    assert kb["resize_keyboard"] is True
    flat = [b["text"] for row in kb["keyboard"] for b in row]
    # Every button must be a command the router actually accepts.
    for text in flat:
        assert text.startswith("/")
        assert text.lstrip("/") in [c for c, _ in tb.COMMANDS], text


def test_keyboard_pins_the_daily_drivers():
    """The buttons this expansion was about: one-tap report + live prices."""
    flat = [b["text"] for row in tb.REPLY_KEYBOARD["keyboard"] for b in row]
    assert "/report" in flat
    assert "/price" in flat
    # Four buttons per row max - bulky keyboards crowd the chat on phones.
    assert all(len(row) <= 4 for row in tb.REPLY_KEYBOARD["keyboard"])


def test_send_attaches_the_keyboard_by_default():
    bot = RecordingCallBot()
    asyncio.run(bot.send(1, "hello", markdown=True))

    method, params = bot.calls[0]
    assert method == "sendMessage"
    assert params["reply_markup"] == tb.REPLY_KEYBOARD


def test_keyboard_rides_only_the_last_chunk():
    """A long report must not stack one keyboard per message."""
    bot = RecordingCallBot()
    long_text = "\n".join(f"line {i}" for i in range(2000))
    asyncio.run(bot.send(1, long_text, markdown=True))

    assert len(bot.calls) > 1
    with_kb = [p for _, p in bot.calls if "reply_markup" in p]
    assert len(with_kb) == 1
    assert bot.calls[-1][1]["reply_markup"] == tb.REPLY_KEYBOARD


def test_keyboard_can_be_suppressed():
    bot = RecordingCallBot()
    asyncio.run(bot.send(1, "hello", markdown=True, keyboard=False))
    assert "reply_markup" not in bot.calls[0][1]


# ------------------------------------------------- the commands still answer
def test_help_reply_contains_every_command(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "/help"))
    _, text, _ = app.bot.sent[0]
    for name, _desc in tb.COMMANDS:
        assert f"/{name}" in text


def test_start_is_an_alias_for_help(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "/start"))
    assert "J+Rock bot" in app.bot.sent[0][1]


def test_orders_with_no_orders_says_so(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "/orders"))
    assert "no pending orders" in app.bot.sent[0][1]


def test_status_renders_the_screenshot_numbers(monkeypatch):
    """The exact figures from the operator's screenshot must round-trip."""
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "/status"))
    text = app.bot.sent[0][1]
    assert "0.68" in text
    assert "INIUSDT" in text
    assert "25x" in text
