# Bitunix Futures API — Parameter Reference

Machine-generated from `jrock/trader/api_reference.py` — the same contract 
the engine validates every call against. Never guess: cross-check a parameter 
here (or in `docs/bitunix/`) before sending it.

- Base URL (REST): `https://fapi.bitunix.com`
- Public WS: `wss://fapi.bitunix.com/public/`
- Private WS: `wss://fapi.bitunix.com/private/`
- Curated docs: `docs/bitunix/`  •  Raw HTML: `docs/_raw_api_docs/`

## Authentication & Signature

```
REST (timestamp in MILLISECONDS):
message = nonce + timestamp + apiKey + queryString + body
digest  = sha256(message)
sign    = sha256(digest + secretKey)
headers: api-key, sign, nonce, timestamp, language, Content-Type
queryString = sorted-by-key 'key+value' concatenation, NO '=' and NO '&'
body        = compact JSON, keys sorted, identical to the bytes sent
urlencoding the query for signing is the #1 cause of error 10007
```

Required headers on every signed request:

| Header | Value |
|---|---|
| `api-key` | your API key string |
| `timestamp` | current Unix epoch in **milliseconds** (int) |
| `nonce` | unique random string, e.g. `str(uuid.uuid4())` |
| `sign` | SHA-256 hex digest (double SHA-256, see above) |
| `language` | e.g. `en-US` |
| `Content-Type` | `application/json` |

> **#1 cause of error 10007**: url-encoding the query string for signing. 
> Sign the raw `key+value` concatenation, no `=` and no `&`.

## WebSocket Channels

Channel names are **exact wire strings** — the server ACKs a wrong name 
(`{"op":"connect","data":{"result":true}}`) then never pushes. Do not invent.

### Public

| Channel | Description |
|---|---|
| `price` | market price (mp), index price (ip), funding rate (fr) |
| `ticker` | one symbol, 24h rolling stats |
| `tickers` | all symbols, aggregated |
| `trade` | public trades |
| `depth_books` | full depth snapshot, then deltas |
| `depth_book1/5/15` | N-level depth |
| `market_kline_<interval>` | kline; suffixes 1min 3min 5min 15min 30min 60min 2h 4h 6h 8h 12h 1day 3day 1week 1month (1h is 60min, never '1hour') |

### Private

| Channel | Description |
|---|---|
| `balance` | account balance updates |
| `order` | order updates |
| `position` | position updates |
| `tpsl` | TP/SL updates |

Private WS login:

```
login args: {op:'login', args:[{apiKey, nonce, timestamp, sign}]}
sign = sha256(sha256(nonce + timestamp + apiKey) + secretKey)
order is nonce, then timestamp, then apiKey; timestamp in SECONDS; nonce < 64 chars
```

## Endpoints

### ACCOUNT

#### `get_single_account` — `GET /api/v1/futures/account`

Account for one margin coin (list, one row per coin)  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_single_account.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `marginCoin` | string | **yes** | USDT |

#### `adjust_position_margin` — `POST /api/v1/futures/account/adjust_position_margin`

Add/remove isolated margin (side OR positionId required)  
- Auth: **signed**  •  Rate: `5 req/sec/uid`
- Doc: `docs/bitunix/adjust_position_margin.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `marginCoin` | string | **yes** |  |
| `amount` | string | **yes** | positive increases, negative reduces |
| `side` | string | no | LONG \| SHORT |
| `positionId` | string | no |  |

#### `change_leverage` — `POST /api/v1/futures/account/change_leverage`

Set leverage for a symbol  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/change_leverage.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `marginCoin` | string | **yes** |  |
| `symbol` | string | **yes** |  |
| `leverage` | int | **yes** |  |

#### `change_margin_mode` — `POST /api/v1/futures/account/change_margin_mode`

ISOLATION | CROSS  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/change_margin_mode.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `marginMode` | string | **yes** |  |
| `marginCoin` | string | **yes** |  |

#### `change_position_mode` — `POST /api/v1/futures/account/change_position_mode`

HEDGE | ONE_WAY  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/change_position_mode.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `positionMode` | string | **yes** |  |

#### `get_leverage_and_margin_mode` — `GET /api/v1/futures/account/get_leverage_margin_mode`

Current leverage + margin mode  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_leverage_and_margin_mode.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `marginCoin` | string | **yes** |  |

#### `get_position_mode` — `GET /api/v1/futures/account/position_mode`

The account's futures position mode (one-way or hedge)  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/get_position_mode.md`

