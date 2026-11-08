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
              "10 req/sec/ip", "All tickers or a symbol list",
              [_p("symbols", "string", False, "BTCUSDT,ETHUSDT")], "get_tickers.md"))
_add(Endpoint("get_ticker", "GET", "/api/v1/futures/market/tickers", False,
              "10 req/sec/ip", "Single ticker (delegates to /market/tickers)",
              [_p("symbols", "string", True, "one symbol, e.g. BTCUSDT")], "get_tickers.md"))
_add(Endpoint("get_depth", "GET", "/api/v1/futures/market/depth", False,
              "10 req/sec/ip", "Order book depth",
              [_p("symbol", "string", True),
               _p("limit", "string", False, "1 | 5 | 15 | 50 | max")], "get_depth.md"))
_add(Endpoint("get_kline", "GET", "/api/v1/futures/market/kline", False,
              "10 req/sec/ip", "Klines (closed candles only)",
              [_p("symbol", "string", True), _p("interval", "string", True,
                                                "1m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M"),
               _p("limit", "int", False, "default 100, max 200"),
               _p("endTime", "int64", False, "exclusive end bound, ms; startTime removed"),
               _p("type", "string", False, "LAST_PRICE | MARK_PRICE")], "get_kline.md"))
_add(Endpoint("get_trading_pairs", "GET", "/api/v1/futures/market/trading_pairs", False,
              "10 req/sec/ip", "Pair rules: precision, min/max volume, leverage",
              [_p("symbols", "string", False)], "get_trading_pairs.md"))
_add(Endpoint("get_funding_rate", "GET", "/api/v1/futures/market/funding_rate", False,
              "10 req/sec/ip", "Current funding rate", [_p("symbol", "string", True)],
              "funding_rate.md"))
_add(Endpoint("get_funding_rate_batch", "GET", "/api/v1/futures/market/funding_rate/batch", False,
              "10 req/sec/ip", "Funding rates for EVERY symbol (no request params)",
              [], "funding_rate_batch.md"))
_add(Endpoint("get_funding_rate_history", "GET",
              "/api/v1/futures/market/get_funding_rate_history",
              False, "10 req/sec/ip", "Funding rate history (note the get_ prefix)",
              [_p("symbol", "string", True), _p("startTime", "int64", False),
               _p("endTime", "int64", False),
               _p("limit", "int", False, "default 100, max 200")],
              "funding_rate_history.md"))
# NOTE: `change_log` is NOT an API endpoint - the Bitunix "Change Log" page is
# documentation only and has no path in the official docs. The client method
# and this entry were removed.

# ------------------------------------------------------------------ account
_add(Endpoint("get_single_account", "GET", "/api/v1/futures/account", True,
              "10 req/sec/uid", "Account for one margin coin",
              [_p("marginCoin", "string", True, "USDT")], "get_single_account.md"))
_add(Endpoint("get_accounts", "GET", "/api/v1/futures/account", True,
              "10 req/sec/uid", "All margin-coin accounts (delegates to get_single_account)",
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
              "10 req/sec/uid", "Copy-trading asset query (no request params)",
              [], "asset_query.md"))

# ------------------------------------------------------------------ position
_add(Endpoint("get_pending_positions", "GET", "/api/v1/futures/position/get_pending_positions",
              True, "10 req/sec/uid", "Open positions",
              [_p("symbol", "string", False), _p("positionId", "string", False)],
              "get_pending_positions.md"))
