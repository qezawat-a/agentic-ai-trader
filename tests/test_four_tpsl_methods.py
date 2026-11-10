"""Regression tests for the FOUR documented Bitunix TP/SL methods.

Source: the official academy article
"Bitunix Futures: 4 TP/SL Methods Guide" (bitunix-futures-positions-guide-to-
four-tp-sl-methods), which defines exactly four methods:

    1. Position TP/SL  - one TP/SL closes the ENTIRE position
    2. Partial TP/SL   - a defined % closes at each trigger (staged exits)
    3. Trailing TP/SL  - dynamic exit after a trend + retracement
    4. Account TP/SL   - closes ALL positions when total account PnL crosses
                         a threshold

Position and Partial map to native endpoints and are enforced by the exchange.
Trailing and Account have NO native endpoint and are software - they only run
while the manage loop is up. These tests pin that distinction, because
pretending a software stop is exchange-enforced would be a lie about risk.
"""
from __future__ import annotations

import asyncio

import pytest

from jrock.config import Settings
from jrock.trader.account_tpsl import (AccountTpSlConfig, account_tpsl_decision,
                                       total_unrealized_pnl)
from jrock.trader.engine import TraderEngine
from jrock.trader.risk import PairRules

AKE = PairRules(
    symbol="AKEUSDT", min_trade_volume=1.0, max_market_order_volume=100_000_000.0,
    max_limit_order_volume=1_500_000_000.0, base_precision=0, quote_precision=6,
    min_leverage=1, max_leverage=50, default_leverage=10,
    price_protect_scope=0.05, symbol_status="OPEN", api_supported=True,
)

PAIR_ROW = {
    "symbol": "AKEUSDT", "minTradeVolume": "1",
    "maxMarketOrderVolume": "100000000", "maxLimitOrderVolume": "1500000000",
    "basePrecision": 0, "quotePrecision": 6, "maxLeverage": 50, "minLeverage": 1,
    "defaultLeverage": 10, "priceProtectScope": "0.05", "symbolStatus": "OPEN",
    "isApiSupported": True,
}


# ============================================================ 1. POSITION
def test_position_method_is_the_default_and_is_exchange_enforced():
    s = Settings()
    assert s.tpsl_method == "POSITION"
    e = TraderEngine(s)
    report = e.tpsl_method_report()
    assert "POSITION" in report
    assert "exchange-enforced" in report


def test_position_method_uses_the_position_endpoint_not_the_standalone_one():
    """Position TP/SL has no qty field - it can only close the whole position."""
    from jrock.trader.client import BitunixClient
    c = BitunixClient(Settings())
    calls: list[tuple] = []

    async def fake_request(method, path, **kw):
        calls.append((method, path, kw.get("body")))
        return {"ok": True}

    c.request = fake_request  # type: ignore[assignment]
    asyncio.run(c.place_position_tp_sl_order("AKEUSDT", "P1", "0.006", "0.004"))
    method, path, body = calls[0]
    assert path == "/api/v1/futures/tpsl/position/place_order"
    assert "tpQty" not in body and "slQty" not in body, (
        "the position endpoint has no quantity field - sending one would be "
        "inventing a parameter")


# ============================================================ 2. PARTIAL
def test_partial_method_parks_native_rung_orders_at_entry():
    """Each rung is its own native tpsl/place_order with tpQty - not a close."""
    s = Settings()
    s.tpsl_method = "PARTIAL"
    s.partial_tp_ladder = "40@1,35@2,25@3"
    e = TraderEngine(s)
    report = e.tpsl_method_report()
    assert "PARTIAL" in report
    assert "40@1,35@2,25@3" in report


def test_partial_ladder_spec_is_validated_before_it_is_stored():
    s = Settings()
    e = TraderEngine(s)
    out = asyncio.run(e.command("partial ladder 40@1,30@1"))
    assert "Invalid ladder" in out, out
    assert e.state.partial_tp_ladder == "40@1,35@2,25@3", \
        "a bad spec must not overwrite the running ladder"
    out = asyncio.run(e.command("partial ladder 50@2,50@4"))
    assert e.state.partial_tp_ladder == "50@2,50@4"
    out = asyncio.run(e.command("partial ladder off"))
    assert e.state.partial_tp_ladder == ""


