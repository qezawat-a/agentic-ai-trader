"""Regression tests for the tiered-risk-limit model of entries.

The old behaviour clamped the configured leverage to whatever still left a
stop-loss room between the liquidation price and the mark, then (after v
history) refused otherwise. The docs model is simpler and does not guess:

* the tiered risk-limit table is required for a LIVE entry; a missing table is
  a REFUSAL, never a fallback ("unavailable - refusing to trade without the
  exchange's limits");
* the tier the SELECTED leverage maps to is the HIGHEST tier whose maximum
  leverage still covers the selection (Bitunix example: 50x -> tier 3);
* the position value of the order must fit that tier's nominal cap;
* otherwise the entry is refused (fail-closed).
"""
from __future__ import annotations

import asyncio

import pytest

from jrock.config import Settings
from jrock.trader.engine import TraderEngine
from jrock.trader.risk import (PairRules, liquidation_price, min_stop_distance,
                               pick_tier, round_step, tier_for_leverage,
                               Tier)

# --- the live JCTUSDT numbers from the bug report ---------------------------
JCT_ENTRY = 0.015833   # last price the engine traded on
JCT_MARK = 0.015625    # mark price the exchange validates against
JCT_MMR = 0.005        # maintenance margin rate at the 100x tier

JCT = PairRules(
    symbol="JCTUSDT", min_trade_volume=1.0, max_market_order_volume=1e9,
    max_limit_order_volume=1e9, base_precision=0, quote_precision=6,
    min_leverage=1, max_leverage=125, default_leverage=20,
    price_protect_scope=0.05, symbol_status="OPEN", api_supported=True,
)


# --- liquidation & rounding ------------------------------------------------

def test_liquidation_price_reproduces_the_screenshot():
    """The reported liq price must come out of the documented formula."""
    liq = liquidation_price(JCT_ENTRY, 100, "LONG", JCT_MMR)
    assert liq == pytest.approx(0.015754, abs=1e-6)


def test_100x_has_no_room_for_a_stop():
    """liq (0.015754) is ABOVE the mark (0.015625): at this leverage the
    desired stop distance does not fit; the user must lower the leverage."""
    liq = liquidation_price(JCT_ENTRY, 100, "LONG", JCT_MMR)
    assert liq > JCT_MARK


def test_round_step_does_not_underflow_on_floats():
    """0.29 * 100 == 28.999999 in binary - the old int() floor gave 0.28."""
    assert round_step(0.29, 2) == 0.29
    assert round_step(1.23456, 2) == 1.23
    assert round_step(0.00019, 4) == 0.0001


def test_pick_tier_raises_on_empty_table():
    with pytest.raises(ValueError):
        pick_tier([], 1000.0)


# --- the docs rule: tier from the SELECTED leverage -------------------------

TIER_TABLE = [
    Tier(1, 0.0, 5000.0, 125, 0.004),
    Tier(2, 5000.0, 20000.0, 100, 0.005),
    Tier(3, 20000.0, 50000.0, 50, 0.01),
]


def test_tier_for_leverage_is_the_highest_tier_covering_the_selection():
    """Bitunix example: 50x with the documented BTCUSDT table -> tier 3."""
    assert tier_for_leverage(TIER_TABLE, 50).level == 3   # the official example
    assert tier_for_leverage(TIER_TABLE, 100).level == 2
    assert tier_for_leverage(TIER_TABLE, 125).level == 1
    # nothing covers 200x
    assert tier_for_leverage(TIER_TABLE, 200) is None
    # 55x is covered only by tier 2 and tier 1 (both allow >= 55) -> tier 2
    assert tier_for_leverage(TIER_TABLE, 55).level == 2


# --- the engine wiring ------------------------------------------------------

PAIR = {
    "symbol": "JCTUSDT", "minTradeVolume": "1",
    "maxMarketOrderVolume": "100000000", "maxLimitOrderVolume": "1500000000",
    "basePrecision": 0, "quotePrecision": 6, "maxLeverage": 125,
    "minLeverage": 1, "defaultLeverage": 20, "priceProtectScope": "0.05",
    "symbolStatus": "OPEN", "isApiSupported": True,
}
TIERS = [
    {"symbol": "JCTUSDT", "level": 1, "startValue": "0", "endValue": "5000",
     "leverage": 125, "maintenanceMarginRate": "0.004"},
    {"symbol": "JCTUSDT", "level": 2, "startValue": "5000", "endValue": "20000",
     "leverage": 100, "maintenanceMarginRate": "0.005"},
    {"symbol": "JCTUSDT", "level": 3, "startValue": "20000", "endValue": "50000",
     "leverage": 50, "maintenanceMarginRate": "0.01"},
]


