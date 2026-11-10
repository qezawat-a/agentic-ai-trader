"""The bot must act like an agent where it used to punt back to the user.

The trigger was real: the user sent
`connect provider https://openrouter.ai/api/v1/sk-or-v1-...` and the LLM replied
"I don't have the exact connect_provider syntax in my context ... If you share
those details, I'll show the exact invocation" - i.e. it asked the user for the
bot's OWN documentation. Three fixes, pinned here:

  1. the system prompt carries the command handbook (derived from HELP), so
     syntax questions are answerable from context;
  2. `connect provider ...` in plain words routes to the command;
  3. a key glued onto the URL is split automatically instead of bouncing.
"""
from __future__ import annotations

import asyncio
import inspect

import jrock.agent as agent_mod
from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.model_selector import AutoSetResult


def _agent(monkeypatch, tmp_path):
    """An agent whose .env writes land in tmp_path and whose provider probe
    never touches the network."""
    async def fake_autoset(base, key, *a, **k):
        return AutoSetResult(base, ok=True, selected="test/test-model",
                             message="probe ok")

    def fake_persist(env, sel):
        return None

    monkeypatch.setattr(agent_mod, "autoset_model", fake_autoset)
    monkeypatch.setattr(agent_mod, "persist_model", fake_persist)
    monkeypatch.setattr(agent_mod, "ROOT", tmp_path)
    a = JRockAgent.__new__(JRockAgent)
    a.s = Settings()
    a.model = None
    a._pending_action = None
    return a


def test_system_prompt_carries_its_own_command_handbook():
    a = JRockAgent.__new__(JRockAgent)
    a.s = None
    prompt = a._system_prompt()

    assert "Your command handbook" in prompt
    for token in ("/connect_provider <url> <key>", "/models",
                  "/check_system", "TRADER ENGINE COMMANDS"):
        assert token in prompt, token


def test_prompt_never_points_the_model_at_a_help_it_cannot_read():
    """The old prompt said the list is 'in /help' - which the model cannot see.
    That is exactly the sentence that produced the punt-back reply."""
    a = JRockAgent.__new__(JRockAgent)
    a.s = None
    prompt = a._system_prompt()

    assert "The full list is in /help" not in prompt
    assert "never ask" in prompt, "the anti-punt rule must be stated"


def test_glued_url_and_key_are_split(tmp_path, monkeypatch):
    a = _agent(monkeypatch, tmp_path)
    out = asyncio.run(a.cmd_connect_provider(
        "https://openrouter.ai/api/v1/sk-or-v1-0123456789abcdef"))

    assert "split" in out
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "BASE_URL=https://openrouter.ai/api/v1" in env
    assert "API_KEY=sk-or-v1-0123456789abcdef" in env


def test_two_arguments_still_work(tmp_path, monkeypatch):
    a = _agent(monkeypatch, tmp_path)
    out = asyncio.run(a.cmd_connect_provider(
        "https://openrouter.ai/api/v1 sk-or-v1-0123456789abcdef"))

    assert "split" not in out
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "BASE_URL=https://openrouter.ai/api/v1" in env
    assert "API_KEY=sk-or-v1-0123456789abcdef" in env


def test_one_plain_token_without_a_key_gets_a_useful_usage(tmp_path, monkeypatch):
    a = _agent(monkeypatch, tmp_path)
    out = asyncio.run(a.cmd_connect_provider("https://example.com/v1"))
    assert "Usage:" in out and "SEPARATE" in out


def test_bare_words_route_to_the_command_not_the_model(tmp_path, monkeypatch):
    """`connect provider url key` with no slash must DO it, never reach chat."""
    a = _agent(monkeypatch, tmp_path)

    async def refuse_chat(text):
        raise AssertionError("this must be handled before the model")

    a.chat = refuse_chat
    out = asyncio.run(a.route_bare(
        "Connect_Provider https://api.kilo.ai/api/gateway sk-test-12345678"))

    assert "provider set to https://api.kilo.ai/api/gateway" in out
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    # the key kept its case: only the keyword is case-insensitive
    assert "API_KEY=sk-test-12345678" in env


def test_slash_form_still_routes():
    src = inspect.getsource(agent_mod.JRockAgent.handle)
    assert 'cmd == "/connect_provider"' in src


def test_glued_key_does_not_swallow_an_ordinary_path(tmp_path, monkeypatch):
    """A URL like https://host/v1/chat is not a key: refuse to split it."""
    a = _agent(monkeypatch, tmp_path)
    out = asyncio.run(a.cmd_connect_provider(
        "https://example.com/v1/chat/completions"))

    assert "Usage:" in out
    assert not (tmp_path / ".env").exists(), "nothing written on a rejected call"