def test_partial_tp_sl_sends_tpqty_and_slqty():
    """tpQty/slQty are what make a staged exit possible."""
    from jrock.trader.client import BitunixClient
    c = BitunixClient(Settings())
    calls: list[dict] = []

    async def fake_request(method, path, **kw):
        calls.append({"path": path, "body": kw.get("body")})
        return {"ok": True}

    c.request = fake_request  # type: ignore[assignment]
    asyncio.run(c.place_tp_sl_order("AKEUSDT", "P1", tp_price="0.006",
                                    sl_price="0.004", tp_qty="1000", sl_qty="1000"))
    body = calls[0]["body"]
    assert calls[0]["path"] == "/api/v1/futures/tpsl/place_order"
    assert body["tpQty"] == "1000" and body["slQty"] == "1000"
    assert body["positionId"] == "P1"


def test_partial_tp_sl_requires_a_quantity():
    """Official: at least one of tpQty/slQty is required."""
    from jrock.trader.client import BitunixClient
    c = BitunixClient(Settings())
    with pytest.raises(ValueError, match="tpQty/slQty"):
        asyncio.run(c.place_tp_sl_order("AKEUSDT", "P1", tp_price="0.006"))


def test_partial_tp_sl_requires_a_price():
    from jrock.trader.client import BitunixClient
    c = BitunixClient(Settings())
    with pytest.raises(ValueError, match="tpPrice/slPrice"):
        asyncio.run(c.place_tp_sl_order("AKEUSDT", "P1", tp_qty="1000"))


def test_partial_rungs_go_through_the_native_endpoint_when_method_is_partial():
    """The engine must route each rung through place_tp_sl_order (tpQty),
    and the fallback must cover the runner on the position endpoint."""
    s = Settings()
    s.tpsl_method = "PARTIAL"
    s.partial_tp_ladder = "40@1,35@2,25@3"
    e = TraderEngine(s)
    sent: list[dict] = []

    class C:
        async def place_tp_sl_order(self, *a, **kw):
            sent.append(kw)
            return {"orderId": "T9"}

    e.client = C()  # type: ignore[assignment]
    from jrock.trader.tpsl import parse_partial_ladder
    rungs = parse_partial_ladder(s.partial_tp_ladder)
    asyncio.run(e._place_partial_rungs(
        "AKEUSDT", "P1", "LONG", 0.005, 1000.0,
        sl_dist=0.001, rungs=rungs, rules=AKE, trigger_type="MARK_PRICE"))
    assert len(sent) == 3, sent
    first = sent[0]
    assert first["tp_qty"] == "400"      # 40% of 1000
    assert first["tp_price"] == "0.006000"   # 1R = 0.001 above entry
    assert first["tp_stop_type"] == "MARK_PRICE"
    third = sent[2]
    assert third["tp_qty"] == "250" and third["tp_price"] == "0.008000"


def test_partial_rungs_survive_the_array_shaped_response():
    """The live server answers /tpsl/place_order with an ARRAY.

    The doc page shows an object, but the OFFICIAL demo types the endpoint
    `List<OrderIdResp>` and the live server follows the demo. The engine read
    `(res or {}).get("orderId")` on that list and died with
    `AttributeError: 'list' object has no attribute 'get'` AFTER the first rung
    was already created - so the exchange held ONE take-profit where the ladder
    wanted three, and the scan loop died on every cycle.

    This pins the fix at both layers: the client collapses the array into one
    order object, and the rung loop still places every rung.
    """
    from jrock.trader.client import BitunixClient

    # --- the client boundary: an array response becomes one order dict
    c = BitunixClient(Settings())

    async def fake_request(method, path, **kw):
        return [{"orderId": "T1", "clientId": "c1"}]   # the live array shape

    c.request = fake_request  # type: ignore[assignment]
    res = asyncio.run(c.place_tp_sl_order("AKEUSDT", "P1", tp_price="0.006",
                                         tp_qty="400"))
    assert isinstance(res, dict), type(res)
    assert res["orderId"] == "T1"

    # --- the engine: every rung goes out even with the array shape
    s = Settings()
    s.tpsl_method = "PARTIAL"
    s.partial_tp_ladder = "40@1,35@2,25@3"
    e = TraderEngine(s)
    sent: list[dict] = []

    class C:
        async def place_tp_sl_order(self, *a, **kw):
            sent.append(kw)
            return [{"orderId": "T9"}]               # the live array shape

    e.client = C()  # type: ignore[assignment]
    from jrock.trader.tpsl import parse_partial_ladder
    rungs = parse_partial_ladder(s.partial_tp_ladder)
    asyncio.run(e._place_partial_rungs(
        "AKEUSDT", "P1", "LONG", 0.005, 1000.0,
        sl_dist=0.001, rungs=rungs, rules=AKE, trigger_type="MARK_PRICE"))
    assert len(sent) == 3, sent


