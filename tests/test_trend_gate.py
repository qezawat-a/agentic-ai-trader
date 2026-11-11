"""The TREND GATE: refuse a signal the biggest timeframe is fighting.

The operator's four losing trades were all the same trade: a LONG signal
fired while INITUSDT was dumping on the bigger picture, the engine bought,
the stop hit, the cooldown passed, it bought AGAIN at a worse price - four
times. The profitable trades all rode one clean trend the bigger picture
agreed with.

The gate does not demand agreement (that would block most entries); it
only refuses a signal the gate timeframe ACTIVELY FIGHTS: a LONG with
SHORT votes on the gate TF, or a SHORT with LONG votes there. Pinned:

1. a LONG with 15m SHORT votes is refused, with the refusing strategy
   named in the note,
2. the same aggregate enters when the gate is OFF,
3. no opposing votes on the gate TF -> the entry is not blocked,
4. the gate TF falls back to the biggest configured TF,
5. the setting persists and shows in /settings,
6. 'trend gate on|off|timeframe' all work.
"""
from __future__ import annotations

import asyncio

from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.trader.engine import TraderEngine
from jrock.trader.strategies.base import Signal


def _sig(strategy: str, direction: str, tf: str, conf: float = 80.0) -> Signal:
    return Signal(strategy=strategy, direction=direction, confidence=conf,
                  reason="test", timeframe=tf, symbol="INITUSDT")


def _agg(engine: TraderEngine, direction: str, votes) -> object:
    from jrock.trader.aggregator import Aggregate
    agg = Aggregate(symbol="INITUSDT")
    agg.direction = direction
    agg.agreeing = 3
    agg.opposing = 0
    if direction == "LONG":
        agg.votes_long, agg.votes_short = votes, []
    else:
        agg.votes_long, agg.votes_short = [], votes
    return agg


def _agent(tmp_path) -> JRockAgent:
    a = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    a.engine.data_dir = tmp_path
    return a


def _engine() -> TraderEngine:
    return TraderEngine(Settings())


def _run(coro):
    return asyncio.run(coro)


def _s(strategy: str, direction: str, tf: str, conf: float = 80.0):
    return _sig(strategy, direction, tf, conf)


class _RefusedContext:
    """try_enter that records it was called - the gate must stop before."""

    def __init__(self, e: TraderEngine):
        self.e = e
        self.entered = False

    async def __call__(self, agg):
        e = self.e
        refusal = e._trend_gate_blocks(agg)
        if refusal:
            e.state.notes.append(refusal)
            return {"ok": False, "reason": refusal}
        self.entered = True
        return {"ok": True}


# ------------------------------------------------------------------- gate
def test_a_long_fought_by_15m_shorts_is_refused():
    e = _engine()
    agg = _agg(e, "LONG", [_s("Ma200Cross", "LONG", "1m"),
                           _s("VegasTunnel", "LONG", "3m"),
                           _s("TemaCross", "LONG", "5m")])
    agg.votes_short = [_s("VwapBear", "SHORT", "15m", conf=70.0)]
    refusal = e._trend_gate_blocks(agg)
    assert refusal is not None
    assert "15m" in refusal and "SHORT" in refusal
    assert "VwapBear" in refusal


def test_a_short_fought_by_15m_longs_is_refused():
    e = _engine()
    agg = _agg(e, "SHORT", [_s("Breakout", "SHORT", "1m")])
    agg.votes_long = [_s("Trend", "LONG", "15m")]
    refusal = e._trend_gate_blocks(agg)
    assert refusal is not None and "LONG" in refusal


def test_gate_off_lets_the_same_trade_through():
    e = _engine()
    e.state.trend_gate = False
    agg = _agg(e, "LONG", [_s("A", "LONG", "1m")])
    agg.votes_short = [_s("B", "SHORT", "15m")]
    assert e._trend_gate_blocks(agg) is None


def test_no_opposing_votes_on_the_gate_tf_does_not_block():
    e = _engine()
    agg = _agg(e, "LONG", [_s("A", "LONG", "1m"), _s("B", "LONG", "15m")])
    agg.votes_short = [_s("C", "SHORT", "1m")]  # fights on a SMALL tf only
    assert e._trend_gate_blocks(agg) is None


def test_gate_timeframe_falls_back_to_the_biggest_configured():
    e = _engine()
    e.state.timeframes = ["1m", "3m", "5m"]
    e.state.trend_gate_timeframe = "1h"  # not scanned
    assert e._gate_timeframe() == "5m"
    agg = _agg(e, "LONG", [_s("A", "LONG", "1m")])
    agg.votes_short = [_s("B", "SHORT", "5m")]
    assert e._trend_gate_blocks(agg) is not None


def test_refusal_is_visible_in_the_notes(tmp_path):
    a = _agent(tmp_path)
    e = a.engine

    async def refused_entry(agg):
        refusal = e._trend_gate_blocks(agg)
        e.state.notes.append(refusal)
        return {"ok": False, "reason": refusal}

    e.try_enter = refused_entry
    agg = _agg(e, "LONG", [_s("A", "LONG", "1m")])
    agg.votes_short = [_s("B", "SHORT", "15m")]
    _run(e.try_enter(agg))
    assert any("trend gate" in n for n in e.state.notes)


# --------------------------------------------------------------- commands
def test_trend_gate_commands(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("trend gate off"))
    assert "Trend gate = OFF" in out
    assert not a.engine.state.trend_gate
    out = _run(a.handle("trend gate on"))
    assert "Trend gate = ON" in out
    out = _run(a.handle("trend gate timeframe 5m"))
    assert "5m" in out
    assert a.engine.state.trend_gate_timeframe == "5m"
    out = _run(a.handle("trend gate"))
    assert "ON" in out and "5m" in out


def test_trend_gate_persists(tmp_path):
    a = _agent(tmp_path)
    _run(a.handle("trend gate off"))
    b = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    b.engine.data_dir = tmp_path
    assert b.engine.state.trend_gate is False


def test_settings_show_the_gate(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("/settings"))
    assert "trend gate:" in out
    assert "trend gate timeframe:" in out
