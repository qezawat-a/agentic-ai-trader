"""Regression tests for the two defects the operator reported after the first
Telegram-menu change.

1. **`/help engine` did nothing useful.** The router split on whitespace and
   took the first token, so a code-fenced command (``/help engine`` rendered as
   a code block) became `` ```/help `` and fell through to the engine router -
   the operator tapped a topic and got no help page. Fixed by stripping the
   fence and by giving ``/help <topic>`` real pages.

2. **A wall of `python-dotenv could not parse statement` warnings at startup.**
   ``mirror_env`` wrote a multi-line value raw, so every line after the first
   became an unparseable statement. Fixed by quoting/escaping the value.

No test touches the network.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from jrock import telegram_bot as tb
from jrock.settings_store import _env_value, mirror_env


class RecordingBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, chat_id, text, *, markdown=False):
        self.sent.append((chat_id, text, markdown))
        return True


STATE = {
    "updated_at": "11:35:56",
    "engine": {
        "engine_on": False, "autotrade": False, "scan_on": True,
        "symbols": ["INIUSDT"], "leverage": 25, "tpsl_method": "POSITION",
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


class StubAgent:
    """Stands in for JRockAgent: records the line it was handed."""

    def __init__(self, output="engine says hi"):
        self.output = output
        self.last_line: str | None = None

    async def handle(self, line):
        self.last_line = line
        return self.output


def _app(monkeypatch, state=STATE, agent_output="engine says hi"):
    app = tb.JRockTelegramApp(agent=StubAgent(agent_output), bot=RecordingBot())

    async def fake_state():
        return state

    monkeypatch.setattr(app, "state", fake_state)
    return app


# ------------------------------------------------- 1. command parsing / topics
@pytest.mark.parametrize("raw,cmd,rest", [
    ("/help engine", "help", "engine"),
    ("/status", "status", ""),
    ("/status@Negar_trad_bot", "status", ""),
    ("/status@Negar_trad_bot engine", "status", "engine"),
    ("  /help engine  ", "help", "engine"),
    ("```/help engine```", "help", "engine"),
    ("``` /help engine ```", "help", "engine"),
    ("`/help engine`", "help", "engine"),
    ("/help", "help", ""),
    ("turn the engine on", "", "turn the engine on"),
    ("Yes", "", "Yes"),
])
def test_split_command_handles_every_real_shape(raw, cmd, rest):
    assert tb._split_command(raw) == (cmd, rest)


def test_help_engine_returns_a_topic_page_not_a_status_dump(monkeypatch):
    """The exact tap from the screenshot must produce a help page."""
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "/help engine"))
    text = app.bot.sent[0][1]
    assert "/help engine" in text
    assert "engine" in text.lower()
    # It must NOT have been forwarded to the engine router.
    assert app.agent.last_line is None


def test_help_engine_works_when_code_fenced(monkeypatch):
    """Telegram renders a tapped command as a code block - that must work too."""
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "```/help engine```"))
    assert "/help engine" in app.bot.sent[0][1]
    assert app.agent.last_line is None


def test_help_unknown_topic_lists_the_real_topics(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "/help nonsense"))
    text = app.bot.sent[0][1]
    assert "no help page" in text
    for topic in tb.HELP_TOPICS:
        assert f"/{topic}" in text


def test_help_alone_lists_commands_and_topics(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "/help"))
    text = app.bot.sent[0][1]
    for name, _ in tb.COMMANDS:
        assert f"/{name}" in text
    for topic in tb.HELP_TOPICS:
        assert f"/{topic}" in text


def test_every_help_topic_is_a_real_command():
    """A topic page must point at a command the bot actually routes."""
    names = {c for c, _ in tb.COMMANDS}
    for topic in tb.HELP_TOPICS:
        assert topic in names, topic


def test_bare_text_still_reaches_the_engine_router(monkeypatch):
    """'turn the engine on' must go to the agent, whole line intact."""
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "turn the engine on"))
    assert app.agent.last_line == "turn the engine on"


def test_yes_reaches_the_engine_router(monkeypatch):
    app = _app(monkeypatch)
    asyncio.run(app.handle(1, "Yes"))
    assert app.agent.last_line == "Yes"


# ------------------------------------------------- 2. the dotenv warning wall
def test_env_value_quotes_multiline():
    assert _env_value("a\nb") == '"a\\nb"'


def test_env_value_leaves_simple_values_alone():
    assert _env_value("plain") == "plain"
    assert _env_value("1R:25,2R:25") == "1R:25,2R:25"


def test_env_value_escapes_quotes_and_backslashes():
    assert _env_value('say "hi"') == '"say \\"hi\\""'
    assert _env_value("a\\b") == '"a\\\\b"'


def test_mirror_env_multiline_value_parses_without_warnings(tmp_path, caplog):
    """The exact failure: a multi-line value must not produce parse warnings."""
    from dotenv import dotenv_values

    env = tmp_path / ".env"
    env.write_text("KEEP=1\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="dotenv.main"):
        mirror_env(env, "SOUL", "line one\nline two\nline three")
        parsed = dotenv_values(env)

    assert not [r for r in caplog.records if "could not parse" in r.getMessage()]
    assert parsed["SOUL"] == "line one\nline two\nline three"
    assert parsed["KEEP"] == "1"


def test_mirror_env_preserves_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nB=2\n", encoding="utf-8")
    mirror_env(env, "B", "changed")
    body = env.read_text(encoding="utf-8")
    assert "A=1" in body
    assert "B=changed" in body


def test_mirror_env_appends_when_key_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    mirror_env(env, "NEW", "v")
    assert "NEW=v" in env.read_text(encoding="utf-8")


def test_persist_model_quotes_a_hostile_model_id(tmp_path, caplog):
    from dotenv import dotenv_values

    from jrock.model_selector import persist_model

    env = tmp_path / ".env"
    env.write_text("MODEL=old\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="dotenv.main"):
        persist_model(env, 'weird "model"\nname')
        parsed = dotenv_values(env)

    assert not [r for r in caplog.records if "could not parse" in r.getMessage()]
    assert parsed["MODEL"] == 'weird "model"\nname'