def test_partial_ladder_too_small_falls_back_to_one_whole_position_tp():
    """A position the exchange will not split still gets ONE plan, not none."""
    s = Settings()
    s.tpsl_method = "PARTIAL"
    s.partial_tp_ladder = "40@1,35@2,25@3"
    e = TraderEngine(s)
    modified: list[dict] = []

    class C:
        async def place_tp_sl_order(self, *a, **kw):
            # minTradeVolume is 1.0 and every rung is smaller -> all skipped
            raise ValueError("qty below minTradeVolume")

        async def modify_position_tp_sl_order(self, sym, pid, **kw):
            modified.append(kw)
            return {"ok": True}

    e.client = C()  # type: ignore[assignment]
    from jrock.trader.tpsl import parse_partial_ladder, PositionState
    rungs = parse_partial_ladder(s.partial_tp_ladder)
    e.positions["P1"] = PositionState(
        position_id="P1", symbol="AKEUSDT", side="LONG", entry=0.005,
        qty=0.5, leverage=10, sl=0.004, tp=0.0, liq=0.003)
    asyncio.run(e._place_partial_rungs(
        "AKEUSDT", "P1", "LONG", 0.005, 0.5,
        sl_dist=0.001, rungs=rungs, rules=AKE, trigger_type="MARK_PRICE"))
    assert modified, "the fallback whole-position TP must be attached"
    assert modified[0]["tp_price"] == "0.008000"   # outermost rung (3R)
    assert e.positions["P1"].tp == 0.008


# ============================================================ 3. TRAILING
def test_trailing_is_always_armed_and_reported_as_software():
    s = Settings()
    e = TraderEngine(s)
    report = e.tpsl_method_report()
    assert "trailing" in report.lower()
    assert "software" in report, (
        "a software ratchet must not be described as exchange-enforced")
    assert "manage loop" in report


def test_trailing_method_is_refused_as_an_entry_method():
    """TRAILING is always armed - only the NATIVE exit shape is a choice."""
    s = Settings()
    e = TraderEngine(s)
    out = asyncio.run(e.command("tpsl method TRAILING"))
    assert "PARTIAL" in out or "POSITION" in out
    assert e.state.tpsl_method in ("POSITION", "PARTIAL")


def test_unknown_method_is_refused():
    s = Settings()
    e = TraderEngine(s)
    out = asyncio.run(e.command("tpsl method BANANA"))
    assert "must be" in out
    assert e.state.tpsl_method == "POSITION", "a bad value must not be stored"


# ============================================================ 4. ACCOUNT
def test_account_method_is_off_by_default():
    cfg = AccountTpSlConfig()
    assert not cfg.enabled
    assert "off" in cfg.describe()


def test_account_tp_fires_on_total_pnl():
    cfg = AccountTpSlConfig(tp_usdt=100.0)
    d = account_tpsl_decision(total_pnl=120.0, equity=1000.0, cfg=cfg)
    assert d.should_close_all and d.trigger == "tp"
    assert d.threshold == 100.0
    assert "closing all positions" in d.reason


def test_account_sl_fires_on_total_pnl():
    cfg = AccountTpSlConfig(sl_usdt=50.0)
    d = account_tpsl_decision(total_pnl=-60.0, equity=1000.0, cfg=cfg)
    assert d.should_close_all and d.trigger == "sl"


def test_account_thresholds_do_not_fire_inside_the_band():
    cfg = AccountTpSlConfig(tp_usdt=100.0, sl_usdt=50.0)
    d = account_tpsl_decision(total_pnl=10.0, equity=1000.0, cfg=cfg)
    assert not d.should_close_all
    assert d.trigger == ""


def test_total_unrealized_pnl_sums_the_exchange_field():
    positions = [{"unrealizedPNL": "12.5"}, {"unrealizedPNL": "-2.5"},
                 {"unrealizedPNL": None}, {"no_pnl": 1}]
    assert total_unrealized_pnl(positions) == 10.0