*No request parameters.*

#### `get_trading_settings` — `GET /api/v1/futures/account/trading_settings`

Per-symbol margin mode and leverage  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/get_trading_settings.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbols` | string | no | comma-separated, max 50; omit for all |

### MARKET

#### `get_depth` — `GET /api/v1/futures/market/depth`

Order book depth  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/get_depth.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `limit` | int | no | default 15 |

#### `get_funding_rate` — `GET /api/v1/futures/market/funding_rate`

Current funding rate  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/funding_rate.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |

#### `get_funding_rate_batch` — `GET /api/v1/futures/market/funding_rate/batch`

Funding rates for every symbol - NO request parameters (the old `symbols` filter is gone from the docs and ignored by the server; filter locally, e.g. filter_funding_rows)  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/funding_rate_batch.md`

*No request parameters.*

#### `get_funding_rate_history` — `GET /api/v1/futures/market/get_funding_rate_history`

Funding rate history (note the get_ prefix)  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/funding_rate_history.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `startTime` | int64 | no |  |
| `endTime` | int64 | no |  |
| `limit` | int | no | default 100, max 200 |

#### `get_kline` — `GET /api/v1/futures/market/kline`

Klines (closed candles only; startTime is NOT supported)  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/get_kline.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `interval` | string | **yes** | 1m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M |
| `limit` | int | no | default 100, max 200 |
| `endTime` | int64 | no | exclusive end bound, ms |
| `type` | string | no | LAST_PRICE \| MARK_PRICE |

#### `get_tickers` — `GET /api/v1/futures/market/tickers`

All tickers or a symbol list (includes markPrice)  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/get_tickers.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbols` | string | no | BTCUSDT,ETHUSDT |

#### `get_trading_pairs` — `GET /api/v1/futures/market/trading_pairs`

Pair rules: precision, min/max volume, leverage  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/get_trading_pairs.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbols` | string | no |  |

### POSITION

#### `get_history_positions` — `GET /api/v1/futures/position/get_history_positions`

Closed positions  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/Position_history.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |
| `positionId` | string | no |  |
| `startTime` | int64 | no |  |
| `endTime` | int64 | no |  |
| `skip` | int64 | no | default 0 |
| `limit` | int64 | no | default 10, max 100 |
| `subAccountId` | int64 | no |  |

#### `get_pending_positions` — `GET /api/v1/futures/position/get_pending_positions`

Open positions  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_pending_positions.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |
| `positionId` | string | no |  |
| `subAccountId` | int64 | no |  |
| `includeSubAccounts` | bool | no |  |

#### `get_position_tiers` — `GET /api/v1/futures/position/get_position_tiers`

