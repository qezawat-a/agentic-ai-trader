"""Machine-readable Bitunix API reference, embedded in the agent.

Every endpoint the engine can call, with method, path, auth requirement, and the
exact parameter contract from the official docs (docs/bitunix/).

The agent uses this to:
  * validate a call before sending it (missing required param -> clear error)
  * answer "what does this endpoint need?" without guessing
  * power /check_system and /autopsy with the real contract
"""
from __future__ import annotations

from dataclasses import dataclass, field

BASE_URL = "https://fapi.bitunix.com"
WS_PUBLIC = "wss://fapi.bitunix.com/public/"
WS_PRIVATE = "wss://fapi.bitunix.com/private/"


@dataclass
class Param:
    name: str
    type: str
    required: bool = False
    description: str = ""


@dataclass
class Endpoint:
    name: str
    method: str
    path: str
    signed: bool
    rate_limit: str = ""
    description: str = ""
    params: list[Param] = field(default_factory=list)
    doc: str = ""

    def validate(self, kwargs: dict) -> list[str]:
        missing = [p.name for p in self.params if p.required and p.name not in kwargs]
        unknown = [k for k in kwargs if k not in {p.name for p in self.params}]
        errs = []
        if missing:
            errs.append(f"missing required: {', '.join(missing)}")
        if unknown:
            errs.append(f"unknown params: {', '.join(unknown)}")
        return errs


def _p(name, type_, required=False, desc=""):
    return Param(name, type_, required, desc)


ENDPOINTS: dict[str, Endpoint] = {}


def _add(ep: Endpoint) -> None:
    ENDPOINTS[ep.name] = ep


# ------------------------------------------------------------------ market
_add(Endpoint("get_tickers", "GET", "/api/v1/futures/market/tickers", False,
              "10 req/sec/ip", "All tickers or a symbol list (includes markPrice)",
              [_p("symbols", "string", False, "BTCUSDT,ETHUSDT")], "get_tickers.md"))
_add(Endpoint("get_depth", "GET", "/api/v1/futures/market/depth", False,
              "10 req/sec/ip", "Order book depth",
              [_p("symbol", "string", True), _p("limit", "int", False, "default 15")], "get_depth.md"))
_add(Endpoint("get_kline", "GET", "/api/v1/futures/market/kline", False,
              "10 req/sec/ip", "Klines (closed candles only; startTime is NOT supported)",
              [_p("symbol", "string", True), _p("interval", "string", True,
                                                "1m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M"),
               _p("limit", "int", False, "default 100, max 200"),
               _p("endTime", "int64", False, "exclusive end bound, ms"),
               _p("type", "string", False, "LAST_PRICE | MARK_PRICE")], "get_kline.md"))
_add(Endpoint("get_trading_pairs", "GET", "/api/v1/futures/market/trading_pairs", False,
              "10 req/sec/ip", "Pair rules: precision, min/max volume, leverage",
              [_p("symbols", "string", False)], "get_trading_pairs.md"))
_add(Endpoint("get_funding_rate", "GET", "/api/v1/futures/market/funding_rate", False,
              "10 req/sec/ip", "Current funding rate", [_p("symbol", "string", True)],
              "funding_rate.md"))
_add(Endpoint("get_funding_rate_batch", "GET", "/api/v1/futures/market/funding_rate/batch", False,
              "10 req/sec/ip",
              "Funding rates for every symbol - NO request parameters "
              "(the old `symbols` filter is gone from the docs and ignored "
              "by the server; filter locally, e.g. filter_funding_rows)",
              [], "funding_rate_batch.md"))
_add(Endpoint("get_funding_rate_history", "GET",
              "/api/v1/futures/market/get_funding_rate_history",
              False, "10 req/sec/ip", "Funding rate history (note the get_ prefix)",
              [_p("symbol", "string", True), _p("startTime", "int64", False),
               _p("endTime", "int64", False),
               _p("limit", "int", False, "default 100, max 200")],
              "funding_rate_history.md"))

