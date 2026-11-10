"""Engine state snapshots for the chat/Telegram surfaces.

Extracted from the old web dashboard (module removed): it is the SAME
snapshot the bot renders, built through the same ``agent.handle`` readouts so
any surface that shows it cannot disagree with the CLI.

    * ``build_state(agent)``  one structured snapshot (engine state, equity,
      positions, PnL, open orders, the read-only settings).
"""
from __future__ import annotations

import time

from .agent import JRockAgent
from .trader.client import order_rows

#: How often a surface is expected to refresh. The engine's own report loop
#: uses the same default (EngineState.report_interval), so they agree on what
#: "a report every 30 seconds" means.
REPORT_INTERVAL_SEC = 30

#: Read engine/agent commands surfaced as readouts. Each is a command the CLI
#: accepts verbatim - including the bare ``margin amount`` read.
READOUT_COMMANDS: dict[str, str] = {
    "status": "status",
    "settings": "settings trade",
    "margin_amount": "margin amount",
    "tpsl": "tpsl",
    "pnl": "pnl",
    "trades_history": "trades_history",
    "tpsl_history": "tpsl_history",
}


def _f(value) -> float:
    """Parse an exchange numeric string. Never raises."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_keys(agent: JRockAgent) -> bool:
    """True when Bitunix credentials are configured (same test the CLI uses)."""
    s = agent.s
    return bool(s.bitunix_api_key and s.bitunix_api_secret)


def _position_rows(positions: list[dict]) -> list[dict]:
    """Normalise open positions for a state table.

    ROI is computed with the engine's own ``roi_pct`` so the rendered rows and
    the engine's report cannot disagree about what a position is doing.
    """
    from .trader.risk import roi_pct

    rows = []
    for p in positions or []:
        entry = _f(p.get("avgOpenPrice"))
        mark = _f(p.get("markPrice")) or entry
        lev = int(_f(p.get("leverage")) or 1)
        side = str(p.get("side") or "")
        try:
            roi = roi_pct(entry, mark, side, lev)
        except Exception:  # noqa: BLE001
            roi = 0.0
        rows.append({
            "symbol": p.get("symbol"),
            "side": side,
            "qty": p.get("qty"),
            "entry": entry,
            "mark": mark,
            "liq": p.get("liqPrice"),
            "leverage": lev,
            "roi": round(roi, 2),
            "unrealized": _f(p.get("unrealizedPNL")),
            "margin": p.get("margin"),
            "margin_mode": p.get("marginMode"),
        })
    return rows


def _order_rows(orders) -> list[dict]:
    """Render order rows for the snapshot.

    ``orders`` is normalised through ``order_rows`` rather than assumed to be a
    list: the client unwraps the exchange's ``{"orderList": [...]}`` envelope,
    but this renderer must not crash the whole state snapshot if a caller ever
    hands it the raw envelope (or a single row) instead.
    """
    return [{
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "type": o.get("orderType"),
        "qty": o.get("qty"),
        "price": o.get("price"),
        "status": o.get("status"),
        "id": o.get("orderId"),
    } for o in order_rows(orders)]


def _account_summary(account_rows, positions, orders) -> dict:
    """Equity / available / margin / unrealized, from the exchange's own rows."""
    from .account import parse_accounts

    accounts = parse_accounts(account_rows)
    equity = sum(a.equity for a in accounts)
    available = sum(a.available for a in accounts)
    margin = sum(a.margin for a in accounts)
    unrealized = sum(a.unrealized for a in accounts)
    realized = 0.0
    for p in positions or []:
        realized += _f(p.get("realizedPNL"))
    return {
        "equity": round(equity, 4),
        "available": round(available, 4),
        "margin": round(margin, 4),
        "unrealized": round(unrealized, 4),
        "realized": round(realized, 4),
        "open_positions": len(positions or []),
        # Count ROWS, not the keys of an envelope dict: the exchange returns
        # {"orderList": [...], "total": N}, and len() on that is 2 regardless of
        # how many orders are actually open.
        "open_orders": len(order_rows(orders)),
        "coins": [a.margin_coin for a in accounts],
    }


def _setting_fields(agent: JRockAgent) -> list[dict]:
    """Every engine setting, with its current value (one knob, one meaning)."""
    s = agent.engine.state
    fields = [
        ("margin_amount_pct", "margin per trade (%)", s.margin_amount_pct),
        ("leverage", "leverage (x)", s.leverage),
        ("risk_pct", "risk per trade (%)", s.risk_pct),
        ("max_open_positions", "max open positions", s.max_open_positions),
        ("tpsl_method", "TP/SL method", s.tpsl_method),
        ("partial_tp_ladder", "PARTIAL ladder spec", s.partial_tp_ladder),
        ("breakeven_pct", "breakeven threshold (%)", s.breakeven_pct),
        ("trailing_trigger_roi_pct", "trailing trigger ROI (%)", s.trailing_trigger_roi_pct),
        ("liq_sl_distance_pct", "liq SL distance (%)", s.liq_sl_distance_pct),
        ("min_agreeing", "min agreeing strategies", s.min_agreeing),
        ("min_confidence", "min confidence", s.min_confidence),
        ("tf_min_confidence", "tf min confidence", s.tf_min_confidence),
        ("scan_interval", "scan interval (s)", s.scan_interval),
        ("report_interval", "report interval (s)", s.report_interval),
        ("reversal_confidence", "reversal confidence", s.reversal_confidence),
        ("account_tp_usdt", "account TP (USDT)", s.account_tp_usdt),
        ("account_sl_usdt", "account SL (USDT)", s.account_sl_usdt),
        ("symbols", "symbols", ",".join(s.symbols)),
        ("timeframes", "timeframes", ",".join(s.timeframes)),
    ]
    return [{"key": k, "label": lab, "value": v} for k, lab, v in fields]


