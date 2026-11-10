"""Regression: a TP ladder must produce N staged exits, not one.

The defect this pins down
-------------------------
`plan_tpsl()` knows nothing about the ladder. It picks a single take-profit at
~1.8R from the signal strength, and `open_position` attached it to the entry
order as `tpPrice`. The position TP/SL endpoint has NO quantity field, so that
is a WHOLE-POSITION take-profit. It lands just above the first rung, so the
moment the price touches it the entire remainder is flattened and rungs 2..N
never fire - the ladder silently degenerates into a single staged exit.

These tests assert the contract the feature promises:
  * the entry order carries NO whole-position TP while a ladder is active,
  * each rung closes exactly its own share of the ORIGINAL size,
  * the rung quantities sum to the ladder's total percentage,
  * no rung ever closes more than the position still holds.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jrock.config import Settings
from jrock.store import Store
from jrock.trader.aggregator import Aggregate
from jrock.trader.engine import TraderEngine
from jrock.trader.ladder import level_qtys, parse_ladder, trigger_price_for
from jrock.trader.risk import PairRules
from jrock.trader.tpsl import PositionState

SYM = "AKEUSDT"
ENTRY = 0.005
RISK = 0.0002          # initial risk per unit -> 1R = 0.0052
QTY = 1000.0
SPEC = "1R:25,2R:25,3R:25"


class FakeClient:
    """Records every call; never touches the network."""

    def __init__(self, positions, price, mark):
        self._positions, self._price, self._mark = positions, price, mark
        self.calls: list[tuple[str, dict]] = []

    async def get_pending_positions(self, symbol=None):
        return self._positions

    async def get_tickers(self, symbol=None):
        return [{"symbol": symbol, "lastPrice": str(self._price),
                 "markPrice": str(self._mark)}]

    async def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        # A real exchange reports the new position on the next read; the engine
        # registers PositionState from that read, so the fake must do the same.
        if not self._positions:
            self._positions = [{
                "positionId": "P1", "symbol": kw.get("symbol", SYM),
                "side": "LONG", "qty": str(kw.get("qty", QTY)),
                "avgOpenPrice": str(self._price), "leverage": "20",
                "liqPrice": "0.0047", "unrealizedPNL": "0.0"}]
        return {"orderId": "O%d" % len(self.calls)}

    async def modify_position_tp_sl_order(self, *a, **kw):
        self.calls.append(("modify_position_tp_sl_order", {"args": a, **kw}))
        return {"orderId": "M%d" % len(self.calls)}

    async def flash_close_position(self, pid):
        self.calls.append(("flash_close_position", {"positionId": pid}))
        return {"ok": True}

    async def close_all_position(self, symbol=None):
        self.calls.append(("close_all_position", {"symbol": symbol}))
        return {"ok": True}

    async def get_single_account(self):
        return [{"available": "1000"}]

    async def verify_credentials(self):
        return True, "fake"

    async def change_leverage(self, *a, **kw):
        self.calls.append(("change_leverage", {"args": a, **kw}))
        return {"ok": True}

    async def change_margin_mode(self, *a, **kw):
        self.calls.append(("change_margin_mode", {"args": a, **kw}))
        return {"ok": True}

    async def get_position_tiers(self, symbol=None):
        return [{"level": 1, "startValue": "0", "endValue": "999999999",
                 "leverage": 125, "maintenanceMarginRate": "0.004"}]

    async def get_pending_tp_sl_order(self, *a, **kw):
        return {"orderList": []}


def _engine(tmp: Path, spec: str = SPEC) -> TraderEngine:
    s = Settings(symbols=[SYM], leverage=20, risk_pct=1.0,
                 tp_ladder=spec, tp_ladder_trigger_basis="R",
                 tp_ladder_breakeven_after=1, tp_ladder_runner="trail")
    eng = TraderEngine(s, data_dir=tmp, store=Store(path=tmp / "state.json"))
    eng.rules[SYM] = PairRules(symbol=SYM, min_trade_volume=1.0,
                               base_precision=0, quote_precision=6,
                               max_leverage=50)
    return eng


def _pos(eng, qty=QTY, fired=0):
    eng.positions["P1"] = PositionState(
        position_id="P1", symbol=SYM, side="LONG", entry=ENTRY,
        qty=qty, leverage=20, sl=ENTRY - RISK, tp=0.0, liq=0.0047,
        original_qty=QTY, fired_levels=fired, initial_risk=RISK)


def _client(eng, price, qty=QTY, positions=None):
    if positions is None:
        positions = [{"positionId": "P1", "symbol": SYM, "side": "LONG",
                      "qty": str(qty), "avgOpenPrice": str(ENTRY),
                      "leverage": "20", "liqPrice": "0.0047",
                      "unrealizedPNL": "1.0"}]
    eng.client = FakeClient(positions, price, price)


def _agg(direction: str = "LONG") -> Aggregate:
    """A minimal actionable aggregate - enough for try_enter to proceed."""
    return Aggregate(symbol=SYM, direction=direction, confidence=80.0,
                     agreeing=4, opposing=0, strength="normal")



# --------------------------------------------------------------- the contract


def test_rung_quantities_sum_to_the_ladder_total():
    """Each rung is a share of the ORIGINAL size and they add up."""
    lad = parse_ladder(SPEC, basis="R")
    qtys = level_qtys(QTY, lad, 0)
    assert qtys == [250.0, 250.0, 250.0]
    assert sum(qtys) == pytest.approx(QTY * lad.total_pct / 100.0)
    assert sum(qtys) == 750.0          # 25% runner left over


def test_each_rung_closes_only_its_own_share(tmp_path):
    """Three rungs at three prices -> three slices, never the whole position."""
    eng = _engine(tmp_path)
    _pos(eng)
    lad = parse_ladder(SPEC, basis="R")
    triggers = [trigger_price_for("LONG", lv, basis="R", entry=ENTRY,
                                  sl=ENTRY - RISK, leverage=20,
                                  initial_risk=RISK)
                for lv in lad.levels]
    assert triggers == pytest.approx([0.0052, 0.0054, 0.0056])

    closed: list[float] = []
    for px in triggers:
        _client(eng, px, qty=eng.positions["P1"].qty)
        actions = asyncio.run(eng.manage_positions())
        fired = [a for a in actions if a["action"] == "ladder_level"]
        assert len(fired) == 1, f"expected exactly one rung at {px}"
        closed.append(fired[0]["qty"])

    assert closed == [250.0, 250.0, 250.0]
    assert sum(closed) == 750.0
    assert eng.positions["P1"].fired_levels == 3
    # 25% runner is still open - the ladder must NOT have flattened it.
    assert eng.positions["P1"].qty == pytest.approx(250.0)


def test_no_rung_ever_closes_more_than_is_open(tmp_path):
    """A rung's slice can never exceed the live position size."""
    eng = _engine(tmp_path)
    _pos(eng)
    lad = parse_ladder(SPEC, basis="R")
    for lv in lad.levels:
        px = trigger_price_for("LONG", lv, basis="R", entry=ENTRY,
                               sl=ENTRY - RISK, leverage=20, initial_risk=RISK)
        live = eng.positions["P1"].qty
        _client(eng, px, qty=live)
        asyncio.run(eng.manage_positions())
        assert eng.positions["P1"].qty >= 0.0


