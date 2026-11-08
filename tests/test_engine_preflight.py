"""End-to-end tests for TraderEngine.try_enter's pre-flight gates.

These drive the real engine with a fake client, so they cover the wiring
(mark price fetched, tier checked, order built) rather than just the helpers.
"""
from __future__ import annotations

import asyncio

import pytest

from jrock.config import Settings
from jrock.trader.engine import TraderEngine

PAIR = {
    "symbol": "AKEUSDT", "minTradeVolume": "1",
    "maxMarketOrderVolume": "100000000", "maxLimitOrderVolume": "1500000000",
    "basePrecision": 0, "quotePrecision": 6, "maxLeverage": 50, "minLeverage": 1,
    "defaultLeverage": 10, "priceProtectScope": "0.05", "symbolStatus": "OPEN",
    "isApiSupported": True,
}
TIERS = [
    {"symbol": "AKEUSDT", "level": 1, "startValue": "0", "endValue": "10000",
     "leverage": 50, "maintenanceMarginRate": "0.015"},
    {"symbol": "AKEUSDT", "level": 2, "startValue": "10000", "endValue": "20000",
     "leverage": 25, "maintenanceMarginRate": "0.02"},
    {"symbol": "AKEUSDT", "level": 3, "startValue": "20000", "endValue": "40000",
     "leverage": 20, "maintenanceMarginRate": "0.025"},
]


class FakeClient:
    def __init__(self, equity, mark, last):
        self.equity, self.mark, self.last = equity, mark, last
        self.placed: list[dict] = []

    async def get_tickers(self, sym):
        return [{"symbol": sym, "lastPrice": str(self.last),
                 "markPrice": str(self.mark)}]

    async def get_single_account(self):
        return [{"marginCoin": "USDT", "available": str(self.equity),
                 "frozen": "0", "margin": "0", "unrealized": "0"}]

    async def get_trading_pairs(self, symbols=None):
        return [PAIR]

    async def get_position_tiers(self, sym):
        return TIERS

    async def get_pending_positions(self, sym=None):
        return []

    async def change_margin_mode(self, *a, **k):
        return {}

    async def change_leverage(self, *a, **k):
        return {}

    async def place_order(self, **b):
        self.placed.append(b)
        return {"orderId": "X1"}


class Agg:
    symbol = "AKEUSDT"
    direction = "LONG"
    actionable = True
    strength = "MEDIUM"
    confidence = 75.0
    agreeing = 4


def _run(equity, mark, last, leverage):
    s = Settings()
    s.market_data_source = "rest"
    e = TraderEngine(s)
    e.client = FakeClient(equity, mark, last)
    e.state.leverage = leverage

    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.try_enter(Agg())

    return asyncio.run(go()), e.client.placed


def test_small_order_is_not_refused_for_a_tier_it_never_reaches():
    """Regression for the false 20003.

    The tier cap used to be checked against ``equity * leverage`` - the
    balance-capped MAXIMUM notional, not the order. At 30x on a 1000 USDT
    balance that is 30,000 USDT, which lands in tier 3 (max 20x), so the
    engine refused a trade whose real notional is ~2,400 USDT and sits
    comfortably in tier 1 (max 50x). The cap must be checked against the
    notional we will actually send.
    """
    r, placed = _run(1000.0, 0.005020, 0.005009, 30)
    assert r["ok"] is True, r["reason"]
    assert len(placed) == 1
    # the real notional really is inside tier 1, not the hypothetical band
    notional = float(placed[0]["qty"]) * 0.005020
    assert notional < 10000.0


def test_tier_cap_still_refuses_a_genuinely_oversized_order():
    """The cap must still bite when the REAL notional lands in a stricter tier.

    At 30x on a 10,000 USDT balance the risk-sized order is ~24,000 USDT,
    which is tier 3 (max 20x) - the exchange would want more margin than we
    computed, so this must be refused locally with 20003 named.
    """
    r, placed = _run(10000.0, 0.005020, 0.005009, 30)
    assert r["ok"] is False
    assert placed == [], "no order may be sent when the tier forbids the leverage"
    assert "exceeds the maximum" in r["reason"]
    assert "20x" in r["reason"]
    assert r["hint"] == "20003 Insufficient balance"


