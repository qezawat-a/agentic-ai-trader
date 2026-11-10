"""The exchange wraps order rows in an envelope; the dashboard must survive it.

DEF-1 (2026-11-10): ``BitunixClient.request()`` returns ``payload["data"]``
verbatim, and for ``get_pending_orders`` that ``data`` is
``{"orderList": [...], "total": N}`` - a DICT, not a list. Three symptoms
followed from that one shape assumption:

  1. ``_order_rows()`` iterated the dict, got its string keys, and called
     ``.get()`` on a ``str`` -> ``AttributeError``, which crashed
     ``GET /api/state`` whenever open orders existed;
  2. ``_account_summary()`` computed ``len(orders or [])``, which counted the
     dict's KEYS (2) instead of the orders - a silent wrong number, no crash;
  3. ``JRockAgent.cmd_orders()`` looped over the same value outside its
     try/except, so the chat command ``orders`` raised the same error uncaught.

Every existing dashboard test stubbed the client with a BARE LIST, which is a
shape the exchange never sends - that is why 1080 green tests missed this. The
tests here stub the REAL envelope instead, and assert the three symptoms are
gone. They are deliberately end-to-end through ``/api/state`` rather than
calling the helpers directly, so a future refactor cannot quietly reintroduce
the crash behind a passing unit test.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.trader.client import order_rows
from jrock.web import make_app

TOKEN = "test-token"

#: Three real orders, in the envelope the exchange actually returns.
ORDER_ENVELOPE = {
    "orderList": [
        {"symbol": "BTCUSDT", "side": "BUY", "orderType": "LIMIT", "qty": "0.01",
         "price": "49000", "status": "NEW", "orderId": "ord-btc"},
        {"symbol": "ETHUSDT", "side": "SELL", "orderType": "LIMIT", "qty": "0.2",
         "price": "3000", "status": "NEW", "orderId": "ord-eth"},
        {"symbol": "SOLUSDT", "side": "BUY", "orderType": "LIMIT", "qty": "5",
         "price": "100", "status": "NEW", "orderId": "ord-sol"},
    ],
    "total": 3,
}

ACCOUNT_ROW = {
    "marginCoin": "USDT", "available": "1000.5", "frozen": "0",
    "margin": "10", "transfer": "1000.5", "positionMode": "ONE_WAY",
    "crossUnrealizedPNL": "0", "isolationUnrealizedPNL": "2.5", "bonus": "0",
}


def _agent(tmp_path) -> JRockAgent:
    s = Settings(symbols=["BTCUSDT"], market_data_source="rest")
    s.bitunix_api_key = "k"
    s.bitunix_api_secret = "s"
    a = JRockAgent(s)
    a.engine.data_dir = tmp_path
    return a


def _stub(agent: JRockAgent, *, orders) -> None:
    """Stub the client with the REAL envelope shape, not a bare list."""
    async def _account(*a, **k):
        return [ACCOUNT_ROW]

    async def _positions(*a, **k):
        return []

    async def _orders(*a, **k):
        return orders

    async def _verify():
        return (True, "OK - account reachable (1 margin coin(s))")

    agent.engine.client.get_single_account = _account
    agent.engine.client.get_pending_positions = _positions
    agent.engine.client.get_pending_orders = _orders
    agent.engine.client.verify_credentials = _verify


def _run(coro):
    return asyncio.run(coro)


async def _with_client(tmp_path, fn, *, orders):
    agent = _agent(tmp_path)
    _stub(agent, orders=orders)
    app = make_app(agent, TOKEN)
    # Skip the startup hook: it calls the network (model probe, Bitunix auth).
    app.on_startup.clear()
    app.on_cleanup.clear()
    try:
        async with TestClient(TestServer(app)) as client:
            return await fn(client, agent)
    finally:
        await agent.engine.stop()


# ===================================================== the crash (symptom 1)
def test_state_returns_200_when_orders_come_back_in_an_envelope(tmp_path):
    """GET /api/state must not 500 just because open orders exist.

    This is the exact production failure: the traceback ended in
    ``_order_rows`` -> ``AttributeError: 'str' object has no attribute 'get'``.
    """
    async def go(client, agent):
        r = await client.get("/api/state", headers={"X-Auth-Token": TOKEN})
        assert r.status == 200, await r.text()
        return await r.json()
    body = _run(_with_client(tmp_path, go, orders=ORDER_ENVELOPE))
    assert body["orders"] is not None


def test_one_order_row_renders_from_the_envelope(tmp_path):
    """The rows must be the ORDERS, not the envelope's keys."""
    async def go(client, agent):
        return await (await client.get("/api/state",
                                       headers={"X-Auth-Token": TOKEN})).json()
    body = _run(_with_client(tmp_path, go, orders=ORDER_ENVELOPE))

    rows = body["orders"]
    assert len(rows) == 3
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    first = rows[0]
    assert first["side"] == "BUY"
    assert first["type"] == "LIMIT"
    assert first["id"] == "ord-btc"
    # The failure mode was rendering the string "orderList" as a row.
    assert all(isinstance(r["symbol"], str) and r["symbol"] != "orderList"
               for r in rows)


