"""The manage loop must be VISIBLE, and the trailing stop must never undo
the breakeven protection.

The operator's live failure, from their report paste:

    INITUSDT LONG entry=0.23528 sl=0.22693 be=moved trail=on@0.22693
    peak_roi=17.00%   (live ROI was 31%)

Three things were wrong at once:

1. peak_roi was FROZEN at 17% while the live ROI was 31% - the manage loop
   had not processed the position since the price was 0.2369. The report
   kept saying "breakeven armed, trailing armed - both always on" while
   nothing moved the stops. A dead loop must say so in every report.
2. sl=0.22693 sits UNDER the entry (0.23528) while be=moved - the trailing
   stop's first candidate (price - 1.5*ATR) can fall below the entry, and
   writing it UNDID the breakeven protection the position had earned.
3. the operator typed /exits to find out why and could not see the loop
   state anywhere.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from jrock.config import Settings
from jrock.trader.engine import TraderEngine, PositionState
from jrock.trader.indicators import Candle
from jrock.trader.tpsl import clamp_sl
from tests.test_tpsl_managed_path import PAIR_ROW, FakeClient


def _stub_big_atr(e: TraderEngine) -> None:
    """get_candles returning violent 5m candles: ATR ~ 10% of price, so the
    raw trailing candidate (price - 1.5*ATR) lands FAR below the entry."""
    candles = [Candle(time=i, open=0.005, high=0.0055, low=0.0045,
                      close=0.005, base_vol=1, quote_vol=1) for i in range(60)]

    async def big_candles(sym, tf, limit=300):
        return candles

    e.get_candles = big_candles  # type: ignore[method-assign]


def _engine(mark: float, seed: PositionState | None = None,
            **kw) -> tuple[TraderEngine, FakeClient]:
    s = Settings()
    s.market_data_source = "rest"
    s.breakeven_pct = 5.0          # breakeven must NOT fire in these tests
    s.trailing_trigger_roi_pct = 1.0  # trailing fires at +1% ROI
    e = TraderEngine(s)
    e.client = FakeClient(mark, **kw)
    if seed is not None:
        e.positions[seed.position_id] = seed
    return e, e.client


def _go(e: TraderEngine):
    async def run():
        await e.load_rules(["AKEUSDT"])
        acts = await e.manage_positions()
        return acts
    return asyncio.run(run())


def _seed(pid: str = "P1", **kw) -> PositionState:
    base = dict(position_id=pid, symbol="AKEUSDT", side="LONG",
                entry=0.005, qty=4000, leverage=10, sl=0.0, tp=0.0,
                liq=0.0046, breakeven_done=True)
    base.update(kw)
    return PositionState(**base)


# ----------------------------------------------------------------- floor
def test_trailing_never_falls_below_the_breakeven_entry_long():
    """be=moved, then the trail candidate lands far under the entry: the wire
    must carry the breakeven floor (the same stop the breakeven path writes),
    not the looser candidate."""
    e, client = _engine(0.00501, seed=_seed())  # +0.2% -> ROI +2% >= trigger
    _stub_big_atr(e)
    _go(e)
    assert client.modified, "trailing should have fired at +2% ROI"
    sent = float(client.modified[0]["slPrice"])
    floor = clamp_sl(side="LONG", sl=0.005, entry=0.005, mark=0.00501,
                     rules=e.rules["AKEUSDT"],
                     min_ticks=e.state.min_stop_ticks, liq=0.0046)
    assert sent == pytest.approx(floor), (
        f"the stop was sent below the breakeven floor: {sent} vs {floor}")


def test_trailing_never_rises_above_the_breakeven_entry_short():
    e, client = _engine(0.00499, seed=_seed(entry=0.005, side="SHORT"),
                        last=0.00499, side="SHORT")
    _stub_big_atr(e)
    acts = _go(e)
    assert client.modified, "trailing should have fired on the short"
    sent = float(client.modified[0]["slPrice"])
    floor = clamp_sl(side="SHORT", sl=0.005, entry=0.005, mark=0.00499,
                     rules=e.rules["AKEUSDT"],
                     min_ticks=e.state.min_stop_ticks, liq=0.0054)
    assert sent == pytest.approx(floor), (
        f"the short's stop was sent above the breakeven floor: {sent} vs {floor}")


def test_trailing_ratchet_still_ratchets_above_the_floor():
    """The floor must not freeze the ratchet: with the seeded trailing_stop
    already at the entry and the price far above it, the next candidate is
    above the entry and must be sent."""
    e, client = _engine(0.00530, seed=_seed(trailing_stop=0.005,
                                            trailing_active=True))
    _go(e)
    assert client.modified, "the ratchet must move the stop up"
    sent = float(client.modified[0]["slPrice"])
    assert sent > 0.005, "a profit-locking ratchet was frozen by the floor"
    assert sent >= 0.005


# -------------------------------------------------------------- heartbeat
def test_heartbeat_is_set_after_a_manage_cycle():
    e, _ = _engine(0.00501)
    assert e.state.manage_last_ok == 0.0
    _go(e)
    assert e.state.manage_last_ok > 0.0
    assert time.time() - e.state.manage_last_ok < 5


def test_heartbeat_survives_a_cycle_where_a_position_is_managed():
    e, _ = _engine(0.00501)
    _go(e)
    assert e.state.manage_last_ok > 0.0, (
        "a cycle that moved a stop is still a full cycle")


def _tpsl_block(e: TraderEngine) -> str:
    return "\n".join(asyncio.run(e._report_tpsl()))


def test_report_says_the_loop_is_running_after_a_cycle():
    e, _ = _engine(0.00501)
    _go(e)
    block = _tpsl_block(e)
    assert "management loop: running" in block
    assert "last cycle" in block


def test_report_says_not_running_before_any_cycle():
    """The report must not claim the exits are armed when the loop has
    never completed a cycle."""
    e, _ = _engine(0.00501)
    block = _tpsl_block(e)
    assert "management loop: NOT RUNNING" in block
    assert "engine on" in block


def test_report_says_off_when_mid_management_is_off():
    e, _ = _engine(0.00501)
    e.state.mid_management = False
    block = _tpsl_block(e)
    assert "management loop: OFF" in block
    assert "mid management on" in block


def test_report_says_stalled_when_the_heartbeat_is_old():
    e, _ = _engine(0.00501)
    e.state.manage_last_ok = time.time() - 400
    block = _tpsl_block(e)
    assert "management loop: STALLED" in block
    assert "engine off" in block


def test_exits_brief_carries_the_loop_state():
    e, _ = _engine(0.00501)
    e.state.manage_last_ok = time.time() - 10
    brief = e.exits_brief()
    assert "management loop: running" in brief
    e.state.manage_last_ok = 0.0
    assert "management loop: NOT RUNNING" in e.exits_brief()
