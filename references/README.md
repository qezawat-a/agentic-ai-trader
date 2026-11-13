# Bitunix Futures Development & Debugging Skill — reference home

This directory is the authoritative reference store for the **bitunix-futures-dev**
skill (and its companions `bitunix-endpoints-examples` and `bitunix-debugger`).
When debugging or building any Bitunix integration, load these files before
touching code.

## Layout

```
references/
├── BITUNIX_API_PARAMETERS.md      # every endpoint, every parameter, signing, WS  (generated)
├── official-endpoints-bitunix.md  # 37-endpoint inventory: method/path/auth/rate     (generated)
├── REVIEW_CHECKLIST.md            # pass-by-pass debug & repair checklist
├── api-docs/                      # raw-HTML index, grouped by category (Account, Market, Trade,
│   │                              #   Position, TP/SL, CopyTrading, WebSocket, ErrorCode)
│   └── <category>/README.md
├── api-markdown/                  # curated markdown per endpoint, grouped by category
│   ├── market/ account/ position/ trade/ tp_sl/ copyTrading/ websocket/
│   └── websocket/*.md             # generated channel docs (Public_*, Private_*)
└── examples/                      # multi-language clients, each self-checks the sign vector
    ├── node/index.js              # Node (ESM, fetch)
    ├── python/bitunix_client.py   # Python (stdlib)
    ├── go/main.go                 # Go (stdlib)
    ├── java/BitunixSign.java      # Java (MessageDigest)
    └── php/bitunix_client.php     # PHP (stdlib)
```

## How to read it

- **Fast contract lookup** → `BITUNIX_API_PARAMETERS.md` (machine-generated from
  `jrock/trader/api_reference.py`, so it can never drift from what the engine enforces).
- **Endpoint inventory** → `official-endpoints-bitunix.md`.
- **Ground-truth docs** → `api-markdown/` (curated) then `api-docs/` (raw HTML index).
- **Debug workflow** → `REVIEW_CHECKLIST.md` (do the passes top to bottom).
- **Signing across languages** → `examples/` — every file is a runnable self-check
  that asserts the official test vector and rejects the urlencoded form.

## Regenerating

The three generated/reference-able pieces rebuild from the source of truth:

```bash
python3 scripts/generate_api_references.py   # -> BITUNIX_API_PARAMETERS.md, official-endpoints-bitunix.md
python3 scripts/build_reference_docs.py      # -> api-docs/, api-markdown/
```

## Golden rules (see REVIEW_CHECKLIST for the full list)

1. Never invent endpoints or parameter names — cross-check `BITUNIX_API_PARAMETERS.md`.
2. Signing is double SHA-256, query string is `key+value` (no `=`/`&`), timestamp ms.
3. Kline price selector is `type` (`LAST_PRICE | MARK_PRICE`), never `klineType`.
4. Margin mode enum is `ISOLATION | CROSS`, position mode `HEDGE | ONE_WAY`.
5. WS channel names are exact wire strings; the server ACKs a wrong name then never pushes.