Risk tiers + maintenance margin rate  
- Auth: **public**  •  Rate: `10 req/sec/ip`
- Doc: `docs/bitunix/get_position_tiers.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |

### TPSL

#### `cancel_tp_sl_order` — `POST /api/v1/futures/tpsl/cancel_order`

Cancel a TP/SL order (symbol AND orderId required)  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/cancel_tp_sl_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `orderId` | string | **yes** |  |

#### `get_history_tp_sl_order` — `GET /api/v1/futures/tpsl/get_history_orders`

TP/SL history (skip/limit, max 100)  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_history_tp_sl_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |
| `side` | int32 | no |  |
| `positionMode` | int32 | no |  |
| `startTime` | int64 | no |  |
| `endTime` | int64 | no |  |
| `skip` | int64 | no |  |
| `limit` | int64 | no |  |

#### `get_pending_tp_sl_order` — `GET /api/v1/futures/tpsl/get_pending_orders`

Pending TP/SL orders (skip/limit, max 100)  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_pending_tp_sl_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |
| `positionId` | string | no |  |
| `side` | int32 | no |  |
| `positionMode` | int32 | no |  |
| `skip` | int64 | no |  |
| `limit` | int64 | no |  |

#### `modify_tp_sl_order` — `POST /api/v1/futures/tpsl/modify_order`

Modify a standalone TP/SL order  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/modify_tp_sl_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `orderId` | string | **yes** |  |
| `tpPrice` | string | no |  |
| `tpStopType` | string | no |  |
| `slPrice` | string | no |  |
| `slStopType` | string | no |  |
| `tpOrderType` | string | no |  |
| `tpOrderPrice` | string | no |  |
| `slOrderType` | string | no |  |
| `slOrderPrice` | string | no |  |
| `tpQty` | string | no |  |
| `slQty` | string | no |  |

#### `place_tp_sl_order` — `POST /api/v1/futures/tpsl/place_order`

Standalone TP/SL order - the PARTIAL method (tpQty/slQty)  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/place_tp_sl_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `positionId` | string | **yes** |  |
| `tpPrice` | string | no |  |
| `tpStopType` | string | no | LAST_PRICE \| MARK_PRICE |
| `slPrice` | string | no |  |
| `slStopType` | string | no |  |
| `tpOrderType` | string | no | LIMIT \| MARKET |
| `tpOrderPrice` | string | no |  |
| `slOrderType` | string | no |  |
| `slOrderPrice` | string | no |  |
| `tpQty` | string | no |  |
| `slQty` | string | no |  |

#### `modify_position_tp_sl_order` — `POST /api/v1/futures/tpsl/position/modify_order`

Modify a position's TP/SL (used for breakeven + trailing)  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/modify_position_tp_sl_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `positionId` | string | **yes** |  |
| `tpPrice` | string | no |  |
| `tpStopType` | string | no |  |
| `slPrice` | string | no |  |
| `slStopType` | string | no |  |

#### `place_position_tp_sl_order` — `POST /api/v1/futures/tpsl/position/place_order`

Attach TP/SL to a position (one per position)  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/place_position_tp_sl_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `positionId` | string | **yes** |  |
| `tpPrice` | string | no |  |
| `tpStopType` | string | no | LAST_PRICE \| MARK_PRICE |
| `slPrice` | string | no |  |
| `slStopType` | string | no |  |

### TRADE

#### `batch_order` — `POST /api/v1/futures/trade/batch_order`

Batch place orders (orderList max 5)  
- Auth: **signed**  •  Rate: `2 req/sec/uid`
- Doc: `docs/bitunix/batch_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `orderList` | array | **yes** |  |

#### `cancel_all_orders` — `POST /api/v1/futures/trade/cancel_all_orders`

Cancel all open orders  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/cancel_all_orders.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |

#### `cancel_orders` — `POST /api/v1/futures/trade/cancel_orders`

Cancel specific orders (symbol required; orderList, or orderId/clientId for a single-order cancel)  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/cancel_orders.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `orderList` | array | **yes** |  |
| `orderId` | string | no | Either orderId or clientId is required; orderId prevails |
| `clientId` | string | no | Either orderId or clientId is required; orderId prevails |

#### `close_all_position` — `POST /api/v1/futures/trade/close_all_position`

Close all positions (market)  
- Auth: **signed**  •  Rate: `1 req/sec/uid`
- Doc: `docs/bitunix/close_all_position.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |

#### `flash_close_position` — `POST /api/v1/futures/trade/flash_close_position`

Close one position at market  
- Auth: **signed**  •  Rate: `5 req/sec/uid`
- Doc: `docs/bitunix/flash_close_position.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `positionId` | string | **yes** |  |

