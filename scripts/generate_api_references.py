#!/usr/bin/env python3
"""Generate references/BITUNIX_API_PARAMETERS.md and references/official-endpoints-bitunix.md
straight from jrock/trader/api_reference.py, which is kept in sync with docs/bitunix/
by tests/test_api_reference.py.

This guarantees the reference files can never drift from the contract the engine
actually enforces. Re-run any time api_reference.py changes:

    python3 scripts/generate_api_references.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jrock.trader.api_reference import (  # noqa: E402
    BASE_URL,
    ENDPOINTS,
    SIGNATURE_RULE,
    WS_CHANNELS,
    WS_LOGIN_RULE,
)

REFS = os.path.join(os.path.dirname(__file__), "..", "references")


def _rows() -> list[tuple]:
    return sorted(ENDPOINTS.items(), key=lambda kv: (kv[1].path, kv[0]))


def _group(ep):
    parts = ep.path.strip("/").split("/")
    if "futures" in parts:
        return parts[parts.index("futures") + 1]
    return parts[1] if len(parts) > 1 else parts[0]


def build_parameters_md() -> str:
    L = []
    L.append("# Bitunix Futures API — Parameter Reference")
    L.append("")
    L.append("Machine-generated from `jrock/trader/api_reference.py` — the same contract ")
    L.append("the engine validates every call against. Never guess: cross-check a parameter ")
    L.append("here (or in `docs/bitunix/`) before sending it.")
    L.append("")
    L.append(f"- Base URL (REST): `{BASE_URL}`")
    L.append("- Public WS: `wss://fapi.bitunix.com/public/`")
    L.append("- Private WS: `wss://fapi.bitunix.com/private/`")
    L.append("- Curated docs: `docs/bitunix/`  •  Raw HTML: `docs/_raw_api_docs/`")
    L.append("")

    # ---- signing
    L.append("## Authentication & Signature")
    L.append("")
    L.append("```")
    L.append(SIGNATURE_RULE)
    L.append("```")
    L.append("")
    L.append("Required headers on every signed request:")
    L.append("")
    L.append("| Header | Value |")
    L.append("|---|---|")
    L.append("| `api-key` | your API key string |")
    L.append("| `timestamp` | current Unix epoch in **milliseconds** (int) |")
    L.append("| `nonce` | unique random string, e.g. `str(uuid.uuid4())` |")
    L.append("| `sign` | SHA-256 hex digest (double SHA-256, see above) |")
    L.append("| `language` | e.g. `en-US` |")
    L.append("| `Content-Type` | `application/json` |")
    L.append("")
    L.append("> **#1 cause of error 10007**: url-encoding the query string for signing. ")
    L.append("> Sign the raw `key+value` concatenation, no `=` and no `&`.")
    L.append("")

    # ---- websocket channels
    L.append("## WebSocket Channels")
    L.append("")
    L.append("Channel names are **exact wire strings** — the server ACKs a wrong name ")
    L.append("(`{\"op\":\"connect\",\"data\":{\"result\":true}}`) then never pushes. Do not invent.")
    L.append("")
    for kind, chans in WS_CHANNELS.items():
        L.append(f"### {kind.title()}")
        L.append("")
        L.append("| Channel | Description |")
        L.append("|---|---|")
        for name, desc in chans.items():
            L.append(f"| `{name}` | {desc} |")
        L.append("")
    L.append("Private WS login:")
    L.append("")
    L.append("```")
    L.append(WS_LOGIN_RULE)
    L.append("```")
    L.append("")

    # ---- endpoints grouped
    L.append("## Endpoints")
    L.append("")
    groups: dict[str, list] = {}
    for name, ep in _rows():
        groups.setdefault(_group(ep), []).append((name, ep))

    for group in sorted(groups):
        L.append(f"### {group.upper()}")
        L.append("")
        for name, ep in groups[group]:
            auth = "signed" if ep.signed else "public"
            L.append(f"#### `{name}` — `{ep.method} {ep.path}`")
            L.append("")
            if ep.description:
                L.append(f"{ep.description}  ")
            L.append(f"- Auth: **{auth}**  •  Rate: `{ep.rate_limit or '-'}`")
            if ep.doc:
                L.append(f"- Doc: `docs/bitunix/{ep.doc}`")
            if ep.params:
                L.append("")
                L.append("| Parameter | Type | Required | Description |")
                L.append("|---|---|---|---|")
                for p in ep.params:
                    req = "**yes**" if p.required else "no"
                    desc = (p.description or "").replace("|", "\\|")
                    L.append(f"| `{p.name}` | {p.type} | {req} | {desc} |")
            else:
                L.append("")
                L.append("*No request parameters.*")
            L.append("")
    return "\n".join(L)


def build_endpoints_md() -> str:
    L = []
    L.append("# Bitunix Futures — Official Endpoint Inventory")
    L.append("")
    L.append("All endpoints the engine can call, with method, path, auth and rate limit. ")
    L.append("Generated from `jrock/trader/api_reference.py` (kept in sync with `docs/bitunix/`).")
    L.append("")
    L.append(f"Base URL: `{BASE_URL}`")
    L.append("")
    L.append("| Endpoint | Method | Path | Auth | Rate |")
    L.append("|---|---|---|---|---|")
    for name, ep in _rows():
        auth = "signed" if ep.signed else "public"
        L.append(f"| `{name}` | {ep.method} | `{ep.path}` | {auth} | {ep.rate_limit or '-'} |")
    L.append("")
    L.append("## WebSocket")
    L.append("")
    L.append("| Kind | Channels |")
    L.append("|---|---|")
    for kind, chans in WS_CHANNELS.items():
        L.append(f"| {kind.title()} | {', '.join(f'`{c}`' for c in chans)} |")
    L.append("")
    L.append("## Change log")
    L.append("")
    L.append("The Get Kline contract was pinned to the 2026-11 changelog "
             "(`type` = `LAST_PRICE | MARK_PRICE`). See `docs/bitunix/change_log.md` / "
             "`docs/_raw_api_docs/...change_log.html`.")
    L.append("")
    return "\n".join(L)


def main() -> None:
    params = os.path.join(REFS, "BITUNIX_API_PARAMETERS.md")
    ends = os.path.join(REFS, "official-endpoints-bitunix.md")
    with open(params, "w") as f:
        f.write(build_parameters_md())
    with open(ends, "w") as f:
        f.write(build_endpoints_md())
    print(f"wrote {params}")
    print(f"wrote {ends}")


if __name__ == "__main__":
    main()