"""A free model that answers in imagined tool-call syntax still gets run.

The operator's report (the model is liquid/lfm-2.5-2.6b:free):

    Negar: <|tool_call_start|>[/-partial_ladder_off()]<|tool_call_end|>
    Negar: <|tool_call_start|>[/tpsl()]<|tool_call_end|>

The junk went to the Telegram chat VERBATIM - the agent printed the syntax
of a request it never executed, and the follow-up ("Is it done ?") got the
same junk again. Two failures pinned here:

1. a reply containing `<|tool_call_start|>[/cmd()]<|tool_call_end|>` (or a
   bare `[/cmd()]`) is executed through the real grammar - the operator
   gets the command's result, the junk is gone,
2. "Thank you if it's done then tell me to start" is NOT smalltalk: the
   old rule matched any message that started with "thank you" and answered
   with the welcome banner instead of the status the sentence asked for.
   Smalltalk now requires the message to be ONLY the greeting.
"""
from __future__ import annotations

import asyncio

from jrock.agent import JRockAgent
from jrock.config import Settings

WELCOME = "Hey - I'm J+Rock, your Bitunix futures agent."


def _agent(tmp_path=None) -> JRockAgent:
    a = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s",
                            api_key="mk", base_url="https://api.test/v1",
                            model="test/free-model"))
    if tmp_path is not None:
        a.engine.data_dir = tmp_path
    return a


def _stub_chat(a: JRockAgent, reply: str) -> None:
    """chat() completes with `reply` - the model produced it verbatim."""

    async def fake_post_chat(url, payload):
        return 200, "", {"choices": [{"message": {"content": reply}}]}

    a._post_chat = fake_post_chat


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- junk gets executed
def test_wrapped_ladder_call_is_executed(tmp_path):
    a = _agent(tmp_path)
    _stub_chat(a, "<|tool_call_start|>[/-partial_ladder_off()]<|tool_call_end|>")
    out = _run(a.chat("turn off the ladder"))
    assert "<|tool_call" not in out
    assert "PARTIAL ladder OFF" in out
    assert not a.engine.state.partial_tp_ladder


def test_wrapped_tpsl_call_shows_the_exits(tmp_path):
    a = _agent(tmp_path)
    _stub_chat(a, "<|tool_call_start|>[/tpsl()]<|tool_call_end|>")
    out = _run(a.chat("is it done?"))
    assert "<|tool_call" not in out
    assert "[/" not in out
    assert "the TP/SL methods" in out


def test_bare_bracket_call_without_the_tokens(tmp_path):
    a = _agent(tmp_path)
    _stub_chat(a, "[/status()]")
    out = _run(a.chat("status"))
    assert "[/" not in out
    assert "engine" in out.lower()


def test_prose_stays_around_the_executed_call(tmp_path):
    a = _agent(tmp_path)
    _stub_chat(a, "Sure - turning it off now.\n"
                  "<|tool_call_start|>[/-partial_ladder_off()]<|tool_call_end|>\n"
                  "Done.")
    out = _run(a.chat("turn the ladder off"))
    assert "turning it off now" in out
    assert "PARTIAL ladder OFF" in out
    assert "<|tool_call" not in out


def test_unrunnable_junk_does_not_loop_into_the_model(tmp_path):
    a = _agent(tmp_path)
    calls: list[str] = []

    def fake_post_chat(url, payload):
        async def post(url, payload):
            calls.append(url)
            return 200, "", {"choices": [{"message": {"content":
                "<|tool_call_start|>[/definitely_not_a_command()]<|tool_call_end|>"}}]}
        return post(url, payload)

    a._post_chat = fake_post_chat
    out = _run(a.chat("do the thing"))
    # exactly ONE model call - no recursion - and no junk in the answer
    assert len(calls) == 1
    assert "definitely_not_a_command" not in out
    assert "<|tool_call" not in out
    assert out


def test_normal_markdown_survives(tmp_path):
    a = _agent(tmp_path)
    _stub_chat(a, "See the docs [here](https://example.com) for details.")
    out = _run(a.chat("where are the docs"))
    assert "See the docs [here](https://example.com)" in out


def test_ladder_call_routes_through_the_confirm_free_path(tmp_path):
    """`partial ladder off` is not destructive - it must be APPLIED, not
    parked in a confirm prompt."""
    a = _agent(tmp_path)
    _stub_chat(a, "[/-partial_ladder_off()]")
    out = _run(a.chat("ladder off please"))
    assert not a.engine.state.partial_tp_ladder
    assert "confirm" not in out.lower()


# ---------------------------------------------------- smalltalk false hit
def test_thanks_with_a_request_is_not_smalltalk():
    a = _agent()
    out = _run(a.handle("Thank you if it's done then tell me to start"))
    assert WELCOME not in out


def test_thanks_with_a_question_is_not_smalltalk():
    a = _agent()
    out = _run(a.handle("Thank you, but is the ladder off?"))
    assert WELCOME not in out


def test_bare_thanks_is_still_smalltalk():
    a = _agent()
    assert WELCOME in _run(a.handle("thanks"))
    assert WELCOME in _run(a.handle("Thank you!"))
    assert WELCOME in _run(a.handle("are you there?"))
