"""The exchange's stop is the truth: read it back before acting on ours.

Live failure, from the operator's paste:

    app:   CPUSDT  Last <= 0.00643  qty 311  Partial TP/SL   (set by hand)
    report: trail-on@0.00639  peak_roi=43.49%

`st.sl` was only ever WRITTEN by the engine - at entry, by the breakeven,
by the trailing ratchet - and never read back from the exchange. So:

  * the report quoted a stop that was not on the exchange,
  * the next breakeven/trailing pass silently put the engine's own value
    back, undoing the operator's edit,
  * `next_trailing_stop` ratchets against `st.trailing_stop` alone, so it
    could not see the manual tighten either.

These tests pin the read-back (`get_pending_tp_sl_order` -> `slPrice`), its
adoption into the report and into the ratchet, and the two ways it must
refuse to guess: a stop this engine just wrote (not an edit) and two rows
that disagree (no single "the stop").
"""
from __future__ import annotations

import asyncio
import time

import pytest

from jrock.config import Settings
from jrock.trader.engine import PositionState, TraderEngine
from tests.test_tpsl_managed_path import PAIR_ROW, FakeClient


class SlFakeClient(FakeClient):
    """FakeClient that also answers the pending-TP/SL read."""

    def __init__(self, mark: float, sl_rows: list[dict] | None = None, **kw):
        super().__init__(mark, **kw)
        self.sl_rows = list(sl_rows or [])
        self.pending_sl_calls = 0

    async def get_pending_tp_sl_order(self, symbol=None, position_id=None,
                                      **kw) -> list[dict]:
        self.pending_sl_calls += 1
        return list(self.sl_rows)


def _engine(mark: float, seed: PositionState | None = None,
            sl_rows: list[dict] | None = None, tmp_path=None,
            readback: bool = True, **kw) -> tuple[TraderEngine, FakeClient]:
    s = Settings()
    s.market_data_source = "rest"
    s.breakeven_pct = 1.0
    s.trailing_trigger_roi_pct = 100.0   # trailing OFF unless a test arms it
    e = TraderEngine(s, data_dir=tmp_path)
    # `readback=False` hands back a client that does not implement the
    # pending-TP/SL read at all - the "old client" degradation path.
    e.client = (SlFakeClient(mark, sl_rows=sl_rows, **kw) if readback
                else FakeClient(mark, **kw))
    if seed is not None:
        e.positions[seed.position_id] = seed
    return e, e.client


def _seed(pid: str = "P1", **kw) -> PositionState:
    base = dict(position_id=pid, symbol="AKEUSDT", side="LONG",
                entry=0.005, qty=4000, leverage=10, sl=0.00490, tp=0.0,
                liq=0.0046, breakeven_done=True)
    base.update(kw)
    return PositionState(**base)


def _run(coro):
    return asyncio.run(coro)


def _manage(e: TraderEngine):
    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.manage_positions()
    return asyncio.run(go())


def _row(pid: str, sl: str) -> dict:
    return {"positionId": pid, "symbol": "AKEUSDT", "slPrice": sl,
            "slStopType": "LAST_PRICE", "slQty": "4000"}


# ------------------------------------------------------------- the report
def test_report_quotes_the_stop_the_exchange_holds(tmp_path):
    """The operator compares the report with the app - they must agree."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490),
                   sl_rows=[_row("P1", "0.005020")], tmp_path=tmp_path)
    out = _run(e.report())
    assert e.positions["P1"].sl == pytest.approx(0.005020)
    assert "sl=0.00502" in out, out
    # the ratchet starts from the operator's stop too, not the stale one
    assert e.positions["P1"].trailing_stop == pytest.approx(0.005020)


def test_report_readback_failure_keeps_the_report_alive(tmp_path):
    """A read that fails degrades to the engine's own bookkeeping."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490), tmp_path=tmp_path,
                   readback=False)
    out = _run(e.report())
    assert "sl=0.0049" in out
    assert e.positions["P1"].sl == pytest.approx(0.00490)


def test_readback_without_the_method_answers_empty(tmp_path):
    e, _ = _engine(0.00530, tmp_path=tmp_path, readback=False)
    assert _run(e._exchange_sl_map()) == {}


