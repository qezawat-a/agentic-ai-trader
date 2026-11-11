"""Bitunix USDT-M futures REST client.

Signature (official, verified against the demo repo's test vector):
    message = nonce + timestamp + apiKey + queryString + body
    digest  = sha256(message)
    sign    = sha256(digest + secretKey)

CRITICAL: `queryString` in the signature is NOT the URL query string.
The official demo (Demo/Golang/open_api_http_sign.go -> SortParams, and
Demo/Java/.../SignUtils.java) concatenates sorted key+value pairs with NO
separator and NO '=':

    params {marginCoin: USDT}  ->  signed as "marginCoinUSDT"
    but the URL sent is          "?marginCoin=USDT"

Signing the urlencoded form is the #1 cause of error 10007. The official
test vector is asserted in tests/test_signing.py.

Headers: api-key, sign, nonce, timestamp, language, Content-Type.
Response envelope: {"code": 0, "data": ..., "msg": "Success"}  (code != 0 => error)

Every endpoint below is implemented from the official docs in docs/bitunix/.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .errors import BitunixAPIError, describe

log = logging.getLogger(__name__)


def _nonce(n: int = 32) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


#: The keys the exchange uses to wrap a list of rows inside ``data``. Some
#: endpoints return a bare list, others an envelope; callers should not have to
#: know which. ``orderList`` is the documented one for the order endpoints.
_ROW_LIST_KEYS = ("orderList", "rows", "list", "items")


def _wire_bool(v: bool | None) -> str | None:
    """Render an optional boolean for the wire: ``true``/``false`` strings.

    The query-string params and JSON bodies both use the lowercase JSON
    spelling (docs: ``boolean, default: false``). Python's ``True`` would
    urlencode as ``True`` which the server does not read as true.
    """
    if v is None:
        return None
    return "true" if v else "false"


def order_row(data: Any) -> dict:
    """ONE order object out of an order-ish response.

    The doc pages show an object (``{"orderId": "11111"}``), but the OFFICIAL
    demo types the standalone TP/SL endpoint as an ARRAY:
    ``FuturesPrivateApiClient.placeTpslOrders`` returns
    ``List<OrderIdResp>`` for ``POST /api/v1/futures/tpsl/place_order``, while
    ``placeOrder`` returns a single ``OrderIdResp``. The live server follows
    the demo: the standalone TP/SL endpoint answers with a list.

    The engine read ``(res or {}).get("orderId")`` on that response and died
    with ``AttributeError: 'list' object has no attribute 'get'`` - AFTER the
    first rung had already been created. The result on the exchange was one
    take-profit where the ladder wanted three, and the scan loop died on
    every cycle.

    Normalising at the client boundary means no caller can trip over the
    shape again: a dict stays a dict, a list yields its first order object.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                return row
    return {}