# ------------------------------------------------------------------ account
_add(Endpoint("get_single_account", "GET", "/api/v1/futures/account", True,
              "10 req/sec/uid", "Account for one margin coin (list, one row per coin)",
              [_p("marginCoin", "string", True, "USDT")], "get_single_account.md"))
_add(Endpoint("change_leverage", "POST", "/api/v1/futures/account/change_leverage", True,
              "10 req/sec/uid", "Set leverage for a symbol",
              [_p("marginCoin", "string", True), _p("symbol", "string", True),
               _p("leverage", "int", True)], "change_leverage.md"))
_add(Endpoint("change_margin_mode", "POST", "/api/v1/futures/account/change_margin_mode", True,
              "10 req/sec/uid", "ISOLATION | CROSS",
              [_p("symbol", "string", True), _p("marginMode", "string", True),
               _p("marginCoin", "string", True)], "change_margin_mode.md"))
_add(Endpoint("change_position_mode", "POST", "/api/v1/futures/account/change_position_mode", True,
              "10 req/sec/uid", "HEDGE | ONE_WAY",
              [_p("positionMode", "string", True)],
              "change_position_mode.md"))
_add(Endpoint("get_leverage_and_margin_mode", "GET",
              "/api/v1/futures/account/get_leverage_margin_mode", True, "10 req/sec/uid",
              "Current leverage + margin mode",
              [_p("symbol", "string", True), _p("marginCoin", "string", True)],
              "get_leverage_and_margin_mode.md"))
_add(Endpoint("get_position_mode", "GET", "/api/v1/futures/account/position_mode", True,
              "20 req/sec/uid", "The account's futures position mode (one-way or hedge)",
              [], "get_position_mode.md"))
_add(Endpoint("get_trading_settings", "GET", "/api/v1/futures/account/trading_settings", True,
              "20 req/sec/uid", "Per-symbol margin mode and leverage",
              [_p("symbols", "string", False, "comma-separated, max 50; omit for all")],
              "get_trading_settings.md"))
_add(Endpoint("adjust_position_margin", "POST",
              "/api/v1/futures/account/adjust_position_margin", True, "5 req/sec/uid",
              "Add/remove isolated margin (side OR positionId required)",
              [_p("symbol", "string", True), _p("marginCoin", "string", True),
               _p("amount", "string", True, "positive increases, negative reduces"),
               _p("side", "string", False, "LONG | SHORT"),
               _p("positionId", "string", False)],
              "adjust_position_margin.md"))
_add(Endpoint("asset_query", "GET", "/api/v1/cp/asset/query", True,
              "10 req/sec/uid", "Copy-trading asset query (no request parameters)",
              [], "asset_query.md"))

# ------------------------------------------------------------------ position
_add(Endpoint("get_pending_positions", "GET", "/api/v1/futures/position/get_pending_positions",
              True, "10 req/sec/uid", "Open positions",
              [_p("symbol", "string", False), _p("positionId", "string", False),
               _p("subAccountId", "int64", False), _p("includeSubAccounts", "bool", False)],
              "get_pending_positions.md"))
_add(Endpoint("get_history_positions", "GET", "/api/v1/futures/position/get_history_positions",
              True, "10 req/sec/uid", "Closed positions",
              [_p("symbol", "string", False), _p("positionId", "string", False),
               _p("startTime", "int64", False),
               _p("endTime", "int64", False),
               _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100"),
               _p("subAccountId", "int64", False)], "Position_history.md"))
_add(Endpoint("get_position_tiers", "GET", "/api/v1/futures/position/get_position_tiers", False,
              "10 req/sec/ip", "Risk tiers + maintenance margin rate",
              [_p("symbol", "string", True)], "get_position_tiers.md"))

