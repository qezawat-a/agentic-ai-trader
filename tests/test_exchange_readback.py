"""Everything the engine shows or acts on must come from the EXCHANGE.

The stop-loss read-back (v0.4.4) fixed one field. This file pins the rest of
the class:

  * the private WS feed is opened even when MARKET_DATA_SOURCE=rest - it is
    the account stream, not market data - and its pushes DRIVE engine state
    the moment they arrive (before this, the frames were only counted),
  * the take-profit is read back too: `st.tp` was written at entry and never
    refreshed, so the next breakeven/trailing modify overwrote a TP the
    operator had moved by hand,
  * the report shows the rungs the EXCHANGE holds, not the engine's log of
    what it once placed (a cancelled rung used to keep reporting as working),
  * entry / liquidation price / leverage are reconciled from the position
    row on every pass instead of being copied once at open,
  * a position closed by the exchange (WS position CLOSE) is dropped with
    the same cooldown + log line as the REST reconcile.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from jrock.config import Settings
from jrock.trader.engine import PositionState, TraderEngine
from tests.test_tpsl_managed_path import PAIR_ROW, FakeClient


# --------------------------------------------------------------- fakes
class FakePrivateWS:
    """Just enough of BitunixWebSocket for the engine wiring."""

    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.logged_in = True
        self.connected = True

    def on(self, ch, handler):
        self.handlers.setdefault(ch, []).append(handler)

    def push(self, ch, data):
        for h in self.handlers.get(ch, []):
            h(SimpleNamespace(channel=ch, data=data))

    def stats(self):
        return {"url": "", "private": True, "connected": True,
                "logged_in": True, "uptime_s": 1.0, "reconnects": 0,
                "subscriptions": 4, "messages": 0, "dropped": 0,
                "pings_sent": 0, "pongs_received": 0,
                "last_message_age_s": 0.0, "last_error": "",
                "channels": ["balance", "order", "position", "tpsl"],
                "counts": {"tpsl": 0}, "tpsl_events": 0}


class FakePrivateFeed:
    def __init__(self, cfg):
        self.ws = FakePrivateWS()

    async def start(self):
        return None

    async def stop(self):
        return None

    def stats(self):
        return self.ws.stats()


class TpslClient(FakeClient):
    """FakeClient + the pending TP/SL read."""

    def __init__(self, mark: float, rows: list[dict] | None = None,
                 fail: bool = False, raw=None, **kw):
        super().__init__(mark, **kw)
        self.rows = list(rows or [])
        self.fail = fail
        self.raw = raw

    async def get_pending_tp_sl_order(self, symbol=None, position_id=None,
                                      **kw):
        if self.fail:
            raise RuntimeError("exchange unreachable")
        if self.raw is not None:
            return self.raw
        return list(self.rows)


def _engine(mark: float, seed: PositionState | None = None,
            rows: list[dict] | None = None, tmp_path=None,
            **kw) -> tuple[TraderEngine, TpslClient]:
    s = Settings()
    s.market_data_source = "rest"
    s.breakeven_pct = 1.0
    s.trailing_trigger_roi_pct = 100.0
    e = TraderEngine(s, data_dir=tmp_path)
    e.client = TpslClient(mark, rows=rows, **kw)
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


# ------------------------------------------------- private feed is always on
def test_private_feed_opens_in_rest_mode_when_credentials_exist(
        monkeypatch, tmp_path):
    """MARKET_DATA_SOURCE is about prices, not about the account stream."""
    import jrock.trader.engine as eng

    monkeypatch.setattr(eng, "PrivateFeed", FakePrivateFeed)
    s = Settings(market_data_source="rest",
                 bitunix_api_key="k", bitunix_api_secret="s")
    e = TraderEngine(s, data_dir=tmp_path)
    _run(e.start_feed())
    assert e.feed is None, "rest mode must not open the PUBLIC socket"
    assert e.private_feed is not None, (
        "the account stream must not depend on the price source")
    assert "logged in" in e.private_feed_report()
    _run(e.stop_feed())


def test_private_feed_report_says_why_without_credentials(tmp_path):
    e = TraderEngine(Settings(market_data_source="rest"), data_dir=tmp_path)
    _run(e.start_feed())
    assert e.private_feed is None
    assert "BITUNIX_API_KEY" in e.private_feed_report()


# ------------------------------------------------------- WS drives the state
def test_ws_tpsl_adopts_a_manual_stop_immediately(tmp_path):
    """No manage pass, no report: the push itself updates the engine."""
    e, _ = _engine(0.00530, tmp_path=tmp_path)
    st = _seed(sl=0.00490)
    e.positions["P1"] = st
    e._on_ws_tpsl(SimpleNamespace(data={
        "event": "UPDATE", "status": "NEW", "positionId": "P1",
        "symbol": "AKEUSDT", "slPrice": "0.005020", "slQty": "4000"}))
    assert st.sl == pytest.approx(0.005020)
    assert st.trailing_stop == pytest.approx(0.005020)


def test_ws_tpsl_ignores_a_cancelled_stop(tmp_path):
    """A cancelled stop is not on the exchange - it must not be adopted."""
    e, _ = _engine(0.00530, tmp_path=tmp_path)
    st = _seed(sl=0.00490)
    e.positions["P1"] = st
    e._on_ws_tpsl(SimpleNamespace(data={
        "event": "CLOSE", "status": "CANCELED", "positionId": "P1",
        "slPrice": "0.005020"}))
    assert st.sl == pytest.approx(0.00490)
    recs = [r for r in e.tpsl_history(20)
            if r.get("event") == "sl_removed_on_exchange"]
    assert recs, "the operator must be able to see the stop was removed"


def test_ws_tpsl_does_not_mistake_a_rung_for_the_position_tp(tmp_path):
    """Under PARTIAL, a TP-only push is a ladder rung - st.tp stays 0."""
    e, _ = _engine(0.00530, tmp_path=tmp_path)
    e.state.tpsl_method = "PARTIAL"
    st = _seed(tp=0.0)
    e.positions["P1"] = st
    e._on_ws_tpsl(SimpleNamespace(data={
        "event": "CREATE", "status": "NEW", "positionId": "P1",
        "tpPrice": "0.006000", "tpQty": "1000"}))
    assert st.tp == 0.0


def test_ws_position_close_drops_it_and_arms_the_cooldown(tmp_path):
    e, _ = _engine(0.00530, tmp_path=tmp_path)
    e.positions["P1"] = _seed()
    e._on_ws_position(SimpleNamespace(data={
        "event": "CLOSE", "positionId": "P1", "symbol": "AKEUSDT",
        "qty": "0"}))
    assert "P1" not in e.positions
    assert e._reentry_block("AKEUSDT"), "the anti-churn cooldown must arm"
    events = [r.get("event") for r in e.trades_history(10)]
    assert "closed_on_exchange" in events


def test_ws_position_update_adopts_qty_and_leverage(tmp_path):
    e, _ = _engine(0.00530, tmp_path=tmp_path)
    st = _seed()
    e.positions["P1"] = st
    e._on_ws_position(SimpleNamespace(data={
        "event": "UPDATE", "positionId": "P1", "qty": "1200",
        "leverage": "20"}))
    assert st.qty == pytest.approx(1200)
    assert st.leverage == 20


# ---------------------------------------------------------- TP read-back
def test_the_manual_tp_is_adopted_and_preserved_on_the_next_write(tmp_path):
    """`st.tp` was write-only too: a manual TP was overwritten by the next
    breakeven/trailing modify. Read it back, then send THEIR number."""
    e, client = _engine(
        0.00530, seed=_seed(sl=0.00490, tp=0.0, breakeven_done=False),
        tmp_path=tmp_path,
        rows=[{"positionId": "P1", "symbol": "AKEUSDT",
               "slPrice": "0.004900", "tpPrice": "0.005500"}])
    _manage(e)
    assert e.positions["P1"].tp == pytest.approx(0.005500)
    assert client.modified, "breakeven should have fired at +6% ROI"
    assert client.modified[0]["tpPrice"] == "0.005500", (
        f"the operator's TP was overwritten: {client.modified[0]}")


def test_tp_is_read_back_into_the_report(tmp_path):
    e, _ = _engine(
        0.00530, seed=_seed(sl=0.00490, tp=0.0), tmp_path=tmp_path,
        rows=[{"positionId": "P1", "symbol": "AKEUSDT",
               "slPrice": "0.004900", "tpPrice": "0.005500"}])
    out = _run(e.report())
    assert e.positions["P1"].tp == pytest.approx(0.005500)
    assert "tp=0.0055" in out, out


# ------------------------------------------- report shows the exchange rungs
def test_report_shows_the_rungs_the_exchange_holds_not_the_log(tmp_path):
    """A rung cancelled in the app must stop being reported as working."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490), tmp_path=tmp_path)
    e.state.tpsl_method = "PARTIAL"
    e.state.partial_tp_ladder = "40@1,35@2,25@3"
    # what the engine ONCE placed ...
    e._log_tpsl({"event": "partial_rungs_placed", "ts": time.time(),
                 "symbol": "AKEUSDT", "position_id": "P1",
                 "placed": [{"share": 40, "r": 1.0, "price": 0.005500},
                            {"share": 35, "r": 2.0, "price": 0.006000}],
                 "skipped": []})
    # ... versus what the exchange ACTUALLY holds now
    e.client.rows = [
        {"positionId": "P1", "symbol": "AKEUSDT",
         "tpPrice": "0.006100", "tpQty": "1000"},
        {"positionId": "P1", "symbol": "AKEUSDT",
         "tpPrice": "0.006500", "tpQty": "500"},
    ]
    out = _run(e.report())
    assert "parked rungs on the exchange (AKEUSDT)" in out
    assert "0.0061x1000" in out and "0.0065x500" in out
    assert "0.0055" not in out, "a stale log rung was reported as working"


