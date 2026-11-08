"""Universe wiring into the engine, and the JS-parity checks for sizing/liq.

Two things are pinned here:

1. The universe layer is actually reachable from the engine - `refresh_universe`
   rebuilds `state.symbols` from the exchange, keeps held positions, and never
   empties the list on a failure. An explicit symbol list is never touched.

2. The Python position-sizing and liquidation-price math matches the JavaScript
   engine (bitunix-ai-trader, `src/trading/risk.js`). These are parity tests, not
   new behaviour: they exist so a future edit to either side is caught.
"""
from __future__ import annotations

import asyncio

import pytest

from jrock.config import Settings
from jrock.trader.engine import TraderEngine
from jrock.trader.errors import BitunixAPIError
from jrock.trader.risk import (PairRules, Tier, liquidation_price,
                               margin_required, max_notional_for_balance,
                               pick_tier, position_size_by_risk,
                               required_cost)
from jrock.trader.tpsl import PositionState


# ------------------------------------------------------------------ fixtures
class FakeClient:
    """Just enough of BitunixClient for the universe path."""

    def __init__(self, pairs, tickers, fail=False):
        self._pairs = pairs
        self._tickers = tickers
        self.fail = fail
        self.pair_calls = 0
        self.ticker_calls = 0

    async def get_trading_pairs(self, symbols=None):
        self.pair_calls += 1
        if self.fail:
            raise BitunixAPIError(-1, "network down")
        return self._pairs

    async def get_tickers(self, symbols=None):
        self.ticker_calls += 1
        if self.fail:
            raise BitunixAPIError(-1, "network down")
        return self._tickers


def _pairs(*symbols):
    return [{"symbol": s, "symbolStatus": "OPEN", "isApiSupported": True,
             "quote": "USDT", "minTradeVolume": "1", "basePrecision": 0,
             "quotePrecision": 6, "maxLeverage": 50} for s in symbols]


def _tickers(rows):
    return [{"symbol": s, "lastPrice": str(p), "open": str(p / (1 + chg / 100)),
             "high": str(p * 1.05), "low": str(p * 0.95), "quoteVol": str(qv)}
            for s, p, qv, chg in rows]


def _engine(tmp_path, **over):
    s = Settings(symbols=["BTCUSDT"], bitunix_api_key="", bitunix_api_secret="")
    for k, v in over.items():
        setattr(s, k, v)
    return TraderEngine(s, data_dir=tmp_path)


# ------------------------------------------------------- engine wiring
def test_universe_is_off_by_default_and_leaves_symbols_alone(tmp_path):
    eng = _engine(tmp_path)
    assert eng.state.universe_auto is False
    assert asyncio.run(eng.refresh_universe()) is None
    assert eng.state.symbols == ["BTCUSDT"]


def test_refresh_universe_rebuilds_symbols_from_the_exchange(tmp_path):
    eng = _engine(tmp_path, universe_auto=True, universe_size=3,
                  min_24h_volume_usd=20e6)
    rows = [(f"S{i}USDT", 1.0, 30e6, 1.0) for i in range(6)]
    eng.client = FakeClient(_pairs(*[r[0] for r in rows]), _tickers(rows))
    res = asyncio.run(eng.refresh_universe())
    assert res is not None
    assert len(eng.state.symbols) == 3
    assert set(eng.state.symbols) <= {r[0] for r in rows}


def test_refresh_universe_keeps_held_positions_even_if_they_drop_out(tmp_path):
    eng = _engine(tmp_path, universe_auto=True, universe_size=2,
                  min_24h_volume_usd=20e6)
    rows = [(f"S{i}USDT", 1.0, 30e6, 1.0) for i in range(6)]
    eng.client = FakeClient(_pairs(*[r[0] for r in rows]), _tickers(rows))
    # pretend we hold a position on a symbol that will not be picked
    eng.positions["HELDUSDT"] = PositionState(
        position_id="p1", symbol="HELDUSDT", side="LONG", entry=1.0, qty=1.0,
        leverage=10, sl=0.9, tp=1.2, liq=0.5)
    asyncio.run(eng.refresh_universe())
    assert "HELDUSDT" in eng.state.symbols


def test_a_failed_refresh_keeps_the_previous_symbol_list(tmp_path):
    eng = _engine(tmp_path, universe_auto=True)
    eng.state.symbols = ["KEEPUSDT"]
    eng.client = FakeClient([], [], fail=True)
    asyncio.run(eng.refresh_universe())
    assert eng.state.symbols == ["KEEPUSDT"], "a network failure must not blind the scanner"


def test_universe_report_says_off_when_explicit(tmp_path):
    eng = _engine(tmp_path)
    assert "OFF" in eng.universe_report()


