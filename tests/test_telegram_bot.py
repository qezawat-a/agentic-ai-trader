"""Tests for the Telegram front-end (jrock/telegram_bot.py).

Three things are being protected here:

1. The bot must never contain a bot token, and must fail loudly (exit 2 with
   guidance) when TELEGRAM_BOT_TOKEN is absent - a channel that starts and
   silently answers nothing is worse than one that refuses to start.
2. Every number it reports comes from `build_state`, the SAME snapshot the web
   dashboard uses, so the two front-ends cannot disagree.
3. The REPORT PUSH channel (report on -> engine report to every chat that
   talked to the bot, each report_interval) and the TELEGRAM_ALLOWED_CHATS
   gate, which is documented in .env.example and must not be decorative.

No test touches the network: TelegramBot.call is replaced by a recorder.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
        "partial_tp_ladder": "40@1,35@2,25@3",
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


# ------------------------------------------------------------ engine-backed fakes


class FakeEngine:
    """Mini engine stand-in: a report counter plus the state the app touches."""

    def __init__(self, *, report_on: bool = False, interval: float = 30.0):
        self.state = SimpleNamespace(report_on=report_on,
                                     report_interval=interval,
                                     last_report_ts=0.0)
        self.report_calls = 0

    async def report(self) -> str:
        self.report_calls += 1
        return f"report #{self.report_calls}"


def _engine_app(monkeypatch, engine: FakeEngine) -> tb.JRockTelegramApp:
    """_app but whose agent carries a fake engine instead of only handle()."""
    app = _app(monkeypatch)
    app.agent = SimpleNamespace(handle=app.agent.handle, engine=engine)
    return app


class _Stop(BaseException):
    """Ends report_push_loop from a fake sleep (BaseException: the loop's
    `except Exception` must NOT swallow it - that is part of what we test)."""


class FullFakeBot:
    """RecordingBot + the methods run() needs (get_me / set_my_commands / poll)."""

    def __init__(self, updates: list[dict] | None = None):
        self.sent: list[tuple] = []
        self.commands = None
        self._updates = updates or []

    async def get_me(self):
        return {"username": "TestBot"}

    async def set_my_commands(self, commands):
        self.commands = list(commands)
        return True

    async def poll(self, offset, timeout=30):
        return self._updates

    async def send(self, chat_id, text, *, markdown=False):
        self.sent.append((chat_id, text, markdown))
        return True


# ------------------------------------------------------------------- /report cmd


def test_report_command_pulls_the_engine_report(monkeypatch):
    """/report is the one-shot engine report, straight from engine.report()."""
    eng = FakeEngine()
    app = _engine_app(monkeypatch, eng)
    asyncio.run(app.handle(9, "/report"))
    assert eng.report_calls == 1
    assert "report #1" in app.bot.sent[-1][1]
    assert app.bot.sent[-1][2] is True  # markdown


def test_report_command_ticks_the_counter_each_call(monkeypatch):
    eng = FakeEngine()
    app = _engine_app(monkeypatch, eng)
    asyncio.run(app.handle(9, "/report"))
    asyncio.run(app.handle(9, "/report"))
    assert eng.report_calls == 2
    assert "report #2" in app.bot.sent[-1][1]


# -------------------------------------------------------------- the chat fan-out


def test_handle_remembers_the_chat(monkeypatch):
    """The push list is built by talking to the bot: first message registers it."""
    app = _app(monkeypatch)
    assert app.chats == set()
    asyncio.run(app.handle(7, "/status"))
    assert app.chats == {7}


def test_handle_tracks_every_chat_that_writes(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(7, "/status"))
    asyncio.run(app.handle(8, "/status"))
    asyncio.run(app.handle(9, "turn the engine on"))
    assert app.chats == {7, 8, 9}


# --------------------------------------------------------- TELEGRAM_ALLOWED_CHATS


def test_allowed_chats_env_parses_spaces_and_commas(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "111, 222 333")
    assert tb._allowed_chats_from_env() == {111, 222, 333}


def test_allowed_chats_env_empty_means_any(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    assert tb._allowed_chats_from_env() == set()


def test_allowed_chats_env_ignores_junk(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", " , abc, 555 , -1")
    assert tb._allowed_chats_from_env() == {555}


def test_chat_allowed_empty_list_accepts_everyone(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    app = _app(monkeypatch)
    assert app._chat_allowed(1) is True
    assert app._chat_allowed(99999) is True


def test_chat_allowed_list_limits_answers(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "7")
    app = _app(monkeypatch)
    assert app._chat_allowed(7) is True
    assert app._chat_allowed(9) is False


def test_run_ignores_updates_from_disallowed_chats(monkeypatch):
    """The allowlist must gate ANSWERS, not just the help text: an update from
    a chat outside the list gets no reply and never joins the fan-out."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "7")
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/status",
                                     "from": {"username": "operator"}}},
        {"update_id": 2, "message": {"chat": {"id": 9}, "text": "/status",
                                     "from": {"username": "intruder"}}},
    ]
    app = _app(monkeypatch)
    app.bot = FullFakeBot(updates)
    asyncio.run(app.run(once=True))
    assert [c for c, _, _ in app.bot.sent] == [7]
    assert app.chats == {7}


