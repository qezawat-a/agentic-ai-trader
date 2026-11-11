"""`/symbols` must be able to SET the watchlist, not only look symbols up.

The operator typed "/symbols INITUSDT, ONEUSDT" and got
"no symbols matching 'INITUSDT, ONEUSDT'" - the old code filtered the
exchange's trading pairs with the WHOLE rest string as one substring,
which matches nothing, so the watchlist setter was unreachable from the
slash form (only the bare engine command "symbols ..." could set it).

Now:
  - exact, comma/space separated tokens that all exist  -> SET the watchlist
  - bare "/symbols"                                     -> the watchlist
  - "/symbols all"                                      -> the exchange list
  - a fragment ("/symbols INIT")                        -> substring lookup
  - a wrong name                                        -> named, nothing set
"""
from __future__ import annotations

import asyncio

from jrock.agent import JRockAgent
from jrock.config import Settings

PAIRS = [
    {"symbol": "BTCUSDT", "symbolStatus": "TRADING"},
    {"symbol": "INITUSDT", "symbolStatus": "TRADING"},
    {"symbol": "ONEUSDT", "symbolStatus": "TRADING"},
    {"symbol": "ININITUSDT", "symbolStatus": "TRADING"},
]


class PairClient:
    async def get_trading_pairs(self, symbols=None):
        return PAIRS

    async def get_trading_pairs_one(self, symbol):
        return next((p for p in PAIRS if p["symbol"] == symbol), None)


def _agent(tmp_path) -> JRockAgent:
    a = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    a.engine.data_dir = tmp_path
    a.engine.client = PairClient()  # type: ignore[assignment]
    return a


def _run(coro):
    return asyncio.run(coro)


def test_comma_separated_exact_symbols_set_the_watchlist(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("/symbols INITUSDT, ONEUSDT"))
    assert a.engine.state.symbols == ["INITUSDT", "ONEUSDT"]
    assert "INITUSDT,ONEUSDT" in out
    assert "watchlist is now: INITUSDT, ONEUSDT" in out


def test_space_separated_and_mixed_separators_set_too(tmp_path):
    a = _agent(tmp_path)
    _run(a.handle("/symbols INITUSDT ONEUSDT"))
    assert a.engine.state.symbols == ["INITUSDT", "ONEUSDT"]
    _run(a.handle("/symbols  BTCUSDT , ONEUSDT  "))
    assert a.engine.state.symbols == ["BTCUSDT", "ONEUSDT"]


def test_bare_symbols_shows_the_watchlist(tmp_path):
    a = _agent(tmp_path)
    _run(a.handle("/symbols INITUSDT, ONEUSDT"))
    out = _run(a.handle("/symbols"))
    assert "watchlist (2)" in out
    assert "INITUSDT" in out and "ONEUSDT" in out
    assert "tradable" in out


def test_bare_symbols_empty_watchlist_tells_how_to_set(tmp_path):
    a = _agent(tmp_path)
    a.engine.state.symbols = []
    out = _run(a.handle("/symbols"))
    assert "watchlist is empty" in out
    assert "symbols BTCUSDT,ETHUSDT" in out


def test_a_wrong_symbol_is_named_and_nothing_is_set(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("/symbols INITUSDT, NOPEUSDT"))
    assert "NOPEUSDT" in out, "the bad name must be called out"
    assert "not in the exchange" in out
    assert "nothing was set" in out
    assert a.engine.state.symbols == ["BTCUSDT"], "a partial set must not half-apply"


def test_a_fragment_still_looks_symbols_up(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("/symbols INIT"))
    assert "ININITUSDT" in out and "INITUSDT" in out
    assert a.engine.state.symbols == ["BTCUSDT"], "a fragment lookup must not set"


def test_symbols_all_lists_the_exchange(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("/symbols all"))
    assert "4 symbol(s)" in out


def test_setting_persists(tmp_path):
    a = _agent(tmp_path)
    _run(a.handle("/symbols INITUSDT, ONEUSDT"))
    b = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    b.engine.data_dir = tmp_path
    assert b.engine.state.symbols == ["INITUSDT", "ONEUSDT"]


def test_watchlist_capped_at_fifty(tmp_path):
    """GET /api/v1/futures/account/trading_settings documents max 50 symbols
    for its filter, and the watchlist feeds those calls. The cap lives in the
    ENGINE setter (the last gate before the state is written)."""
    a = _agent(tmp_path)
    fifty_one = ",".join(f"SYM{i:03d}USDT" for i in range(51))
    out = _run(a.engine.command(f"symbols {fifty_one}"))
    assert "max 50" in out
    assert len(a.engine.state.symbols) == 1  # unchanged (the BTCUSDT default)
    fifty = ",".join(f"SYM{i:03d}USDT" for i in range(50))
    out = _run(a.engine.command(f"symbols {fifty}"))
    assert len(a.engine.state.symbols) == 50


def test_trading_settings_symbols_filter_is_chunked_per_50(tmp_path):
    """The agent's per-symbol settings readback chunks the filter so even an
    over-long persisted watchlist never sends more than 50 symbols."""
    class CountingClient(PairClient):
        def __init__(self):
            self.calls: list[str | None] = []

        async def get_trading_pairs(self, symbols=None):
            return [{"symbol": f"SYM{i:03d}USDT"} for i in range(75)]

        async def get_trading_settings(self, symbols=None):
            self.calls.append(symbols)
            return []

    a = _agent(tmp_path)
    a.engine.state.symbols = [f"SYM{i:03d}USDT" for i in range(75)]
    client = CountingClient()
    a.engine.client = client  # type: ignore[assignment]
    _run(a._settings_per_symbol(
        [f"SYM{i:03d}USDT" for i in range(75)], lines=[]))
    assert client.calls == [
        ",".join(f"SYM{i:03d}USDT" for i in range(50)),
        ",".join(f"SYM{i:03d}USDT" for i in range(50, 75)),
    ]