def _margin_plan(s, account: dict) -> dict:
    """What MARGIN_AMOUNT_PCT actually means right now, in USDT.

    The setting is a percentage, and a percentage of what is not obvious from
    the number alone - the user's complaint was that the margin on a real
    position did not match the percentage they set. Showing the derived USDT
    amount next to the percentage makes the two directly comparable, and makes
    a clamp visible instead of silent.

    ``target_usdt`` is the margin one position will aim to lock. It is derived
    from the spendable balance (``available``), which is what the engine sizes
    from - not equity, which includes unrealized PnL that cannot be spent.
    """
    pct = float(getattr(s, "margin_amount_pct", 0.0) or 0.0)
    available = float(account.get("available") or 0.0)
    equity = float(account.get("equity") or 0.0)
    target = available * pct / 100.0 if pct > 0 else 0.0
    return {
        "pct": pct,
        "basis": "available",
        "available": round(available, 4),
        "equity": round(equity, 4),
        "target_usdt": round(target, 4),
        "leverage": int(getattr(s, "leverage", 0) or 0),
        # margin * leverage = the notional the target margin controls. Shown so
        # the user can see that leverage changes the SIZE, not the margin.
        "target_notional": round(target * int(getattr(s, "leverage", 0) or 0), 4),
        "enabled": pct > 0,
        "note": ("0 disables the margin target; sizing falls back to risk_pct."
                 if pct <= 0 else
                 f"{pct:g}% of the {available:.2f} USDT available balance "
                 f"= {target:.2f} USDT margin per position at "
                 f"{int(getattr(s, 'leverage', 0) or 0)}x."),
    }


async def _readouts(agent: JRockAgent) -> dict[str, str]:
    """The read/query commands, surfaced as readouts.

    Each goes through ``agent.handle()`` - the same entry point the CLI uses -
    so a snapshot shows exactly what the CLI would print, including the bare
    ``margin amount`` read and the engine's ``settings trade``.
    """
    out: dict[str, str] = {}
    for name, cmd in READOUT_COMMANDS.items():
        try:
            out[name] = await agent.handle(cmd)
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERROR: {type(e).__name__}: {e}"
    return out


def _strategy_summary() -> dict:
    """The registered strategies, by category - the engine's own registry."""
    from .trader.strategies import registry

    reg = registry()
    cats: dict[str, int] = {}
    for v in reg.values():
        cats[v["category"]] = cats.get(v["category"], 0) + 1
    return {"count": len(reg), "by_category": cats, "names": sorted(reg)}


def _permission_summary(agent: JRockAgent) -> dict:
    """The permission flags the agent enforces, as the CLI reports them."""
    p = agent.perms
    return {
        "auto_accept_edit": p.auto_accept_edit,
        "full_device_access": p.full_device_access,
        "block_destructive": p.block_destructive,
    }


async def build_state(agent: JRockAgent) -> dict:
    """Build the state snapshot. Shared by the Telegram bot and the tests."""
    s = agent.engine.state
    api_ok, api_msg = False, "not checked"
    account_rows: list = []
    positions: list = []
    orders: list = []
    if _has_keys(agent):
        try:
            api_ok, api_msg = await agent.engine.client.verify_credentials()
        except Exception as e:  # noqa: BLE001
            api_ok, api_msg = False, f"{type(e).__name__}: {e}"
        try:
            account_rows = await agent.engine.client.get_single_account() or []
        except Exception as e:  # noqa: BLE001
            api_msg = f"{api_msg}; account: {type(e).__name__}: {e}"
        try:
            positions = await agent.engine.client.get_pending_positions() or []
        except Exception as e:  # noqa: BLE001
            api_msg = f"{api_msg}; positions: {type(e).__name__}: {e}"
        try:
            orders = await agent.engine.client.get_pending_orders() or []
        except Exception as e:  # noqa: BLE001
            api_msg = f"{api_msg}; orders: {type(e).__name__}: {e}"
    else:
        api_msg = "Bitunix API keys are not set - account data unavailable."

    account = _account_summary(account_rows, positions, orders)
    account["api_ok"] = api_ok
    account["api_msg"] = api_msg

    return {
        "updated_at": time.strftime("%H:%M:%S"),
        "updated_ts": time.time(),
        "report_interval": REPORT_INTERVAL_SEC,
        "margin_plan": _margin_plan(s, account),
        "engine": {
            "engine_on": s.engine_on,
            "autotrade": s.autotrade,
            "scan_on": s.scan_on,
            "report_on": s.report_on,
            "report_interval": s.report_interval,
            "scan_interval": s.scan_interval,
            "mid_management": s.mid_management,
            "reversal": s.reversal,
            "reversal_confidence": s.reversal_confidence,
            "symbols": list(s.symbols),
            "leverage": s.leverage,
            "margin_mode": s.margin_mode,
            "position_mode": s.position_mode,
            "tpsl_method": s.tpsl_method,
            "partial_tp_ladder": s.partial_tp_ladder,
            "scans": s.scans,
            "last_scan": (time.strftime("%H:%M:%S", time.localtime(s.last_scan_ts))
                          if s.last_scan_ts else ""),
            "errors": len(s.errors),
            "last_error": s.errors[-1] if s.errors else "",
            "autotrade_was_on": agent.engine.autotrade_was_on,
            "engine_was_on": agent.engine.engine_was_on,
        },
        "account": account,
        "positions": _position_rows(positions),
        "orders": _order_rows(orders),
        "settings": _setting_fields(agent),
        "strategies": _strategy_summary(),
        "permissions": _permission_summary(agent),
        "readouts": await _readouts(agent),
    }