def test_account_thresholds_arm_regardless_of_the_entry_method():
    """ACCOUNT is always armed - a USDT threshold fires even with PARTIAL."""
    s = Settings()
    s.tpsl_method = "PARTIAL"
    s.account_sl_usdt = 50.0
    e = TraderEngine(s)
    assert e.account_cfg().enabled
    d = account_tpsl_decision(total_pnl=-60.0, equity=1000.0,
                              cfg=e.account_cfg())
    assert d.should_close_all and d.trigger == "sl"


def test_account_method_is_reported_as_software():
    s = Settings()
    s.account_tp_usdt = 100.0
    e = TraderEngine(s)
    report = e.tpsl_method_report()
    assert "account TP/SL" in report and "software" in report
    assert "100" in report


def test_account_method_closes_all_positions_when_triggered():
    """End to end: the manage loop must flatten the account."""
    s = Settings()
    s.account_tp_usdt = 100.0
    e = TraderEngine(s)
    closed: list[str] = []

    class C:
        async def get_pending_positions(self, sym=None):
            return [{"positionId": "P1", "symbol": "AKEUSDT", "side": "LONG",
                     "avgOpenPrice": "0.005", "qty": "1000", "leverage": "10",
                     "liqPrice": "0.004", "markPrice": "0.0051",
                     "unrealizedPNL": "150.0"}]

        async def get_single_account(self):
            return [{"available": "1000"}]

        async def close_all_position(self):
            closed.append("all")
            return {"ok": True}

        async def get_trading_pairs(self, symbols=None):
            return [PAIR_ROW]

    e.client = C()  # type: ignore[assignment]

    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.manage_positions()

    actions = asyncio.run(go())
    assert closed == ["all"], "the account threshold must flatten everything"
    assert actions[0]["action"] == "account_tp"
    assert actions[0]["total_pnl"] == 150.0


def test_account_method_does_not_fire_below_the_threshold():
    s = Settings()
    s.account_tp_usdt = 100.0
    e = TraderEngine(s)
    closed: list[str] = []

    class C:
        async def get_pending_positions(self, sym=None):
            return [{"positionId": "P1", "symbol": "AKEUSDT", "side": "LONG",
                     "avgOpenPrice": "0.005", "qty": "1000", "leverage": "10",
                     "liqPrice": "0.004", "markPrice": "0.0051",
                     "unrealizedPNL": "10.0"}]

        async def get_single_account(self):
            return [{"available": "1000"}]

        async def get_tickers(self, sym):
            return [{"symbol": sym, "lastPrice": "0.0051",
                     "markPrice": "0.0051"}]

        async def modify_position_tp_sl_order(self, *a, **k):
            return {"orderId": "T1"}

        async def close_all_position(self):
            closed.append("all")
            return {"ok": True}

        async def get_trading_pairs(self, symbols=None):
            return [PAIR_ROW]

    e.client = C()  # type: ignore[assignment]

    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.manage_positions()

    asyncio.run(go())
    assert closed == [], "10 USDT must not trip a 100 USDT threshold"


def test_close_all_positions_falls_back_to_per_position_close():
    """A bulk-call failure must not leave the account half-flattened."""
    from jrock.trader.errors import BitunixAPIError
    s = Settings()
    e = TraderEngine(s)
    flashed: list[str] = []

    class C:
        async def close_all_position(self):
            raise BitunixAPIError(10001, "boom")

        async def get_pending_positions(self, sym=None):
            return [{"positionId": "P1", "symbol": "AKEUSDT"},
                    {"positionId": "P2", "symbol": "ETHUSDT"}]

        async def flash_close_position(self, pid):
            flashed.append(pid)
            return {"ok": True}

    e.client = C()  # type: ignore[assignment]
    res = asyncio.run(e.close_all_positions())
    assert flashed == ["P1", "P2"]
    assert res["via"] == "flash_close_position" and res["ok"]


# ============================================================ the four, together
def test_native_method_choices_are_accepted_and_software_is_refused():
    s = Settings()
    e = TraderEngine(s)
    for m in ("POSITION", "PARTIAL"):
        out = asyncio.run(e.command(f"tpsl method {m}"))
        assert e.state.tpsl_method == m
        assert m in out
    for m in ("TRAILING", "ACCOUNT", "BANANA"):
        out = asyncio.run(e.command(f"tpsl method {m}"))
        assert "must be" in out or "Method must be" in out
        assert e.state.tpsl_method in ("POSITION", "PARTIAL"), (
            "a refused value must not be stored")