# ================================================== the miscount (symptom 2)
def test_open_orders_counts_rows_not_envelope_keys(tmp_path):
    """``open_orders`` must be 3, not the 2 keys of the envelope dict."""
    async def go(client, agent):
        return await (await client.get("/api/state",
                                       headers={"X-Auth-Token": TOKEN})).json()
    body = _run(_with_client(tmp_path, go, orders=ORDER_ENVELOPE))
    assert body["account"]["open_orders"] == 3
    assert body["account"]["open_orders"] == len(ORDER_ENVELOPE["orderList"])


def test_open_orders_is_zero_for_an_empty_envelope(tmp_path):
    """An empty order book is 0 orders - and must not crash either."""
    async def go(client, agent):
        r = await client.get("/api/state", headers={"X-Auth-Token": TOKEN})
        assert r.status == 200
        return await r.json()
    body = _run(_with_client(tmp_path, go, orders={"orderList": [], "total": 0}))
    assert body["account"]["open_orders"] == 0
    assert body["orders"] == []


# ============================================ the chat command (symptom 3)
def test_cmd_orders_renders_the_envelope_instead_of_raising(tmp_path):
    """The chat command ``orders`` must return text, not raise AttributeError."""
    async def go():
        agent = _agent(tmp_path)
        _stub(agent, orders=ORDER_ENVELOPE)
        try:
            out = await agent.cmd_orders()
        finally:
            await agent.engine.stop()
        return out
    out = _run(go())
    assert "=== pending orders ===" in out
    assert "BTCUSDT" in out and "ord-btc" in out
    assert "orderList" not in out
    assert "AttributeError" not in out


def test_cmd_orders_reports_no_orders_for_an_empty_envelope(tmp_path):
    async def go():
        agent = _agent(tmp_path)
        _stub(agent, orders={"orderList": [], "total": 0})
        try:
            return await agent.cmd_orders()
        finally:
            await agent.engine.stop()
    assert _run(go()) == "no pending orders"


# ================================================ the normaliser itself
@pytest.mark.parametrize("payload,expected", [
    (None, 0),
    ([], 0),
    ({}, 0),
    ("nonsense", 0),
    ([{"symbol": "A"}, {"symbol": "B"}], 2),
    ({"orderList": [{"symbol": "A"}] * 3, "total": 3}, 3),
    ({"orderList": []}, 0),
    ({"orderId": "x", "symbol": "BTCUSDT"}, 1),
])
def test_order_rows_normalises_every_shape(payload, expected):
    """One normaliser, every shape the exchange might hand us."""
    rows = order_rows(payload)
    assert isinstance(rows, list)
    assert len(rows) == expected
    assert all(isinstance(r, dict) for r in rows)


def test_order_rows_drops_non_dict_entries():
    """A malformed row must be skipped, not crash the whole readout."""
    rows = order_rows([{"symbol": "A"}, "junk", None, {"symbol": "B"}])
    assert [r["symbol"] for r in rows] == ["A", "B"]
