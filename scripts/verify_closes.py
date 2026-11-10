#!/usr/bin/env python3
"""Show WHY a position opened and closed - from the engine's own logs.

The question "why did it open and close several positions in a minute?" can
only be answered from the engine's records, not from the exchange's app: the
position history shows THAT a position closed, not WHICH path closed it.

This prints, read-only, from the local data directory:

  1. the last opens and closes with their reason (reversal / account_tp /
     account_sl / user / closed_on_exchange);
  2. the last TP/SL plans and parked rungs;
  3. the engine settings that decide the behaviour (TP/SL method, ladder,
     breakeven, trailing, reversal, cooldown, leverage, risk, margin);
  4. the last errors and notes.

Usage:
    python scripts/verify_closes.py            # the default data dir
    python scripts/verify_closes.py /path/to/data
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _read_jsonl(path: Path, n: int = 15) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-n:]


def _print_events(title: str, rows: list[dict], keys: tuple[str, ...]) -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print("(none)")
        return
    for r in rows:
        parts = []
        for k in keys:
            if k in r and r[k] not in (None, "", [], {}):
                parts.append(f"{k}={r[k]}")
        print("  " + "  ".join(parts))


def main() -> int:
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1
                    else os.environ.get("JROCK_DATA_DIR", "data"))
    print(f"data dir: {data_dir.resolve()}")
    if not data_dir.exists():
        print("no data directory here - run this on the machine that runs the engine")
        return 2

    trades = _read_jsonl(data_dir / "trades.jsonl", 25)
    tpsl = _read_jsonl(data_dir / "tpsl.jsonl", 15)

    _print_events("OPENS and CLOSES (newest last)", trades,
                  ("event", "ts", "symbol", "side", "qty", "entry", "position_id",
                   "reason", "via", "order_id"))
    _print_events("TP/SL plans and rungs (newest last)", tpsl,
                  ("event", "ts", "symbol", "position_id", "sl", "tp", "liq",
                   "sl_roi_pct", "tp_roi_pct", "placed", "skipped", "reason"))

    state_path = data_dir / "state.json"
    print(f"\n=== settings ({state_path.name}) ===")
    if not state_path.exists():
        print("(no state file)")
    else:
        try:
            blob = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"(unreadable: {e})")
            blob = {}
        engine = blob.get("engine", {}) if isinstance(blob, dict) else {}
        interesting = ("tpsl_method", "partial_tp_ladder", "breakeven_pct",
                       "trailing_trigger_roi_pct", "mid_management", "reversal",
                       "reversal_confidence", "reentry_cooldown_sec",
                       "leverage", "risk_pct", "margin_amount_pct",
                       "margin_mode", "position_mode", "max_open_positions",
                       "account_tp_usdt", "account_sl_usdt", "symbols",
                       "min_agreeing", "min_confidence", "scan_interval")
        for k in interesting:
            if k in engine:
                print(f"  {k} = {engine[k]}")
        positions = blob.get("positions", {}) if isinstance(blob, dict) else {}
        print(f"  positions in the store: {len(positions)} "
              f"{list(positions) if positions else ''}")

    print("\nPaste this whole output back - it says which path closed the "
          "positions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
