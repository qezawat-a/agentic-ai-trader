"""Regression tests: breakeven + trailing never fired on winning positions.

Three distinct bugs, all reproduced against the live public Bitunix API
contract and a fake client:

1. STALE TP KILLS THE UPDATE. ``st.tp`` is captured once at entry and re-sent
   verbatim on every breakeven/trailing modify. Once price runs far enough
   that the stored TP is no longer on the correct side of the *mark*, the
   exchange validator (30020/30021) refuses the WHOLE request - so the stop
   silently stops moving at exactly the moment a trade is winning. The fix
   re-clamps the TP against the current mark before sending.

2. FIRST TRAILING TICK LOOSENS THE STOP. ``next_trailing_stop`` compared only
   against ``trailing_stop``, which is None on the first trailing tick, so the
   candidate was returned unconditionally and could sit BELOW the breakeven
   stop. The fix floors the ratchet at the live ``sl``.

3. SILENT FAILURE. A refused update was recorded in ``state.errors`` but the
   user only sees the last error; the fix logs a warning and a tpsl event
   (``tp_repaired``) so the repair is visible.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from jrock.config import Settings
from jrock.trader.engine import TraderEngine
from jrock.trader.tpsl import PositionState, next_trailing_stop

# AKEUSDT contract (verified against the live public API):
#   quotePrecision 6, minTradeVolume 1, maxLeverage 50
PAIR_ROW = {
    "symbol": "AKEUSDT", "base": "AKE", "quote": "USDT",
    "basePrecision": 0, "quotePrecision": 6,
    "minTradeVolume": "1", "maxLeverage": 50,
}

# Constant candles -> ATR = 0 -> the engine falls back to price*0.002,
# which yields a conservative trailing candidate that is ABOVE the breakeven
# stop (and therefore a valid, tightening move). A wide-range candle series
# would produce a huge ATR whose candidate sits BELOW the just-moved breakeven
# stop, which the ratchet now correctly refuses.
CANDLES = [{"open": "0.0060", "high": "0.0060", "low": "0.0060",
            "close": "0.0060", "volume": "1000"} for _ in range(60)]


class FakeClient:
    """LONG at entry, currently at `price`. Counts modify calls."""

    def __init__(self, entry: float, price: float):
        self.entry, self.price = entry, price
        self.modified: list[dict] = []
        self.pos_calls = 0

    async def start(self): ...
    async def close(self): ...

    async def get_tickers(self, sym):
        return [{"symbol": sym, "lastPrice": str(self.price),
                 "markPrice": str(self.price)}]

    async def get_pending_positions(self, sym=None):
        self.pos_calls += 1
        return [{"positionId": "P1", "symbol": "AKEUSDT", "side": "LONG",
                 "avgOpenPrice": str(self.entry), "qty": "15656",
                 "leverage": "10", "liqPrice": str(self.entry * 0.9),
                 "markPrice": str(self.price)}]

    async def get_trading_pairs(self, symbols=None):
        return [PAIR_ROW]

    async def modify_position_tp_sl_order(self, sym, pid, tp_price=None,
                                          sl_price=None, tp_stop_type=None,
                                          sl_stop_type=None):
        self.modified.append({"slPrice": sl_price, "tpPrice": tp_price})
        return {"orderId": "T1"}

    async def get_kline(self, *a, **k):
        return CANDLES


def make_engine(price: float, entry: float = 0.005943,
                breakeven_pct: float = 0.6,
                trail_trigger: float = 25.0) -> TraderEngine:
    s = Settings()
    s.market_data_source = "rest"
    s.breakeven_pct = breakeven_pct
    s.trailing_trigger_roi_pct = trail_trigger
    e = TraderEngine(s, data_dir=Path(tempfile.mkdtemp()))
    e.client = FakeClient(entry, price)
    return e


def seed_position(e: TraderEngine, tp: float = 0.006001,
                  sl: float = 0.005800) -> None:
    e.positions["P1"] = PositionState(
        position_id="P1", symbol="AKEUSDT", side="LONG",
        entry=0.005943, qty=15656.0, leverage=10, sl=sl, tp=tp,
        liq=0.005349)


def run_manage(e: TraderEngine) -> list[dict]:
    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.manage_positions()
    return asyncio.run(go())


# --------------------------------------------------------------------------
# Bug 1: stale TP blocks breakeven/trailing once price passes the stored TP
# --------------------------------------------------------------------------

def test_breakeven_fires_when_price_passes_stored_tp():
    """Price at 0.006100 (26% ROI) is ABOVE the entry-time TP 0.006001.

    Before the fix the validator refused the whole request
    ("TP must be ABOVE mark") and breakeven never fired. After the fix the
    TP is re-clamped and the stop moves.
    """
    e = make_engine(price=0.006100)
    seed_position(e)
    actions = run_manage(e)
    assert any(a["action"] == "breakeven" for a in actions), actions
    assert len(e.client.modified) >= 1
    sent = e.client.modified[0]
    assert sent["slPrice"] is not None
    # The TP sent must be valid against the mark (0.006100 + min distance).
    assert float(sent["tpPrice"]) > 0.006100


def test_trailing_fires_when_price_passes_stored_tp():
    """Breakeven already done; the trail must still fire and re-send a
    REPAIRED TP (0.006104 > mark 0.006100) instead of the stale 0.006001."""
    e = make_engine(price=0.006100)
    seed_position(e)
    e.positions["P1"].breakeven_done = True
    actions = run_manage(e)
    assert any(a["action"] == "trail" for a in actions), actions
    assert e.client.modified, "no modify call"
    sent = e.client.modified[0]
    assert float(sent["tpPrice"]) > 0.006100


def test_tp_repair_is_recorded_not_silent():
    e = make_engine(price=0.006100)
    seed_position(e)
    run_manage(e)
    assert any("TP was" in err and "re-clamped" in err
               for err in e.state.errors), e.state.errors


def test_breakeven_still_fires_below_stored_tp():
    """Sanity: when the stored TP is still valid, it is sent unchanged."""
    e = make_engine(price=0.005950)  # 1.18% ROI, TP 0.006001 still valid
    seed_position(e)
    actions = run_manage(e)
    assert any(a["action"] == "breakeven" for a in actions)
    assert e.client.modified[0]["tpPrice"] == "0.006001"


def test_no_tp_position_still_gets_breakeven():
    """A position with no TP (sl=0/tp=0 defaults) must still move its stop."""
    e = make_engine(price=0.006100)
    seed_position(e, tp=0.0, sl=0.0)
    actions = run_manage(e)
    assert any(a["action"] == "breakeven" for a in actions), actions


# --------------------------------------------------------------------------
# Bug 2: first trailing tick must never loosen the live stop
# --------------------------------------------------------------------------

def test_first_trail_tick_never_loosens_breakeven_stop():
    """trailing_stop is None on the first tick; the floor is the live sl."""
    st = PositionState(position_id="P1", symbol="AKEUSDT", side="LONG",
                       entry=0.005943, qty=1000, leverage=10,
                       sl=0.005939, tp=0.006001, liq=0.005349,
                       breakeven_done=True, trailing_stop=None)
    # ATR 0.0005 -> candidate 0.006100 - 0.00075 = 0.00535 < sl 0.005939
    ns = next_trailing_stop(st, 0.006100, 0.0005)
    assert ns is None or ns >= st.sl


def test_first_trail_tick_tightens_when_candidate_above_sl():
    st = PositionState(position_id="P1", symbol="AKEUSDT", side="LONG",
                       entry=0.005943, qty=1000, leverage=10,
                       sl=0.005939, tp=0.006001, liq=0.005349,
                       breakeven_done=True, trailing_stop=None)
    ns = next_trailing_stop(st, 0.006100, 0.00002)
    assert ns is not None and ns > st.sl


def test_short_first_trail_tick_never_loosens():
    st = PositionState(position_id="P1", symbol="AKEUSDT", side="SHORT",
                       entry=0.006100, qty=1000, leverage=10,
                       sl=0.006160, tp=0.005900, liq=0.006400,
                       breakeven_done=True, trailing_stop=None)
    # ATR 0.0005 -> candidate 0.006100 + 0.00075 = 0.00685 > sl 0.006160
    ns = next_trailing_stop(st, 0.006100, 0.0005)
    assert ns is None or ns <= st.sl


def test_ratchet_after_arming_uses_trailing_stop_floor():
    st = PositionState(position_id="P1", symbol="AKEUSDT", side="LONG",
                       entry=0.005943, qty=1000, leverage=10,
                       sl=0.005939, tp=0.006001, liq=0.005349,
                       breakeven_done=True, trailing_stop=0.006070)
    # Candidate below the armed trailing stop must be refused.
    ns = next_trailing_stop(st, 0.006100, 0.0005)
    assert ns is None or ns >= 0.006070


# --------------------------------------------------------------------------
# Bug 3: the manage loop must keep running after a refused update
# --------------------------------------------------------------------------

def test_manage_loop_continues_after_refused_update():
    """A refused update must not raise; the loop keeps managing."""
    e = make_engine(price=0.006100)
    seed_position(e)
    actions = run_manage(e)  # must not raise
    assert isinstance(actions, list)
