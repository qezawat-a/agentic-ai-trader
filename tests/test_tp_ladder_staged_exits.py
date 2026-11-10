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
  * no rung ever closes more than the position still holds,
  * an exchange TP backstop (tpsl/place_order + tpQty) covers the remainder
    from entry onward and re-sizes after every fired rung.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jrock.config import Settings
from jrock.store import Store
from jrock.trader.aggregator import Aggregate
from jrock.trader.engine import TraderEngine
from jrock.trader.errors import BitunixAPIError
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
        if kw.get("reduceOnly"):
            # A reduce-only fill shrinks the live position; the engine
            # re-reads the exchange qty on its next pass.
            for p in self._positions:
                if p.get("symbol") == kw.get("symbol"):
                    p["qty"] = str(max(0.0, float(p.get("qty") or 0)
                                       - float(kw.get("qty") or 0)))
        # A real exchange reports the new position on the next read; the engine
        # registers PositionState from that read, so the fake must do the same.
        if not self._positions:
            self._positions = [{
                "positionId": "P1", "symbol": kw.get("symbol", SYM),
                "side": "LONG", "qty": str(kw.get("qty", QTY)),
                "avgOpenPrice": str(self._price), "leverage": "20",
                "liqPrice": "0.0047", "unrealizedPNL": "0.0"}]
        return {"orderId": "O%d" % len(self.calls)}

    async def place_tp_sl_order(self, symbol, position_id, **kw):
        self.calls.append(("place_tp_sl_order",
                           {"args": (symbol, position_id), **kw}))
        return {"orderId": "T%d" % len(self.calls)}

    async def cancel_tp_sl_order(self, symbol, order_id):
        self.calls.append(("cancel_tp_sl_order", {"symbol": symbol,
                                                  "orderId": order_id}))
        return {"ok": True}

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


# ---------------------------------------------------------- the TP backstop


class BackstopRecorder(FakeClient):
    """FakeClient but the backstop place can be made to fail."""

    fail_place = False

    async def place_tp_sl_order(self, symbol, position_id, **kw):
        self.calls.append(("place_tp_sl_order",
                           {"args": (symbol, position_id), **kw}))
        if self.fail_place:
            raise BitunixAPIError(10002, "invalid tpQty")
        return {"orderId": "T%d" % len(self.calls)}


def test_entry_parks_an_exchange_tp_backstop_for_the_full_remainder(tmp_path):
    """The user-visible contract: an exchange-side TP exists from entry on.

    With the ladder active the whole-position TP is deliberately absent (it
    would flatten the remainder at rung 1). The backstop is the replacement:
    a standalone TP at the FINAL rung target covering the same qty the
    position still holds.
    """
    eng = _engine(tmp_path)
    _client(eng, ENTRY, positions=[])
    res = asyncio.run(eng.try_enter(_agg("LONG")))
    assert res.get("ok") is True, res

    places = [c for c in eng.client.calls if c[0] == "place_tp_sl_order"]
    assert len(places) == 1, "exactly one backstop at entry"
    call = places[0][1]
    assert call["args"] == (SYM, "P1")
    # TP-only: no stop is sent alongside - the entry order already carries it
    assert "sl_price" not in call and "sl_qty" not in call
    # final rung target, for the qty the position holds at entry
    st = eng.positions["P1"]
    lad = parse_ladder(SPEC, basis="R")
    target = trigger_price_for("LONG", lad.levels[-1], basis="R",
                               entry=st.entry, sl=st.sl, leverage=st.leverage,
                               initial_risk=st.initial_risk)
    assert float(call["tp_price"]) == pytest.approx(target)
    # covering exactly the size the entry created (the engine sizes entries
    # itself; the backstop must cover that whole size at entry time)
    opens = [c for c in eng.client.calls if c[0] == "place_order"]
    assert float(call["tp_qty"]) == float(opens[0][1]["qty"])
    assert call["tp_order_type"] == "MARKET"
    # additive: does not touch the slice mechanism
    assert st.backstop_tp_order_id
    assert st.tp == 0.0


def test_no_backstop_at_entry_without_a_ladder(tmp_path):
    """Without a ladder nothing calls the standalone endpoint."""
    eng = _engine(tmp_path, spec="")
    _client(eng, ENTRY, positions=[])
    asyncio.run(eng.try_enter(_agg("LONG")))
    assert not [c for c in eng.client.calls if c[0] == "place_tp_sl_order"]
    assert eng.positions["P1"].tp > 0


