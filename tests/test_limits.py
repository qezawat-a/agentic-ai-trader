"""Tests for the agent's execution limits.

Two guards, two failure modes:

  * ``RateLimiter`` throttles outbound provider calls in a rolling window.
    It must REFUSE loudly (with the wait time) instead of silently letting
    a runaway loop through.
  * ``StepBudget`` bounds the work ONE operation may do. It must stop the
    operation and say so - never pretend the operation finished.
"""
from __future__ import annotations

import time

import pytest

from jrock.limits import RateLimiter, StepBudget


# --------------------------------------------------------------- rate limiter
def test_limiter_allows_up_to_the_cap_then_refuses():
    lim = RateLimiter(window_sec=60, max_calls=3)
    for _ in range(3):
        allowed, retry = lim.allow()
        assert allowed is True
        assert retry == 0.0
    allowed, retry = lim.allow()
    assert allowed is False
    assert retry > 0, "a refusal must say how long to wait"


def test_limiter_refusal_never_raises():
    lim = RateLimiter(window_sec=60, max_calls=1)
    assert lim.allow()[0] is True
    assert lim.allow()[0] is False  # no exception, just a refusal


def test_limiter_window_expires_old_calls():
    lim = RateLimiter(window_sec=0.2, max_calls=1)
    assert lim.allow()[0] is True
    assert lim.allow()[0] is False
    time.sleep(0.25)
    assert lim.allow()[0] is True, "the old call must age out of the window"


def test_limiter_zero_cap_blocks_everything():
    lim = RateLimiter(window_sec=60, max_calls=0)
    allowed, retry = lim.allow()
    assert allowed is False
    assert retry > 0


def test_limiter_counts_the_calls_in_the_window():
    lim = RateLimiter(window_sec=60, max_calls=5)
    assert lim.used() == 0
    lim.allow()
    lim.allow()
    assert lim.used() == 2
    lim.reset()
    assert lim.used() == 0


# ---------------------------------------------------------------- step budget
def test_budget_stops_at_the_max():
    b = StepBudget(max_steps=3)
    assert [b.step() for _ in range(3)] == [True, True, True]
    assert b.exhausted is True
    assert b.step() is False, "the fourth step must be refused"
    assert b.spent == 3, "a refused step must not be counted"


def test_budget_zero_means_unlimited():
    b = StepBudget(max_steps=0)
    for _ in range(500):
        assert b.step() is True
    assert b.exhausted is False
    assert b.remaining == -1


def test_budget_reset_clears_the_count():
    b = StepBudget(max_steps=2)
    b.step()
    b.step()
    assert b.exhausted
    b.reset(max_steps=5, label="next")
    assert b.spent == 0
    assert b.remaining == 5
    assert b.step() is True


def test_budget_note_is_honest():
    b = StepBudget(max_steps=4, label="selfheal")
    b.step()
    note = b.note()
    assert "1/4" in note and "3 left" in note
    unlimited = StepBudget(max_steps=0, label="x")
    assert "unlimited" in unlimited.note()


# ------------------------------------------------------------ agent integration
def _agent(tmp_path=None, monkeypatch=None):
    """An agent whose store lives in a temp dir.

    The limit settings are PERSISTED, so a test that used the real store
    would leave the operator's runtime changed - that is exactly the kind
    of side effect a test must not have. ``Settings.data_dir`` is derived
    from ``jrock.config.ROOT``, so that is what gets redirected.
    """
    from jrock.agent import JRockAgent
    import jrock.config as config_mod
    if tmp_path is None:
        return JRockAgent()
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    return JRockAgent(config_mod.load_settings())


def test_agent_refuses_a_provider_call_when_the_window_is_full(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    a.agent_rate_limit_max = 1
    a.agent_rate_limit_window = 60
    allowed, why = a.provider_call_allowed()
    assert allowed is True
    allowed, why = a.provider_call_allowed()
    assert allowed is False
    assert "rate limit" in why
    assert "Retry in" in why


def test_agent_chat_refuses_when_the_rate_limit_is_hit(tmp_path, monkeypatch):
    import asyncio
    a = _agent(tmp_path, monkeypatch)
    a.s.api_key = "sk-test-not-a-real-key"
    a.agent_rate_limit_max = 0  # no provider calls allowed at all
    out = asyncio.run(a.chat("hello"))
    assert "rate limit" in out
    assert "hello" in out, "the operator must see what was not sent"


def test_agent_operation_budget_is_reset_per_turn(tmp_path, monkeypatch):
    import asyncio
    a = _agent(tmp_path, monkeypatch)
    a.agent_max_steps = 2
    a.begin_operation("first")
    a.take_step("one")
    a.take_step("two")
    assert a._budget.exhausted
    a.begin_operation("second")
    assert not a._budget.exhausted
    assert a._budget.spent == 0


def test_agent_limit_commands_read_and_write(tmp_path, monkeypatch):
    import jrock.agent as agent_mod
    monkeypatch.setattr(agent_mod, "ROOT", tmp_path)
    a = _agent(tmp_path, monkeypatch)
    assert "agent max steps" in a._handle_limit_command("agent limits")
    assert "agent max steps = 7" == a._handle_limit_command("agent max steps 7")
    assert a.agent_max_steps == 7
    assert "Usage" in a._handle_limit_command("agent max steps")
    assert "agent rate limit window = 45s" == a._handle_limit_command(
        "agent rate limit window 45")
    assert a.agent_rate_limit_window == 45
    assert "agent rate limit max = 9" == a._handle_limit_command(
        "agent rate limit max 9")
    assert a.agent_rate_limit_max == 9
    # restore the defaults so the persisted store is not left altered
    a.agent_max_steps = 128
    a.agent_rate_limit_window = 128
    a.agent_rate_limit_max = 128


def test_settings_expose_the_rate_limit_cap():
    from jrock.config import load_settings
    s = load_settings()
    assert s.agent_rate_limit_window == 128
    assert s.agent_rate_limit_max == 128
    assert s.agent_max_steps == 128