#### `get_history_orders` — `GET /api/v1/futures/trade/get_history_orders`

Order history  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_history_orders.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |
| `orderId` | string | no |  |
| `clientId` | string | no |  |
| `status` | string | no | FILLED \| CANCELED \| PART_FILLED_CANCELED \| EXPIRED |
| `type` | string | no | LIMIT \| MARKET |
| `startTime` | int64 | no |  |
| `endTime` | int64 | no |  |
| `skip` | int64 | no | default 0 |
| `limit` | int64 | no | default 10, max 100 |
| `subAccountId` | int64 | no |  |
| `queryCanceled` | boolean | no | true = canceled only (3d range) |

#### `get_history_trades` — `GET /api/v1/futures/trade/get_history_trades`

Fill history  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_history_trades.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |
| `orderId` | string | no |  |
| `positionId` | string | no |  |
| `startTime` | int64 | no |  |
| `endTime` | int64 | no |  |
| `skip` | int64 | no | default 0 |
| `limit` | int64 | no | default 10, max 100 |

#### `get_order_detail` — `GET /api/v1/futures/trade/get_order_detail`

One order by id (orderId OR clientId required)  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_order_detail.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `orderId` | string | no |  |
| `clientId` | string | no |  |

#### `get_pending_orders` — `GET /api/v1/futures/trade/get_pending_orders`

Open orders  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/get_pending_orders.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | no |  |
| `orderId` | string | no |  |
| `clientId` | string | no |  |
| `status` | string | no | NEW \| PART_FILLED |
| `startTime` | int64 | no |  |
| `endTime` | int64 | no |  |
| `skip` | int64 | no | default 0 |
| `limit` | int64 | no | default 10, max 100 |

#### `modify_order` — `POST /api/v1/futures/trade/modify_order`

Modify a pending order (orderId or clientId required)  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/modify_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `orderId` | string | no |  |
| `clientId` | string | no |  |
| `qty` | string | **yes** | amount (base coin) |
| `price` | string | no | required for LIMIT |
| `tpPrice` | string | no |  |
| `tpStopType` | string | no |  |
| `tpOrderType` | string | no |  |
| `tpOrderPrice` | string | no |  |
| `slPrice` | string | no |  |
| `slStopType` | string | no |  |
| `slOrderType` | string | no |  |
| `slOrderPrice` | string | no |  |

#### `place_order` — `POST /api/v1/futures/trade/place_order`

Place an order (qty is BASE COIN)  
- Auth: **signed**  •  Rate: `20 req/sec/uid`
- Doc: `docs/bitunix/place_order.md`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | **yes** |  |
| `qty` | string | **yes** | base coin amount |
| `side` | string | **yes** | BUY \| SELL |
| `tradeSide` | string | no | OPEN \| CLOSE - only required in hedge mode; omit in one-way |
| `orderType` | string | **yes** | LIMIT \| MARKET |
| `price` | string | no | required for LIMIT |
| `positionId` | string | no | required when tradeSide=CLOSE |
| `effect` | string | no | IOC \| FOK \| GTC \| POST_ONLY |
| `clientId` | string | no |  |
| `reduceOnly` | boolean | no |  |
| `tpPrice` | string | no |  |
| `tpStopType` | string | no | MARK_PRICE \| LAST_PRICE |
| `tpOrderType` | string | no | LIMIT \| MARKET |
| `tpOrderPrice` | string | no |  |
| `slPrice` | string | no |  |
| `slStopType` | string | no |  |
| `slOrderType` | string | no |  |
| `slOrderPrice` | string | no |  |

### V1

#### `asset_query` — `GET /api/v1/cp/asset/query`

Copy-trading asset query (no request parameters)  
- Auth: **signed**  •  Rate: `10 req/sec/uid`
- Doc: `docs/bitunix/asset_query.md`

*No request parameters.*
