# Bitunix Futures — Review & Repair Checklist

A systematic, pass-by-pass checklist for debugging or repairing any Bitunix
integration in this project. Work top-to-bottom; each pass is independent.

Ground truth, in priority order (never guess above these):
1. `references/BITUNIX_API_PARAMETERS.md` — machine-generated from the live contract
2. `references/api-markdown/` and `docs/bitunix/` — curated docs
3. `references/api-docs/` — raw HTML index
4. `jrock/trader/api_reference.py` — the contract the engine actually enforces
5. `docs/_raw_api_docs/` — the raw scraped pages

---

## 0. Scope the surface

- [ ] Map the call sites: `src/exchange/`, `src/trading/`, `src/ai/`, `src/telegram/`,
      `src/mcp/`, `soul/`, plus tests and the settings schema.
- [ ] List every Bitunix endpoint invoked and tick it against
      `official-endpoints-bitunix.md` — no invented endpoints, no invented params.
- [ ] Confirm the exact base URLs:
      - REST: `https://fapi.bitunix.com`
      - Public WS: `wss://fapi.bitunix.com/public/`
      - Private WS: `wss://fapi.bitunix.com/private/`

## 1. Authentication & signing

- [ ] Double SHA-256, not HMAC:
      ```
      digest = sha256(nonce + timestamp + apiKey + sortedQueryParams + body)
      sign   = sha256(digest + secretKey)
      ```
- [ ] Headers present: `api-key`, `sign`, `nonce`, `timestamp`, `language`, `Content-Type`.
- [ ] `timestamp` is in **milliseconds**.
- [ ] Query string for signing is `key+value` concatenation — NO `=`, NO `&`, keys
      sorted, NOT url-encoded (url-encoding is the #1 cause of **error 10007**).
- [ ] POST body for signing is compact JSON, keys sorted, byte-identical to what is sent.
- [ ] WS login: `sign = sha256(sha256(nonce + timestamp + apiKey) + secretKey)`,
      order is nonce, timestamp, apiKey; timestamp in **seconds**; nonce < 64 chars.

## 2. Endpoint correctness (per endpoint)

For every endpoint, tick each line:
- [ ] Path and method match `official-endpoints-bitunix.md`.
- [ ] Every required parameter from `BITUNIX_API_PARAMETERS.md` is supplied.
- [ ] No parameter is passed that is not in the table (server ignores unknowns silently).
- [ ] Type matches (string / int / int64 / bool / array) and enum values are exact:
      - `orderType`: `LIMIT | MARKET`
      - `side`: `BUY | SELL`
      - `tradeSide`: `OPEN | CLOSE` (hedge mode only)
      - `marginMode`: `ISOLATION | CROSS`  (note: **ISOLATION**, not ISOLATED)
      - `positionMode`: `HEDGE | ONE_WAY`
      - `effect`: `IOC | FOK | GTC | POST_ONLY`
      - kline `type`: `LAST_PRICE | MARK_PRICE` (never `klineType`)
- [ ] Rate limits respected (see the Rate column in the inventory).

Gotcha endpoints that bite:
- `get_kline`: `startTime` is NOT supported; `endTime` is exclusive; `type` selects price.
- `get_funding_rate_batch`: takes NO params (the old `symbols` filter is gone) — filter locally.
- `get_funding_rate_history`: note the `get_` prefix in the path.
- `cancel_orders`: needs `symbol` + `orderList`, or `orderId`/`clientId` for a single cancel.
- `flash_close_position`: `positionId` is required (not `symbol`+`holdSide`).

## 3. TP/SL & liquidation distance

- [ ] Stop-loss keeps a safe distance from liquidation (~50% ROI buffer per the spec).
- [ ] Trigger `type` uses the price family the exchange actually stores for that order
      (`LAST_PRICE` by default; switch to `MARK_PRICE` only when intended).
- [ ] Trailing: trigger ROI %, stop %, distance %, breakeven threshold % all feed the
      correct `modify_position_tp_sl_order` / `modify_tp_sl_order` calls.
- [ ] Margin mode and position mode are set before opening a position when required.

## 4. Trading engine rules

- [ ] Min agreeing strategies: 3 • Min confidence: 80 • TF min confidence: 60
- [ ] Timeframes: 1m, 3m, 5m, 15m
- [ ] Indicators: EMA, MA, RSI, MACD, VOLUME, MOMENTUM, ATR_BREAKOUT,
      super_trend, bollinger
- [ ] Loops: scan 15s • guard 15s • mid-management 15s • report 30s
- [ ] TP/SL values derived from signal strength by the agent, not hard-coded.

## 5. WebSocket correctness

- [ ] Channel names are the exact wire strings (server ACKs a wrong name then never pushes):
      public `price`, `ticker`, `tickers`, `trade`, `depth_books`, `depth_book1/5/15`,
      `market_kline_<interval>`; private `balance`, `order`, `position`, `tpsl`.
- [ ] Kline interval suffixes are `1min 3min 5min 15min 30min 60min 2h 4h 6h 8h 12h
      1day 3day 1week 1month` — `1h` is `60min`, never `1hour`.
- [ ] Private channels complete the login handshake before subscribing.

## 6. Self-heal & agent surface

- [ ] Commands that must exist: `/help /models /set_model /connect_provider /add_mcp
      /mcp /add_skill /skill /add_soul /soul /think /memory /code_review /bug_fix
      /verify /simplify /team_spawn /check_ai /check_system`.
- [ ] Trading commands: engine on/off, status, report on/off + interval, settings trade,
      scan on/off + interval, guard, mid-management, breakeven, trailing, liq-sl distance,
      min agreeing strategies, timeframes, min confidence, positions history, cancels,
      close position, margin mode, position mode, symbols, max positions, TPSL handling.
- [ ] AutoSetModelByKey: probe base URL + key (list models → filter chat → tiny tool-call
      probe → rank), persist the winner.
- [ ] Agent can inspect and edit its own code under the permission / auto-accept toggle.

## 7. Offline / robustness

- [ ] Error code handling maps the documented codes (see `docs/bitunix/error_code.md`).
- [ ] Circuit breaker / rate-limit backoff is present, not a bare retry loop.
- [ ] WS reconnect restores subscriptions and, for private channels, re-authenticates.

## 8. Validate

- [ ] `python -m pytest -q tests`
- [ ] Focused suites: `test_api_reference`, `test_signing`, `test_order_builder`,
      `test_tpsl*`, `test_engine_preflight`, `test_api_contract`.
- [ ] `python scripts/verify_api_contract.py`
- [ ] No invented parameter remains; grep call sites against the parameter tables.