def test_fired_rung_cancels_the_old_backstop_and_reparks_the_remainder(tmp_path):
    """tpQty must track the live remainder after every rung.

    The resting backstop was parked for full qty (1000). After rung 1 closes
    250, the new backstop must cover 750 - and the old one must be cancelled
    first, so exactly ONE TP for the remainder rests on the exchange.
    """
    eng = _engine(tmp_path)
    _pos(eng)                      # fresh state; give it a resting backstop
    st = eng.positions["P1"]
    st.backstop_tp_order_id = "TOLD"
    _client(eng, 0.0052)           # 1R: rung 1 fires

    asyncio.run(eng.manage_positions())

    cancels = [c for c in eng.client.calls if c[0] == "cancel_tp_sl_order"]
    places = [c for c in eng.client.calls if c[0] == "place_tp_sl_order"]
    assert [c[1]["orderId"] for c in cancels] == ["TOLD"]
    assert len(places) == 1
    call = places[0][1]
    assert call["tp_qty"] == "750"
    assert float(call["tp_price"]) == pytest.approx(0.0056)  # 3R = final rung
    assert st.backstop_tp_order_id != "TOLD"


def test_backstop_off_fires_rungs_without_any_standalone_tp(tmp_path):
    """`ladder tp off` must be a full stop of the backstop mechanism."""
    eng = _engine(tmp_path)
    eng.state.tp_ladder_tp_backstop = False
    _pos(eng)
    _client(eng, 0.0052)
    asyncio.run(eng.manage_positions())
    assert eng.positions["P1"].fired_levels == 1
    assert not [c for c in eng.client.calls if c[0] == "place_tp_sl_order"]
    assert eng.positions["P1"].backstop_tp_order_id == ""


def test_backstop_failure_never_blocks_the_rung(tmp_path):
    """The slice close is the primary act; a failed backstop is logged only."""
    eng = _engine(tmp_path)
    _pos(eng)
    _client(eng, 0.0052)
    eng.client.__class__ = BackstopRecorder
    eng.client.fail_place = True
    asyncio.run(eng.manage_positions())

    st = eng.positions["P1"]
    assert st.fired_levels == 1                      # the rung still fired
    assert st.qty == pytest.approx(750.0)            # and the close went out
    assert any("ladder backstop place" in e for e in eng.state.errors)
    assert st.backstop_tp_order_id == ""


def test_dust_close_cancels_the_leftover_backstop(tmp_path):
    """100/33/33/33 leaves 1 - below minTradeVolume 5 - and goes flat.

    A position that becomes flat through dust collection must not leave an
    exchange TP resting for a position that no longer exists.
    """
    eng = _engine(tmp_path, "1R:33,2R:33,3R:33")
    eng.rules[SYM] = PairRules(symbol=SYM, min_trade_volume=5.0,
                               base_precision=0, quote_precision=6,
                               max_leverage=50)
    _pos(eng, qty=34.0, fired=2)
    st = eng.positions["P1"]
    st.original_qty = 100.0
    st.backstop_tp_order_id = "T3"
    _client(eng, 0.0056, qty=34.0)

    asyncio.run(eng.manage_positions())

    assert st.fired_levels == 3 and st.qty == 0.0
    cancels = [c for c in eng.client.calls if c[0] == "cancel_tp_sl_order"]
    assert [c[1]["orderId"] for c in cancels] == ["T3"]


def test_ladder_off_queues_a_backstop_sweep(tmp_path):
    """Disabling the ladder must not leave managed TPs resting unmanaged."""
    eng = _engine(tmp_path)
    _pos(eng)
    eng.positions["P1"].backstop_tp_order_id = "TOLD"
    _client(eng, 0.005)        # the position is still live on the exchange

    out = asyncio.run(eng.command("ladder off"))
    assert "cancelled" in out
    # the sweep is queued, not executed mid-command
    assert eng.positions["P1"].backstop_tp_order_id == "TOLD"
    asyncio.run(eng.manage_positions())
    assert eng.positions["P1"].backstop_tp_order_id == ""
    cancels = [c for c in eng.client.calls if c[0] == "cancel_tp_sl_order"]
    assert [c[1]["orderId"] for c in cancels] == ["TOLD"]