def test_universe_report_shows_the_settings_when_auto(tmp_path):
    eng = _engine(tmp_path, universe_auto=True, universe_rank="GAINERS",
                  universe_size=12, min_24h_volume_usd=5e6)
    r = eng.universe_report()
    assert "AUTO" in r and "GAINERS" in r and "12" in r


def test_universe_settings_survive_a_restart(tmp_path):
    eng = _engine(tmp_path)
    # a runtime change, exactly as the `universe rank MOVERS` command makes it
    eng.state.universe_auto = True
    eng.state.universe_rank = "MOVERS"
    eng.state.universe_size = 7
    eng.state.min_24h_volume_usd = 3e6
    eng._persist_if_changed()
    eng2 = _engine(tmp_path)
    assert eng2.state.universe_rank == "MOVERS"
    assert eng2.state.universe_size == 7
    assert eng2.state.min_24h_volume_usd == pytest.approx(3e6)


# ------------------------------------------- JS parity: liquidation price
def test_liq_matches_the_js_formula_long():
    """JS: liq = entry * (1 - 1/lev + mmr)."""
    entry, lev, mmr = 100_000.0, 100, 0.005
    assert liquidation_price(entry, lev, "LONG", mmr) == pytest.approx(
        entry * (1 - 1 / lev + mmr))
    # the doc's worked example: 100000 @ 100x, MMR 0.5% -> 99500
    assert liquidation_price(entry, lev, "LONG", mmr) == pytest.approx(99_500.0)


def test_liq_matches_the_js_formula_short():
    entry, lev, mmr = 100_000.0, 100, 0.005
    assert liquidation_price(entry, lev, "SHORT", mmr) == pytest.approx(
        entry * (1 + 1 / lev - mmr))


def test_liq_is_closer_to_entry_as_leverage_rises():
    lo = liquidation_price(100, 5, "LONG", 0.004)
    hi = liquidation_price(100, 50, "LONG", 0.004)
    assert hi > lo, "higher leverage liquidates closer to entry"


def test_liq_is_closer_to_entry_as_mmr_rises():
    thin = liquidation_price(100, 10, "LONG", 0.004)
    fat = liquidation_price(100, 10, "LONG", 0.02)
    assert fat > thin


# ------------------------------------------- JS parity: tier selection
def test_tier_lookup_matches_the_js_band_semantics():
    """JS tierAt: startValue < v <= endValue, first band wins."""
    tiers = [Tier(1, 0, 50_000, 125, 0.004), Tier(2, 50_000, 200_000, 100, 0.005)]
    assert pick_tier(tiers, 10_000).level == 1
    assert pick_tier(tiers, 60_000).level == 2
    assert pick_tier(tiers, 10_000_000).level == 2, "above every band -> the top band"


# ------------------------------------------- JS parity: sizing
def test_margin_is_notional_over_leverage():
    assert margin_required(1000, 10) == pytest.approx(100.0)


def test_sizing_cap_matches_the_js_fee_aware_form():
    """JS computeMargin leaves an 80% buffer; Python solves the fee explicitly.

    Both refuse to spend the whole balance on margin. The Python form is the
    stricter of the two because it accounts for the actual fee rate:
        notional <= equity / (1/lev + fee)
    """
    equity, lev, fee = 100.0, 10, 0.0006
    cap = max_notional_for_balance(equity, lev, fee_rate=fee)
    assert cap == pytest.approx(equity / (1 / lev + fee))
    assert cap < equity * lev, "the fee term must shrink the cap below equity*lev"


def test_required_cost_is_margin_plus_fee():
    n, lev, fee = 1000.0, 10, 0.0006
    assert required_cost(n, lev, fee) == pytest.approx(n / lev + n * fee)


def test_sizing_never_asks_for_more_than_the_balance():
    rules = PairRules("AKEUSDT", base_precision=0, min_trade_volume=1,
                      max_market_order_volume=1e9)
    equity, price, lev = 9.3044, 0.005943, 10
    qty = position_size_by_risk(equity, 100.0, price, price * 0.99, lev, rules)
    cost = required_cost(qty * price, lev)
    assert cost <= equity, f"cost {cost} must fit the balance {equity}"


def test_sizing_respects_the_exchange_minimum():
    rules = PairRules("AKEUSDT", base_precision=0, min_trade_volume=1)
    qty = position_size_by_risk(0.0001, 1.0, 0.005943, 0.0059, 10, rules)
    assert qty == 0.0, "below minTradeVolume -> refuse, never round up"


def test_sizing_respects_max_market_order_volume():
    rules = PairRules("AKEUSDT", base_precision=0, min_trade_volume=1,
                      max_market_order_volume=100)
    qty = position_size_by_risk(1e6, 100.0, 0.005943, 0.0059, 10, rules)
    assert qty <= 100