def test_report_says_so_when_no_rung_is_parked(tmp_path):
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490), tmp_path=tmp_path)
    e.state.tpsl_method = "PARTIAL"
    out = _run(e.report())
    assert "none on the exchange" in out


# ------------------------------------------ position row fields are refreshed
def test_entry_liq_and_leverage_are_reconciled_every_pass(tmp_path):
    """Adding margin/qty on the exchange moves entry and liq; the engine must
    quote the exchange's numbers, not the ones copied at open."""
    e, _ = _engine(0.00530, seed=_seed(entry=0.00490, liq=0.0040,
                                       leverage=5), tmp_path=tmp_path)
    _manage(e)
    st = e.positions["P1"]
    assert st.entry == pytest.approx(0.005)
    assert st.liq == pytest.approx(0.0046)
    assert st.leverage == 10
    recs = [r for r in e.tpsl_history(20)
            if r.get("event") == "position_reconciled"]
    assert recs


def test_rules_still_load_for_the_manage_pass(tmp_path):
    e, _ = _engine(0.00530, seed=_seed(), tmp_path=tmp_path)
    _manage(e)
    assert e.rules["AKEUSDT"].quote_precision == PAIR_ROW["quotePrecision"]


# ------------------------------------------- absence is state too, not silence
def test_a_stop_removed_on_the_exchange_is_cleared_and_reported(tmp_path):
    """The engine must not report a stop the exchange does not hold.

    Before this, a stop cancelled in the app left `st.sl` (and
    `breakeven_done=True`) untouched: the report kept quoting it and the
    `breakeven_already` guard refused to re-arm anything - a naked position
    that every engine surface described as protected.
    """
    e, _ = _engine(0.00530,
                   seed=_seed(sl=0.00490, breakeven_done=True,
                              trailing_active=True, trailing_stop=0.00490),
                   tmp_path=tmp_path)
    out = _run(e.report())
    st = e.positions["P1"]
    assert st.sl == 0.0
    assert st.trailing_stop == 0.0
    assert st.breakeven_done is False
    assert st.trailing_active is False
    assert "NO STOP ON THE EXCHANGE" in out, out
    events = [r.get("event") for r in e.tpsl_history(20)]
    assert "sl_missing_on_exchange" in events