# ------------------------------------------------------------------ trade
_add(Endpoint("place_order", "POST", "/api/v1/futures/trade/place_order", True,
              "20 req/sec/uid", "Place an order (qty is BASE COIN)",
              [_p("symbol", "string", True), _p("qty", "string", True, "base coin amount"),
               _p("side", "string", True, "BUY | SELL"),
               _p("tradeSide", "string", False,
                  "OPEN | CLOSE - only required in hedge mode; omit in one-way"),
               _p("orderType", "string", True, "LIMIT | MARKET"),
               _p("price", "string", False, "required for LIMIT"),
               _p("positionId", "string", False, "required when tradeSide=CLOSE"),
               _p("effect", "string", False, "IOC | FOK | GTC | POST_ONLY"),
               _p("clientId", "string", False), _p("reduceOnly", "boolean", False),
               _p("tpPrice", "string", False), _p("tpStopType", "string", False,
                                                  "MARK_PRICE | LAST_PRICE"),
               _p("tpOrderType", "string", False, "LIMIT | MARKET"),
               _p("tpOrderPrice", "string", False),
               _p("slPrice", "string", False), _p("slStopType", "string", False),
               _p("slOrderType", "string", False), _p("slOrderPrice", "string", False)],
              "place_order.md"))
_add(Endpoint("batch_order", "POST", "/api/v1/futures/trade/batch_order", True,
              "2 req/sec/uid", "Batch place orders (orderList max 5)",
              [_p("symbol", "string", True), _p("orderList", "array", True)],
              "batch_order.md"))
_add(Endpoint("cancel_orders", "POST", "/api/v1/futures/trade/cancel_orders", True,
              "10 req/sec/uid",
              "Cancel specific orders (symbol required; orderList, or orderId/"
              "clientId for a single-order cancel)",
              [_p("symbol", "string", True), _p("orderList", "array", True),
               _p("orderId", "string", False,
                  "Either orderId or clientId is required; orderId prevails"),
               _p("clientId", "string", False,
                  "Either orderId or clientId is required; orderId prevails")],
              "cancel_orders.md"))
_add(Endpoint("cancel_all_orders", "POST", "/api/v1/futures/trade/cancel_all_orders", True,
              "20 req/sec/uid", "Cancel all open orders",
              [_p("symbol", "string", False)], "cancel_all_orders.md"))
_add(Endpoint("modify_order", "POST", "/api/v1/futures/trade/modify_order", True,
              "20 req/sec/uid", "Modify a pending order (orderId or clientId required)",
              [_p("orderId", "string", False), _p("clientId", "string", False),
               _p("qty", "string", True, "amount (base coin)"),
               _p("price", "string", False, "required for LIMIT"),
               _p("tpPrice", "string", False), _p("tpStopType", "string", False),
               _p("tpOrderType", "string", False), _p("tpOrderPrice", "string", False),
               _p("slPrice", "string", False), _p("slStopType", "string", False),
               _p("slOrderType", "string", False), _p("slOrderPrice", "string", False)],
              "modify_order.md"))