class FakeClient:
    def __init__(self, equity, mark, last, tiers=None):
        self.equity, self.mark, self.last = equity, mark, last
        self.tiers = tiers if tiers is not None else TIERS
        self.placed: list[dict] = []
        self.leverage_calls: list[int] = []

    async def get_tickers(self, sym):
        return [{"symbol": sym, "lastPrice": str(self.last),
                 "markPrice": str(self.mark)}]

    async def get_single_account(self):
        return [{"marginCoin": "USDT", "available": str(self.equity),
                 "frozen": "0", "margin": "0", "unrealized": "0"}]

    async def get_trading_pairs(self, symbols=None):
        return [PAIR]

    async def get_position_tiers(self, sym):
        return self.tiers

    async def get_pending_positions(self, sym=None):
        return []

    async def change_margin_mode(self, *a, **k):
        return {}

    async def change_leverage(self, sym, lev):
        self.leverage_calls.append(lev)
        return {}

    async def place_order(self, **b):
        self.placed.append(b)
        return {"orderId": "X1"}


class Agg:
    symbol = "JCTUSDT"
    direction = "LONG"
    actionable = True
    strength = "MEDIUM"
    confidence = 85.4
    agreeing = 5


def _run(equity, mark, last, leverage, sl_distance_pct=5.0, tiers=None):
    s = Settings()
    s.market_data_source = "rest"
    s.sl_distance_pct = sl_distance_pct
    e = TraderEngine(s)
    e.client = FakeClient(equity, mark, last, tiers=tiers)
    e.state.leverage = leverage

    async def go():
        await e.load_rules(["JCTUSDT"])
        return await e.try_enter(Agg())

    return asyncio.run(go()), e.client


def test_the_screenshot_case_refuses_cleanly_at_100x():
    """At 100x with this mark no stop exists (liq is above the mark): the
    engine refuses with the numbers; it does not clamp or pretend."""
    r, client = _run(100.0, JCT_MARK, JCT_ENTRY, 100, sl_distance_pct=2.0)
    assert r["ok"] is False
    assert client.placed == []
    assert "no valid stop-loss exists" in r["reason"]


def test_a_sane_leverage_trades_the_screenshot_case():
    """At 30x with a 2% stop there is plenty of room: the order goes through."""
    r, client = _run(100.0, JCT_MARK, JCT_ENTRY, 30, sl_distance_pct=2.0)
    assert r["ok"] is True, r.get("reason")
    assert len(client.placed) == 1
    assert client.leverage_calls and client.leverage_calls[-1] == 30


def test_empty_tier_table_refuses():
    r, client = _run(100.0, JCT_MARK, JCT_ENTRY, 10, tiers=[])
    assert r["ok"] is False
    assert client.placed == []
    assert "unavailable" in r["reason"]


def test_selection_exceeding_every_tier_refuses():
    r, client = _run(100.0, JCT_MARK, JCT_ENTRY, 200)
    assert r["ok"] is False
    assert client.placed == []


def test_position_value_exceeding_tier_cap_refuses():
    """100 000 USDT equity at 50x on tier 2 (cap 20000): the tier the 55x
    selection maps to cannot cover the size -> refuse, never downsize."""
    r, client = _run(100000.0, JCT_MARK, JCT_ENTRY, 55, sl_distance_pct=2.0)
    # margin 10% of 100000 = 10000 x 55x = 550000 > tier 2 cap 20000
    assert r["ok"] is False
    assert client.placed == []


def test_sane_leverage_places_an_order():
    r, client = _run(100.0, JCT_MARK, JCT_ENTRY, 10, sl_distance_pct=2.0)
    assert r["ok"] is True
    assert client.leverage_calls and client.leverage_calls[-1] == 10
