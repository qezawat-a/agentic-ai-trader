"""Regression tests for the second official-doc audit (2026-11-08).

Every assertion here pins a path, a parameter name, a type, or a required-set
rule that was WRONG in the engine before this change. Each one would have
failed at the exchange (404, 10002 Parameter Error, or 10007 Sign error), so
each one is worth a test.

Docs audited: the full official endpoint reference
(official-endpoints-bitunix.md) plus the live server, cross-checked against the
official demo repo (github.com/qezawat-a/open-api).
"""
from __future__ import annotations

import asyncio

import pytest

from jrock.config import Settings
from jrock.trader.client import BitunixClient


def _client() -> tuple[BitunixClient, list[dict]]:
    """A client whose transport is captured instead of sent."""
    c = BitunixClient(Settings())
    calls: list[dict] = []

    async def fake_request(method, path, **kw):
        calls.append({"method": method, "path": path,
                      "body": kw.get("body"), "params": kw.get("params")})
        return {"ok": True}

    c.request = fake_request  # type: ignore[assignment]
    return c, calls


# ==================================================== wrong endpoint paths
def test_get_ticker_uses_the_plural_documented_path():
    """`/market/ticker` (singular) is not documented and 404s live."""
    c, calls = _client()
    asyncio.run(c.get_ticker("BTCUSDT"))
    assert calls[0]["path"] == "/api/v1/futures/market/tickers"
    assert calls[0]["params"]["symbols"] == "BTCUSDT"


def test_get_accounts_uses_the_documented_account_path():
    """`/futures/account/accounts` is not documented and 404s live."""
    c, calls = _client()
    asyncio.run(c.get_accounts())
    assert calls[0]["path"] == "/api/v1/futures/account"
    assert calls[0]["params"]["marginCoin"] == "USDT"


def test_asset_query_uses_the_cp_path():
    """Was `/futures/copyTrading/asset/query`; the docs say `/api/v1/cp/asset/query`."""
    c, calls = _client()
    asyncio.run(c.asset_query())
    assert calls[0]["path"] == "/api/v1/cp/asset/query"
    # the docs list NO request parameters for this endpoint
    assert not calls[0]["params"]


def test_change_log_is_gone():
    """The Change Log page is documentation, not an endpoint - no path exists."""
    assert not hasattr(BitunixClient, "change_log")


def test_sync_time_does_not_call_the_nonexistent_time_endpoint():
    """`/market/time` is not documented and 404s live.

    Checked on the AST (string constants actually used in the code), not on the
    raw source, so the docstring that explains the old bug does not trip it.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(BitunixClient.sync_time))
    fn = ast.parse(src).body[0]
    # drop the docstring so the explanation of the old bug is not scanned
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    consts: set[str] = set()
    for node in body:
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                consts.add(n.value)
            elif isinstance(n, ast.JoinedStr):
                for part in n.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        consts.add(part.value)
    assert not any("market/time" in c for c in consts), \
        "sync_time must not call the nonexistent /market/time endpoint"
    assert any("market/tickers" in c for c in consts)


# ==================================================== wrong types / enums
def test_depth_limit_is_a_string_gear_enum():
    """The docs type `limit` as a string with gears 1/5/15/50/max."""
    c, calls = _client()
    asyncio.run(c.get_depth("BTCUSDT", limit="max"))
    assert calls[0]["params"]["limit"] == "max"


def test_depth_rejects_an_undocumented_gear():
    c, _ = _client()
    with pytest.raises(ValueError, match="limit must be one of"):
        asyncio.run(c.get_depth("BTCUSDT", limit="7"))


def test_kline_limit_is_capped_at_200():
    """The docs cap `limit` at 200."""
    c, calls = _client()
    asyncio.run(c.get_kline("BTCUSDT", "1m", limit=9999))
    assert calls[0]["params"]["limit"] == 200


def test_kline_rejects_an_undocumented_type():
    c, _ = _client()
    with pytest.raises(ValueError, match="LAST_PRICE or MARK_PRICE"):
        asyncio.run(c.get_kline("BTCUSDT", "1m", ktype="MID_PRICE"))


# ==================================================== undocumented params
def test_funding_rate_batch_sends_no_params():
    """The docs list NO request parameters; `symbols` was undocumented."""
    c, calls = _client()
    asyncio.run(c.get_funding_rate_batch())
    assert calls[0]["path"] == "/api/v1/futures/market/funding_rate/batch"
    assert not calls[0]["params"]


def test_asset_query_sends_no_margincoin():
    c, calls = _client()
    asyncio.run(c.asset_query())
    assert "marginCoin" not in (calls[0]["params"] or {})


# ==================================================== conditional required
def test_place_order_does_not_require_tradeside_unconditionally():
    """`tradeSide` is documented as \"Only required in hedge-mode\"."""
    c, calls = _client()
    asyncio.run(c.place_order(symbol="BTCUSDT", qty="1", side="BUY",
                              orderType="MARKET"))
    assert calls[0]["body"]["symbol"] == "BTCUSDT"


def test_place_order_requires_price_for_limit():
    c, _ = _client()
    with pytest.raises(ValueError, match="price is required"):
        asyncio.run(c.place_order(symbol="BTCUSDT", qty="1", side="BUY",
                                  orderType="LIMIT"))


def test_place_order_requires_positionid_when_closing():
    c, _ = _client()
    with pytest.raises(ValueError, match="positionId is required"):
        asyncio.run(c.place_order(symbol="BTCUSDT", qty="1", side="SELL",
                                  tradeSide="CLOSE", orderType="MARKET"))