_add(Endpoint("get_pending_orders", "GET", "/api/v1/futures/trade/get_pending_orders", True,
              "10 req/sec/uid", "Open orders",
              [_p("symbol", "string", False), _p("orderId", "string", False),
               _p("clientId", "string", False),
               _p("status", "string", False, "NEW | PART_FILLED"),
               _p("startTime", "int64", False), _p("endTime", "int64", False),
               _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100")],
              "get_pending_orders.md"))
_add(Endpoint("get_history_orders", "GET", "/api/v1/futures/trade/get_history_orders", True,
              "10 req/sec/uid", "Order history",
              [_p("symbol", "string", False), _p("orderId", "string", False),
               _p("clientId", "string", False),
               _p("status", "string", False,
                  "FILLED | CANCELED | PART_FILLED_CANCELED | EXPIRED"),
               _p("type", "string", False, "LIMIT | MARKET"),
               _p("startTime", "int64", False), _p("endTime", "int64", False),
               _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100"),
               _p("subAccountId", "int64", False),
               _p("queryCanceled", "boolean", False, "true = canceled only (3d range)")],
              "get_history_orders.md"))
_add(Endpoint("get_order_detail", "GET", "/api/v1/futures/trade/get_order_detail", True,
              "10 req/sec/uid", "One order by id (orderId OR clientId required)",
              [_p("orderId", "string", False), _p("clientId", "string", False)],
              "get_order_detail.md"))
_add(Endpoint("get_history_trades", "GET", "/api/v1/futures/trade/get_history_trades", True,
              "10 req/sec/uid", "Fill history",
              [_p("symbol", "string", False), _p("orderId", "string", False),
               _p("positionId", "string", False),
               _p("startTime", "int64", False), _p("endTime", "int64", False),
               _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100")],
              "get_history_trades.md"))
_add(Endpoint("close_all_position", "POST", "/api/v1/futures/trade/close_all_position", True,
              "1 req/sec/uid", "Close all positions (market)",
              [_p("symbol", "string", False)], "close_all_position.md"))
_add(Endpoint("flash_close_position", "POST", "/api/v1/futures/trade/flash_close_position", True,
              "5 req/sec/uid", "Close one position at market",
              [_p("positionId", "string", True)], "flash_close_position.md"))

# ------------------------------------------------------------------ tp/sl
_add(Endpoint("place_tp_sl_order", "POST", "/api/v1/futures/tpsl/place_order", True,
              "20 req/sec/uid", "Standalone TP/SL order - the PARTIAL method (tpQty/slQty)",
              [_p("symbol", "string", True), _p("positionId", "string", True),
               _p("tpPrice", "string", False), _p("tpStopType", "string", False,
                                                  "LAST_PRICE | MARK_PRICE"),
               _p("slPrice", "string", False), _p("slStopType", "string", False),
               _p("tpOrderType", "string", False, "LIMIT | MARKET"),
               _p("tpOrderPrice", "string", False), _p("slOrderType", "string", False),
               _p("slOrderPrice", "string", False), _p("tpQty", "string", False),
               _p("slQty", "string", False)], "place_tp_sl_order.md"))
_add(Endpoint("place_position_tp_sl_order", "POST", "/api/v1/futures/tpsl/position/place_order",
              True, "20 req/sec/uid", "Attach TP/SL to a position (one per position)",
              [_p("symbol", "string", True), _p("positionId", "string", True),
               _p("tpPrice", "string", False), _p("tpStopType", "string", False,
                                                  "LAST_PRICE | MARK_PRICE"),
               _p("slPrice", "string", False), _p("slStopType", "string", False)],
              "place_position_tp_sl_order.md"))
_add(Endpoint("modify_tp_sl_order", "POST", "/api/v1/futures/tpsl/modify_order", True,
              "20 req/sec/uid", "Modify a standalone TP/SL order",
              [_p("orderId", "string", True), _p("tpPrice", "string", False),
               _p("tpStopType", "string", False), _p("slPrice", "string", False),
               _p("slStopType", "string", False), _p("tpOrderType", "string", False),
               _p("tpOrderPrice", "string", False), _p("slOrderType", "string", False),
               _p("slOrderPrice", "string", False), _p("tpQty", "string", False),
               _p("slQty", "string", False)], "modify_tp_sl_order.md"))
_add(Endpoint("modify_position_tp_sl_order", "POST",
              "/api/v1/futures/tpsl/position/modify_order", True, "20 req/sec/uid",
              "Modify a position's TP/SL (used for breakeven + trailing)",
              [_p("symbol", "string", True), _p("positionId", "string", True),
               _p("tpPrice", "string", False), _p("tpStopType", "string", False),
               _p("slPrice", "string", False), _p("slStopType", "string", False)],
              "modify_position_tp_sl_order.md"))
_add(Endpoint("cancel_tp_sl_order", "POST", "/api/v1/futures/tpsl/cancel_order", True,
              "10 req/sec/uid", "Cancel a TP/SL order (symbol AND orderId required)",
              [_p("symbol", "string", True), _p("orderId", "string", True)],
              "cancel_tp_sl_order.md"))
_add(Endpoint("get_pending_tp_sl_order", "GET", "/api/v1/futures/tpsl/get_pending_orders", True,
              "10 req/sec/uid", "Pending TP/SL orders (skip/limit, max 100)",
              [_p("symbol", "string", False), _p("positionId", "string", False),
               _p("side", "int32", False), _p("positionMode", "int32", False),
               _p("skip", "int64", False), _p("limit", "int64", False)],
              "get_pending_tp_sl_order.md"))
_add(Endpoint("get_history_tp_sl_order", "GET", "/api/v1/futures/tpsl/get_history_orders", True,
              "10 req/sec/uid", "TP/SL history (skip/limit, max 100)",
              [_p("symbol", "string", False), _p("side", "int32", False),
               _p("positionMode", "int32", False), _p("startTime", "int64", False),
               _p("endTime", "int64", False), _p("skip", "int64", False),
               _p("limit", "int64", False)], "get_history_tp_sl_order.md"))

# ------------------------------------------------------------------ websocket
#: Names are exact wire strings: the server happily ACKs a wrong channel name
#: ({"op":"connect","data":{"result":true}}) and then never pushes anything.
WS_CHANNELS = {
    "public": {
        "price": "market price (mp), index price (ip), funding rate (fr)",
        "ticker": "one symbol, 24h rolling stats",
        "tickers": "all symbols, aggregated",
        "trade": "public trades",
        "depth_books": "full depth snapshot, then deltas",
        "depth_book1/5/15": "N-level depth",
        "market_kline_<interval>": ("kline; suffixes 1min 3min 5min 15min 30min 60min "
                                    "2h 4h 6h 8h 12h 1day 3day 1week 1month "
                                    "(1h is 60min, never '1hour')"),
    },
    "private": {
        "balance": "account balance updates",
        "order": "order updates",
        "position": "position updates",
        "tpsl": "TP/SL updates",
    },
}

SIGNATURE_RULE = (
    "REST (timestamp in MILLISECONDS):\n"
    "message = nonce + timestamp + apiKey + queryString + body\n"
    "digest  = sha256(message)\n"
    "sign    = sha256(digest + secretKey)\n"
    "headers: api-key, sign, nonce, timestamp, language, Content-Type\n"
    "queryString = sorted-by-key 'key+value' concatenation, NO '=' and NO '&'\n"
    "body        = compact JSON, keys sorted, identical to the bytes sent\n"
    "urlencoding the query for signing is the #1 cause of error 10007"
)

WS_LOGIN_RULE = (
    "login args: {op:'login', args:[{apiKey, nonce, timestamp, sign}]}\n"
    "sign = sha256(sha256(nonce + timestamp + apiKey) + secretKey)\n"
    "order is nonce, then timestamp, then apiKey; timestamp in SECONDS; nonce < 64 chars"
)


def describe_endpoint(name: str) -> str:
    ep = ENDPOINTS.get(name)
    if not ep:
        return f"unknown endpoint {name!r}. Known: {', '.join(sorted(ENDPOINTS))}"
    lines = [f"{ep.name}: {ep.method} {ep.path}",
             f"  auth: {'signed' if ep.signed else 'public'}  rate: {ep.rate_limit}",
             f"  {ep.description}"]
    if ep.params:
        lines.append("  params:")
        for p in ep.params:
            req = "required" if p.required else "optional"
            lines.append(f"    {p.name} ({p.type}, {req}) {p.description}")
    if ep.doc:
        lines.append(f"  doc: docs/bitunix/{ep.doc}")
    return "\n".join(lines)


def all_endpoints() -> list[str]:
    return sorted(ENDPOINTS)