def test_method_survives_a_restart(tmp_path, monkeypatch):
    """The chosen method is a setting, so it must persist like one."""
    monkeypatch.setattr("jrock.config.ROOT", tmp_path)
    monkeypatch.setattr("jrock.agent.ROOT", tmp_path)
    from jrock.store import Store

    s = Settings()
    store = Store(data_dir=tmp_path / "data")
    e = TraderEngine(s, store=store)
    asyncio.run(e.command("tpsl method PARTIAL"))
    asyncio.run(e.command("partial ladder 50@2,50@4"))
    asyncio.run(e.command("account tp usdt 120"))
    e._persist_if_changed()

    store2 = Store(data_dir=tmp_path / "data")
    e2 = TraderEngine(Settings(), store=store2)
    assert e2.state.tpsl_method == "PARTIAL"
    assert e2.state.partial_tp_ladder == "50@2,50@4"
    assert e2.state.account_tp_usdt == 120.0


def test_account_threshold_is_absolute_usdt_not_available():
    """The threshold is an ACCOUNT-level USDT number.

    `available` excludes margin locked by open positions; the check reads the
    exchange's unrealizedPNL total against the ABSOLUTE USDT threshold, so it
    does not care how much of the account is locked in margin.
    """
    s = Settings()
    s.account_tp_usdt = 100.0
    e = TraderEngine(s)
    closed: list[str] = []

    class C:
        async def get_pending_positions(self, sym=None):
            return [{"positionId": "P1", "symbol": "AKEUSDT", "side": "LONG",
                     "avgOpenPrice": "0.005", "qty": "1000", "leverage": "10",
                     "liqPrice": "0.004", "markPrice": "0.0051",
                     "unrealizedPNL": "3000.0"}]

        async def get_single_account(self):
            return [{"marginCoin": "USDT", "available": "101", "frozen": "0",
                     "margin": "9000", "crossUnrealizedPNL": "0",
                     "isolationUnrealizedPNL": "0"}]

        async def get_tickers(self, sym=None):
            return [{"symbol": sym, "lastPrice": "0.0051",
                     "markPrice": "0.0051"}]

        async def modify_position_tp_sl_order(self, *a, **k):
            return {"orderId": "T1"}

        async def close_all_position(self):
            closed.append("all")
            return {"ok": True}

        async def get_trading_pairs(self, symbols=None):
            return [PAIR_ROW]

    e.client = C()  # type: ignore[assignment]

    async def go():
        await e.load_rules(["AKEUSDT"])
        return await e.manage_positions()

    actions = asyncio.run(go())
    assert closed == ["all"], "+3,000 PnL must cross a +100 USDT TP"
    assert actions[0]["action"] == "account_tp"
    assert actions[0]["total_pnl"] == 3000.0


# ==================================================== the report shows TP/SL
def test_report_shows_the_tpsl_state():
    """The report used to stop at the signal and the position line.

    An operator could not see whether the exits were armed - the method, the
    ladder, the stop, the target, the trailing state were all invisible
    outside `/tpsl`. The Telegram report push carries the same text, so the
    blind spot was the operator's only window into the risk.
    """
    s = Settings()
    s.tpsl_method = "PARTIAL"
    s.partial_tp_ladder = "40@1,35@2,25@3"
    s.account_tp_usdt = 100.0
    e = TraderEngine(s)
    from jrock.trader.tpsl import PositionState
    e.positions["P1"] = PositionState(
        position_id="P1", symbol="AKEUSDT", side="LONG", entry=0.005, qty=1000.0,
        leverage=10, sl=0.004, tp=0.0, liq=0.003, breakeven_done=True)

    class C:
        async def get_pending_positions(self, sym=None):
            return [{"positionId": "P1", "symbol": "AKEUSDT", "side": "LONG",
                     "avgOpenPrice": "0.005", "qty": "1000", "leverage": "10",
                     "liqPrice": "0.003", "markPrice": "0.0051",
                     "unrealizedPNL": "12.5"}]

        async def get_tickers(self, sym=None):
            return [{"symbol": sym, "lastPrice": "0.0051", "markPrice": "0.0051"}]

    e.client = C()  # type: ignore[assignment]
    out = asyncio.run(e.report())
    assert "-- TP/SL --" in out, out
    assert "PARTIAL" in out and "40@1,35@2,25@3" in out, out
    assert "sl=0.004" in out, out          # the stop is visible
    assert "breakeven" in out.lower(), out
    assert "account TP" in out, out       # the account thresholds are visible


