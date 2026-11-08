"""JS-parity tests for the universe layer, sizing, and the liq calculator.

Ported from the JavaScript engine (bitunix-ai-trader). Each test pins a rule
that exists in the JS source, so a future edit to either side is caught:

  * universe ranking (src/scanner/scanner.js -> rankUniverse/buildUniverse)
  * the 24h liquidity floor and the MIN_POOL fallback
  * the GAINERS/LOSERS filter (the "I set GAINERS but it isn't scanning
    gainers" bug: a ceiling larger than the qualifying set must not pad)
  * position sizing (src/trading/risk.js -> computeMargin / minOrderNotional)
  * the liquidation price and the liqUnsafe guard (estimateLiqPrice / mmrFor)
  * the profit-side stop pass-through (clampStopInsideLiq's side guard)

Everything here is pure - no network, no client.
"""
from __future__ import annotations

import pytest

from jrock.trader.risk import (PairRules, clamp_sl, liquidation_price,
                               liq_unsafe, max_notional_for_balance,
                               max_safe_leverage, min_margin_for_min_order,
                               min_order_notional, mmr_for,
                               position_size_by_risk, required_cost,
                               validate_tpsl_side)
from jrock.trader.universe import (LIQUIDITY_WEIGHT, MIN_POOL, MOVEMENT_CAP,
                                   MOVEMENT_WEIGHT, UniverseItem, build_universe,
                                   candidates_from_tickers, rank_universe,
                                   tradable_pairs)

AKE = PairRules("AKEUSDT", base_precision=0, quote_precision=6,
                min_trade_volume=1, max_market_order_volume=1e9)


# ============================================================ universe ranking
POOL = [
    UniverseItem("WUSDT", qv=41e6, range=0.6, price=1, chg=42.8),
    UniverseItem("NEARUSDT", qv=77e6, range=0.1, price=1, chg=5.6),
    UniverseItem("ZECUSDT", qv=72e6, range=0.08, price=1, chg=4.7),
    UniverseItem("ETHUSDT", qv=1229e6, range=0.03, price=1, chg=1.3),
    UniverseItem("SUIUSDT", qv=25e6, range=0.05, price=1, chg=0.0),     # flat
    UniverseItem("BTCUSDT", qv=1815e6, range=0.02, price=1, chg=-0.1),  # red
    UniverseItem("SPCXUSDT", qv=35e6, range=0.04, price=1, chg=-0.2),   # red
    UniverseItem("HYPEUSDT", qv=24e6, range=0.07, price=1, chg=-0.4),   # red
    UniverseItem("XAUUSDT", qv=54e6, range=0.01, price=1, chg=-0.7),    # red
    UniverseItem("CRASH", qv=22e6, range=0.2, price=1, chg=-16.5),      # deep red
]


def syms(items):
    return [x.symbol for x in items]


def test_gainers_returns_only_coins_that_are_actually_up():
    g = rank_universe(POOL, "GAINERS")
    assert all(x.chg > 0 for x in g), "no flat or red name survives GAINERS"


def test_gainers_is_biggest_gain_first():
    g = rank_universe(POOL, "GAINERS")
    assert syms(g) == ["WUSDT", "NEARUSDT", "ZECUSDT", "ETHUSDT"]


def test_gainers_never_pads_with_losers_when_the_ceiling_is_larger():
    """The exact bug: a ceiling bigger than the qualifying set must not pad."""
    g = rank_universe(POOL, "GAINERS")
    assert len(g) < len(POOL)
    assert not {"BTCUSDT", "SPCXUSDT", "HYPEUSDT", "XAUUSDT", "CRASH",
                "SUIUSDT"} & set(syms(g))


def test_losers_is_the_exact_mirror():
    lo = rank_universe(POOL, "LOSERS")
    assert all(x.chg < 0 for x in lo)
    assert lo[0].symbol == "CRASH", "deepest fall first"


def test_a_flat_coin_is_neither_a_gainer_nor_a_loser():
    assert "SUIUSDT" not in syms(rank_universe(POOL, "GAINERS"))
    assert "SUIUSDT" not in syms(rank_universe(POOL, "LOSERS"))


def test_movers_keeps_both_directions_biggest_absolute_first():
    m = rank_universe(POOL, "MOVERS")
    assert len(m) == len(POOL)
    assert m[0].symbol == "WUSDT"      # +42.8
    assert m[1].symbol == "CRASH"      # -16.5


def test_volume_ranks_by_liquidity_not_by_range():
    v = rank_universe(POOL, "VOLUME")
    assert v[0].symbol == "BTCUSDT", "deepest book first"
    assert v[1].symbol == "ETHUSDT"


