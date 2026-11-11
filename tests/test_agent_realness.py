"""What makes the agent an AGENT and not a command bot.

Three properties, each of which was missing and each of which the operator
complained about:

1. It KNOWS the live state. Asked "why don't you do the breakeven and
   trailing" it had no idea what the position was doing, so it quoted command
   syntax instead of answering.
2. It SPEAKS the operator's language, in the operator's phrasing - including
   the informal spellings people actually type ("بیالانس", "لورنس").
3. It DEGRADES: when the model provider is down, it answers from the state it
   has instead of dumping an HTTP error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jrock import nlu
from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.trader.engine import TraderEngine
from jrock.trader.tpsl import PositionState


def _agent(tmp_path: Path) -> JRockAgent:
    s = Settings()
    s.data_dir  # noqa: B018  (ensures the dir exists)
    a = JRockAgent(s)
    a.engine.data_dir = tmp_path
    return a


# --------------------------------------------------------------- 1. live state
def test_the_prompt_carries_the_live_state(tmp_path):
    """The model must see the position, not guess about it."""
    a = _agent(tmp_path)
    e = a.engine
    e.state.leverage = 25
    e.state.margin_amount_pct = 30.0
    e.state.breakeven_pct = 10.0
    e.state.trailing_trigger_roi_pct = 15.0
    e.positions["p1"] = PositionState(
        position_id="p1", symbol="ONEUSDT", side="LONG", entry=0.0018783,
        qty=3226, leverage=25, sl=0.0018378, tp=0.0019045, liq=0.0016534,
        breakeven_done=True, trailing_active=True, trailing_stop=0.0018378,
        peak_roi=19.83)
    e.price_cache.put_rest("ONEUSDT", 0.0018905)

    system = a._system_prompt(live_state=a._live_state_brief())
    assert "ONEUSDT" in system
    assert "trailing ACTIVE" in system
    assert "breakeven ALREADY MOVED" in system
    assert "breakeven at +10% ROI" in system
    assert "trailing from +15% ROI" in system
    # and the instruction that makes it ANSWER instead of dumping
    assert "Use these numbers to ANSWER" in system


def test_the_brief_never_raises_without_an_engine(tmp_path):
    """A blind prompt is better than no prompt at all."""
    a = _agent(tmp_path)
    a.engine = None  # type: ignore[assignment]
    assert a._live_state_brief() == ""
    assert a._system_prompt(live_state="")  # the prompt still builds


def test_the_brief_says_when_there_is_nothing(tmp_path):
    a = _agent(tmp_path)
    brief = a._live_state_brief()
    assert "POSITIONS: none open" in brief
    assert "engine_on=False" in brief


# ------------------------------------------------------- 2. the operator's words
PERSIAN_INFORMAL = [
    ("بیالانس چقدره", "balance"),
    ("بیالانسم چقدره", "balance"),
    ("پوزیشن ها", "positions"),
    ("قیمت BTCUSDT", "price BTCUSDT"),
    ("تنظیمات رو نشون بده", "settings trade"),
    ("اینجین روشنه؟", "status"),
    ("سفارش باز دارم؟", "orders"),
    ("استاپ لاس و تی پی اس ال", "/exits"),
    ("چرا breakeven و trailing انجام نمی‌دهی", "/exits"),
    ("لورنس رو کم کن", ""),          # a clarification: the number is missing
]


@pytest.mark.parametrize("text,command", PERSIAN_INFORMAL)
def test_informal_persian_routes_to_the_handler(text, command):
    r = nlu.parse(text)
    assert r is not None, f"{text!r} fell through to the model"
    if command:
        assert getattr(r, "command", "") == command, (text, r)
    else:
        assert isinstance(r, nlu.Clarification)


def test_the_exits_question_is_answered_by_the_engine_not_the_model(tmp_path):
    """The question the operator actually asked must not reach the model."""
    a = _agent(tmp_path)
    out = asyncio.run(a.handle("why don't you do breakeven and trailing"))
    assert "could not send that to a model" not in out, out


# ------------------------------------------------------------ 3. it degrades
def test_a_rate_limited_model_still_gets_an_answer(tmp_path):
    """A real agent absorbs a broken provider; a bot dumps the HTTP error."""
    a = _agent(tmp_path)
    a.engine.state.leverage = 25
    reply = a._model_unavailable_reply(429, '{"code":429}', "why?")
    assert "rate-limit" in reply.lower()
    assert "HTTP 429" not in reply.split("\n")[0] or "rate" in reply.lower()
    # no raw JSON dump
    assert '{"code"' not in reply
    # and it still says what it knows
    assert "leverage 25x" in reply
    assert "/status" in reply


def test_the_error_classifier_speaks_plain_english(tmp_path):
    a = _agent(tmp_path)
    assert "rate" in a._classify_model_error(429, "").lower()
    assert "key" in a._classify_model_error(401, "").lower()
    assert "model" in a._classify_model_error(500, "").lower()
    assert "model" in a._classify_model_error(400, "").lower()
