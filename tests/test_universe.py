"""Universe selection: rank, size, and the 24h liquidity floor.

Ported from the JavaScript engine's ``tests/test-universe-rank.js``. The bug
that test pins is the one that matters here too: with ``universe_rank=GAINERS``
and a ceiling larger than the qualifying set, the old code sorted and sliced,
so every flat and red name was swept into a "gainers" scan. The user's report
was literally "I set GAINERS but it isn't scanning gainers".

Everything below is pure - no network, no client.
"""
from __future__ import annotations

import pytest

from jrock.trader.universe import (MIN_POOL, UniverseItem, build_universe,
                                   candidates_from_tickers, rank_universe,
                                   tradable_pairs)

# A pool shaped like the live probe: a couple of real gainers, some flat, some
# red, at very different volumes.
POOL = [
    UniverseItem("WUSDT", qv=41e6, range=0.6, price=1, chg=42.8),
    UniverseItem("NEARUSDT", qv=77e6, range=0.1, price=1, chg=5.6),
    UniverseItem("ZECUSDT", qv=72e6, range=0.08, price=1, chg=4.7),
    UniverseItem("ETHUSDT", qv=1229e6, range=0.03, price=1, chg=1.3),
    UniverseItem("SUIUSDT", qv=25e6, range=0.05, price=1, chg=0.0),    # flat
    UniverseItem("BTCUSDT", qv=1815e6, range=0.02, price=1, chg=-0.1),  # red
    UniverseItem("SPCXUSDT", qv=35e6, range=0.04, price=1, chg=-0.2),   # red
    UniverseItem("HYPEUSDT", qv=24e6, range=0.07, price=1, chg=-0.4),   # red
    UniverseItem("XAUUSDT", qv=54e6, range=0.01, price=1, chg=-0.7),    # red
    UniverseItem("CRASH", qv=22e6, range=0.2, price=1, chg=-16.5),      # deep red
]


def syms(items):
    return [x.symbol for x in items]


# ---------------------------------------------------------------- GAINERS
def test_gainers_returns_only_coins_that_are_actually_up():
    g = rank_universe(POOL, "GAINERS")
    assert all(x.chg > 0 for x in g), "no flat or red name survives GAINERS"


def test_gainers_is_biggest_gain_first_and_drops_the_rest():
    g = rank_universe(POOL, "GAINERS")
    assert syms(g) == ["WUSDT", "NEARUSDT", "ZECUSDT", "ETHUSDT"]
    assert g[0].symbol == "WUSDT"


def test_gainers_never_pads_with_losers_when_the_ceiling_is_larger():
    """The exact bug: a ceiling bigger than the qualifying set must not pad."""
    g = rank_universe(POOL, "GAINERS")
    assert len(g) < len(POOL)
    assert not {"BTCUSDT", "SPCXUSDT", "HYPEUSDT", "XAUUSDT", "CRASH",
                "SUIUSDT"} & set(syms(g))


# ----------------------------------------------------------------- LOSERS
def test_losers_is_the_exact_mirror():
    lo = rank_universe(POOL, "LOSERS")
    assert all(x.chg < 0 for x in lo)
    assert lo[0].symbol == "CRASH", "deepest fall first"


def test_a_flat_coin_is_neither_a_gainer_nor_a_loser():
    assert "SUIUSDT" not in syms(rank_universe(POOL, "GAINERS"))
    assert "SUIUSDT" not in syms(rank_universe(POOL, "LOSERS"))


# ----------------------------------------------------------------- MOVERS
def test_movers_keeps_both_directions_biggest_absolute_first():
    m = rank_universe(POOL, "MOVERS")
    assert len(m) == len(POOL)
    assert m[0].symbol == "WUSDT"      # +42.8
    assert m[1].symbol == "CRASH"      # -16.5


# ----------------------------------------------------------------- VOLUME
def test_volume_ranks_by_liquidity_not_by_range():
    v = rank_universe(POOL, "VOLUME")
    assert v[0].symbol == "BTCUSDT", "deepest book first"
    assert v[1].symbol == "ETHUSDT"


def test_a_thin_high_range_coin_does_not_leapfrog_the_majors():
    """The incident this score exists for: BTC ranked 64th behind $600k coins."""
    v = rank_universe(POOL, "VOLUME")
    w = next(i for i, x in enumerate(v) if x.symbol == "WUSDT")
    assert w > 1


def test_default_mode_is_volume():
    assert syms(rank_universe(POOL)) == syms(rank_universe(POOL, "VOLUME"))


def test_unknown_mode_falls_back_to_volume_instead_of_raising():
    assert syms(rank_universe(POOL, "NONSENSE")) == syms(rank_universe(POOL, "VOLUME"))


def test_empty_pool_does_not_throw():
    assert rank_universe([], "GAINERS") == []
    assert rank_universe([], "VOLUME") == []


# ------------------------------------------------------- tradable filtering
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
    assert [c.symbol for c in candidates_from_tickers(tickers, {"AUSDT", "BUSDT", "CUSDT"})] == ["CUSDT"]


# ------------------------------------------------------------ build_universe
def _pairs(*symbols):
    return [{"symbol": s, "symbolStatus": "OPEN", "isApiSupported": True,
             "quote": "USDT"} for s in symbols]


def _tickers(rows):
    return [{"symbol": s, "lastPrice": str(p), "open": str(p / (1 + chg / 100)),
             "high": str(p * 1.05), "low": str(p * 0.95), "quoteVol": str(qv)}
            for s, p, qv, chg in rows]


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