# ------------------------------------------------- acting on the real stop
def test_manage_adopts_the_manual_stop_before_touching_it(tmp_path):
    """The manage loop reads the exchange BEFORE it decides to move a stop."""
    e, client = _engine(0.00530, seed=_seed(sl=0.00490, breakeven_done=True),
                        sl_rows=[_row("P1", "0.005020")], tmp_path=tmp_path)
    _manage(e)
    assert client.pending_sl_calls >= 1
    assert e.positions["P1"].sl == pytest.approx(0.005020)


def test_a_manually_tightened_stop_is_never_widened(tmp_path):
    """The operator tightens to 0.00529 by hand; the trail must not send
    its own looser value back over it on the next pass."""
    e, client = _engine(0.00530, seed=_seed(sl=0.00490, breakeven_done=True,
                                            trailing_active=True,
                                            trailing_stop=0.00490),
                        sl_rows=[_row("P1", "0.005290")], tmp_path=tmp_path)
    e.state.trailing_trigger_roi_pct = 1.0      # trailing armed at +1% ROI
    _manage(e)
    assert client.modified == [], (
        f"the engine widened the operator's stop: {client.modified}")
    assert e.positions["P1"].sl == pytest.approx(0.005290)
    assert e.positions["P1"].trailing_stop == pytest.approx(0.005290)


def test_breakeven_does_not_undo_a_manual_tighten(tmp_path):
    """ROI passes the breakeven threshold while the operator has already
    put the stop PAST breakeven by hand: writing `entry` would loosen it."""
    e, client = _engine(0.005062, seed=_seed(sl=0.00490, breakeven_done=False),
                        sl_rows=[_row("P1", "0.005040")], tmp_path=tmp_path)
    _manage(e)
    assert client.modified == [], (
        f"breakeven widened the stop the operator had tightened: "
        f"{client.modified}")
    st = e.positions["P1"]
    assert st.breakeven_done, "the protection is in place - credit it"
    assert st.sl == pytest.approx(0.005040)


def test_nothing_is_adopted_when_the_stop_has_not_changed(tmp_path):
    """The common case: exchange and engine agree - no log noise, no write."""
    e, client = _engine(0.00530, seed=_seed(sl=0.00490),
                        sl_rows=[_row("P1", "0.004900")], tmp_path=tmp_path)
    _manage(e)
    assert e.positions["P1"].sl == pytest.approx(0.00490)
    assert client.modified == []
    recs = [r for r in e.tpsl_history(50) if r.get("event") == "sl_reconciled"]
    assert recs == []


# --------------------------------------------------------- refusing to guess
def test_a_fresh_engine_write_is_not_read_back_as_an_edit(tmp_path):
    """The engine just told the exchange to use 0.005010; the echo of that
    write is not an operator edit and must not be adopted over it."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.005010),
                   sl_rows=[_row("P1", "0.004900")], tmp_path=tmp_path)
    e._sl_writes["P1"] = time.time()
    _manage(e)
    assert e.positions["P1"].sl == pytest.approx(0.005010)


def test_conflicting_stop_rows_are_not_guessed(tmp_path):
    """Two pending rows disagree about the stop: there is no single value
    to adopt, so the engine keeps its own bookkeeping instead of picking."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490),
                   sl_rows=[_row("P1", "0.005040"), _row("P1", "0.005010")],
                   tmp_path=tmp_path)
    _run(e._read_back_sl())
    assert e.positions["P1"].sl == pytest.approx(0.00490)


def test_rows_for_another_position_are_ignored(tmp_path):
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490),
                   sl_rows=[_row("P9", "0.005040")], tmp_path=tmp_path)
    _run(e._read_back_sl())
    assert e.positions["P1"].sl == pytest.approx(0.00490)


def test_tp_only_rungs_carry_no_stop_and_are_skipped(tmp_path):
    """The PARTIAL rungs are TP-only: a row without slPrice is not a stop."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490),
                   sl_rows=[{"positionId": "P1", "symbol": "AKEUSDT",
                             "tpPrice": "0.005500", "slPrice": "0"}],
                   tmp_path=tmp_path)
    _run(e._read_back_sl())
    assert e.positions["P1"].sl == pytest.approx(0.00490)


def test_trading_pair_rules_still_load_for_the_manage_pass(tmp_path):
    """The read-back must not bypass the rules the manage pass needs."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490),
                   sl_rows=[_row("P1", "0.005020")], tmp_path=tmp_path)
    _manage(e)
    assert "AKEUSDT" in e.rules
    assert e.rules["AKEUSDT"].quote_precision == PAIR_ROW["quotePrecision"]
