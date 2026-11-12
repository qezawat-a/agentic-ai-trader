"""The report must not print a ROI that contradicts its own uPnL.

Live failure, from the operator's report paste (2026-11-12):

    ONEUSDT BUY qty=3045 entry=0.0017149 px=0.0017323 roi=25.37% upnl=0.0478065

The same line carried two numbers that cannot both be true:

  * roi=25.37% is (last - entry)/entry * leverage. It is computed from the
    LAST price (get_price).
  * upnl=0.0478065 is the exchange's own unrealizedPNL, which is settled
    against the MARK price. margin is 0.2088, so the PnL implies
    0.0478065/0.2088 = 22.89%, not 25.37%.

The gap (0.0052 USDT) is exactly the last-to-mark spread on 3045 qty. A
report that prints a ROI the exchange's own PnL contradicts is a report the
operator cannot audit - and the operator called it a lie.

The fix: the ROI on the position line is derived from the exchange's own
unrealizedPNL and margin, so roi and upnl are the same number seen two ways.
The price-based ROI stays available (it is what the exit thresholds use) but
it is labelled as such, and the margin is printed so the reader can check.
"""
from __future__ import annotations

import asyncio

from types import SimpleNamespace

import pytest

from jrock.config import Settings
from jrock.trader.engine import TraderEngine, _EntryContext
from jrock.trader.risk import PairRules
from jrock.trader.tpsl import PositionState


def _engine() -> TraderEngine:
    s = Settings()
    s.market_data_source = "rest"
    s.symbols = ["ONEUSDT"]
    e = TraderEngine(s)
    return e


def _position() -> dict:
    """The operator's live row, verbatim."""
    return {
        "positionId": "P1", "symbol": "ONEUSDT", "qty": "3045",
        "avgOpenPrice": "0.0017149", "side": "LONG", "leverage": 25,
        "unrealizedPNL": "0.0478065", "liqPrice": "0.0016548",
        "margin": "0.2088",
    }


class _FakeClient:
    def __init__(self, row: dict):
        self.row = row
        self.tickers = {
            "ONEUSDT": [{"lastPrice": "0.0017323", "markPrice": "0.0017306"}],
        }

    async def get_pending_positions(self):
        return [self.row]

    async def get_tickers(self, symbol=None):
        return self.tickers.get((symbol or "").upper(), [])


@pytest.mark.asyncio
async def test_report_roi_agrees_with_the_exchange_upnl(monkeypatch):
    e = _engine()
    e.client = _FakeClient(_position())

    # last price 0.0017323 -> price-based ROI 25.37%; the exchange PnL says
    # 0.0478065 / 0.2088 = 22.90%. The report must print the number the
    # exchange will settle on.
    rep = await e.report()

    line = next(l for l in rep.splitlines() if "ONEUSDT" in l and "roi=" in l)
    assert "roi=22.90%" in line, line
    assert "upnl=0.0478065" in line, line
    # the reader can audit it: the margin is printed
    assert "margin=0.2088" in line, line


@pytest.mark.asyncio
async def test_report_roi_falls_back_to_price_when_margin_missing(monkeypatch):
    """A row with no margin must not print roi=0 - fall back to the price."""
    row = _position()
    row["margin"] = "0"
    e = _engine()
    e.client = _FakeClient(row)
    rep = await e.report()
    line = next(l for l in rep.splitlines() if "ONEUSDT" in l and "roi=" in l)
    assert "roi=25.37%" in line, line


# ------------------------------------------------------------------ briefs
def test_exits_brief_quotes_the_mark_price_roi():
    """The agent must not be able to state a ROI the report contradicts.

    exits_brief is injected into the agent's prompt. If it computes ROI from
    the LAST price while the report derives it from the exchange's uPnL
    (mark-based), the bot can quote 25.37% in chat and print 22.90% in the
    report for the same position.
    """
    e = _engine()
    st = PositionState(position_id="P1", symbol="ONEUSDT", side="LONG",
                       entry=0.0017149, qty=3045, leverage=25,
                       sl=0.0017146, tp=0.0017698, liq=0.0016548,
                       breakeven_done=True)
    e.positions["P1"] = st
    e.price_cache.put_rest("ONEUSDT", 0.0017323)   # last
    e._last_marks["ONEUSDT"] = 0.0017306           # mark

    brief = e.exits_brief()
    line = next(l for l in brief.splitlines() if "mark" in l)
    assert "mark 0.0017306" in line, line
    assert "ROI +22.89%" in line, line


def test_live_brief_quotes_the_mark_price_roi():
    e = _engine()
    st = PositionState(position_id="P1", symbol="ONEUSDT", side="LONG",
                       entry=0.0017149, qty=3045, leverage=25,
                       sl=0.0017146, tp=0.0017698, liq=0.0016548,
                       breakeven_done=True)
    e.positions["P1"] = st
    e.price_cache.put_rest("ONEUSDT", 0.0017323)
    e._last_marks["ONEUSDT"] = 0.0017306

    brief = e.live_brief()
    line = next(l for l in brief.splitlines() if "mark" in l)
    assert "mark 0.0017306" in line, line
    assert "ROI +22.89%" in line, line


# ------------------------------------------------- errors vs notes
def test_a_successful_entry_is_not_reported_as_an_error():
    """The live report read "errors: 1" on a WORKING position.

    The line was the routine sizing note ("size ONEUSDT: MARGIN_AMOUNT_PCT=75%
    -> qty 3045.0 ... risk at the stop is 0.0469 USDT (16.26% of balance)").
    It was appended to ctx.notes and then copied into state.errors, so a
    successful entry looked like a failure - and the stale "risk at the stop"
    figure kept being quoted long after the breakeven had moved the stop to
    the entry. Routine notes belong in `notes`; only an exchange-forced
    leverage clamp belongs in `errors`.
    """
    e = _engine()
    ctx = _EntryContext(
        sym="ONEUSDT", side="LONG", price=0.0017149, mark=0.0017306,
        equity=100.0, rules=PairRules(symbol="ONEUSDT", quote_precision=7),
        tiers=[], agg=SimpleNamespace(strength=1.0, confidence=90.0),
        leverage=25, qty=3045, notional=5.2203,
    )
    ctx.notes = ["size ONEUSDT: MARGIN_AMOUNT_PCT=75% -> qty 3045.0 "
                 "(notional 5.2203 USDT, margin 0.2088 USDT at 25x); "
                 "risk at the stop is 0.0469 USDT (16.26% of balance)"]
    ctx.warnings = ["leverage ONEUSDT: 25x exceeds the exchange tier cap 20x "
                    "for a 5.22 USDT position; using 20x for this order"]

    e._preflight_balance(ctx)

    joined_errors = " ".join(e.state.errors)
    joined_notes = " ".join(e.state.notes)
    assert "size ONEUSDT" in joined_notes, joined_notes
    assert "size ONEUSDT" not in joined_errors, joined_errors
    assert "leverage ONEUSDT" in joined_errors, joined_errors


