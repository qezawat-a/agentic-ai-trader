#!/usr/bin/env python3
"""Populate references/api-docs/ and references/api-markdown/ with category folders.

- api-markdown/<category>/  : curated markdown copies (docs/bitunix) + generated
                              WebSocket-channel markdown for what was raw-HTML only.
- api-docs/<category>/       : README index mapping each endpoint to the raw HTML file.

Re-run after fetching new docs:
    python3 scripts/build_reference_docs.py
"""
from __future__ import annotations

import html as htmllib
import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MD_SRC = os.path.join(ROOT, "docs", "bitunix")
HTML_SRC = os.path.join(ROOT, "docs", "_raw_api_docs")
HTML_PREFIX = "__api-docs__futures__"
API_DOCS = os.path.join(ROOT, "references", "api-docs")
API_MD = os.path.join(ROOT, "references", "api-markdown")

# category -> [(markdown name, raw html basename), ...]
CATEGORY = {
    "market": [
        ("get_tickers", "market__get_tickers.html"),
        ("get_trading_pairs", "market__get_trading_pairs.html"),
        ("get_depth", "market__get_depth.html"),
        ("get_kline", "market__get_kline.html"),
        ("get_funding_rate", "market__get_funding_rate.html"),
        ("get_funding_rate_batch", "market__get_funding_rate_batch.html"),
        ("get_funding_rate_history", "market__get_funding_rate_history.html"),
    ],
    "account": [
        ("get_single_account", "account__get_single_account.html"),
        ("get_leverage_and_margin_mode", "account__get_leverage_and_margin_mode.html"),
        ("get_position_mode", "account__get_position_mode.html"),
        ("get_trading_settings", "account__get_trading_settings.html"),
        ("change_leverage", "account__change_leverage.html"),
        ("change_margin_mode", "account__change_margin_mode.html"),
        ("change_position_mode", "account__change_position_mode.html"),
        ("adjust_position_margin", "account__adjust_position_margin.html"),
    ],
    "position": [
        ("get_pending_positions", "position__get_pending_positions.html"),
        ("get_history_positions", "position__get_history_positions.html"),
        ("get_position_tiers", "position__get_position_tiers.html"),
    ],
    "trade": [
        ("place_order", "trade__place_order.html"),
        ("batch_order", "trade__batch_order.html"),
        ("modify_order", "trade__modify_order.html"),
        ("cancel_orders", "trade__cancel_orders.html"),
        ("cancel_all_orders", "trade__cancel_all_orders.html"),
        ("get_pending_orders", "trade__get_pending_orders.html"),
        ("get_history_orders", "trade__get_history_orders.html"),
        ("get_order_detail", "trade__get_order_detail.html"),
        ("get_history_trades", "trade__get_history_trades.html"),
        ("close_all_position", "trade__close_all_position.html"),
        ("flash_close_position", "trade__flash_close_position.html"),
    ],
    "tp_sl": [
        ("place_tp_sl_order", "tp_sl__place_tp_sl_order.html"),
        ("place_position_tp_sl_order", "tp_sl__place_position_tp_sl_order.html"),
        ("modify_tp_sl_order", "tp_sl__modify_tp_sl_order.html"),
        ("modify_position_tp_sl_order", "tp_sl__modify_position_tp_sl_order.html"),
        ("cancel_tp_sl_order", "tp_sl__cancel_tp_sl_order.html"),
        ("get_pending_tp_sl_order", "tp_sl__get_pending_tp_sl_order.html"),
        ("get_history_tp_sl_order", "tp_sl__get_history_tp_sl_order.html"),
    ],
    "copyTrading": [
        ("asset_query", "copyTrading__asset__asset_query.html"),
        ("transfer_from_main_to_sub", "copyTrading__asset__transfer_asset_from_main_account_to_sub_account.html"),
        ("transfer_from_sub_to_main", "copyTrading__asset__transfer_asset_from_subaccount_to_main_account.html"),
    ],
    "websocket": [
        ("WebSocket", "websocket__prepare__WebSocket.html"),
    ],
}

