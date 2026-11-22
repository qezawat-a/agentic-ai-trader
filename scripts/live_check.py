"""Live smoke test against the REAL Bitunix API.

Run it:

    .venv/bin/python scripts/live_check.py

What it checks, and what it needs:

  * PUBLIC endpoints (no API key): tickers, trading pairs, klines, the
    position tier table. These are checked against the live server.
  * AUTHENTICATED endpoints (need BITUNIX_API_KEY / BITUNIX_API_SECRET):
    account, positions, orders. Without a key these are reported as
    SKIPPED - the script never pretends to have checked them.

It NEVER places, modifies or cancels an order, and never changes a
setting. It only reads.

Exit code 0 = every check that could run passed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jrock.config import load_settings  # noqa: E402
from jrock.trader.engine import TraderEngine  # noqa: E402
from jrock.trader.risk import Tier, tier_for_leverage  # noqa: E402
from jrock.trader.tpsl import plan_tpsl  # noqa: E402

OK = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str) -> None:
    results.append((name, status, detail))
    print(f"[{status}] {name}: {detail}")


async def main() -> int:
    s = load_settings()
    e = TraderEngine(s)
    has_keys = bool(s.bitunix_api_key and s.bitunix_api_secret)

    print("=" * 72)
    print("J+Rock live check - Bitunix")
    print(f"base url: {s.bitunix_base_url}")
    print(f"api keys: {'present' if has_keys else 'MISSING (public checks only)'}")
    print(f"engine settings: symbols={s.symbols} leverage={s.leverage}x "
          f"margin_mode={s.margin_mode} max_pos={s.max_open_positions}")
    print("=" * 72)

    # ---------------------------------------------------------- public: tickers
    try:
        t0 = time.time()
        tickers = await e.client.get_tickers()
        record("GET /tickers (public)", OK,
               f"{len(tickers)} symbols in {time.time() - t0:.1f}s")
    except Exception as ex:  # noqa: BLE001
        record("GET /tickers (public)", FAIL, f"{type(ex).__name__}: {ex}")
        await e.client.close()
        return 1

    # ------------------------------------------------------- public: pair rules
    sym = "BTCUSDT"
    try:
        await e.load_rules([sym])
        rules = e.rules.get(sym)
        if rules is None:
            record("GET /trading_pairs (public)", FAIL, f"no rules for {sym}")
        else:
            record("GET /trading_pairs (public)", OK,
                   f"{sym} status={rules.symbol_status} api_supported={rules.api_supported} "
                   f"basePrecision={rules.base_precision} quotePrecision={rules.quote_precision} "
                   f"minTradeVolume={rules.min_trade_volume} maxLeverage={rules.max_leverage}")
    except Exception as ex:  # noqa: BLE001
        record("GET /trading_pairs (public)", FAIL, f"{type(ex).__name__}: {ex}")

    # ------------------------------------------------------ public: tier table
    tiers = []
    try:
        raw = await e.client.get_position_tiers(sym)
        tiers = [Tier(int(r["level"]), float(r["startValue"]), float(r["endValue"]),
                      int(r["leverage"]), float(r["maintenanceMarginRate"]))
                 for r in raw]
        t = tier_for_leverage(tiers, s.leverage)
        record("GET /position_tiers (public)", OK,
               f"{len(tiers)} tiers; {s.leverage}x -> tier {t.level} "
               f"mmr={t.maintenance_margin_rate} maxNotional={t.end_value}")
    except Exception as ex:  # noqa: BLE001
        record("GET /position_tiers (public)", FAIL, f"{type(ex).__name__}: {ex}")

    # ---------------------------------------------------------- public: klines
    try:
        candles = await e.get_candles(sym, "5m", 300)
        record("GET /klines (public)", OK,
               f"{sym} 5m -> {len(candles)} candles, last close="
               f"{candles[-1].close if candles else 'n/a'}")
    except Exception as ex:  # noqa: BLE001
        record("GET /klines (public)", FAIL, f"{type(ex).__name__}: {ex}")

    # ---------------------------------------------------------- engine: universe
    try:
        t0 = time.time()
        syms = await e._universe_symbols()
        record("universe ranking", OK if syms else FAIL,
               f"{len(syms)} symbols (rank={e.state.universe_rank}, "
               f"size={e.state.universe_sizing}, min24h={e.state.min_24h_volume_usdt:g}) "
               f"in {time.time() - t0:.1f}s; top5={syms[:5]}")
    except Exception as ex:  # noqa: BLE001
        record("universe ranking", FAIL, f"{type(ex).__name__}: {ex}")

    # ------------------------------------------------------------ engine: scan
    try:
        agg = await e.scan_symbol(sym)
        record("engine scan (strategies + ATR)", OK,
               f"{sym} dir={agg.direction} conf={agg.confidence:.1f} "
               f"agreeing={agg.agreeing} strength={agg.strength} "
               f"atr_pct={agg.atr_pct:.4f} actionable={agg.actionable}")
    except Exception as ex:  # noqa: BLE001
        record("engine scan (strategies + ATR)", FAIL, f"{type(ex).__name__}: {ex}")

    # ----------------------------------------------------------- engine: TP/SL
    try:
        rules = e.rules.get(sym)
        if rules is None or not tiers:
            record("auto TP/SL plan", SKIP, "rules or tiers unavailable")
        else:
            price = await e.get_price(sym)
            agg = await e.scan_symbol(sym)
            side = agg.direction if agg.direction in ("LONG", "SHORT") else "LONG"
            p = plan_tpsl(side=side, entry=price, leverage=s.leverage,
                          notional=1000, strength=agg.strength,
                          confidence=agg.confidence, tiers=tiers, rules=rules,
                          sl_distance_auto=True, atr_pct=agg.atr_pct,
                          breakeven_pct=s.breakeven_pct,
                          trailing_trigger_roi_pct=s.trailing_trigger_roi_pct)
            liq_roi = abs(p.liq - p.entry) / p.entry * s.leverage * 100
            sl_roi = abs(p.entry - p.sl) / p.entry * s.leverage * 100
            record("auto TP/SL plan", OK,
                   f"entry={p.entry} sl={p.sl} tp={p.tp} liq={p.liq} | "
                   f"sl_roi={sl_roi:.1f}% liq_roi={liq_roi:.1f}% "
                   f"gap={liq_roi - sl_roi:.1f} ROI pts | rr={p.rr:.2f} "
                   f"breakeven={p.breakeven_pct}% trailing={p.trailing_trigger_roi_pct}%")
    except Exception as ex:  # noqa: BLE001
        record("auto TP/SL plan", FAIL, f"{type(ex).__name__}: {ex}")

    # ------------------------------------------------------------- authenticated
    ok, msg = await e.client.verify_credentials()
    if ok:
        record("auth", OK, msg)
        try:
            acct = await e.client.get_single_account()
            a0 = acct[0] if isinstance(acct, list) and acct else (acct or {})
            record("GET /account", OK,
                   f"available={a0.get('available')} mode={a0.get('positionMode')}")
        except Exception as ex:  # noqa: BLE001
            record("GET /account", FAIL, f"{type(ex).__name__}: {ex}")
        try:
            positions = await e.client.get_positions()
            record("GET /positions", OK, f"{len(positions or [])} open position(s)")
        except Exception as ex:  # noqa: BLE001
            record("GET /positions", FAIL, f"{type(ex).__name__}: {ex}")
        try:
            orders = await e.client.get_orders(None, None)
            record("GET /orders", OK, f"{len(orders or [])} open order(s)")
        except Exception as ex:  # noqa: BLE001
            record("GET /orders", FAIL, f"{type(ex).__name__}: {ex}")
    else:
        record("auth", SKIP, f"{msg} - set BITUNIX_API_KEY/BITUNIX_API_SECRET")
        record("GET /account", SKIP, "needs API keys")
        record("GET /positions", SKIP, "needs API keys")
        record("GET /orders", SKIP, "needs API keys")

    await e.client.close()

    print("=" * 72)
    passed = sum(1 for _, st, _ in results if st == OK)
    failed = sum(1 for _, st, _ in results if st == FAIL)
    skipped = sum(1 for _, st, _ in results if st == SKIP)
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("\nFAILED:")
        for name, st, detail in results:
            if st == FAIL:
                print(f"  - {name}: {detail}")
    if skipped:
        print("\nSKIPPED (not verified):")
        for name, st, detail in results:
            if st == SKIP:
                print(f"  - {name}: {detail}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