def order_rows(data: Any) -> list[dict]:
    """Normalise an exchange ``data`` payload into a list of row dicts.

    The Bitunix REST envelope is ``{"code": 0, "data": ..., "msg": "Success"}``
    and ``request()`` returns ``data`` verbatim - but ``data`` is not always a
    list. For ``get_pending_orders`` it is
    ``{"orderList": [...], "total": N}``. Returning that dict to a caller that
    expects rows is a silent trap: iterating it yields the string keys, so
    ``row.get(...)`` raises ``AttributeError`` and ``len(...)`` counts keys.

    Accepts a bare list, an envelope dict, or ``None``/empty, and always returns
    a list of dicts. Anything unrecognised yields ``[]`` rather than raising -
    a malformed payload should degrade the readout, not crash the dashboard.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in _ROW_LIST_KEYS:
            rows = data.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        # A single row returned unwrapped (e.g. get_order_detail).
        if "orderId" in data or "symbol" in data:
            return [data]
    return []


def sign_query_string(params: dict[str, Any] | None) -> str:
    """Build the string that goes INTO the signature.

    Official demo behaviour: sorted keys, then key+value concatenated with no
    '=' and no '&'. Empty values are skipped entirely (SignUtils.java).
    """
    if not params:
        return ""
    parts = []
    for k in sorted(params):
        v = params[k]
        if v is None or v == "":
            continue
        parts.append(f"{k}{v}")
    return "".join(parts)


def url_query_string(params: dict[str, Any] | None) -> str:
    """Build the string that goes into the URL (normal urlencoded form)."""
    if not params:
        return ""
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return urlencode(sorted(clean.items())) if clean else ""


@dataclass
class BitunixConfig:
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://fapi.bitunix.com"
    # Used only by sync_time(): the public socket is the only documented way to
    # read the exchange clock (there is no REST server-time endpoint).
    ws_public: str = "wss://fapi.bitunix.com/public/"
    language: str = "en-US"
    timeout: float = 20.0
    max_retries: int = 3


class BitunixClient:
    """Async REST client. Use as an async context manager."""

    def __init__(self, cfg: BitunixConfig):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None
        self._time_offset_ms: int = 0
        self.debug_sign: bool = os.getenv("BITUNIX_DEBUG_SIGN", "").strip().lower() in (
            "1", "true", "yes", "on")
        self.last_sign_debug: dict[str, str] = {}

    async def __aenter__(self) -> "BitunixClient":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.cfg.timeout)
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ signing
    def sign(self, nonce: str, timestamp: str, query: str = "", body: str = "") -> str:
        """sign = sha256(sha256(nonce+ts+apiKey+query+body) + secret).

        `query` here is the SIGNING form (key+value, no '='), not the URL form.
        """
        message = f"{nonce}{timestamp}{self.cfg.api_key}{query}{body}"
        digest = hashlib.sha256(message.encode()).hexdigest()
        sign = hashlib.sha256((digest + self.cfg.api_secret).encode()).hexdigest()
        if self.debug_sign:
            self.last_sign_debug = {
                "preimage": message,
                "digest": digest,
                "sign": sign,
                "query_signed": query,
                "body_signed": body,
            }
            # Spec: log the exact pre-image and the resulting signature so the
            # user can diff them against the official demo repo.
            log.debug("[BITUNIX_DEBUG_SIGN] preimage = %s", message)
            log.debug("[BITUNIX_DEBUG_SIGN] digest   = %s", digest)
            log.debug("[BITUNIX_DEBUG_SIGN] sign     = %s", sign)
        return sign

    def _headers(self, nonce: str, timestamp: str, sign: str) -> dict[str, str]:
        return {
            "api-key": self.cfg.api_key,
            "sign": sign,
            "nonce": nonce,
            "timestamp": timestamp,
            "language": self.cfg.language,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ request
    async def request(self, method: str, path: str, *,
                      params: dict[str, Any] | None = None,
                      body: dict[str, Any] | None = None,
                      signed: bool = True) -> Any:
        await self.start()
        assert self._session is not None

        params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        # Two DIFFERENT strings: one for the URL, one for the signature.
        url_query = url_query_string(params)
        sign_query = sign_query_string(params)
        body_str = json.dumps(body, separators=(",", ":"), sort_keys=True) if body else ""
        url = f"{self.cfg.base_url}{path}" + (f"?{url_query}" if url_query else "")

        headers: dict[str, str] = {"Content-Type": "application/json", "language": self.cfg.language}
        if signed:
            if not self.cfg.api_key or not self.cfg.api_secret:
                raise BitunixAPIError(10003, "api-key can't be empty (set BITUNIX_API_KEY / BITUNIX_API_SECRET)")
            ts = str(int(time.time() * 1000) + self._time_offset_ms)
            nonce = _nonce()
            headers = self._headers(nonce, ts, self.sign(nonce, ts, sign_query, body_str))

        last_err: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                async with self._session.request(
                    method, url, headers=headers,
                    data=body_str if body_str else None,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 500:
                        last_err = BitunixAPIError(resp.status, f"HTTP {resp.status}: {text[:300]}")
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        raise BitunixAPIError(resp.status, f"Non-JSON response: {text[:300]}") from None

                    code = payload.get("code", -1)
                    if code != 0:
                        if code == 10011 and attempt == 0:
                            await self.sync_time()
                            ts = str(int(time.time() * 1000) + self._time_offset_ms)
                            nonce = _nonce()
                            headers = self._headers(
                                nonce, ts, self.sign(nonce, ts, sign_query, body_str))
                            continue
                        raise BitunixAPIError(code, payload.get("msg", ""), payload.get("data"))
                    return payload.get("data")
            except BitunixAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                await asyncio.sleep(0.4 * (2 ** attempt))
        raise BitunixAPIError(10001, f"Network error after {self.cfg.max_retries} attempts: {last_err}")

    async def sync_time(self) -> int:
        """Align the local clock with the exchange (error 10011 guard).

        There is NO REST server-time endpoint on fapi (every candidate path
        returns `{"code":404}`, verified live 2026-11-07). The documented clock
        source is the public WebSocket:

            -> {"op": "ping", "ping": <unix SECONDS>}
            <- {"op": "ping", "pong": <echo>, "ping": <server unix SECONDS>}

        The socket also greets every connection with
        `{"op":"connect","ts":<server ms>,"data":{"result":true}}` BEFORE it
        answers the ping, so the first frame received is normally the ack. Both
        frames carry the server clock (`ts` in milliseconds, the pong's `ping`
        in seconds); whichever arrives first is used.

        Never raises: this runs on the error-retry path, so a socket failure
        must leave the previous offset in place rather than break the request.
        """
        try:
            import websockets
        except Exception:  # pragma: no cover - websockets is a hard requirement
            return self._time_offset_ms
        t0 = time.time() * 1000
        deadline = time.monotonic() + 5.0
        server_ms: float | None = None
        try:
            async with websockets.connect(self.cfg.ws_public,
                                          open_timeout=5, close_timeout=2) as ws:
                await ws.send(json.dumps({"op": "ping", "ping": int(t0 // 1000)}))
                while server_ms is None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                    if msg.get("ping"):
                        server_ms = float(msg["ping"]) * 1000   # pong: SECONDS
                    elif msg.get("ts"):
                        server_ms = float(msg["ts"])            # ack/push: MILLISECONDS
        except Exception:  # noqa: BLE001 - best effort, keep the old offset
            return self._time_offset_ms
        t1 = time.time() * 1000
        if server_ms is None:
            return self._time_offset_ms
        self._time_offset_ms = int(server_ms - (t0 + t1) / 2)
        return self._time_offset_ms

    # ============================================================ MARKET (public)
    async def get_tickers(self, symbols: str | None = None) -> Any:
        return await self.request("GET", "/api/v1/futures/market/tickers",
                                  params={"symbols": symbols}, signed=False)

    async def get_depth(self, symbol: str, limit: int = 15) -> Any:
        return await self.request("GET", "/api/v1/futures/market/depth",
                                  params={"symbol": symbol, "limit": limit}, signed=False)

    async def get_kline(self, symbol: str, interval: str, limit: int = 200,
                        end_time: int | None = None, ktype: str = "LAST_PRICE") -> Any:
        """Market kline history.

        VERIFIED against the 2026-11 changelog and docs/bitunix/get_kline.md:
        - `startTime` is NOT a supported parameter and was removed from the
          docs - it must never be sent (the other endpoints that still take
          startTime are separate APIs).
        - The query is `endTime` + `limit` COUNTING BACKWARD; the candle whose
          open time equals `endTime` is NOT included (exclusive bound). Best
          practice is an interval-aligned `endTime`; omitted, the server uses
          the current forming candle's open time - so a plain call returns the
          latest CLOSED candles, which is exactly what the strategies need.
        - `type` defaults to LAST_PRICE; MARK_PRICE klines now actually work
          (the parameter used to be ignored). The engine deliberately trades
          on LAST_PRICE candles and validates TP/SL triggers against the MARK
          price read from tickers instead.
        - limit defaults to 100, maximum 200 (REST_KLINE_MAX matches).
        """
        return await self.request("GET", "/api/v1/futures/market/kline",
                                  params={"symbol": symbol, "interval": interval, "limit": limit,
                                          "endTime": end_time, "type": ktype}, signed=False)

    async def get_trading_pairs(self, symbols: str | None = None) -> Any:
        return await self.request("GET", "/api/v1/futures/market/trading_pairs",
                                  params={"symbols": symbols}, signed=False)

    async def get_funding_rate(self, symbol: str) -> Any:
        return await self.request("GET", "/api/v1/futures/market/funding_rate",
                                  params={"symbol": symbol}, signed=False)

    async def get_funding_rate_batch(self, symbols: str = "") -> Any:
        """Funding rates for many symbols.

        VERIFIED 2026-11-10 against the official doc page: the request carries
        NO parameters at all - there is no Request Parameters table, and the
        old `symbols` filter was removed from the docs. The server ignores it
        anyway (live: `?symbols=BTCUSDT,ETHUSDT`, `?symbol=BTCUSDT` and no
        parameter all return the same 931 rows). `symbols` is therefore
        ACCEPTED here for caller convenience but never SENT; filtering
        happens client-side via `filter_funding_rows`.
        """
        return await self.request("GET", "/api/v1/futures/market/funding_rate/batch",
                                  signed=False)

    async def get_funding_rate_history(self, symbol: str,
                                       start_time: int | None = None,
                                       end_time: int | None = None,
                                       limit: int = 100) -> Any:
        """Funding rate history.

        VERIFIED LIVE 2026-11-07: the path is
        `/api/v1/futures/market/get_funding_rate_history` - with the `get_`
        prefix. `/funding_rate/history` returns `{"code":404,"msg":"Not Found"}`.

        Official params: `symbol` (required), `startTime`, `endTime`, `limit`
        (default 100, max 200). `page`/`pageSize` are NOT parameters - the live
        server ignores them and returns the default 100 rows.
        """
        return await self.request("GET",
                                  "/api/v1/futures/market/get_funding_rate_history",
                                  params={"symbol": symbol,
                                          "startTime": start_time,
                                          "endTime": end_time,
                                          "limit": limit},
                                  signed=False)

    # ============================================================ ACCOUNT
    async def get_single_account(self, margin_coin: str = "USDT") -> Any:
        return await self.request("GET", "/api/v1/futures/account",
                                  params={"marginCoin": margin_coin})

    @staticmethod
    def filter_funding_rows(rows: Any, symbols: list[str]) -> list[dict]:
        """Keep only the requested symbols from a funding_rate/batch response.

        Needed because the live endpoint ignores its `symbols` parameter and
        always returns every symbol (verified 2026-11-07: 931 rows regardless).
        Without this, `/funding BTCUSDT` would print the whole market.
        """
        want = {s.strip().upper() for s in symbols if s and s.strip()}
        if not want:
            return list(rows or [])
        return [r for r in (rows or [])
                if str(r.get("symbol", "")).upper() in want]

    async def change_leverage(self, symbol: str, leverage: int, margin_coin: str = "USDT") -> Any:
        return await self.request("POST", "/api/v1/futures/account/change_leverage",
                                  body={"symbol": symbol, "leverage": int(leverage),
                                        "marginCoin": margin_coin})

    async def change_margin_mode(self, symbol: str, margin_mode: str,
                                 margin_coin: str = "USDT") -> Any:
        return await self.request("POST", "/api/v1/futures/account/change_margin_mode",
                                  body={"symbol": symbol, "marginMode": margin_mode.upper(),
                                        "marginCoin": margin_coin})

    async def change_position_mode(self, position_mode: str) -> Any:
        """Switch between one-way and hedge mode.

        Official: `positionMode` is the ONLY request parameter. The old
        signature also sent `marginCoin`, which the docs do not list - an
        undocumented extra field.
        """
        return await self.request("POST", "/api/v1/futures/account/change_position_mode",
                                  body={"positionMode": position_mode.upper()})

    async def get_leverage_and_margin_mode(self, symbol: str,
                                           margin_coin: str = "USDT") -> Any:
        """Leverage and margin mode for one symbol.

        Official path is `/api/v1/futures/account/get_leverage_margin_mode`
        (with the `get_` prefix). The old code called
        `/api/v1/futures/account/leverage_margin_mode`, which is not a
        documented path. Both `symbol` and `marginCoin` are REQUIRED.
        """
        return await self.request("GET", "/api/v1/futures/account/get_leverage_margin_mode",
                                  params={"symbol": symbol, "marginCoin": margin_coin})

    async def get_position_mode(self) -> Any:
        """The account's futures position mode (one-way or hedge).

        Official: `GET /api/v1/futures/account/position_mode`, no parameters.
        """
        return await self.request("GET", "/api/v1/futures/account/position_mode")

    async def get_trading_settings(self, symbols: str | None = None) -> Any:
        """Per-symbol margin mode and leverage.

        Official: `GET /api/v1/futures/account/trading_settings`; `symbols` is
        optional (comma-separated, max 50). Omit it to get every configured
        symbol.
        """
        params: dict[str, Any] = {}
        if symbols:
            params["symbols"] = symbols
        return await self.request("GET", "/api/v1/futures/account/trading_settings",
                                  params=params)

    async def adjust_position_margin(self, symbol: str, amount: str,
                                     position_id: str | None = None,
                                     side: str | None = None,
                                     margin_coin: str = "USDT") -> Any:
        """Add or reduce isolated margin. Positive amount increases, negative reduces.

        Official: `symbol`, `marginCoin`, `amount` required; `side` (LONG/SHORT)
        OR `positionId` required - "Either side or positionId required". The old
        signature always sent positionId and never sent side, so a caller that
        only knew the side could not use this endpoint at all.
        """
        if not position_id and not side:
            raise ValueError("adjust_position_margin needs positionId or side")
        body: dict[str, Any] = {"symbol": symbol, "marginCoin": margin_coin,
                                "amount": str(amount)}
        if position_id:
            body["positionId"] = str(position_id)
        if side:
            body["side"] = str(side).upper()
        return await self.request("POST", "/api/v1/futures/account/adjust_position_margin",
                                  body=body)

    async def asset_query(self) -> Any:
        """Copy-trading asset query.

        Official: GET /api/v1/cp/asset/query - NO request parameters.
        The old `/api/v1/futures/copyTrading/asset/query` (with a `marginCoin`
        parameter) is not a real endpoint; it returns `{"code":404}` live.
        Response: {"available": "...", "maxTransfer": "..."}.
        """
        return await self.request("GET", "/api/v1/cp/asset/query")

    # ============================================================ POSITION
    async def get_pending_positions(self, symbol: str | None = None,
                                    position_id: str | None = None,
                                    sub_account_id: int | None = None,
                                    include_sub_accounts: bool | None = None) -> Any:
        """Open positions.

        Official params (all optional): `symbol`, `positionId`, `subAccountId`,
        `includeSubAccounts` (boolean). A sub-account query needs `subAccountId`;
        without it the response is the main account's own positions. Booleans
        are sent as ``true``/``false`` strings - the wire format is boolean.
        """
        return await self.request("GET", "/api/v1/futures/position/get_pending_positions",
                                  params={"symbol": symbol, "positionId": position_id,
                                          "subAccountId": sub_account_id,
                                          "includeSubAccounts": _wire_bool(include_sub_accounts)})

    async def get_history_positions(self, symbol: str | None = None, *, skip: int = 0,
                                    limit: int = 20,
                                    start_time: int | None = None,
                                    end_time: int | None = None,
                                    position_id: str | None = None,
                                    sub_account_id: int | None = None) -> Any:
        """Closed positions. Official paging params are `skip`/`limit`.

        `positionId` and `subAccountId` are the remaining optional filters; both
        can be used with or without a symbol filter.
        """
        return await self.request("GET", "/api/v1/futures/position/get_history_positions",
                                  params={"symbol": symbol, "skip": skip, "limit": limit,
                                          "startTime": start_time, "endTime": end_time,
                                          "positionId": position_id,
                                          "subAccountId": sub_account_id})

    async def get_position_tiers(self, symbol: str) -> Any:
        return await self.request("GET", "/api/v1/futures/position/get_position_tiers",
                                  params={"symbol": symbol}, signed=False)

    # ============================================================ TRADE
    # Official required params for place_order, from the docs. Validated here so
    # a malformed order is refused locally instead of costing a round trip.
    # tradeSide is NOT in the set: the docs say it is "only required in
    # hedge-mode", so one-way accounts omit it (see build_order_body).
    PLACE_ORDER_REQUIRED = ("symbol", "qty", "side", "orderType")
    MODIFY_ORDER_REQUIRED = ("qty", "price")

    async def place_order(self, **kwargs) -> Any:
        """Place an order.

        Official required: symbol, qty, side, orderType. tradeSide is required
        only in hedge mode. Optional: price, positionId, effect, clientId,
        reduceOnly, and the tp*/sl* family. The body is passed through verbatim,
        but the required set is checked first.
        """
        missing = [k for k in self.PLACE_ORDER_REQUIRED if not kwargs.get(k)]
        if missing:
            raise ValueError(
                f"place_order is missing required parameter(s): {', '.join(missing)} "
                f"(official required: {', '.join(self.PLACE_ORDER_REQUIRED)})")
        return order_row(await self.request("POST", "/api/v1/futures/trade/place_order",
                                            body=kwargs))

    async def batch_order(self, symbol: str, order_list: list[dict]) -> Any:
        """Batch place orders.

        Official: `symbol` AND `orderList` are BOTH required, and orderList has
        a maximum length of 5. The old signature sent only orderList, so every
        batch call was missing a required parameter.
        """
        if not symbol:
            raise ValueError("batch_order requires symbol (official: required)")
        if not order_list:
            raise ValueError("batch_order requires a non-empty orderList")
        if len(order_list) > 5:
            raise ValueError(
                f"batch_order accepts at most 5 orders, got {len(order_list)}")
        return await self.request("POST", "/api/v1/futures/trade/batch_order",
                                  body={"symbol": symbol, "orderList": order_list})

    async def cancel_orders(self, symbol: str, order_list: list[dict] | None = None,
                            order_id: str | None = None,
                            client_id: str | None = None) -> Any:
        """Cancel a batch of orders.

        Official: BOTH `symbol` and `orderList` are required. The old signature
        sent only orderList, so every call was missing a required parameter.

        `orderId`/`clientId` are the remaining optional fields - the docs say
        "Either orderId or clientId is required" as the order identifier inside
        the request, so a single-order cancel may send just those instead of an
        orderList entry. When `orderList` is omitted, at least one of them
        becomes mandatory.
        """
        if not symbol:
            raise ValueError("cancel_orders requires symbol (official: required)")
        if not order_list and not order_id and not client_id:
            raise ValueError(
                "cancel_orders requires an orderList, or orderId/clientId "
                "(official: either orderId or clientId identifies the order)")
        if order_list is not None and not order_list:
            raise ValueError("cancel_orders requires a non-empty orderList")
        body: dict[str, Any] = {"symbol": symbol}
        if order_id:
            body["orderId"] = str(order_id)
        if client_id:
            body["clientId"] = str(client_id)
        if order_list:
            body["orderList"] = order_list
        return await self.request("POST", "/api/v1/futures/trade/cancel_orders", body=body)

    async def cancel_all_orders(self, symbol: str | None = None) -> Any:
        return await self.request("POST", "/api/v1/futures/trade/cancel_all_orders",
                                  body={"symbol": symbol} if symbol else {})

    async def modify_order(self, **kwargs) -> Any:
        """Modify a pending order.

        Official required: qty, price. Optional: orderId or clientId (one of
        them identifies the order), plus the tp*/sl* family.
        """
        missing = [k for k in self.MODIFY_ORDER_REQUIRED if not kwargs.get(k)]
        if missing:
            raise ValueError(
                f"modify_order is missing required parameter(s): {', '.join(missing)} "
                f"(official required: {', '.join(self.MODIFY_ORDER_REQUIRED)})")
        if not kwargs.get("orderId") and not kwargs.get("clientId"):
            raise ValueError("modify_order needs orderId or clientId")
        return order_row(await self.request("POST", "/api/v1/futures/trade/modify_order",
                                            body=kwargs))

    async def get_pending_orders(self, symbol: str | None = None, *, skip: int = 0,
                                 limit: int = 20, order_id: str | None = None,
                                 client_id: str | None = None, status: str | None = None,
                                 start_time: int | None = None,
                                 end_time: int | None = None) -> list[dict]:
        """Pending orders, as a LIST of order rows.

        Official paging params are `skip`/`limit` (max 100), NOT
        `page`/`pageSize` - the live server ignores the latter and returns the
        default page. Remaining optional filters: `orderId`, `clientId`,
        `status` (``NEW`` | ``PART_FILLED``), `startTime`, `endTime`.

        The exchange wraps the rows in an envelope: ``data`` is
        ``{"orderList": [...], "total": N}``, NOT a bare list. ``request()``
        returns ``payload["data"]`` verbatim, so without unwrapping here every
        caller receives a dict and iterating it yields the string keys
        ("orderList", "total") - which is what made ``_order_rows`` raise
        ``AttributeError: 'str' object has no attribute 'get'`` and made
        ``len(orders)`` count keys instead of orders. Normalising at the client
        boundary means every caller gets the same shape the tests stub.
        """
        data = await self.request("GET", "/api/v1/futures/trade/get_pending_orders",
                                  params={"symbol": symbol, "skip": skip, "limit": limit,
                                          "orderId": order_id, "clientId": client_id,
                                          "status": status, "startTime": start_time,
                                          "endTime": end_time})
        return order_rows(data)

    async def get_history_orders(self, symbol: str | None = None, *, skip: int = 0,
                                 limit: int = 20, start_time: int | None = None,
                                 end_time: int | None = None,
                                 order_id: str | None = None,
                                 client_id: str | None = None,
                                 status: str | None = None,
                                 order_type: str | None = None,
                                 sub_account_id: int | None = None,
                                 query_canceled: bool | None = None) -> Any:
        """Order history. Official paging params are `skip`/`limit`.

        Remaining optional filters: `orderId`, `clientId`, `status` (``FILLED``
        | ``CANCELED`` | ``PART_FILLED_CANCELED`` | ``EXPIRED``), `type`
        (``LIMIT`` | ``MARKET``, default all), `subAccountId` and
        `queryCanceled` (boolean, default false - true returns canceled orders
        only). `status` and `type` are upper-cased; `queryCanceled` goes on the
        wire as ``true``/``false``.
        """
        return await self.request("GET", "/api/v1/futures/trade/get_history_orders",
                                  params={"symbol": symbol, "skip": skip, "limit": limit,
                                          "startTime": start_time, "endTime": end_time,
                                          "orderId": order_id, "clientId": client_id,
                                          "status": status.upper() if status else None,
                                          "type": order_type.upper() if order_type else None,
                                          "subAccountId": sub_account_id,
                                          "queryCanceled": _wire_bool(query_canceled)})

    async def get_order_detail(self, order_id: str | None = None,
                               client_id: str | None = None) -> Any:
        return await self.request("GET", "/api/v1/futures/trade/get_order_detail",
                                  params={"orderId": order_id, "clientId": client_id})

    async def get_history_trades(self, symbol: str | None = None, *, skip: int = 0,
                                 limit: int = 20, start_time: int | None = None,
                                 end_time: int | None = None,
                                 order_id: str | None = None,
                                 position_id: str | None = None) -> Any:
        """Trade history. Official paging params are `skip`/`limit`.

        `orderId` and `positionId` are the remaining optional filters, usable
        alone or with a symbol/time window.
        """
        return await self.request("GET", "/api/v1/futures/trade/get_history_trades",
                                  params={"symbol": symbol, "skip": skip, "limit": limit,
                                          "startTime": start_time, "endTime": end_time,
                                          "orderId": order_id, "positionId": position_id})

    async def close_all_position(self, symbol: str | None = None) -> Any:
        return await self.request("POST", "/api/v1/futures/trade/close_all_position",
                                  body={"symbol": symbol} if symbol else {})

    async def flash_close_position(self, position_id: str) -> Any:
        return await self.request("POST", "/api/v1/futures/trade/flash_close_position",
                                  body={"positionId": position_id})

    # ============================================================ TP/SL
    #
    # Paths and parameters verified against the official docs on 2026-11-07.
    # The previous version had three wrong paths and two wrong parameter names,
    # every one of which would have failed at the exchange:
    #   get_pending_order -> get_pending_orders
    #   get_history_order -> get_history_orders
    #   cancel_order      -> also needs `symbol`, not just `orderId`
    #   get_*_order       -> use `skip`/`limit`, not `page`/`pageSize`
    async def place_tp_sl_order(self, symbol: str, position_id: str, *,
                                tp_price: str | None = None,
                                sl_price: str | None = None,
                                tp_stop_type: str | None = None,
                                sl_stop_type: str | None = None,
                                tp_order_type: str | None = None,
                                tp_order_price: str | None = None,
                                sl_order_type: str | None = None,
                                sl_order_price: str | None = None,
                                tp_qty: str | None = None,
                                sl_qty: str | None = None) -> Any:
        """Standalone TP/SL order - the PARTIAL TP/SL method.

        Official: `symbol` + `positionId` required; at least one of
        tpPrice/slPrice; at least one of tpQty/slQty. The tpQty/slQty fields are
        what make a staged exit possible - the position TP/SL endpoint has no
        quantity field at all, so it can only ever close the whole position.
        """
        body: dict[str, Any] = {"symbol": symbol, "positionId": str(position_id)}
        if tp_price is not None and tp_price != "":
            body["tpPrice"] = str(tp_price)
        if sl_price is not None and sl_price != "":
            body["slPrice"] = str(sl_price)
        if tp_stop_type:
            body["tpStopType"] = str(tp_stop_type).upper()
        if sl_stop_type:
            body["slStopType"] = str(sl_stop_type).upper()
        if tp_order_type:
            body["tpOrderType"] = str(tp_order_type).upper()
        if tp_order_price is not None and tp_order_price != "":
            body["tpOrderPrice"] = str(tp_order_price)
        if sl_order_type:
            body["slOrderType"] = str(sl_order_type).upper()
        if sl_order_price is not None and sl_order_price != "":
            body["slOrderPrice"] = str(sl_order_price)
        if tp_qty is not None and tp_qty != "":
            body["tpQty"] = str(tp_qty)
        if sl_qty is not None and sl_qty != "":
            body["slQty"] = str(sl_qty)
        if "tpPrice" not in body and "slPrice" not in body:
            raise ValueError("place_tp_sl_order needs at least one of tpPrice/slPrice")
        if "tpQty" not in body and "slQty" not in body:
            raise ValueError("place_tp_sl_order needs at least one of tpQty/slQty")
        # The live server answers this endpoint with an ARRAY (the official
        # demo types it `List<OrderIdResp>`); the doc page shows an object.
        # `order_row` collapses both into one dict.
        return order_row(await self.request("POST", "/api/v1/futures/tpsl/place_order",
                                            body=body))

    async def place_position_tp_sl_order(self, symbol: str, position_id: str,
                                         tp_price: str | None = None,
                                         sl_price: str | None = None,
                                         tp_stop_type: str = "MARK_PRICE",
                                         sl_stop_type: str = "MARK_PRICE") -> Any:
        """Position TP/SL - closes the ENTIRE position. One per position."""
        body: dict[str, Any] = {"symbol": symbol, "positionId": position_id,
                                "tpStopType": tp_stop_type, "slStopType": sl_stop_type}
        if tp_price:
            body["tpPrice"] = str(tp_price)
        if sl_price:
            body["slPrice"] = str(sl_price)
        return order_row(await self.request("POST", "/api/v1/futures/tpsl/position/place_order",
                                            body=body))

    async def modify_tp_sl_order(self, order_id: str, *,
                                 tp_price: str | None = None,
                                 sl_price: str | None = None,
                                 tp_stop_type: str | None = None,
                                 sl_stop_type: str | None = None,
                                 tp_order_type: str | None = None,
                                 tp_order_price: str | None = None,
                                 sl_order_type: str | None = None,
                                 sl_order_price: str | None = None,
                                 tp_qty: str | None = None,
                                 sl_qty: str | None = None) -> Any:
        """Modify a standalone TP/SL order. Official: `orderId` required."""
        body: dict[str, Any] = {"orderId": str(order_id)}
        if tp_price is not None and tp_price != "":
            body["tpPrice"] = str(tp_price)
        if sl_price is not None and sl_price != "":
            body["slPrice"] = str(sl_price)
        if tp_stop_type:
            body["tpStopType"] = str(tp_stop_type).upper()
        if sl_stop_type:
            body["slStopType"] = str(sl_stop_type).upper()
        if tp_order_type:
            body["tpOrderType"] = str(tp_order_type).upper()
        if tp_order_price is not None and tp_order_price != "":
            body["tpOrderPrice"] = str(tp_order_price)
        if sl_order_type:
            body["slOrderType"] = str(sl_order_type).upper()
        if sl_order_price is not None and sl_order_price != "":
            body["slOrderPrice"] = str(sl_order_price)
        if tp_qty is not None and tp_qty != "":
            body["tpQty"] = str(tp_qty)
        if sl_qty is not None and sl_qty != "":
            body["slQty"] = str(sl_qty)
        return order_row(await self.request("POST", "/api/v1/futures/tpsl/modify_order", body=body))

    async def modify_position_tp_sl_order(self, symbol: str, position_id: str,
                                          tp_price: str | None = None,
                                          sl_price: str | None = None,
                                          tp_stop_type: str | None = None,
                                          sl_stop_type: str | None = None) -> Any:
        body: dict[str, Any] = {"symbol": symbol, "positionId": position_id}
        if tp_price:
            body["tpPrice"] = str(tp_price)
        if sl_price:
            body["slPrice"] = str(sl_price)
        # Documented optional fields (LAST_PRICE | MARK_PRICE). Omitting them
        # makes the exchange fall back to its own default, which may not be the
        # price the local pre-flight validated against.
        if tp_stop_type:
            body["tpStopType"] = str(tp_stop_type).upper()
        if sl_stop_type:
            body["slStopType"] = str(sl_stop_type).upper()
        return order_row(await self.request("POST", "/api/v1/futures/tpsl/position/modify_order",
                                            body=body))

    async def cancel_tp_sl_order(self, symbol: str, order_id: str) -> Any:
        """Cancel a TP/SL order. Official: BOTH `symbol` and `orderId` required.

        The old signature sent only orderId, so every cancel was missing a
        required parameter.
        """
        return order_row(await self.request("POST", "/api/v1/futures/tpsl/cancel_order",
                                            body={"symbol": symbol, "orderId": str(order_id)}))

    async def get_pending_tp_sl_order(self, symbol: str | None = None,
                                      position_id: str | None = None,
                                      side: int | None = None,
                                      position_mode: int | None = None,
                                      skip: int = 0, limit: int = 10) -> Any:
        """Pending TP/SL orders.

        Official path is get_pending_orders (PLURAL) and the paging params are
        `skip`/`limit` (max 100), not `page`/`pageSize`.
        """
        return await self.request("GET", "/api/v1/futures/tpsl/get_pending_orders",
                                  params={"symbol": symbol, "positionId": position_id,
                                          "side": side, "positionMode": position_mode,
                                          "skip": skip, "limit": limit})

    async def get_history_tp_sl_order(self, symbol: str | None = None, *,
                                      side: int | None = None,
                                      position_mode: int | None = None,
                                      start_time: int | None = None,
                                      end_time: int | None = None,
                                      skip: int = 0, limit: int = 10) -> Any:
        """TP/SL history.

        Official path is get_history_orders (PLURAL) and the paging params are
        `skip`/`limit` (max 100), not `page`/`pageSize`.
        """
        return await self.request("GET", "/api/v1/futures/tpsl/get_history_orders",
                                  params={"symbol": symbol, "side": side,
                                          "positionMode": position_mode,
                                          "startTime": start_time, "endTime": end_time,
                                          "skip": skip, "limit": limit})

    # ============================================================ misc
    async def ping(self) -> bool:
        try:
            await self.get_tickers()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def verify_credentials(self) -> tuple[bool, str]:
        try:
            acct = await self.get_single_account()
            return True, f"OK - account reachable ({len(acct or [])} margin coin(s))"
        except BitunixAPIError as e:
            return False, f"code {e.code}: {describe(e.code)} ({e})"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
