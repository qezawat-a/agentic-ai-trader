"""Regression tests for the official-doc audit fixes.

Every assertion here pins a parameter name, a path, or a required field that
was WRONG in the engine before this change. Each one would have failed at the
exchange, so each one is worth a test.

The docs audited (2026-11-07):
  Tp Sl private WS channel, funding rate (single/batch/history), kline,
  trading pairs, the 7 tp_sl REST endpoints, batch_order, adjust_position_margin.
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


# ==================================================== tp_sl: the wrong paths
def test_pending_tp_sl_uses_the_plural_path():
    """Was get_pending_order (singular) - the documented path is plural."""
    c, calls = _client()
    asyncio.run(c.get_pending_tp_sl_order(symbol="BTCUSDT"))
    assert calls[0]["path"] == "/api/v1/futures/tpsl/get_pending_orders"


def test_history_tp_sl_uses_the_plural_path():
    """Was get_history_order (singular) - the documented path is plural."""
    c, calls = _client()
    asyncio.run(c.get_history_tp_sl_order(symbol="BTCUSDT"))
    assert calls[0]["path"] == "/api/v1/futures/tpsl/get_history_orders"


def test_tp_sl_paging_uses_skip_and_limit_not_page_and_pagesize():
    """The docs specify skip/limit (max 100). page/pageSize is not a parameter."""
    c, calls = _client()
    asyncio.run(c.get_pending_tp_sl_order(symbol="BTCUSDT", skip=20, limit=50))
    params = calls[0]["params"]
    assert params["skip"] == 20 and params["limit"] == 50
    assert "page" not in params and "pageSize" not in params


def test_history_tp_sl_uses_starttime_endtime():
    c, calls = _client()
    asyncio.run(c.get_history_tp_sl_order(symbol="BTCUSDT",
                                          start_time=1, end_time=2))
    params = calls[0]["params"]
    assert params["startTime"] == 1 and params["endTime"] == 2


def test_cancel_tp_sl_requires_symbol_as_well_as_orderid():
    """Was orderId only - the docs require BOTH."""
    c, calls = _client()
    asyncio.run(c.cancel_tp_sl_order("BTCUSDT", "123"))
    body = calls[0]["body"]
    assert body == {"symbol": "BTCUSDT", "orderId": "123"}


def test_cancel_tp_sl_signature_refuses_a_missing_symbol():
    c, _ = _client()
    with pytest.raises(TypeError):
        asyncio.run(c.cancel_tp_sl_order("123"))  # type: ignore[call-arg]


# ==================================================== batch_order
def test_batch_order_sends_symbol_and_orderlist():
    """Was orderList only - symbol is a REQUIRED parameter."""
    c, calls = _client()
    asyncio.run(c.batch_order("BTCUSDT", [{"qty": "1"}]))
    body = calls[0]["body"]
    assert body["symbol"] == "BTCUSDT"
    assert body["orderList"] == [{"qty": "1"}]


def test_batch_order_refuses_more_than_five_orders():
    """The docs cap orderList at 5."""
    c, _ = _client()
    with pytest.raises(ValueError, match="at most 5"):
        asyncio.run(c.batch_order("BTCUSDT", [{}] * 6))


def test_batch_order_refuses_an_empty_list():
    c, _ = _client()
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(c.batch_order("BTCUSDT", []))


# ==================================================== adjust_position_margin
def test_adjust_position_margin_accepts_side_instead_of_positionid():
    """The docs say "Either side or positionId required" - side alone is valid."""
    c, calls = _client()
    asyncio.run(c.adjust_position_margin("BTCUSDT", "100", side="LONG"))
    body = calls[0]["body"]
    assert body["side"] == "LONG"
    assert "positionId" not in body
    assert body["amount"] == "100" and body["marginCoin"] == "USDT"


def test_adjust_position_margin_accepts_positionid():
    c, calls = _client()
    asyncio.run(c.adjust_position_margin("BTCUSDT", "100", position_id="P1"))
    body = calls[0]["body"]
    assert body["positionId"] == "P1" and "side" not in body


def test_adjust_position_margin_refuses_neither():
    c, _ = _client()
    with pytest.raises(ValueError, match="positionId or side"):
        asyncio.run(c.adjust_position_margin("BTCUSDT", "100"))


def test_adjust_position_margin_keeps_a_negative_amount():
    """A negative amount reduces margin - it must not be dropped as falsy."""
    c, calls = _client()
    asyncio.run(c.adjust_position_margin("BTCUSDT", "-50", side="SHORT"))
    assert calls[0]["body"]["amount"] == "-50"


# ==================================================== funding rate
def test_funding_rate_batch_does_not_require_symbols():
    """The docs mark `symbols` optional (omit = all symbols)."""
    c, calls = _client()
    asyncio.run(c.get_funding_rate_batch())
    assert calls[0]["path"] == "/api/v1/futures/market/funding_rate/batch"


def test_funding_rate_history_uses_starttime_endtime_limit():
    """Was page/pageSize - the docs specify startTime/endTime/limit."""
    c, calls = _client()
    asyncio.run(c.get_funding_rate_history("BTCUSDT", limit=50))
    params = calls[0]["params"]
    assert params["symbol"] == "BTCUSDT" and params["limit"] == 50
    assert "page" not in params and "pageSize" not in params


def test_funding_rate_single_endpoint_exists():
    c, calls = _client()
    asyncio.run(c.get_funding_rate("BTCUSDT"))
    assert calls[0]["path"] == "/api/v1/futures/market/funding_rate"


# ==================================================== kline / pairs
def test_kline_uses_type_not_klinetype():
    """The price-type selector is `type` (LAST_PRICE | MARK_PRICE)."""
    c, calls = _client()
    asyncio.run(c.get_kline("BTCUSDT", "1m", limit=10))
    params = calls[0]["params"]
    assert "type" in params
    assert "klineType" not in params


def test_trading_pairs_endpoint_path():
    c, calls = _client()
    asyncio.run(c.get_trading_pairs())
    assert calls[0]["path"] == "/api/v1/futures/market/trading_pairs"


# ==================================================== the api reference table
def test_api_reference_matches_the_fixed_paths():
    """The reference table is what a future reader trusts - it must not lie."""
    from jrock.trader import api_reference as ar

    by_name = ar.ENDPOINTS
    assert by_name["get_pending_tp_sl_order"].path.endswith("get_pending_orders")
    assert by_name["get_history_tp_sl_order"].path.endswith("get_history_orders")
    assert by_name["batch_order"].path.endswith("batch_order")
    assert by_name["adjust_position_margin"].path.endswith("adjust_position_margin")


def test_api_reference_marks_batch_order_symbol_required():
    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["batch_order"]
    names = {p.name: p for p in ep.params}
    assert names["symbol"].required, "symbol is required per the docs"
    assert names["orderList"].required


def test_api_reference_marks_cancel_tp_sl_symbol_required():
    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["cancel_tp_sl_order"]
    names = {p.name: p for p in ep.params}
    assert names["symbol"].required and names["orderId"].required


def test_api_reference_marks_adjust_margin_side_optional():
    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["adjust_position_margin"]
    names = {p.name: p for p in ep.params}
    assert not names["side"].required
    assert not names["positionId"].required
    assert names["symbol"].required and names["amount"].required


def test_api_reference_place_tp_sl_documents_tpqty():
    """tpQty/slQty are what make the Partial method possible."""
    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["place_tp_sl_order"]
    names = {p.name for p in ep.params}
    assert {"tpQty", "slQty", "tpStopType", "slStopType"} <= names


# ==================================================== private WS channel
def test_private_feed_subscribes_all_four_private_channels():
    """PRIVATE_CHANNELS was defined but never subscribed - only printed."""
    from jrock.trader.ws import PRIVATE_CHANNELS, PrivateFeed, WSConfig

    pf = PrivateFeed(WSConfig())
    subs = {s["ch"] for s in pf.ws.subs}
    assert subs == set(PRIVATE_CHANNELS.values())
    assert "tpsl" in subs


def test_private_feed_parses_a_tpsl_frame():
    """The documented Tp Sl push shape must update engine-visible state."""
    from jrock.trader.ws import Event, PrivateFeed, WSConfig

    pf = PrivateFeed(WSConfig())
    frame = {"ch": "tpsl", "ts": 1794011946619,
             "data": {"id": "T1", "positionId": "P1", "symbol": "BTCUSDT",
                      "tpPrice": "80000", "slPrice": "70000",
                      "tpQty": "1", "slQty": "1", "status": "NEW",
                      "ctime": 1794011946619}}
    ev = Event.from_message(frame)
    assert ev is not None
    pf._make_handler("tpsl")(ev)
    assert pf.counts["tpsl"] == 1
    assert pf.latest["tpsl"]["symbol"] == "BTCUSDT"
    assert pf.tpsl_events[0]["data"]["tpPrice"] == "80000"


def test_private_feed_tpsl_buffer_is_bounded():
    from jrock.trader.ws import Event, PrivateFeed, WSConfig

    pf = PrivateFeed(WSConfig())
    h = pf._make_handler("tpsl")
    for i in range(250):
        h(Event.from_message({"ch": "tpsl", "ts": i, "data": {"id": str(i)}}))
    assert len(pf.tpsl_events) == 200, "the buffer must not grow forever"


def test_private_feed_is_not_opened_in_rest_mode():
    """rest mode must still open ZERO sockets."""
    from jrock.trader.engine import TraderEngine

    s = Settings()
    s.market_data_source = "rest"
    e = TraderEngine(s)
    asyncio.run(e.start_feed())
    assert e.feed is None
    assert e.private_feed is None
    assert "not opened" in e.private_feed_report()


def test_private_feed_report_says_why_when_credentials_are_missing():
    from jrock.trader.engine import TraderEngine

    s = Settings()
    s.market_data_source = "ws"
    s.bitunix_api_key = ""
    s.bitunix_api_secret = ""
    e = TraderEngine(s)
    e.private_feed_error = "no BITUNIX_API_KEY/SECRET - private channels not subscribed"
    report = e.private_feed_report()
    assert "NOT connected" in report
    assert "BITUNIX_API_KEY" in report, "it must say WHY, not just fail silently"


# ==================================================== command reachability
def test_new_commands_are_reachable_from_the_agent():
    """A new endpoint that no command can reach is dead code."""
    from jrock.agent import AGENT_BARE, QUERY_COMMANDS

    assert AGENT_BARE["tpsl"] == "/tpsl"
    assert AGENT_BARE["batch"] == "/batch"
    assert "/tpsl" in QUERY_COMMANDS and "/batch" in QUERY_COMMANDS


def test_tpsl_command_lists_all_four_methods():
    from jrock.agent import JRockAgent

    a = JRockAgent(Settings())
    out = asyncio.run(a.cmd_tpsl(""))
    for m in ("POSITION", "PARTIAL", "TRAILING", "ACCOUNT"):
        assert m in out
    assert "exchange-enforced" in out
    assert "software" in out


def test_tpsl_command_sets_the_method():
    from jrock.agent import JRockAgent

    a = JRockAgent(Settings())
    asyncio.run(a.cmd_tpsl("method PARTIAL"))
    assert a.engine.state.tpsl_method == "PARTIAL"


def test_tpsl_command_refuses_an_unknown_method():
    from jrock.agent import JRockAgent

    a = JRockAgent(Settings())
    out = asyncio.run(a.cmd_tpsl("method NOPE"))
    assert "must be one of" in out
    assert a.engine.state.tpsl_method == "POSITION"


def test_tpsl_cancel_requires_both_arguments():
    from jrock.agent import JRockAgent

    a = JRockAgent(Settings())
    out = asyncio.run(a.cmd_tpsl("cancel BTCUSDT"))
    assert "usage" in out.lower()
    assert "symbol and orderId" in out


def test_batch_command_documents_the_required_params():
    from jrock.agent import JRockAgent

    a = JRockAgent(Settings())
    out = asyncio.run(a.cmd_batch(""))
    assert "symbol AND orderList" in out
    assert "max 5" in out


def test_funding_command_has_a_history_subcommand():
    """Funding history is a PUBLIC endpoint - it must work without credentials."""
    from jrock.agent import JRockAgent

    a = JRockAgent(Settings())
    out = asyncio.run(a.cmd_funding("history BTCUSDT"))
    assert "funding rate history" in out
    assert "BTCUSDT" in out


def test_funding_batch_is_filtered_client_side():
    """The live endpoint ignores `symbols` and returns every symbol."""
    from jrock.trader.client import BitunixClient

    rows = [{"symbol": "BTCUSDT", "fundingRate": "0.0001"},
            {"symbol": "ETHUSDT", "fundingRate": "0.0002"},
            {"symbol": "SOLUSDT", "fundingRate": "0.0003"}]
    out = BitunixClient.filter_funding_rows(rows, ["BTCUSDT", "SOLUSDT"])
    assert [r["symbol"] for r in out] == ["BTCUSDT", "SOLUSDT"]
    # no filter -> everything (the documented "omit = all symbols" behaviour)
    assert len(BitunixClient.filter_funding_rows(rows, [])) == 3


def test_funding_history_sends_limit_not_page():
    """The history endpoint takes limit, not page/pageSize."""
    c, calls = _client()
    asyncio.run(c.get_funding_rate_history("BTCUSDT", limit=25))
    params = calls[0]["params"]
    assert params["limit"] == 25
    assert "page" not in params and "pageSize" not in params


# ==================================================== 2026-11-10 second audit
# Live probes against fapi.bitunix.com returned {"code":404} for every path
# below, and the official pages do not contain them. They were removed rather
# than kept as "maybe it works" wrappers.
REMOVED_PATHS = (
    "/api/v1/futures/market/ticker",
    "/api/v1/futures/market/time",
    "/api/v1/futures/market/change_log",
    "/api/v1/futures/account/accounts",
    "/api/v1/futures/copyTrading/asset/query",
)


def test_removed_404_endpoints_are_gone_from_the_client():
    for name in ("get_ticker", "get_accounts", "change_log"):
        assert not hasattr(BitunixClient, name), f"{name} calls a 404 endpoint"


def test_no_removed_path_survives_in_the_client():
    import inspect

    src = inspect.getsource(__import__("jrock.trader.client", fromlist=["x"]))
    for path in REMOVED_PATHS:
        assert f'"{path}"' not in src, f"invented endpoint {path} is still in the client"


def test_removed_endpoints_are_gone_from_the_api_reference():
    from jrock.trader import api_reference as ar

    assert "change_log" not in ar.ENDPOINTS
    assert "get_accounts" not in ar.ENDPOINTS
    assert "get_ticker" not in ar.ENDPOINTS
    for ep in ar.ENDPOINTS.values():
        assert ep.path not in REMOVED_PATHS, f"{ep.name} points at a 404 path"


def test_asset_query_uses_the_official_copy_trading_path():
    """Official: GET /api/v1/cp/asset/query, no parameters."""
    c, calls = _client()
    asyncio.run(c.asset_query())
    assert calls[0]["path"] == "/api/v1/cp/asset/query"
    assert not calls[0]["params"]

    from jrock.trader import api_reference as ar

    ep = ar.ENDPOINTS["asset_query"]
    assert ep.path == "/api/v1/cp/asset/query"
    assert ep.params == []


def test_sync_time_uses_the_documented_ws_ping(monkeypatch):
    """There is no REST server-time endpoint; the public WS ping is the clock.

    The live socket sends the `connect` ack BEFORE the pong, so the first
    frame is the ack - sync_time must handle that, not read one frame and give
    up (a silent no-op is exactly the bug this replaced).
    """
    import json
    import time
    import websockets

    from jrock.trader.client import BitunixConfig

    c = BitunixClient(BitunixConfig())
    sent: list[str] = []
    server_seconds = 1_800_000_000
    server_ms = server_seconds * 1000

    class FakeWS:
        def __init__(self):
            self.frames = [
                json.dumps({"op": "connect", "ts": server_ms,
                            "data": {"result": True}}),
                json.dumps({"op": "ping", "pong": server_seconds,
                            "ping": server_seconds + 1}),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, raw):
            sent.append(raw)

        async def recv(self):
            return self.frames.pop(0)

    def fake_connect(url, **kw):
        assert url == "wss://fapi.bitunix.com/public/"
        return FakeWS()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    offset = asyncio.run(c.sync_time())

    assert json.loads(sent[0])["op"] == "ping"
    expected = server_ms - time.time() * 1000
    assert abs(offset - expected) < 10_000, "offset must track the server clock"


def test_sync_time_never_raises_and_keeps_the_old_offset(monkeypatch):
    import websockets

    from jrock.trader.client import BitunixConfig

    c = BitunixClient(BitunixConfig())
    c._time_offset_ms = 1234

    def boom(url, **kw):
        raise OSError("no route to host")

    monkeypatch.setattr(websockets, "connect", boom)
    assert asyncio.run(c.sync_time()) == 1234


# ==================================================== api reference contract
def test_api_reference_ws_channel_names_are_the_wire_names():
    from jrock.trader import api_reference as ar

    public = set(ar.WS_CHANNELS["public"])
    assert {"price", "ticker", "tickers", "trade", "depth_books"} <= public
    assert not {c for c in public if c.startswith("market_")
                and c != "market_kline_<interval>"}
    assert set(ar.WS_CHANNELS["private"]) == {"balance", "order", "position", "tpsl"}


def test_api_reference_signature_rule_does_not_say_urlencode():
    from jrock.trader import api_reference as ar

    assert "sorted+urlencoded" not in ar.SIGNATURE_RULE
    assert "key+value" in ar.SIGNATURE_RULE
    assert "NO '='" in ar.SIGNATURE_RULE


def test_api_reference_ws_login_orders_nonce_before_timestamp():
    """Official samples: sha256(nonce + timestamp + apiKey), then + secretKey.

    (The login *payload* lists apiKey/timestamp/nonce; the SIGNATURE preimage
    order is nonce, timestamp, apiKey - the old reference had the sign order
    wrong and no note about seconds.)
    """
    from jrock.trader import api_reference as ar

    sign_line = next(ln for ln in ar.WS_LOGIN_RULE.splitlines() if "sha256(sha256" in ln)
    assert sign_line.index("nonce") < sign_line.index("timestamp") < sign_line.index("apiKey")
    assert "SECONDS" in ar.WS_LOGIN_RULE


def test_api_reference_uses_skip_limit_for_every_paged_endpoint():
    from jrock.trader import api_reference as ar

    for name in ("get_pending_orders", "get_history_orders",
                 "get_history_trades", "get_history_positions",
                 "get_pending_tp_sl_order", "get_history_tp_sl_order"):
        names = {p.name for p in ar.ENDPOINTS[name].params}
        assert {"skip", "limit"} <= names, name
        assert not {"page", "pageSize"} & names, name


def test_api_reference_cancel_orders_requires_symbol():
    from jrock.trader import api_reference as ar

    names = {p.name: p for p in ar.ENDPOINTS["cancel_orders"].params}
    assert names["symbol"].required and names["orderList"].required
    # optional single-order form: "Either orderId or clientId is required"
    assert not names["orderId"].required and not names["clientId"].required


def test_api_reference_funding_rate_batch_has_no_request_params():
    """2026-11-10: the official page has no Request Parameters table - the
    old `symbols` filter is gone from the docs, so it must not be sent."""
    from jrock.trader import api_reference as ar

    assert ar.ENDPOINTS["get_funding_rate_batch"].params == []


# ==================================================== every documented param
# The 2026-11-10 parameter-coverage pass: one test per endpoint whose optional
# parameters were missing from the client, proving the fields reach the wire
# and that unset ones are omitted (request() drops None/"").


def test_pending_positions_sends_subaccount_and_includesubaccounts():
    from jrock.trader.client import url_query_string

    c, calls = _client()
    asyncio.run(c.get_pending_positions(sub_account_id=7, include_sub_accounts=True))
    params = calls[0]["params"]
    assert params["subAccountId"] == 7
    assert params["includeSubAccounts"] == "true"
    asyncio.run(c.get_pending_positions(symbol="BTCUSDT"))
    # the spy sits before request()'s central None-filter, so the unset
    # params are asserted on the REAL wire form - url_query_string()
    wire = url_query_string(calls[1]["params"])
    assert "subAccountId" not in wire and "includeSubAccounts" not in wire


def test_history_positions_sends_positionid_and_subaccountid():
    c, calls = _client()
    asyncio.run(c.get_history_positions(symbol="BTCUSDT", position_id="42",
                                        sub_account_id=9))
    params = calls[0]["params"]
    assert params["positionId"] == "42" and params["subAccountId"] == 9


def test_cancel_orders_orderlist_form_is_unchanged():
    c, calls = _client()
    asyncio.run(c.cancel_orders("BTCUSDT", [{"orderId": "1"}]))
    assert calls[0]["body"] == {"symbol": "BTCUSDT",
                                "orderList": [{"orderId": "1"}]}


def test_cancel_orders_single_order_form_sends_orderid_and_clientid():
    c, calls = _client()
    asyncio.run(c.cancel_orders("BTCUSDT", order_id="1"))
    assert calls[0]["body"] == {"symbol": "BTCUSDT", "orderId": "1"}
    asyncio.run(c.cancel_orders("BTCUSDT", client_id="c1"))
    assert calls[1]["body"] == {"symbol": "BTCUSDT", "clientId": "c1"}


def test_cancel_orders_refuses_an_empty_call():
    c, _ = _client()
    with pytest.raises(ValueError):
        asyncio.run(c.cancel_orders("BTCUSDT"))
    with pytest.raises(ValueError):
        asyncio.run(c.cancel_orders("BTCUSDT", []))


def test_pending_orders_sends_the_optional_filters():
    c, calls = _client()
    asyncio.run(c.get_pending_orders(symbol="BTCUSDT", order_id="1",
                                     client_id="c", status="NEW",
                                     start_time=100, end_time=200))
    params = calls[0]["params"]
    assert params["orderId"] == "1" and params["clientId"] == "c"
    assert params["status"] == "NEW"
    assert params["startTime"] == 100 and params["endTime"] == 200


def test_history_orders_sends_status_type_subaccount_and_querycanceled():
    from jrock.trader.client import url_query_string

    c, calls = _client()
    asyncio.run(c.get_history_orders(symbol="BTCUSDT", status="filled",
                                     order_type="limit", sub_account_id=3,
                                     query_canceled=True))
    params = calls[0]["params"]
    assert params["status"] == "FILLED"
    assert params["type"] == "LIMIT"
    assert params["subAccountId"] == 3
    assert params["queryCanceled"] == "true"
    asyncio.run(c.get_history_orders(symbol="BTCUSDT", query_canceled=False))
    assert calls[1]["params"]["queryCanceled"] == "false"
    asyncio.run(c.get_history_orders(symbol="BTCUSDT"))
    wire = url_query_string(calls[2]["params"])
    assert "queryCanceled" not in wire


def test_history_trades_sends_orderid_and_positionid():
    c, calls = _client()
    asyncio.run(c.get_history_trades(symbol="BTCUSDT", order_id="9",
                                     position_id="10"))
    params = calls[0]["params"]
    assert params["orderId"] == "9" and params["positionId"] == "10"


def test_funding_rate_batch_sends_no_parameters_anymore():
    """The docs removed `symbols`; the request must be parameter-less."""
    c, calls = _client()
    asyncio.run(c.get_funding_rate_batch("BTCUSDT,ETHUSDT"))
    assert calls[0]["params"] is None
    assert calls[0]["path"] == "/api/v1/futures/market/funding_rate/batch"


def test_wire_bool_renders_lowercase_json():
    from jrock.trader.client import _wire_bool

    assert _wire_bool(True) == "true"
    assert _wire_bool(False) == "false"
    assert _wire_bool(None) is None


def test_api_reference_modify_order_requires_qty():
    from jrock.trader import api_reference as ar

    names = {p.name: p for p in ar.ENDPOINTS["modify_order"].params}
    assert names["qty"].required, "qty is required by the docs"
    assert not names["price"].required, "price is only required for LIMIT"
    assert not names["orderId"].required and not names["clientId"].required


def test_api_reference_rate_limits_match_the_docs():
    from jrock.trader import api_reference as ar

    assert ar.ENDPOINTS["flash_close_position"].rate_limit == "5 req/sec/uid"
    assert ar.ENDPOINTS["close_all_position"].rate_limit == "1 req/sec/uid"
    assert ar.ENDPOINTS["cancel_orders"].rate_limit == "10 req/sec/uid"
    assert ar.ENDPOINTS["place_order"].rate_limit == "20 req/sec/uid"


def test_api_reference_place_order_mentions_hedge_only_tradeside():
    from jrock.trader import api_reference as ar

    names = {p.name: p for p in ar.ENDPOINTS["place_order"].params}
    assert "hedge" in names["tradeSide"].description.lower()


# ==================================================== account equity
def test_account_equity_sums_locked_margin_and_unrealized():
    from jrock.account import account_equity

    rows = [{"available": "100", "frozen": "5", "margin": "50",
             "crossUnrealizedPNL": "10", "isolationUnrealizedPNL": "-2"}]
    assert account_equity(rows) == 163.0


def test_account_equity_accepts_a_single_row_dict_and_sums_coins():
    from jrock.account import account_equity

    assert account_equity({"available": "1", "margin": "2"}) == 3.0
    assert account_equity([{"available": "1"}, {"available": "2"}]) == 3.0
    assert account_equity(None) == 0.0