def test_a_thin_high_range_coin_does_not_leapfrog_the_majors():
    """The incident this score exists for: BTC ranked 64th behind $600k coins."""
    v = rank_universe(POOL, "VOLUME")
    w = next(i for i, x in enumerate(v) if x.symbol == "WUSDT")
    assert w > 1


def test_volume_score_matches_the_js_formula():
    """JS: score = liquidity*0.75 + min(range,0.25)/0.25*0.25."""
    v = rank_universe(POOL, "VOLUME")
    btc = next(x for x in v if x.symbol == "BTCUSDT")
    max_vol = max(x.qv for x in POOL)
    expected = ((btc.qv / max_vol) * LIQUIDITY_WEIGHT
                + (min(btc.range, MOVEMENT_CAP) / MOVEMENT_CAP) * MOVEMENT_WEIGHT)
    assert btc.score == pytest.approx(expected)
    assert LIQUIDITY_WEIGHT == 0.75 and MOVEMENT_WEIGHT == 0.25


def test_default_mode_is_volume():
    assert syms(rank_universe(POOL)) == syms(rank_universe(POOL, "VOLUME"))


def test_unknown_mode_falls_back_to_volume_instead_of_raising():
    assert syms(rank_universe(POOL, "NONSENSE")) == syms(rank_universe(POOL, "VOLUME"))


def test_empty_pool_does_not_throw():
    assert rank_universe([], "GAINERS") == []
    assert rank_universe([], "VOLUME") == []


# ====================================================== tradable filtering
def test_tradable_pairs_drops_closed_api_disabled_and_non_usdt():
    pairs = [
        {"symbol": "AUSDT", "symbolStatus": "OPEN", "isApiSupported": True, "quote": "USDT"},
        {"symbol": "BUSDT", "symbolStatus": "CANCEL_ONLY", "isApiSupported": True, "quote": "USDT"},
        {"symbol": "CUSDT", "symbolStatus": "OPEN", "isApiSupported": False, "quote": "USDT"},
        {"symbol": "DUSDC", "symbolStatus": "OPEN", "isApiSupported": True, "quote": "USDC"},
    ]
    assert [p["symbol"] for p in tradable_pairs(pairs)] == ["AUSDT"]


def test_candidates_use_the_tickers_own_open_for_the_24h_change():
    tickers = [{"symbol": "AUSDT", "lastPrice": "110", "open": "100",
                "high": "120", "low": "90", "quoteVol": "5000000"}]
    c = candidates_from_tickers(tickers, {"AUSDT"})
    assert len(c) == 1
    assert c[0].chg == pytest.approx(10.0)
    assert c[0].range == pytest.approx((120 - 90) / 90)


def test_candidates_drop_zero_volume_and_zero_price_rows():
    tickers = [
        {"symbol": "AUSDT", "lastPrice": "1", "open": "1", "quoteVol": "0"},
        {"symbol": "BUSDT", "lastPrice": "0", "open": "1", "quoteVol": "1000"},
        {"symbol": "CUSDT", "lastPrice": "1", "open": "1", "quoteVol": "1000"},
    ]
    got = candidates_from_tickers(tickers, {"AUSDT", "BUSDT", "CUSDT"})
    assert [c.symbol for c in got] == ["CUSDT"]


# ============================================================ build_universe
def _pairs(*symbols):
    return [{"symbol": s, "symbolStatus": "OPEN", "isApiSupported": True,
             "quote": "USDT"} for s in symbols]


def _tickers(rows):
    """rows: (symbol, price, quoteVol, chg_pct)"""
    out = []
    for sym, price, qv, chg in rows:
        open_ = price / (1 + chg / 100.0) if chg != -100 else price
        out.append({"symbol": sym, "lastPrice": str(price), "open": str(open_),
                    "high": str(price * 1.01), "low": str(price * 0.99),
                    "quoteVol": str(qv)})
    return out


def test_volume_floor_excludes_thin_pairs():
    rows = [(f"S{i}USDT", 1.0, 30e6, 1.0) for i in range(12)]
    rows.append(("THINUSDT", 1.0, 1e6, 50.0))     # huge move, tiny book
    res = build_universe(_pairs(*[r[0] for r in rows]), _tickers(rows),
                         mode="VOLUME", size=40, min_24h_volume_usd=20e6)
    assert "THINUSDT" not in res.symbols
    assert len(res.symbols) == 12


def test_universe_size_is_a_ceiling():
    rows = [(f"S{i}USDT", 1.0, 30e6, 1.0) for i in range(20)]
    res = build_universe(_pairs(*[r[0] for r in rows]), _tickers(rows),
                         mode="VOLUME", size=5, min_24h_volume_usd=20e6)
    assert len(res.symbols) == 5