# websocket channel markdown files to generate (raw HTML only in the source)
WS_CHANNELS_MD = {
    "Public_Price": "websocket__public__MarketPrice%20Channel.html",
    "Public_Ticker": "websocket__public__Ticker%20Channel.html",
    "Public_Tickers": "websocket__public__Tickers%20Channel.html",
    "Public_Trade": "websocket__public__Trade%20Channel.html",
    "Public_Depth": "websocket__public__depth%20channel.html",
    "Public_Kline": "websocket__public__kline%20channel.html",
    "Private_Balance": "websocket__private__Balance%20Channel.html",
    "Private_Order": "websocket__private__Order%20Channel.html",
    "Private_Position": "websocket__private__Position%20Channel.html",
    "Private_Tpsl": "websocket__private__Tp%20Sl%20Channel.html",
}


def clean() -> None:
    for d in (API_DOCS, API_MD):
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)


def copy_markdown() -> None:
    for cat, entries in CATEGORY.items():
        for md_name, _ in entries:
            src = os.path.join(MD_SRC, md_name + ".md")
            if os.path.exists(src):
                dst_dir = os.path.join(API_MD, cat)
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy2(src, os.path.join(dst_dir, md_name + ".md"))


def generate_ws_markdown() -> None:
    for name, html in WS_CHANNELS_MD.items():
        src = os.path.join(HTML_SRC, HTML_PREFIX + html)
        if not os.path.exists(src):
            continue
        dst = os.path.join(API_MD, "websocket", name + ".md")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        text = re.sub(r"<script.*?</script>", "", raw, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = htmllib.unescape(text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        lines = [l.strip() for l in text.splitlines()]
        body = "\n".join(l for l in lines if l)
        # clean doc URL: html = "websocket__public__MarketPrice%20Channel.html"
        tail = html.replace("__", "/")  # -> websocket/public/MarketPrice%20Channel.html
        url = f"https://www.bitunix.com/api-docs/futures/{tail}"
        head = f"<!-- source: {url} -->\n"
        with open(dst, "w", encoding="utf-8") as g:
            g.write(head + "\n# " + name.replace("_", " ") + "\n\n```text\n" + body + "\n```\n")


def build_indexes() -> None:
    for cat, entries in CATEGORY.items():
        dst_dir = os.path.join(API_DOCS, cat)
        os.makedirs(dst_dir, exist_ok=True)
        lines = [f"# {cat.upper()} — raw API docs (html)\n"]
        lines.append("Raw HTML fetched from the official Bitunix futures docs. "
                     "Curated markdown lives in `references/api-markdown/`; "
                     "the authoritative machine-readable contract is "
                     "`references/BITUNIX_API_PARAMETERS.md`.\n")
        lines.append("| Endpoint | Raw HTML | Markdown |")
        lines.append("|---|---|---|")
        for md_name, html in entries:
            md_p = f"`../api-markdown/{cat}/{md_name}.md`"
            html_p = f"`../../docs/_raw_api_docs/{HTML_PREFIX}{html}`"
            lines.append(f"| {md_name} | {html_p} | {md_p} |")
        if cat == "websocket":
            lines.append("\nChannel markdown is under `../api-markdown/websocket/` "
                         "(generated from raw HTML):")
            for name in sorted(WS_CHANNELS_MD):
                lines.append(f"- `../api-markdown/websocket/{name}.md`")
        lines.append("")
        with open(os.path.join(dst_dir, "README.md"), "w") as f:
            f.write("\n".join(lines))


def main() -> None:
    clean()
    copy_markdown()
    generate_ws_markdown()
    build_indexes()
    print("populated references/api-docs and references/api-markdown")


if __name__ == "__main__":
    main()