"""The leverage write is symbol-wide, but hedge mode stores LONG and SHORT
separately - the UI's "Sync Long/Short" toggle manages exactly those two
numbers, and `get_trading_settings` returns `longLeverage` / `shortLeverage`.

The operator's report: they sync both sides to 25x on the exchange UI, then
agent activity leaves Long and Short apart. The official write endpoint
(`POST /api/v1/futures/account/change_leverage`) has NO side parameter - only
`symbol`, `leverage`, `marginCoin` - so the agent cannot write one side; what
it DID do is write the tier-capped value symbol-wide and never show the
per-side result, so the drift was invisible until the user opened the UI.

Pinned here:

1. after a successful leverage write the engine reads the symbol back and
   notes what the exchange actually stored per side (long / short, synced
   or NOT synced) - the evidence lands in the entry notes,
2. a failed write never triggers the readback,
3. a failed readback is silent (a note must never break an entry),
4. `/settings` and `/leverage` show the per-side values instead of only the
   legacy symbol-wide field, with an explicit synced / NOT synced verdict.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.trader.engine import TraderEngine
from jrock.trader.errors import BitunixAPIError


class EngineClient:
    """The client the engine talks to during the leverage write."""

    def __init__(self, readback_rows=None, readback_error=None):
        self.change_margin_mode_calls: list[tuple] = []
        self.change_leverage_calls: list[tuple] = []
        self.readback_calls: list[str] = []
        self._rows = readback_rows
        self._readback_error = readback_error

    async def change_margin_mode(self, symbol, margin_mode):
        self.change_margin_mode_calls.append((symbol, margin_mode))
        return {}

    async def change_leverage(self, symbol, leverage, margin_coin="USDT"):
        self.change_leverage_calls.append((symbol, leverage, margin_coin))
        return {"data": [{"symbol": symbol, "leverage": leverage}]}

    async def get_trading_settings(self, symbols=None):
        self.readback_calls.append(symbols)
        if self._readback_error is not None:
            raise self._readback_error
        return self._rows


def _engine(client) -> TraderEngine:
    e = TraderEngine(Settings())
    e.client = client
    return e


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(sym="BTCUSDT", leverage=25, notes=[])


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- the readback
def test_synced_write_is_noted_as_synced():
    client = EngineClient(readback_rows=[
        {"symbol": "BTCUSDT", "marginCoin": "USDT", "marginMode": "CROSS",
         "leverage": 25, "longLeverage": 25, "shortLeverage": 25},
    ])
    e = _engine(client)
    ctx = _ctx()
    _run(e._apply_leverage_and_margin_mode(ctx))
    assert len(client.change_leverage_calls) == 1
    assert client.readback_calls == ["BTCUSDT"]
    assert any("long 25x / short 25x (synced)" in n for n in ctx.notes), ctx.notes


def test_drifted_write_is_flagged_not_synced():
    client = EngineClient(readback_rows=[
        {"symbol": "BTCUSDT", "marginCoin": "USDT", "marginMode": "CROSS",
         "leverage": 20, "longLeverage": 20, "shortLeverage": 25},
    ])
    e = _engine(client)
    ctx = _ctx()
    _run(e._apply_leverage_and_margin_mode(ctx))
    joined = "\n".join(ctx.notes)
    assert "long 20x / short 25x" in joined
    assert "NOT synced" in joined


def test_failed_write_never_reads_back():
    client = EngineClient()

    async def refuse(symbol, leverage, margin_coin="USDT"):
        raise BitunixAPIError(20003, "Insufficient margin balance")

    client.change_leverage = refuse
    e = _engine(client)
    ctx = _ctx()
    _run(e._apply_leverage_and_margin_mode(ctx))
    assert client.readback_calls == []
    assert "leverage BTCUSDT" in "\n".join(e.state.errors)


def test_failed_readback_is_silent():
    client = EngineClient(readback_error=BitunixAPIError(40007, "transient"))
    e = _engine(client)
    ctx = _ctx()
    _run(e._apply_leverage_and_margin_mode(ctx))
    assert ctx.notes == []
    # the write itself is not mistaken for an error
    assert not any("leverage BTCUSDT:" in err
                   for err in e.state.errors)


def test_legacy_row_without_side_fields_notes_nothing():
    client = EngineClient(readback_rows=[
        {"symbol": "BTCUSDT", "marginCoin": "USDT", "leverage": 25,
         "marginMode": "CROSS"},
    ])
    e = _engine(client)
    ctx = _ctx()
    _run(e._apply_leverage_and_margin_mode(ctx))
    assert ctx.notes == []


# ------------------------------------------------------- the display truth
class DisplayClient:
    """A client that only answers the settings reads."""

    def __init__(self, settings_rows, leverage_row):
        self.settings_rows = settings_rows
        self.leverage_row = leverage_row

    async def get_trading_pairs(self, symbols=None):
        return [{"symbol": "BTCUSDT", "symbolStatus": "TRADING"}]

    async def get_trading_settings(self, symbols=None):
        return self.settings_rows

    async def get_leverage_and_margin_mode(self, symbol, margin_coin="USDT"):
        return self.leverage_row


def _agent(client) -> JRockAgent:
    a = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    a.engine.client = client
    return a


def test_settings_symbol_shows_synced_sides():
    client = DisplayClient(
        settings_rows=[{"symbol": "BTCUSDT", "marginMode": "CROSS",
                        "leverage": 25, "longLeverage": 25, "shortLeverage": 25}],
        leverage_row={})
    out = _run(_agent(client).handle("settings BTCUSDT"))
    assert "leverage=25x (long 25x / short 25x, synced)" in out


def test_settings_symbol_shows_drifted_sides():
    client = DisplayClient(
        settings_rows=[{"symbol": "BTCUSDT", "marginMode": "CROSS",
                        "leverage": 20, "longLeverage": 20, "shortLeverage": 25}],
        leverage_row={})
    out = _run(_agent(client).handle("settings BTCUSDT"))
    assert "long 20x / short 25x - NOT synced" in out


def test_settings_symbol_legacy_row_stays_plain():
    client = DisplayClient(
        settings_rows=[{"symbol": "BTCUSDT", "marginMode": "CROSS",
                        "leverage": 20}],
        leverage_row={})
    out = _run(_agent(client).handle("settings BTCUSDT"))
    assert "leverage=20x marginMode=CROSS" in out
    assert "synced" not in out


def test_leverage_command_shows_the_sides():
    client = DisplayClient(
        settings_rows=[],
        leverage_row={"symbol": "BTCUSDT", "leverage": 25, "marginMode": "CROSS",
                      "longLeverage": 20, "shortLeverage": 25})
    out = _run(_agent(client).handle("leverage"))
    assert "BTCUSDT: leverage=20x (long 20x / short 25x - NOT synced)" in out