# ------------------------------------------------------------- the report pusher


def test_push_report_once_fans_out_to_every_known_chat(monkeypatch):
    eng = FakeEngine(report_on=True)
    app = _engine_app(monkeypatch, eng)
    app.chats.update([9001, 8, 77])
    assert asyncio.run(app._push_report_once()) is True
    # sorted(chat ids as strings) - deterministic fan-out order
    assert [c for c, _, _ in app.bot.sent] == [77, 8, 9001]
    assert all(md is True for _, _, md in app.bot.sent)
    assert eng.report_calls == 1
    assert eng.state.last_report_ts > 0.0


def test_push_report_once_is_a_noop_when_report_off(monkeypatch):
    """The operator never opted into pushes - the bot must stay silent."""
    eng = FakeEngine(report_on=False)
    app = _engine_app(monkeypatch, eng)
    app.chats.add(7)
    assert asyncio.run(app._push_report_once()) is False
    assert app.bot.sent == []
    assert eng.report_calls == 0


def test_push_report_once_is_a_noop_when_no_chats_known(monkeypatch):
    """A chat that never talked to the bot never gets a report."""
    eng = FakeEngine(report_on=True)
    app = _engine_app(monkeypatch, eng)
    assert asyncio.run(app._push_report_once()) is False
    assert app.bot.sent == []
    assert eng.report_calls == 0


def test_push_report_once_is_a_noop_without_an_engine(monkeypatch):
    """agent=None (the bare test app): fail closed, never crash the loop."""
    app = _app(monkeypatch)
    assert asyncio.run(app._push_report_once()) is False
    assert app.bot.sent == []


def test_report_interval_uses_the_engine_interval(monkeypatch):
    eng = FakeEngine(interval=90.0)
    app = _engine_app(monkeypatch, eng)
    assert app._report_interval() == pytest.approx(90.0)


def test_report_interval_has_a_flood_floor(monkeypatch):
    """A mistyped report_interval=1 must not flood Telegram: floor 5s."""
    eng = FakeEngine(interval=2.0)
    app = _engine_app(monkeypatch, eng)
    assert app._report_interval() == tb.REPORT_MIN_INTERVAL


def test_report_interval_survives_a_missing_engine(monkeypatch):
    app = _app(monkeypatch)  # agent=None
    assert app._report_interval() == pytest.approx(30.0)


def test_report_push_loop_pushes_until_stopped(monkeypatch):
    """Two full passes, then a fake sleep that stops it - and the loop's
    `except Exception` must not swallow the stop signal (BaseException)."""
    eng = FakeEngine(report_on=True, interval=30.0)
    app = _engine_app(monkeypatch, eng)
    app.chats.add(7)

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop()
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(app.report_push_loop())

    assert eng.report_calls == 2
    assert len(app.bot.sent) == 2
    assert sleeps == [30.0, 30.0]


def test_report_push_loop_absorbs_a_failing_engine(monkeypatch):
    """A broken engine is logged, not fatal: the pusher keeps its cadence."""
    eng = FakeEngine(report_on=True)
    app = _engine_app(monkeypatch, eng)

    async def boom():
        raise RuntimeError("engine snapshot exploded")

    eng.report = boom  # type: ignore[method-assign]

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop()
        return

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(app.report_push_loop())

    assert len(sleeps) == 2
    assert app.bot.sent == []
