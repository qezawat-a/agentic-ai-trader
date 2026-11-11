"""The two commands the operator typed while the model was DEAD.

    Set partial ladder 25@1,25@2,25@3,25@4
    Min stop tick 4

Both reached the model (a leading verb for the first; no command at all for
the second - MIN_STOP_TICKS was a hardcoded module constant), and with the
provider down each message answered with a 40-line probe-failure dump.
Pinned here:

1. a leading "Set " is a verb - the line routes to the engine, unless what
   follows is a setter the NLU treats as destructive (those keep their
   confirm prompt),
2. min stop ticks is a REAL, persisted setting with a setter (1..50),
3. the settings report shows the live value, not the built-in default,
4. the probe retry: a reasoning model that answers HTTP 500 "empty response
   content" with a tight token budget is alive - one retry with a larger
   budget must revive it,
5. the chat failure message shows the first probe rows only, not 40 lines.
"""
from __future__ import annotations

import asyncio
import json

from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.model_selector import probe_model
from jrock.trader.engine import TraderEngine


def _agent(tmp_path) -> JRockAgent:
    a = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    a.engine.data_dir = tmp_path
    return a


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- routing
def test_set_partial_ladder_is_an_engine_command_not_a_model_call(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("Set partial ladder 25@1,25@2,25@3,25@4"))
    assert a.engine.state.partial_tp_ladder == "25@1,25@2,25@3,25@4"
    assert "model" not in out.lower()[:80], out


def test_destructive_setters_keep_their_confirm(tmp_path):
    """The set-strip must NOT bypass the NLU confirm gate."""
    a = _agent(tmp_path)
    out = _run(a.handle("set leverage to 5x"))
    assert "confirm" in out.lower(), out
    assert a.engine.state.leverage != 5


def test_set_min_stop_tick_4(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("Min stop tick 4"))
    assert "model" not in out.lower()[:80], out
    assert a.engine.state.min_stop_ticks == 4
    assert "4" in out


def test_min_stop_bounds_are_enforced(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("min stop ticks 500"))
    assert a.engine.state.min_stop_ticks == 3  # unchanged
    assert "1..50" in out
    out = _run(a.handle("min stop ticks 0"))
    assert a.engine.state.min_stop_ticks == 3


def test_min_stop_survives_a_restart(tmp_path):
    a = _agent(tmp_path)
    _run(a.handle("min stop ticks 7"))
    b = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    b.engine.data_dir = tmp_path
    assert b.engine.state.min_stop_ticks == 7, "the setting did not persist"


def test_settings_show_the_live_min_stop(tmp_path):
    a = _agent(tmp_path)
    _run(a.handle("min stop ticks 9"))
    out = _run(a.handle("/settings"))
    assert "min stop 9 ticks" in out


def test_engine_planning_uses_the_setting(tmp_path):
    """The planning helpers take the state value, not the module constant."""
    e = TraderEngine(Settings())
    e.state.min_stop_ticks = 5
    assert e.state.min_stop_ticks == 5


# ------------------------------------------------------- the probe retry
_OK_BODY = json.dumps({"choices": [
    {"message": {"content": "ok"}, "finish_reason": "stop"}]})


class _FlakySession:
    """First probe: HTTP 500 empty-response. Second probe: works."""

    def __init__(self):
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.calls.append(json or {})

        class R:
            def __init__(self, s, b):
                self.status, self._b = s, b

            async def text(self):
                return self._b

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        if len(self.calls) == 1:
            return R(500, '{"error":"empty response content","success":false}')
        return R(200, _OK_BODY)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_a_reasoning_model_survives_the_tight_probe():
    """500 'empty response content' with 5 tokens -> retry with 200 -> ok."""
    s = _FlakySession()
    p = _run(probe_model(s, "https://api.test/v1", "key", "x/reasoning:free"))
    assert p.works, (p.status, p.error)
    assert len(s.calls) == 2
    assert s.calls[0]["max_tokens"] == 5
    assert s.calls[1]["max_tokens"] == 200


def test_a_genuinely_dead_model_is_probed_once():
    s = _FlakySession()

    def always_500(url, headers=None, json=None, timeout=None):
        s.calls.append(json or {})

        class R:
            status = 500

            async def text(self):
                return '{"error":"provider on fire"}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return R()

    s.post = always_500
    p = _run(probe_model(s, "https://api.test/v1", "key", "x/dead:free"))
    assert not p.works
    assert len(s.calls) == 1, "a non-empty-content failure must not be retried"


def test_a_200_with_empty_content_is_not_a_working_model():
    s = _FlakySession()

    def empty_200(url, headers=None, json=None, timeout=None):
        s.calls.append(json or {})

        class R:
            status = 200

            async def text(self):
                return ('{"choices":[{"message":{"content":""},'
                        '"finish_reason":"length"}]}')

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return R()

    s.post = empty_200
    p = _run(probe_model(s, "https://api.test/v1", "key", "x/thinking:free"))
    # both probes return an empty content - the model is NOT selected
    assert not p.works
    assert len(s.calls) == 2