def test_tier_cap_names_the_real_notional_not_the_hypothetical_one():
    """The refusal must quote the order's own notional and both margins."""
    r, _ = _run(10000.0, 0.005020, 0.005009, 30)
    # the notional named is the real one (~24k), never equity*leverage (300k)
    assert "300000.00" not in r["reason"]
    assert "USDT position on AKEUSDT" in r["reason"]
    assert "margin, not" in r["reason"]


class HeldLeverageClient(FakeClient):
    """``change_leverage`` is advisory: the exchange keeps a lower value.

    This is the real-world case behind 20003 - the pair's own range, the
    position tier, or an open position (20006) can leave the exchange holding
    less leverage than the engine asked for.
    """

    def __init__(self, equity, mark, last, held):
        super().__init__(equity, mark, last)
        self.held = held
        self.leverage_calls: list[int] = []

    async def change_leverage(self, sym, lev, *a, **k):
        self.leverage_calls.append(int(lev))
        return {"symbol": sym, "leverage": str(self.held)}

    async def get_leverage_and_margin_mode(self, sym, *a, **k):
        return {"symbol": sym, "leverage": str(self.held),
                "marginMode": "ISOLATION"}


def _run_held(equity, mark, last, configured, held):
    s = Settings()
    s.market_data_source = "rest"
    e = TraderEngine(s)
    e.client = HeldLeverageClient(equity, mark, last, held)
    e.state.leverage = configured

    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.try_enter(Agg())

    return asyncio.run(go()), e.client.placed, e


def test_margin_is_sized_against_the_leverage_the_exchange_holds():
    """Regression for 20003 from a leverage mismatch.

    Sizing at the CONFIGURED 10x while the exchange holds 3x understates the
    margin by 3.3x. The engine must read the held value back and size against
    it, so the order it sends is affordable at the leverage that is really in
    force.
    """
    r, placed, e = _run_held(1000.0, 0.005020, 0.005009, 10, 3)
    assert r["ok"] is True, r["reason"]
    assert len(placed) == 1
    notional = float(placed[0]["qty"]) * 0.005020
    # the margin the exchange will really lock is notional/3, not notional/10
    assert notional / 3.0 <= 1000.0
    # and the mismatch is visible, not silent
    assert any("exchange holds 3x" in m for m in e.state.errors)


def test_held_leverage_is_read_back_even_when_change_leverage_succeeds():
    """change_leverage answering a different value must be believed."""
    r, placed, e = _run_held(1000.0, 0.005020, 0.005009, 20, 5)
    assert r["ok"] is True, r["reason"]
    assert e.client.leverage_calls == [20]
    assert any("exchange holds 5x" in m for m in e.state.errors)


def test_leverage_read_back_failure_falls_back_to_configured():
    """A read-back failure must not block trading (FakeClient has no
    get_leverage_and_margin_mode) - the configured value is used."""
    r, placed = _run(100.0, 0.005020, 0.005009, 10)
    assert r["ok"] is True
    assert len(placed) == 1


def test_correctly_sized_order_passes_and_is_built():
    r, placed = _run(100.0, 0.005020, 0.005009, 10)
    assert r["ok"] is True
    assert len(placed) == 1
    body = placed[0]
    assert body["symbol"] == "AKEUSDT"
    assert body["side"] == "BUY"
    assert body["orderType"] == "MARKET"
    # the stop must be below both the entry and the mark
    assert float(body["slPrice"]) < 0.005009
    assert float(body["slPrice"]) < 0.005020
    assert float(body["tpPrice"]) > 0.005020


def test_engine_fetches_the_mark_price_not_just_the_last_price():
    """The mark (0.005020) is above the last (0.005009); the stop must clear
    the mark, which only happens if the mark was actually fetched."""
    r, placed = _run(100.0, 0.005020, 0.005009, 10)
    assert r["ok"] is True
    assert float(placed[0]["slPrice"]) < 0.005020


def test_tiny_account_is_refused_without_sending():
    r, placed = _run(0.0001, 0.005020, 0.005009, 10)
    assert r["ok"] is False
    assert placed == []
    assert "qty is 0" in r["reason"]