_add(Endpoint("get_history_positions", "GET", "/api/v1/futures/position/get_history_positions",
              True, "10 req/sec/uid", "Closed positions",
              [_p("symbol", "string", False), _p("positionId", "string", False),
               _p("startTime", "int64", False), _p("endTime", "int64", False),
               _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100")],
              "Position_history.md"))
_add(Endpoint("get_position_tiers", "GET", "/api/v1/futures/position/get_position_tiers", False,
              "10 req/sec/ip", "Risk tiers + maintenance margin rate",
              [_p("symbol", "string", True)], "get_position_tiers.md"))

# ------------------------------------------------------------------ trade
_add(Endpoint("place_order", "POST", "/api/v1/futures/trade/place_order", True,
              "20 req/sec/uid", "Place an order (qty is BASE COIN)",
              [_p("symbol", "string", True), _p("qty", "string", True, "base coin amount"),
               _p("side", "string", True, "BUY | SELL"),
               _p("tradeSide", "string", False, "OPEN | CLOSE (required in hedge mode)"),
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
              "10 req/sec/uid", "Cancel specific orders (symbol AND orderList required)",
              [_p("symbol", "string", True), _p("orderList", "array", True)],
              "cancel_orders.md"))
_add(Endpoint("cancel_all_orders", "POST", "/api/v1/futures/trade/cancel_all_orders", True,
              "20 req/sec/uid", "Cancel all open orders",
              [_p("symbol", "string", False)], "cancel_all_orders.md"))
_add(Endpoint("modify_order", "POST", "/api/v1/futures/trade/modify_order", True,
              "20 req/sec/uid", "Modify a pending order (no `symbol` param)",
              [_p("orderId", "string", False, "either orderId or clientId"),
               _p("clientId", "string", False), _p("qty", "string", True),
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
               _p("status", "string", False, "FILLED | CANCELED | PART_FILLED_CANCELED | EXPIRED"),
               _p("type", "string", False, "LIMIT | MARKET"),
               _p("startTime", "int64", False), _p("endTime", "int64", False),
               _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100"),
               _p("queryCanceled", "boolean", False)], "get_history_orders.md"))
_add(Endpoint("get_order_detail", "GET", "/api/v1/futures/trade/get_order_detail", True,
              "10 req/sec/uid", "One order by id",
              [_p("orderId", "string", False), _p("clientId", "string", False)],
              "get_order_detail.md"))
_add(Endpoint("get_history_trades", "GET", "/api/v1/futures/trade/get_history_trades", True,
              "10 req/sec/uid", "Fill history",
              [_p("symbol", "string", False), _p("orderId", "string", False),
               _p("positionId", "string", False), _p("startTime", "int64", False),
               _p("endTime", "int64", False), _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100")],
              "get_history_trades.md"))
_add(Endpoint("close_all_position", "POST", "/api/v1/futures/trade/close_all_position", True,
              "20 req/sec/uid", "Close all positions (market)",
              [_p("symbol", "string", False)], "close_all_position.md"))
_add(Endpoint("flash_close_position", "POST", "/api/v1/futures/trade/flash_close_position", True,
              "20 req/sec/uid", "Close one position at market",
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
                                                  "LAST_PRICE | MARK_PRICE (optional)"),
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
               _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100")],
              "get_pending_tp_sl_order.md"))
_add(Endpoint("get_history_tp_sl_order", "GET", "/api/v1/futures/tpsl/get_history_orders", True,
              "10 req/sec/uid", "TP/SL history (skip/limit, max 100)",
              [_p("symbol", "string", False), _p("side", "int32", False),
               _p("positionMode", "int32", False), _p("startTime", "int64", False),
               _p("endTime", "int64", False), _p("skip", "int64", False, "default 0"),
               _p("limit", "int64", False, "default 10, max 100")],
              "get_history_tp_sl_order.md"))

# ------------------------------------------------------------------ websocket
WS_CHANNELS = {
    "public": {
        "price": "market price / index price / funding rate (mp, ip, fr, ft, nft)",
        "ticker": "one symbol, 24h rolling stats (object)",
        "tickers": "all symbols, aggregated (LIST, adds bd/ak/bv/av)",
        "trade": "public trades (list of p/v/s/t)",
        "depth_books": "full snapshot then deltas",
        "depth_book1": "1 depth level",
        "depth_book5": "5 depth levels",
        "depth_book15": "15 depth levels",
        "market_kline_<interval>": "kline, e.g. market_kline_1min / market_kline_15min",
    },
    "private": {
        "balance": "account balance updates",
        "order": "order updates",
        "position": "position updates",
        "tpsl": "TP/SL updates (auto-pushed after login)",
    },
}

SIGNATURE_RULE = (
    "message = nonce + timestamp + apiKey + queryString + body\n"
    "digest  = sha256(message)\n"
    "sign    = sha256(digest + secretKey)\n"
    "headers: api-key, sign, nonce, timestamp, language, Content-Type\n"
    "body must be compact JSON with sorted keys; queryString is the SORTED "
    "key+value concatenation with NO '=' and NO '&' (e.g. marginCoinUSDT), "
    "NOT the urlencoded URL query"
)

WS_LOGIN_RULE = (
    "sign = sha256(sha256(nonce + timestamp + apiKey) + secretKey)  # timestamp in SECONDS"
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