def test_modify_order_rejects_the_undocumented_symbol_param():
    """`symbol` is not a documented modify_order parameter."""
    c, _ = _client()
    with pytest.raises(ValueError, match="does not take `symbol`"):
        asyncio.run(c.modify_order(orderId="1", symbol="BTCUSDT", qty="1"))


def test_modify_order_does_not_require_price_unconditionally():
    c, calls = _client()
    asyncio.run(c.modify_order(orderId="1", qty="1"))
    assert calls[0]["body"]["qty"] == "1"


# ==================================================== paging defaults
def test_paging_defaults_are_10_not_20():
    """The docs say limit default 10, max 100."""
    c, calls = _client()
    asyncio.run(c.get_pending_orders())
    assert calls[0]["params"]["limit"] == 10
    c, calls = _client()
    asyncio.run(c.get_history_orders())
    assert calls[0]["params"]["limit"] == 10
    c, calls = _client()
    asyncio.run(c.get_history_trades())
    assert calls[0]["params"]["limit"] == 10
    c, calls = _client()
    asyncio.run(c.get_history_positions())
    assert calls[0]["params"]["limit"] == 10


def test_paging_limit_is_capped_at_100():
    c, calls = _client()
    asyncio.run(c.get_pending_orders(limit=5000))
    assert calls[0]["params"]["limit"] == 100


# ==================================================== documented filters
def test_history_positions_accepts_positionid():
    c, calls = _client()
    asyncio.run(c.get_history_positions(position_id="P1"))
    assert calls[0]["params"]["positionId"] == "P1"


def test_pending_orders_accepts_documented_filters():
    c, calls = _client()
    asyncio.run(c.get_pending_orders(order_id="1", client_id="c1", status="NEW"))
    p = calls[0]["params"]
    assert p["orderId"] == "1" and p["clientId"] == "c1" and p["status"] == "NEW"


def test_history_orders_accepts_documented_filters():
    c, calls = _client()
    asyncio.run(c.get_history_orders(order_id="1", status="FILLED",
                                     order_type="LIMIT", query_canceled=True))
    p = calls[0]["params"]
    assert p["orderId"] == "1" and p["status"] == "FILLED"
    assert p["type"] == "LIMIT" and p["queryCanceled"] is True


def test_history_trades_accepts_positionid():
    c, calls = _client()
    asyncio.run(c.get_history_trades(position_id="P1"))
    assert calls[0]["params"]["positionId"] == "P1"


# ==================================================== tp/sl stop types
def test_position_tp_sl_stop_type_is_optional_not_hardcoded():
    """The docs mark tpStopType/slStopType optional; the old code forced MARK_PRICE."""
    c, calls = _client()
    asyncio.run(c.place_position_tp_sl_order("BTCUSDT", "P1", sl_price="70000"))
    body = calls[0]["body"]
    assert body["slPrice"] == "70000"
    assert "tpStopType" not in body and "slStopType" not in body


def test_position_tp_sl_requires_a_price():
    c, _ = _client()
    with pytest.raises(ValueError, match="at least one of tpPrice/slPrice"):
        asyncio.run(c.place_position_tp_sl_order("BTCUSDT", "P1"))


def test_modify_position_tp_sl_requires_a_price():
    c, _ = _client()
    with pytest.raises(ValueError, match="at least one of tpPrice/slPrice"):
        asyncio.run(c.modify_position_tp_sl_order("BTCUSDT", "P1"))


def test_modify_tp_sl_requires_a_price():
    c, _ = _client()
    with pytest.raises(ValueError, match="at least one of tpPrice/slPrice"):
        asyncio.run(c.modify_tp_sl_order("O1"))


# ==================================================== the api reference table
def test_api_reference_paths_match_the_fixed_client():
    from jrock.trader import api_reference as ar

    assert ar.ENDPOINTS["get_ticker"].path == "/api/v1/futures/market/tickers"
    assert ar.ENDPOINTS["get_accounts"].path == "/api/v1/futures/account"
    assert ar.ENDPOINTS["asset_query"].path == "/api/v1/cp/asset/query"
    assert "change_log" not in ar.ENDPOINTS


def test_api_reference_marks_tradeside_optional():
    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["place_order"]
    names = {p.name: p for p in ep.params}
    assert not names["tradeSide"].required, "tradeSide is hedge-mode only"


def test_api_reference_marks_modify_order_qty_required_price_optional():
    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["modify_order"]
    names = {p.name: p for p in ep.params}
    assert names["qty"].required
    assert not names["price"].required
    assert "symbol" not in names, "symbol is not a documented modify_order param"


def test_api_reference_depth_limit_is_a_string():
    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["get_depth"]
    names = {p.name: p for p in ep.params}
    assert names["limit"].type == "string"


def test_api_reference_funding_batch_has_no_params():
    from jrock.trader import api_reference as ar

    assert ar.ENDPOINTS["get_funding_rate_batch"].params == []


def test_signature_rule_documents_the_no_separator_query_form():
    from jrock.trader import api_reference as ar

    assert "NO '='" in ar.SIGNATURE_RULE
    assert "marginCoinUSDT" in ar.SIGNATURE_RULE


def test_ws_login_rule_order_is_nonce_then_timestamp():
    from jrock.trader import api_reference as ar

    assert "nonce + timestamp + apiKey" in ar.WS_LOGIN_RULE
