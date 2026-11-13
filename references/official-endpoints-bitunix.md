# Bitunix Futures — Official Endpoint Inventory

All endpoints the engine can call, with method, path, auth and rate limit. 
Generated from `jrock/trader/api_reference.py` (kept in sync with `docs/bitunix/`).

Base URL: `https://fapi.bitunix.com`

| Endpoint | Method | Path | Auth | Rate |
|---|---|---|---|---|
| `asset_query` | GET | `/api/v1/cp/asset/query` | signed | 10 req/sec/uid |
| `get_single_account` | GET | `/api/v1/futures/account` | signed | 10 req/sec/uid |
| `adjust_position_margin` | POST | `/api/v1/futures/account/adjust_position_margin` | signed | 5 req/sec/uid |
| `change_leverage` | POST | `/api/v1/futures/account/change_leverage` | signed | 10 req/sec/uid |
| `change_margin_mode` | POST | `/api/v1/futures/account/change_margin_mode` | signed | 10 req/sec/uid |
| `change_position_mode` | POST | `/api/v1/futures/account/change_position_mode` | signed | 10 req/sec/uid |
| `get_leverage_and_margin_mode` | GET | `/api/v1/futures/account/get_leverage_margin_mode` | signed | 10 req/sec/uid |
| `get_position_mode` | GET | `/api/v1/futures/account/position_mode` | signed | 20 req/sec/uid |
| `get_trading_settings` | GET | `/api/v1/futures/account/trading_settings` | signed | 20 req/sec/uid |
| `get_depth` | GET | `/api/v1/futures/market/depth` | public | 10 req/sec/ip |
| `get_funding_rate` | GET | `/api/v1/futures/market/funding_rate` | public | 10 req/sec/ip |
| `get_funding_rate_batch` | GET | `/api/v1/futures/market/funding_rate/batch` | public | 10 req/sec/ip |
| `get_funding_rate_history` | GET | `/api/v1/futures/market/get_funding_rate_history` | public | 10 req/sec/ip |
| `get_kline` | GET | `/api/v1/futures/market/kline` | public | 10 req/sec/ip |
| `get_tickers` | GET | `/api/v1/futures/market/tickers` | public | 10 req/sec/ip |
| `get_trading_pairs` | GET | `/api/v1/futures/market/trading_pairs` | public | 10 req/sec/ip |
| `get_history_positions` | GET | `/api/v1/futures/position/get_history_positions` | signed | 10 req/sec/uid |
| `get_pending_positions` | GET | `/api/v1/futures/position/get_pending_positions` | signed | 10 req/sec/uid |
| `get_position_tiers` | GET | `/api/v1/futures/position/get_position_tiers` | public | 10 req/sec/ip |
| `cancel_tp_sl_order` | POST | `/api/v1/futures/tpsl/cancel_order` | signed | 10 req/sec/uid |
| `get_history_tp_sl_order` | GET | `/api/v1/futures/tpsl/get_history_orders` | signed | 10 req/sec/uid |
| `get_pending_tp_sl_order` | GET | `/api/v1/futures/tpsl/get_pending_orders` | signed | 10 req/sec/uid |
| `modify_tp_sl_order` | POST | `/api/v1/futures/tpsl/modify_order` | signed | 20 req/sec/uid |
| `place_tp_sl_order` | POST | `/api/v1/futures/tpsl/place_order` | signed | 20 req/sec/uid |
| `modify_position_tp_sl_order` | POST | `/api/v1/futures/tpsl/position/modify_order` | signed | 20 req/sec/uid |
| `place_position_tp_sl_order` | POST | `/api/v1/futures/tpsl/position/place_order` | signed | 20 req/sec/uid |
| `batch_order` | POST | `/api/v1/futures/trade/batch_order` | signed | 2 req/sec/uid |
| `cancel_all_orders` | POST | `/api/v1/futures/trade/cancel_all_orders` | signed | 20 req/sec/uid |
| `cancel_orders` | POST | `/api/v1/futures/trade/cancel_orders` | signed | 10 req/sec/uid |
| `close_all_position` | POST | `/api/v1/futures/trade/close_all_position` | signed | 1 req/sec/uid |
| `flash_close_position` | POST | `/api/v1/futures/trade/flash_close_position` | signed | 5 req/sec/uid |
| `get_history_orders` | GET | `/api/v1/futures/trade/get_history_orders` | signed | 10 req/sec/uid |
| `get_history_trades` | GET | `/api/v1/futures/trade/get_history_trades` | signed | 10 req/sec/uid |
| `get_order_detail` | GET | `/api/v1/futures/trade/get_order_detail` | signed | 10 req/sec/uid |
| `get_pending_orders` | GET | `/api/v1/futures/trade/get_pending_orders` | signed | 10 req/sec/uid |
| `modify_order` | POST | `/api/v1/futures/trade/modify_order` | signed | 20 req/sec/uid |
| `place_order` | POST | `/api/v1/futures/trade/place_order` | signed | 20 req/sec/uid |

## WebSocket

| Kind | Channels |
|---|---|
| Public | `price`, `ticker`, `tickers`, `trade`, `depth_books`, `depth_book1/5/15`, `market_kline_<interval>` |
| Private | `balance`, `order`, `position`, `tpsl` |

## Change log

The Get Kline contract was pinned to the 2026-11 changelog (`type` = `LAST_PRICE | MARK_PRICE`). See `docs/bitunix/change_log.md` / `docs/_raw_api_docs/...change_log.html`.