def test_floor_that_leaves_too_few_pairs_falls_back_to_the_most_liquid():
    rows = [(f"S{i}USDT", 1.0, 30e6, 1.0) for i in range(12)]
    res = build_universe(_pairs(*[r[0] for r in rows]), _tickers(rows),
                         mode="VOLUME", size=40, min_24h_volume_usd=1e12)
    assert res.floor_relaxed is True
    assert len(res.symbols) >= MIN_POOL
    assert any("falling back" in n for n in res.notes)


def test_gainers_mode_with_no_gainers_falls_back_to_volume():
    rows = [(f"S{i}USDT", 1.0, 30e6, -2.0) for i in range(12)]   # all red
    res = build_universe(_pairs(*[r[0] for r in rows]), _tickers(rows),
                         mode="GAINERS", size=40, min_24h_volume_usd=20e6)
    assert res.fell_back is True
    assert res.mode == "VOLUME"
    assert len(res.symbols) == 12, "the scanner keeps working instead of going empty"


def test_gainers_mode_returns_only_gainers_when_some_exist():
    rows = [(f"S{i}USDT", 1.0, 30e6, 1.0) for i in range(12)]
    rows += [("REDUSDT", 1.0, 30e6, -5.0)]
    res = build_universe(_pairs(*[r[0] for r in rows]), _tickers(rows),
                         mode="GAINERS", size=40, min_24h_volume_usd=20e6)
    assert "REDUSDT" not in res.symbols
    assert res.fell_back is False


def test_no_tradable_pair_returns_empty_with_a_note():
    res = build_universe([], [], mode="VOLUME", size=40, min_24h_volume_usd=20e6)
    assert res.symbols == []
    assert res.notes


def test_report_is_human_readable():
    rows = [(f"S{i}USDT", 1.0, 30e6, 1.0) for i in range(12)]
    res = build_universe(_pairs(*[r[0] for r in rows]), _tickers(rows),
                         mode="VOLUME", size=40, min_24h_volume_usd=20e6)
    text = res.report()
    assert "universe by VOLUME" in text
    assert "12 pairs" in text


# ======================================================== liquidation price
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


def test_liq_returns_entry_when_leverage_swallows_the_margin():
    """At L >= 1/mmr the JS returns entry so the caller refuses the trade."""
    assert liquidation_price(100, 200, "LONG", 0.005) == 100
    assert liquidation_price(100, 200, "SHORT", 0.005) == 100


def test_liq_unsafe_flags_the_unopenable_case():
    assert liq_unsafe(100, 200, "LONG", 0.005) is True
    assert liq_unsafe(100, 10, "LONG", 0.005) is False


def test_mmr_for_matches_the_js_fallback_and_never_blocks_high_leverage():
    """JS mmrFor: clamp(min(1/maxLev, 0.01), 0.004, 0.05).

    The old 1/maxLev made liqPrice == entry at 50x+, blocking EVERY trade with
    a false liqUnsafe. The cap at 1% is what fixes that.
    """
    assert mmr_for(125) == pytest.approx(0.008)
    assert mmr_for(100) == pytest.approx(0.01)
    assert mmr_for(50) == pytest.approx(0.01), "capped, not 0.02"
    assert mmr_for(0) == pytest.approx(0.005)
    # and the guard must NOT fire at 50x with the fallback
    assert liq_unsafe(100, 50, "LONG", mmr_for(50)) is False


def test_max_safe_leverage_matches_the_js_solver():
    """JS: lev = 1 / (slDist/(1-buffer)/entry + mmr)."""
    entry, sl_dist, mmr, buf = 100.0, 1.0, 0.005, 0.5
    expected = int(1 / ((sl_dist / (1 - buf)) / entry + mmr))
    assert max_safe_leverage(entry, sl_dist, mmr, buf) == expected


# ================================================================ sizing
def test_margin_is_notional_over_leverage():
    assert required_cost(1000, 10, fee_rate=0.0) == pytest.approx(100.0)


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
    equity, price, lev = 9.3044, 0.005943, 10
    qty = position_size_by_risk(equity, 100.0, price, price * 0.99, lev, AKE)
    cost = required_cost(qty * price, lev)
    assert cost <= equity, f"cost {cost} must fit the balance {equity}"


def test_sizing_respects_the_exchange_minimum():
    qty = position_size_by_risk(0.0001, 1.0, 0.005943, 0.0059, 10, AKE)
    assert qty == 0.0, "below minTradeVolume -> refuse, never round up"