def test_entry_order_carries_no_whole_position_tp_when_ladder_is_active(tmp_path):
    """The core regression: no whole-position TP on the entry order.

    A `tpPrice` on the entry order becomes a position-level TP that closes
    EVERYTHING, which is what made rung 1 look like it closed the whole
    position.
    """
    eng = _engine(tmp_path)
    _client(eng, ENTRY, positions=[])
    res = asyncio.run(eng.try_enter(_agg("LONG")))
    assert res.get("ok") is True, res

    opens = [c for c in eng.client.calls if c[0] == "place_order"]
    assert len(opens) == 1
    body = opens[0][1]
    assert "tpPrice" not in body, (
        "a ladder is active, so the entry order must not carry a "
        f"whole-position TP (got tpPrice={body.get('tpPrice')!r})")
    # the stop is still attached - the ladder owns exits, not protection
    assert "slPrice" in body
    # and the position state must not remember a TP either
    st = eng.positions["P1"]
    assert st.tp == 0.0


def test_entry_order_keeps_its_tp_when_no_ladder_is_configured(tmp_path):
    """Without a ladder the single-level TP behaviour is unchanged."""
    eng = _engine(tmp_path, spec="")
    _client(eng, ENTRY, positions=[])
    res = asyncio.run(eng.try_enter(_agg("LONG")))
    assert res.get("ok") is True, res
    opens = [c for c in eng.client.calls if c[0] == "place_order"]
    assert len(opens) == 1
    assert "tpPrice" in opens[0][1]
    assert eng.positions["P1"].tp > 0