def test_a_failed_read_never_clears_a_stop(tmp_path):
    """No answer is not the same answer as "no stop"."""
    e, client = _engine(0.00530, seed=_seed(sl=0.00490), tmp_path=tmp_path)
    client.fail = True
    out = _run(e.report())
    assert e.positions["P1"].sl == pytest.approx(0.00490)
    assert "sl=0.0049" in out
    assert "NO STOP ON THE EXCHANGE" not in out


def test_a_truncated_page_never_clears_a_stop(tmp_path):
    """A stop missing from page 1 of 2 is not a missing stop."""
    e, client = _engine(0.00530, seed=_seed(sl=0.00490), tmp_path=tmp_path,
                        raw={"orderList": [{"positionId": "P9",
                                            "symbol": "OTHERUSDT",
                                            "slPrice": "1.0"}],
                             "total": 150})
    _run(e.report())
    assert e.positions["P1"].sl == pytest.approx(0.00490)


def test_conflicting_rows_do_not_count_as_absence(tmp_path):
    """Two rows disagree about the stop: no value to adopt, but the stop is
    plainly THERE - clearing it would be worse than not guessing."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490), tmp_path=tmp_path,
                   rows=[{"positionId": "P1", "slPrice": "0.005040"},
                         {"positionId": "P1", "slPrice": "0.005010"}])
    _run(e.report())
    assert e.positions["P1"].sl == pytest.approx(0.00490)


def test_a_tp_removed_on_the_exchange_is_cleared_too(tmp_path):
    """Otherwise the next breakeven/trailing modify resurrects the old TP."""
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490, tp=0.005500),
                   tmp_path=tmp_path,
                   rows=[{"positionId": "P1", "slPrice": "0.004900"}])
    _run(e.report())
    assert e.positions["P1"].tp == 0.0
    events = [r.get("event") for r in e.tpsl_history(20)]
    assert "tp_missing_on_exchange" in events


def test_after_the_stop_is_cleared_the_breakeven_rearms_it(tmp_path):
    """Detection is only useful if management acts on it: the very next pass
    must put protection back instead of trusting `breakeven_done`."""
    e, client = _engine(0.00530,
                        seed=_seed(sl=0.00490, breakeven_done=True),
                        tmp_path=tmp_path)
    _manage(e)
    assert client.modified, "the engine left the position naked"
    assert e.positions["P1"].sl > 0


def test_ws_cancel_of_the_exact_stop_clears_it_immediately(tmp_path):
    e, _ = _engine(0.00530, seed=_seed(sl=0.00490, breakeven_done=True),
                   tmp_path=tmp_path)
    st = e.positions["P1"]
    e._on_ws_tpsl(SimpleNamespace(data={
        "event": "CLOSE", "status": "CANCELED", "positionId": "P1",
        "symbol": "AKEUSDT", "slPrice": "0.004900"}))
    assert st.sl == 0.0
    assert st.breakeven_done is False


def test_partial_report_admits_when_the_rung_read_failed(tmp_path):
    """"none on the exchange" must never stand in for "could not read"."""
    e, client = _engine(0.00530, seed=_seed(sl=0.00490), tmp_path=tmp_path)
    e.state.tpsl_method = "PARTIAL"
    client.fail = True
    out = _run(e.report())
    assert "could not read the exchange" in out, out
    assert "none on the exchange" not in out


# --------------------------------------------- a close is confirmed by reading
class CloseClient(TpslClient):
    """An exchange whose open-position list changes only when it is closed."""

    def __init__(self, mark: float, live: list[dict] | None = None,
                 readable: bool = True, **kw):
        super().__init__(mark, **kw)
        self.live = list(live or [])
        self.readable = readable
        self.flashed: list[str] = []

    async def get_pending_positions(self, sym=None):
        if not self.readable:
            raise RuntimeError("exchange unreachable")
        return list(self.live)

    async def flash_close_position(self, pid):
        self.flashed.append(pid)
        return {"orderId": "C1"}

    async def close_all_position(self, sym=None):
        return {"ok": True}


def _close_engine(tmp_path, client: CloseClient) -> TraderEngine:
    e = TraderEngine(Settings(market_data_source="rest"), data_dir=tmp_path)
    e.client = client
    e.positions["P1"] = _seed()
    return e


def test_a_confirmed_close_drops_the_state_and_arms_the_cooldown(tmp_path):
    client = CloseClient(0.00530, live=[])
    e = _close_engine(tmp_path, client)
    res = _run(e.close_position(symbol="AKEUSDT", position_id="P1"))
    assert res["ok"] is True and res["confirmed"] is True
    assert "P1" not in e.positions
    assert e._reentry_block("AKEUSDT"), "the anti-churn cooldown must arm"


def test_a_close_the_exchange_does_not_confirm_is_not_forgotten(tmp_path):
    """A 200 is an accepted request, not a flat position."""
    client = CloseClient(0.00530,
                         live=[{"positionId": "P1", "symbol": "AKEUSDT"}])
    e = _close_engine(tmp_path, client)
    res = _run(e.close_position(symbol="AKEUSDT", position_id="P1"))
    assert res["ok"] is False and res["confirmed"] is False
    assert "P1" in e.positions, "a still-open position was forgotten"
    assert not e._reentry_block("AKEUSDT"), (
        "a close that did not happen must not start a cooldown")
    events = [r.get("event") for r in e.trades_history(10)]
    assert "close_unconfirmed" in events


def test_an_unreadable_confirmation_does_not_block_the_close(tmp_path):
    """No read is not proof of failure: fall back to the API response, but
    say the confirmation was unreadable."""
    client = CloseClient(0.00530, readable=False)
    e = _close_engine(tmp_path, client)
    res = _run(e.close_position(symbol="AKEUSDT", position_id="P1"))
    assert res["ok"] is True and res["confirmed"] is None
    assert "P1" not in e.positions


def test_a_bulk_close_that_leaves_a_position_keeps_its_state(tmp_path):
    client = CloseClient(0.00530,
                         live=[{"positionId": "P1", "symbol": "AKEUSDT"}])
    e = _close_engine(tmp_path, client)
    res = _run(e.close_all_positions(reason="account_tp"))
    assert res["ok"] is False and res["confirmed"] is False
    assert "P1" in e.positions
    events = [r.get("event") for r in e.trades_history(10)]
    assert "close_unconfirmed" in events


def test_a_symbol_close_confirms_only_that_symbol(tmp_path):
    """`close_all_position(symbol)` closes one pair - another pair still open
    must not make the close look unconfirmed."""
    client = CloseClient(0.00530,
                         live=[{"positionId": "P2", "symbol": "ETHUSDT"}])
    e = _close_engine(tmp_path, client)
    e.positions["P2"] = _seed(pid="P2", symbol="ETHUSDT")
    res = _run(e.close_position(symbol="AKEUSDT"))
    assert res["ok"] is True and res["confirmed"] is True
    assert "P1" not in e.positions
    assert "P2" in e.positions, "a different pair's position was dropped"
    assert e._reentry_block("AKEUSDT")


def test_position_mode_write_is_read_back_from_the_exchange(tmp_path):
    """A successful change request is not the mode the exchange stored."""
    class ModeClient(CloseClient):
        async def get_position_mode(self):
            return {"positionMode": "ONE_WAY"}

        async def change_position_mode(self, mode):
            return {"ok": True}

    e = TraderEngine(Settings(market_data_source="rest"), data_dir=tmp_path)
    e.client = ModeClient(0.00530)
    out = _run(e.command("position mode HEDGE"))
    assert "ONE_WAY" in out, out
    assert e._position_mode == "ONE_WAY"