def test_sizing_respects_max_market_order_volume():
    rules = PairRules("AKEUSDT", base_precision=0, min_trade_volume=1,
                      max_market_order_volume=100)
    qty = position_size_by_risk(1e6, 100.0, 0.005943, 0.0059, 10, rules)
    assert qty <= 100


def test_sizing_caps_each_order_at_its_share_of_the_balance():
    """JS computeMargin: margin <= available / slotsLeft.

    Without this the first of several positions can commit the whole balance
    and every later one is refused with 20003.
    """
    equity, price, lev = 100.0, 1.0, 10
    one = position_size_by_risk(equity, 100.0, price, price * 0.99, lev, AKE,
                                slots_left=1)
    three = position_size_by_risk(equity, 100.0, price, price * 0.99, lev, AKE,
                                  slots_left=3)
    assert three < one, "more free slots must mean a smaller per-order cap"
    assert required_cost(three * price, lev) <= equity / 3 + 1e-9


def test_min_order_notional_matches_the_js_round_up():
    """JS minOrderNotional: ceil(minTradeVolume * 10^p) / 10^p * price."""
    assert min_order_notional(0.005943, 1, 0) == pytest.approx(0.005943)
    # ceil(0.5) == 1, so the minimum qty is 1 and the notional is 1 * 2.0
    assert min_order_notional(2.0, 0.5, 0) == pytest.approx(2.0)
    # with 1 decimal of precision, 0.55 rounds up to 0.6
    assert min_order_notional(2.0, 0.55, 1) == pytest.approx(1.2)


def test_min_margin_for_min_order_matches_the_js_form():
    assert min_margin_for_min_order(0.005943, 1, 10, 0) == pytest.approx(0.0005943)


# ============================================ profit-side stop pass-through
def test_management_path_keeps_a_breakeven_stop_on_the_profit_side():
    """The JS clampStopInsideLiq side guard.

    A breakeven stop for a LONG sits at/above entry. The old clamp measured
    |entry - stop| and pulled it back to `entry - maxDist`, i.e. BELOW entry -
    so every breakeven update rewrote the stop to the same fixed level, the
    ratchet saw no improvement, and breakeven_pct appeared to do nothing.
    """
    entry, mark, liq = 0.005943, 0.006100, 0.005350
    old = clamp_sl(side="LONG", sl=entry, entry=entry, mark=mark, rules=AKE,
                   min_ticks=1, liq=liq)
    new = clamp_sl(side="LONG", sl=entry, entry=entry, mark=mark, rules=AKE,
                   min_ticks=1, liq=liq, allow_profit_side=True)
    assert old < entry, "the old behaviour is the bug"
    assert new == pytest.approx(entry), "the breakeven stop must survive"


def test_management_path_keeps_a_trailing_stop_where_it_is():
    entry, mark, liq = 0.005943, 0.006100, 0.005350
    trail = 0.006050
    new = clamp_sl(side="LONG", sl=trail, entry=entry, mark=mark, rules=AKE,
                   min_ticks=1, liq=liq, allow_profit_side=True)
    assert new == pytest.approx(trail), "a trailing stop must not be pulled back"


def test_management_path_still_fixes_a_stop_above_the_mark_in_loss():
    """A position in LOSS: mark < entry, so a breakeven stop at entry is ABOVE
    the mark and IS a 30029 violation. It must still be clamped."""
    entry, mark = 0.005000, 0.004900
    sl = clamp_sl(side="LONG", sl=entry, entry=entry, mark=mark, rules=AKE,
                  min_ticks=1, allow_profit_side=True)
    assert sl < mark, "the stop must clear the mark even on the management path"


def test_entry_plan_path_is_unchanged_by_the_new_flag():
    """The default must keep the old behaviour exactly."""
    entry, mark = 0.005051, 0.005062
    sl = clamp_sl(side="LONG", sl=entry, entry=entry, mark=mark, rules=AKE)
    assert sl < min(entry, mark)


def test_validate_accepts_a_profit_side_stop_only_when_it_clears_the_mark():
    entry, mark = 0.005943, 0.006100
    ok = validate_tpsl_side(symbol="AKEUSDT", side="LONG", entry=entry, mark=mark,
                            sl=entry, tp=None, rules=AKE, min_ticks=1,
                            allow_profit_side=True)
    assert ok == []
    # in loss, the same stop is a violation even with the flag on
    bad = validate_tpsl_side(symbol="AKEUSDT", side="LONG", entry=0.005000,
                             mark=0.004900, sl=0.005000, tp=None, rules=AKE,
                             min_ticks=1, allow_profit_side=True)
    assert bad, "a stop above the mark must still be reported"