def test_the_last_rung_is_never_dropped():
    """A remainder that no later rung can take must not vanish.

    Real case: a 29-unit position, minTradeVolume 10, ladder 40@1,35@2,25@3.
    The rungs were 11, 10 and 7 units - the last one was below the minimum,
    and with no next rung to merge into it was DROPPED: 8 units of the
    position were left with a stop and no take-profit. The remainder is now
    folded into the last placeable rung, so every unit keeps an exit.
    """
    from jrock.trader.tpsl import parse_partial_ladder
    rules = PairRules(
        symbol="INITUSDT", min_trade_volume=10.0, max_market_order_volume=30000.0,
        max_limit_order_volume=2000000.0, base_precision=0, quote_precision=5,
        min_leverage=1, max_leverage=25, default_leverage=10,
        price_protect_scope=0.05, symbol_status="OPEN", api_supported=True)
    s = Settings()
    s.tpsl_method = "PARTIAL"
    s.partial_tp_ladder = "40@1,35@2,25@3"
    e = TraderEngine(s)
    sent: list[dict] = []

    class C:
        async def place_tp_sl_order(self, *a, **kw):
            sent.append(kw)
            return {"orderId": "T1"}

    e.client = C()  # type: ignore[assignment]
    rungs = parse_partial_ladder(s.partial_tp_ladder)
    asyncio.run(e._place_partial_rungs(
        "INITUSDT", "P1", "LONG", 0.27566, 29.0,
        sl_dist=0.00255, rungs=rungs, rules=rules, trigger_type="MARK_PRICE"))
    covered = sum(float(x["tp_qty"]) for x in sent)
    assert covered >= 28.0, f"only {covered} of 29 units have a take-profit: {sent}"
    assert all(float(x["tp_qty"]) >= 10.0 for x in sent), sent


# ==================================================== a closed position is gone
def test_a_closed_position_is_dropped_from_the_engine_state():
    """The engine's position list was only ever ADDED to.

    A position that was closed stayed in `self.positions` forever: the report
    kept counting it and the state file kept growing. The exchange is the
    source of truth - what it does not report is gone.
    """
    from jrock.trader.tpsl import PositionState
    s = Settings()
    e = TraderEngine(s)
    e.positions["P1"] = PositionState(
        position_id="P1", symbol="AKEUSDT", side="LONG", entry=0.005, qty=1000.0,
        leverage=10, sl=0.004, tp=0.006, liq=0.003)

    closed = e.reconcile_positions([])          # the exchange reports nothing
    assert closed == ["P1"], closed
    assert "P1" not in e.positions

    # a position the exchange DOES report stays
    e.positions["P2"] = PositionState(
        position_id="P2", symbol="AKEUSDT", side="LONG", entry=0.005, qty=1000.0,
        leverage=10, sl=0.004, tp=0.006, liq=0.003)
    e.reconcile_positions([{"positionId": "P2", "symbol": "AKEUSDT"}])
    assert "P2" in e.positions


def test_a_closed_symbol_is_not_re_entered_immediately():
    """The churn: a signal that keeps firing re-opened the position the
    moment it closed - two market fills a minute apart, each paying the
    taker fee twice, for a trade that never had time to work."""
    from jrock.trader.tpsl import PositionState
    s = Settings()
    s.reentry_cooldown_sec = 60
    e = TraderEngine(s)
    e.positions["P1"] = PositionState(
        position_id="P1", symbol="AKEUSDT", side="LONG", entry=0.005, qty=1000.0,
        leverage=10, sl=0.004, tp=0.006, liq=0.003)
    e.reconcile_positions([])                   # it closed
    assert "re-entry waits" in e._reentry_block("AKEUSDT")
    assert e._reentry_block("BTCUSDT") == ""

    e.state.reentry_cooldown_sec = 0               # disabled
    assert e._reentry_block("AKEUSDT") == ""
