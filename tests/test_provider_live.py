"""End-to-end provider-path test against a LOCAL OpenAI-compatible server.

This proves AutoSetModelByKey and the agent's chat path work over real HTTP
against a real socket: list -> probe -> pick -> pin, plus the two execution
limits (rate limiter, step budget) refusing calls BEFORE they reach the
network.

The server is the one in tests/local_provider.py: it advertises a mix of
working and broken models, so the selector has to reject the broken ones.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from local_provider import CALLS, serve  # noqa: E402

from jrock.agent import JRockAgent  # noqa: E402
from jrock.config import load_settings  # noqa: E402
from jrock.model_selector import autoset_model, list_model_info, probe_model  # noqa: E402


@pytest.fixture()
def provider():
    srv, base = serve()
    yield base
    srv.shutdown()


def _agent_for(base: str, monkeypatch, tmp_path=None) -> JRockAgent:
    monkeypatch.setenv("BASE_URL", base)
    monkeypatch.setenv("API_KEY", "sk-local-test-key")
    monkeypatch.setenv("MODEL", "")
    if tmp_path is not None:
        # The limit settings are persisted; a test must never write to the
        # operator's real store. Settings.data_dir is derived from
        # jrock.config.ROOT, so that is what gets redirected.
        import jrock.config as config_mod
        monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    return JRockAgent(load_settings())


# ------------------------------------------------------------- model selection
def test_autoset_picks_the_working_model_and_skips_the_broken_ones(provider):
    async def run():
        res = await autoset_model(provider, "sk-local-test-key",
                                  max_probe=6, concurrency=4)
        assert res.ok, res.message
        assert res.selected == "good-model"
        return res

    res = asyncio.run(run())
    assert "good-model" in res.message


def test_probe_rejects_every_broken_model(provider):
    async def run():
        import aiohttp
        out = {}
        async with aiohttp.ClientSession() as sess:
            for m in ("good-model", "trash-model", "broken-model", "empty-model"):
                p = await probe_model(sess, provider, "sk-local-test-key", m,
                                      timeout=3)
                out[m] = (p.works, p.status)
        return out

    out = asyncio.run(run())
    assert out["good-model"][0] is True
    assert out["trash-model"][0] is False
    assert out["broken-model"][0] is False
    assert out["empty-model"][0] is False


def test_empty_model_is_retried_with_a_bigger_budget(provider):
    """A reasoning model that eats a tight budget must get ONE retry."""
    before = CALLS.count("empty-model")

    async def run():
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            await probe_model(sess, provider, "sk-local-test-key",
                              "empty-model", timeout=3)

    asyncio.run(run())
    assert CALLS.count("empty-model") >= before + 2, \
        "the empty-content model must be retried once"


# ------------------------------------------------------------------ chat path
def test_agent_chat_talks_to_the_provider(provider, monkeypatch):
    a = _agent_for(provider, monkeypatch)
    out = asyncio.run(a.chat("hello"))
    assert "J+Rock here" in out


# --------------------------------------------------------------------- limits
def test_rate_limiter_refuses_before_the_network_is_touched(provider, monkeypatch, tmp_path):
    a = _agent_for(provider, monkeypatch, tmp_path)
    a.model = "good-model"  # pinned: one chat = exactly one provider call
    a.agent_rate_limit_max = 1
    a.agent_rate_limit_window = 60
    calls_before = len(CALLS)

    first = asyncio.run(a.chat("first"))
    assert "J+Rock here" in first
    second = asyncio.run(a.chat("second"))
    assert "rate limit" in second
    assert len(CALLS) == calls_before + 1, \
        "the refused call must never reach the provider"


def test_model_discovery_batch_is_reserved_against_the_limiter(provider, monkeypatch, tmp_path):
    """Finding a model costs many provider calls - the batch is reserved."""
    a = _agent_for(provider, monkeypatch, tmp_path)
    a.model = ""  # not pinned -> discovery
    a.agent_rate_limit_max = 3
    a.agent_rate_limit_window = 60
    calls_before = len(CALLS)
    out = asyncio.run(a.chat("discover a model"))
    assert "rate limit" in out
    assert "did not look for a model" in out
    assert len(CALLS) == calls_before, \
        "the refused discovery must never reach the provider"


def test_step_budget_stops_before_the_network_is_touched(provider, monkeypatch, tmp_path):
    a = _agent_for(provider, monkeypatch, tmp_path)
    calls_before = len(CALLS)
    a.begin_operation("budget-test")
    a._budget.reset(max_steps=1, label="budget-test")
    assert a.take_step("one") is True
    out = asyncio.run(a.chat("should not be sent"))
    assert "stopped before sending" in out
    assert "1/1" in out
    assert len(CALLS) == calls_before, \
        "the budget-stopped call must never reach the provider"


def test_limits_are_reported_honestly_in_settings(provider, monkeypatch, tmp_path):
    a = _agent_for(provider, monkeypatch, tmp_path)
    out = asyncio.run(a.cmd_settings())
    assert "max_steps=128" in out
    assert "rate_limit_max=128" in out
    assert "limits now:" in out
