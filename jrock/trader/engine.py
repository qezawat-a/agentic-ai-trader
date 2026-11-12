"""The trader engine: scanner + trader + autotrade + position management.

State machine:
    engine_on  -> scanner loop + management loop run
    scan_on    -> scanner produces signals (no orders)
    autotrade  -> scanner signals are executed as orders

All commands from the spec are implemented as methods and routed by `command()`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..account import account_equity
from ..config import Settings
from ..store import Store, open_store
from .account_tpsl import (AccountTpSlConfig, account_tpsl_decision,
                           total_unrealized_pnl)
from .aggregator import Aggregate, aggregate
from .client import BitunixClient, BitunixConfig, order_row
from .errors import BitunixAPIError
from .indicators import atr, to_candles
from .kline_cache import REST_KLINE_MAX, KlineCache
from .order_builder import (OrderValidationError, build_order_body,
                            log_order_body)
from .price_cache import PriceCache
from .risk import (PairRules, Tier, TpSlSideError, clamp_sl, clamp_tp,
                   fee_for_notional, format_price, format_qty, margin_budget,
                   margin_required,
                   max_leverage_for_stop, max_notional_for_balance,
                   max_notional_for_margin,
                   pick_tier,
                   position_size_by_margin,
                   position_size_by_risk,
                   qty_from_notional, qty_from_unit, required_cost, roi_pct,
                   round_price, round_step, spendable_balance,
                   tier_for_leverage, tier_leverage_violation,
                   unit_from_qty, validate_tpsl_side)
from .strategies import build_strategies
from .tpsl import (PartialLadderError, PositionState, LadderRung,
                   mid_management_action, next_trailing_stop,
                   parse_partial_ladder, plan_tpsl, rung_target,
                   should_breakeven, should_trail)
from .ws import MarketDataFeed, PrivateFeed, WSConfig

log = logging.getLogger("jrock.engine")


def _fmt_ms_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts) / 1000).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _num(v: Any, default: float = 0.0) -> float:
    """Best-effort float from an exchange string field ('' / None -> default)."""
    try:
        return float(v or default)
    except (TypeError, ValueError):
        return default

#: Which price the exchange compares a TP/SL trigger against. MARK_PRICE is the
#: documented Bitunix default and the engine's historical behaviour; the SAME
#: value goes into the local pre-flight validation and into tpStopType/slStopType
#: in the order body, so the two can never disagree (that mismatch is what
#: produced 30029).
TRIGGER_TYPE = "MARK_PRICE"

#: Minimum distance a stop must keep from the mark/entry price, in ticks
#: (1 tick = 10^-quotePrecision). Prevents rounding from collapsing a stop onto
#: the mark price, which the exchange rejects with 30029/30031.
MIN_STOP_TICKS = 3


@dataclass
class EngineState:
    engine_on: bool = False
    scan_on: bool = True
    autotrade: bool = False
    report_on: bool = False
    report_interval: int = 30
    scan_interval: int = 30
    reversal: bool = False
    reversal_confidence: float = 70.0
    mid_management: bool = True
    #: Volatile health of the manage loop: time.time() of the last FULL,
    #: successful cycle. 0 = never ran. The report uses it to say, loudly,
    #: whether the breakeven/trailing machinery is actually ALIVE - a dead
    #: loop used to leave the report claiming "both always on" while no
    #: stop had moved for hours (peak_roi frozen at 17% while live ROI
    #: was 31%).
    manage_last_ok: float = 0.0
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT"])
    leverage: int = 10
    margin_mode: str = "ISOLATION"
    position_mode: str = "ONE_WAY"
    max_open_positions: int = 3
    #: Seconds the engine waits before re-entering a symbol it just closed.
    reentry_cooldown_sec: int = 60
    risk_pct: float = 1.0
    #: Share of the spendable balance one position may lock as margin (%).
    #: 0 disables the budget and sizes purely by risk_pct.
    margin_amount_pct: float = 10.0
    breakeven_pct: float = 0.6
    trailing_trigger_roi_pct: float = 25.0
    liq_sl_distance_pct: float = 50.0
    min_agreeing: int = 3
    timeframes: list[str] = field(default_factory=lambda: ["1m", "3m", "5m", "15m"])
    min_confidence: float = 70.0
    tf_min_confidence: float = 60.0
    #: The TREND GATE: the strategies are momentum chasers - they buy
    #: strength, and in a failing pump they buy the top four times in a row.
    #: The gate refuses a signal whose direction is fought by the biggest
    #: configured timeframe: a LONG with 15m SHORT votes never enters.
    #: The profitable trades all rode a trend the big TF agreed with; the
    #: dump-chases all fought it. OFF restores the old behaviour.
    trend_gate: bool = True
    trend_gate_timeframe: str = "15m"
    last_scan_ts: float = 0.0
    last_report_ts: float = 0.0
    scans: int = 0
    errors: list[str] = field(default_factory=list)
    #: Sizing decisions that are NOT failures (leverage clamped, margin budget
    #: clamped). Kept apart from `errors` so a report can show a normal
    #: decision without hiding a real failure behind it.
    notes: list[str] = field(default_factory=list)
    # --- the four documented TP/SL methods (see account_tpsl.py / tpsl.py)
    # POSITION and PARTIAL are the native exit shapes at entry; TRAILING
    # (breakeven + ratchet) and ACCOUNT (thresholds below) are always armed in
    # the manage loop and need no selector.
    tpsl_method: str = "POSITION"
    # --- universe auto-ranking
    #: One of GAINERS / LOOSERS / VOLUME / MOVERS; "" disables ranking.
    #: When SYMBOLS=AUTO and engine is on, the scan loop replaces the
    #: static watchlist with the top symbols ranked by this method.
    universe_rank: str = ""
    #: How many top-ranked symbols to keep in the auto universe (1..200). 0 = off.
    universe_sizing: int = 0
    #: Minimum 24h quote volume (USDT) for a symbol to qualify for the auto universe.
    min_24h_volume_usdt: float = 0.0
    #: PARTIAL ladder spec, "share@R" pairs, e.g. "40@1,35@2,25@3".
    partial_tp_ladder: str = "40@1,35@2,25@3"
    # --- ACCOUNT TP/SL thresholds (USDT). 0 disables a side.
    account_tp_usdt: float = 0.0
    account_sl_usdt: float = 0.0
    #: The minimum stop distance in ticks. Was the module constant
    #: MIN_STOP_TICKS (still the default); the operator can raise it when
    #: the exchange rejects a stop that sits too close to the mark
    #: (30029/30031) or when a volatile symbol needs more room.
    min_stop_ticks: int = MIN_STOP_TICKS


@dataclass
class _EntryContext:
    """Everything one entry attempt needs, in one object.

    The pre-flight changes the size, the leverage and the plan several times:
    the tier cap lowers the leverage, which re-sizes the order, which re-plans
    the stop. One object carries them so the steps stay in order and cannot
    drift apart - and so the steps can be split into functions without every
    one of them taking ten parameters.
    """
    sym: str
    side: str
    price: float
    mark: float
    equity: float
    rules: PairRules
    tiers: list[Tier]
    agg: Aggregate
    leverage: int = 0
    qty: float = 0.0
    notional: float = 0.0
    plan: Any = None
    notes: list[str] = field(default_factory=list)
    #: safety decisions (a leverage clamp the exchange forced) - reported as
    #: errors, unlike the routine `notes`.
    warnings: list[str] = field(default_factory=list)


class TraderEngine:
    def __init__(self, settings: Settings, data_dir: Path | None = None,
                 store: Store | None = None):
        self.s = settings
        self.data_dir = data_dir or settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = self._build_state(settings)
        self.client = BitunixClient(BitunixConfig(
            api_key=settings.bitunix_api_key,
            api_secret=settings.bitunix_api_secret,
            base_url=settings.bitunix_base_url,
            ws_public=settings.bitunix_ws_public,
        ))
        self._init_runtime(settings, store)

    def _build_state(self, settings: Settings) -> EngineState:
        """The engine's live settings, seeded from the config."""
        return EngineState(
            symbols=list(settings.symbols),
            leverage=settings.leverage,
            margin_mode=settings.margin_mode,
            position_mode=settings.position_mode,
            max_open_positions=settings.max_open_positions,
            risk_pct=settings.risk_pct,
            margin_amount_pct=settings.margin_amount_pct,
            breakeven_pct=settings.breakeven_pct,
            trailing_trigger_roi_pct=settings.trailing_trigger_roi_pct,
            liq_sl_distance_pct=settings.liq_sl_distance_pct,
            min_agreeing=settings.min_agreeing,
            timeframes=list(settings.timeframes),
            min_confidence=settings.min_confidence,
            tf_min_confidence=settings.tf_min_confidence,
            scan_interval=settings.scan_interval,
            report_interval=settings.report_interval,
            tpsl_method=(settings.tpsl_method or "POSITION").upper(),
            partial_tp_ladder=settings.partial_tp_ladder,
            account_tp_usdt=settings.account_tp_usdt,
            account_sl_usdt=settings.account_sl_usdt,
            universe_rank=settings.universe_rank,
            universe_sizing=settings.universe_sizing,
            min_24h_volume_usdt=settings.min_24h_volume_usdt,
            trend_gate=settings.trend_gate,
        )

    def _init_runtime(self, settings: Settings, store: Store | None) -> None:
        """Everything that is runtime state rather than configuration."""
        # The exchange's real position mode, cached. `state.position_mode` is only
        # the bot's intent: a rejected change_position_mode leaves them different,
        # and the order body must follow what the exchange actually reports.
        self._position_mode: str = ""
        self._position_mode_ts: float = 0.0
        self.strategies = build_strategies()
        self.rules: dict[str, PairRules] = {}
        self.tiers: dict[str, list[Tier]] = {}
        self.positions: dict[str, PositionState] = {}
        #: symbol -> when it was closed (epoch). Drives the re-entry cooldown.
        self._closed_at: dict[str, float] = {}
        #: symbols the exchange says cannot be traded (error 20015). Scanned
        #: once, then skipped: retrying them every cycle was pure noise.
        self._untradable: dict[str, str] = {}
        #: auto-ranked universe symbols (SYMBOLS=AUTO) that the WS feed must
        #: price each cycle. Empty when universe ranking is off.
        self._auto_symbols: list[str] = []
        self.last_aggregates: dict[str, Aggregate] = {}
        #: symbol -> the MARK price last read by the manage loop. The report
        #: settles ROI against the exchange's uPnL (mark-based); the briefs use
        #: this so the agent cannot quote a LAST-price ROI that contradicts it.
        self._last_marks: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # --- market data: one cache, one optional WS feed, one REST counter.
        # Freshness/cap rules are fixed internals (a 1m candle is legitimately
        # 60s old; the cap bounds memory) - not user-facing knobs.
        self.price_cache = PriceCache()
        self.kline_cache = KlineCache()
        self.feed: MarketDataFeed | None = None
        self.feed_error: str = ""
        self.feed_started_at: float = 0.0
        #: Private WS (balance/position/order/tpsl). Only opened when the
        #: credentials are present AND the source is ws - it is never opened in
        #: rest mode, so rest mode still opens zero sockets.
        self.private_feed: PrivateFeed | None = None
        self.private_feed_error: str = ""
        self.rest_price_calls: int = 0
        self.rest_kline_calls: int = 0
        self.kline_warmup_calls: int = 0
        self.trades_path = self.data_dir / "trades.jsonl"
        self.tpsl_path = self.data_dir / "tpsl.jsonl"
        self.signals_path = self.data_dir / "signals.jsonl"
        # --- persistence: one store owns every durable value
        self.store = self._open_store(settings, store)
        self._snapshot: dict[str, Any] = {}
        self.restored = self._restore_from_store()
        #: set on restore when autotrade was ON before the restart, so the
        #: banner can say so out loud instead of silently resuming live orders.
        self.autotrade_was_on: bool = False
        #: set on restore when the engine loop was ON before the restart.
        self.engine_was_on: bool = False
        # Optional hook so the agent can add its own settings to `status`.
        self.status_extra: Callable[[], list[str]] | None = None

    def _open_store(self, settings: Settings, store: Store | None) -> Store:
        """The store, with the API secrets registered as secrets."""
        # No injected store -> build the right one for this configuration
        # (the database when DATABASE_URL is set, the file otherwise).
        st = store if store is not None else open_store(settings)
        if settings.bitunix_api_key:
            st.register_secret(settings.bitunix_api_key)
        if settings.bitunix_api_secret:
            st.register_secret(settings.bitunix_api_secret)
        return st

    # ============================================================== persistence
    #: EngineState fields that survive a restart. Deliberately excludes
    #: last_*_ts (wall-clock, meaningless later) and `errors` (kept as counters).
    PERSISTED_STATE_FIELDS: tuple[str, ...] = (
        "scan_on", "report_on", "report_interval", "scan_interval",
        "reversal", "reversal_confidence", "mid_management",
        "symbols", "leverage", "margin_mode", "position_mode",
        "max_open_positions", "risk_pct", "margin_amount_pct", "breakeven_pct",
        "trailing_trigger_roi_pct", "liq_sl_distance_pct", "reentry_cooldown_sec",
        "min_agreeing", "timeframes", "min_confidence", "tf_min_confidence",
        "scans",
        "tpsl_method", "partial_tp_ladder",
        "account_tp_usdt", "account_sl_usdt",
        "min_stop_ticks", "universe_rank", "universe_sizing", "min_24h_volume_usdt",
    )

    def _state_snapshot(self) -> dict[str, Any]:
        """Everything whose change should trigger a save.

        Includes `autotrade` and `engine_on` even though they are never
        auto-resumed: toggling autotrade alone must still be recorded, or the
        banner cannot warn that it was on before a crash.
        """
        snap = {f: getattr(self.state, f) for f in self.PERSISTED_STATE_FIELDS}
        snap["autotrade"] = self.state.autotrade
        snap["engine_on"] = self.state.engine_on
        return snap

    def _restore_from_store(self) -> dict[str, Any]:
        """Apply persisted state over the .env-seeded defaults.

        Precedence: store > .env > default. `autotrade` and `engine_on` are
        intentionally NOT auto-resumed - see the note in `_persist_if_changed`.
        """
        saved = self.store.section("engine")
        applied: list[str] = []
        for field_name, value in saved.items():
            if field_name in ("autotrade", "engine_on"):
                continue
            if field_name in self.PERSISTED_STATE_FIELDS:
                try:
                    setattr(self.state, field_name, value)
                    applied.append(field_name)
                except (TypeError, ValueError):
                    continue
        self.autotrade_was_on = bool(saved.get("autotrade"))
        self.engine_was_on = bool(saved.get("engine_on"))

        # positions - tolerant: an older store may carry fields this build no
        # longer has (ladder bookkeeping was removed); they are just dropped.
        import dataclasses
        known = {f.name for f in dataclasses.fields(PositionState)}
        raw_pos = self.store.section("positions")
        for pid, body in raw_pos.items():
            try:
                self.positions[pid] = PositionState(
                    **{k: v for k, v in body.items() if k in known})
            except TypeError:
                continue

        # counters
        saved_counters = self.store.section("counters")
        self.state.scans = int(saved_counters.get("scans", self.state.scans) or 0)

        self._snapshot = self._state_snapshot()
        return {"settings": applied, "positions": len(self.positions),
                "scans": self.state.scans,
                "autotrade_was_on": self.autotrade_was_on,
                "engine_was_on": self.engine_was_on,
                "from": self.store.loaded_from if self.store.loaded else ""}

    def _persist_if_changed(self) -> None:
        """Save engine state if any persisted field moved.

        Called after every command, so a runtime change hits the disk in the
        same call - `kill -9` one millisecond later cannot lose it. autotrade
        and engine_on are recorded as *desired* state only and are never
        resumed automatically: silently restarting live order flow after a
        crash is exactly the failure mode this project must not have.
        """
        snap = self._state_snapshot()
        if snap == self._snapshot:
            return
        self.store.set_section("engine", {
            **snap,
            "autotrade": self.state.autotrade,
            "engine_on": self.state.engine_on,
        }, save=False)
        self.store.counters = {"scans": self.state.scans,
                               "errors": len(self.state.errors),
                               "last_error": self.state.errors[-1] if self.state.errors else ""}
        self.store.save()
        self._snapshot = snap

    # ================================================================ lifecycle
    async def start(self) -> None:
        await self.client.start()
        self._stop.clear()
        await self.start_feed()

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await self.stop_feed()
        await self.client.close()

    # ============================================================ market data
    async def start_feed(self) -> None:
        """Open the public WS feed when MARKET_DATA_SOURCE=ws.

        With `rest` this returns immediately and NO socket is ever opened -
        that is asserted by a test.
        """
        if self.s.market_data_source != "ws":
            return
        if self.feed is not None:
            return
        symbols = self._feed_symbols()
        if not symbols:
            return
        cfg = WSConfig.from_settings(self.s)
        self.feed = MarketDataFeed(cfg, symbols, self.kline_timeframes(),
                                   cache=self.price_cache,
                                   kline_cache=self.kline_cache)
        self.feed_started_at = time.time()
        try:
            await self.feed.start()
            # Wait (bounded) for the handshake so the banner tells the truth
            # instead of reporting "NOT connected" during a normal connect.
            for _ in range(50):
                if self.feed.ws.connected:
                    break
                await asyncio.sleep(0.1)
            # Seed the kline cache from REST once. The WS only gives candles
            # from the moment we subscribe, so without this a 200-candle
            # strategy would need 200 minutes of uptime before it could run.
            await self.warmup_klines()
        except Exception as e:  # noqa: BLE001
            self.feed_error = f"{type(e).__name__}: {e}"
            log.warning("ws feed failed to start: %s - falling back to REST", e)
            self.feed = None
        # The private feed carries balance/position/order/tpsl. It needs
        # credentials, so it is skipped (with a reason) when they are absent -
        # the public feed keeps working either way.
        if self.s.bitunix_api_key and self.s.bitunix_api_secret:
            try:
                self.private_feed = PrivateFeed(cfg)
                await self.private_feed.start()
                for _ in range(30):
                    if self.private_feed.ws.logged_in:
                        break
                    await asyncio.sleep(0.1)
            except Exception as e:  # noqa: BLE001
                self.private_feed_error = f"{type(e).__name__}: {e}"
                log.warning("private ws feed failed: %s", e)
                self.private_feed = None
        else:
            self.private_feed_error = "no BITUNIX_API_KEY/SECRET - private channels not subscribed"

    async def stop_feed(self) -> None:
        if self.private_feed is not None:
            try:
                await self.private_feed.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("private ws feed stop: %s", e)
            self.private_feed = None
        if self.feed is not None:
            try:
                await self.feed.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("ws feed stop: %s", e)
            self.feed = None

    def private_feed_report(self) -> str:
        """Honest one-liner for the private channels."""
        if self.s.market_data_source != "ws":
            return "private ws: not opened (MARKET_DATA_SOURCE=rest)"
        if self.private_feed is None:
            why = f" - {self.private_feed_error}" if self.private_feed_error else ""
            return f"private ws: NOT connected{why}"
        st = self.private_feed.stats()
        state = "logged in" if st["logged_in"] else (
            "connected, login pending" if st["connected"] else "DISCONNECTED")
        return (f"private ws: {state}, channels={','.join(st['channels'])} "
                f"msgs={sum(st['counts'].values())} tpsl_events={st['tpsl_events']}")

    def _feed_symbols(self) -> list[str]:
        """Every symbol the engine needs a price for.

        Includes the auto-ranked universe symbols (SYMBOLS=AUTO) so the WS
        feed prices them instead of leaving the scan loop to poll REST.
        """
        out: list[str] = []
        for s in (list(self.state.symbols)
                  + list(self._auto_symbols)
                  + [p.symbol for p in self.positions.values()]):
            u = (s or "").upper()
            if u and u not in out:
                out.append(u)
        return out

    def feed_connected(self) -> bool:
        return bool(self.feed is not None and self.feed.ws.connected)

    def market_data_report(self) -> str:
        """One honest line for the banner and /status."""
        src = self.s.market_data_source
        if src != "ws":
            return "rest (REST polling)"
        if self.feed is None:
            why = f" - {self.feed_error}" if self.feed_error else ""
            return f"ws (NOT connected - falling back to REST{why})"
        if not self.feed.ws.connected:
            return "ws (NOT connected - falling back to REST)"
        return f"ws (connected, {len(self.feed.symbols)} symbols)"

    async def get_price(self, symbol: str) -> float:
        """THE single price accessor. Every price read in the engine goes here.

        ws mode:  fresh cache -> return it. Stale/missing -> REST fallback.
        rest mode: straight to REST, exactly as before.
        """
        sym = (symbol or "").upper()
        if self.s.market_data_source == "ws":
            q = self.price_cache.get_fresh(sym)
            if q is not None:
                return q.price
            age = self.price_cache.age(sym)
            if age is None:
                self.price_cache.note_miss()
            else:
                self.price_cache.note_stale()
                log.warning("stale price for %s: %.1fs old (max %.1fs) - "
                            "refusing it, falling back to REST", sym, age,
                            self.price_cache.max_age)
            self.price_cache.note_rest_fallback()
        return await self._rest_price(sym)

    async def _rest_price(self, symbol: str) -> float:
        self.rest_price_calls += 1
        data = await self.client.get_tickers(symbol)
        if isinstance(data, list) and data:
            d = data[0]
        elif isinstance(data, dict):
            d = data
        else:
            return 0.0
        for k in ("lastPrice", "last", "price", "markPrice"):
            if d.get(k):
                try:
                    px = float(d[k])
                except (TypeError, ValueError):
                    continue
                if self.s.market_data_source == "ws":
                    self.price_cache.put_rest(symbol, px)
                return px
        return 0.0

    async def get_mark_price(self, symbol: str) -> float:
        """The exchange MARK price, used to validate TP/SL placement.

        This is deliberately separate from `get_price()`: the engine trades on
        the last price, but Bitunix validates TP/SL triggers against the mark
        price (errors 30020-30033). Using the last price for that check lets a
        stop pass locally and be rejected by the exchange.
        """
        sym = (symbol or "").upper()
        try:
            data = await self.client.get_tickers(sym)
        except BitunixAPIError as e:
            self.state.errors.append(f"mark_price {sym}: {e}")
            return 0.0
        if isinstance(data, list) and data:
            d = data[0]
        elif isinstance(data, dict):
            d = data
        else:
            return 0.0
        for k in ("markPrice", "mark"):
            if d.get(k):
                try:
                    return float(d[k])
                except (TypeError, ValueError):
                    continue
        return 0.0

    async def get_trigger_price(self, symbol: str, last: float = 0.0) -> float:
        """The price the exchange compares a TP/SL trigger against.

        TRIGGER_TYPE is fixed to MARK_PRICE (the documented Bitunix default).
        This is the SINGLE place the choice is made. Both the local pre-flight
        validation and the tpStopType/slStopType sent in the order body read
        the same constant, so they can never disagree - a disagreement is
        exactly what produced 30029 (checked against last, rejected against
        mark).
        """
        return await self.get_mark_price(symbol)

    @staticmethod
    def _conservative_mark(last: float, side: str, rules: PairRules) -> float:
        """A mark-price stand-in that is safe for BOTH directions.

        Used only when the real mark price cannot be fetched. The last price can
        be on either side of the mark, so we widen it by the symbol's
        price-protection scope and pick the end that makes the stop harder to
        place: for a LONG the reference is pushed DOWN (the stop must clear a
        lower mark), for a SHORT it is pushed UP. A stop that clears this
        reference clears any mark inside the protection band.
        """
        scope = rules.price_protect_scope if rules.price_protect_scope > 0 else 0.02
        if side.upper() in ("LONG", "BUY"):
            return last * (1 - scope)
        return last * (1 + scope)

    # ---------------------------------------------------------- position mode
    POSITION_MODE_TTL = 300.0     # a successful read is good for 5 minutes
    POSITION_MODE_FAIL_TTL = 30.0  # don't hammer a failing endpoint per order

    async def account_position_mode(self) -> str:
        """The exchange's real position mode: 'HEDGE', 'ONE_WAY', or ''.

        `state.position_mode` is only what this bot *asked for* - a rejected
        `change_position_mode` leaves the two different, and getting it wrong
        changes the order body. So the exchange is the authority here, cached so
        that placing an order does not cost a REST round trip.

        '' means "unknown" (endpoint unreachable / unexpected payload) and the
        caller must keep sending the hedge-mode fields, i.e. today's behaviour.
        A failed read is cached for a shorter time so it recovers quickly.
        """
        now = time.time()
        ttl = (self.POSITION_MODE_TTL if self._position_mode
               else self.POSITION_MODE_FAIL_TTL)
        if self._position_mode_ts and (now - self._position_mode_ts) < ttl:
            return self._position_mode
        try:
            res = await self.client.get_position_mode()
            data = res[0] if isinstance(res, list) and res else res
            data = data if isinstance(data, dict) else {}
            mode = str(data.get("positionMode") or "").strip().upper()
            self._position_mode = mode if mode in ("HEDGE", "ONE_WAY") else ""
        except Exception:  # noqa: BLE001 - unknown mode must not block trading
            self._position_mode = ""
        self._position_mode_ts = now
        return self._position_mode

    def _remember_position_mode(self, mode: str) -> None:
        """Record a mode we just set, so the next order does not re-read it."""
        mode = (mode or "").strip().upper()
        if mode in ("HEDGE", "ONE_WAY"):
            self._position_mode = mode
            self._position_mode_ts = time.time()

    async def _trade_side_fields(self, hedge_side: str,
                                 position_id: str | None = None) -> dict:
        """tradeSide/positionId, but only where the docs define them.

        Official place_order: tradeSide is "Only required in hedge-mode", and
        positionId only when tradeSide is CLOSE. In one-way mode both are
        omitted; the position is identified by symbol + reduceOnly. The explicit
        `tradeSide: None` matters: build_order_body's own default is "OPEN", so
        merely not passing the keyword would still send it. Unknown mode keeps
        the fields (never silently change a live order path we cannot verify).
        """
        mode = await self.account_position_mode()
        if mode == "ONE_WAY":
            return {"tradeSide": None}
        fields: dict = {"tradeSide": hedge_side}
        if position_id is not None:
            fields["positionId"] = position_id
        return fields

    def persist_positions(self) -> None:
        """Mirror the in-memory position-management state to the store.

        Without this, a restart forgets `breakeven_done`, `trailing_stop` and
        `peak_roi`, so the engine would re-arm breakeven and re-tighten a
        trailing stop it had already tightened - acting on a trade it believed
        was fresh. The exchange is still the source of truth for the position
        itself; this is the management state on top of it.
        """
        body: dict[str, dict] = {}
        for pid, st in self.positions.items():
            body[pid] = {
                "position_id": st.position_id, "symbol": st.symbol,
                "side": st.side, "entry": st.entry, "qty": st.qty,
                "leverage": st.leverage, "sl": st.sl, "tp": st.tp,
                "liq": st.liq, "breakeven_done": st.breakeven_done,
                "trailing_active": st.trailing_active,
                "trailing_stop": st.trailing_stop,
                "opened_ts": st.opened_ts, "peak_roi": st.peak_roi,
            }
        if body == self.store.section("positions"):
            return
        self.store.set_section("positions", body)

    def _spawn(self, coro, name: str) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.append(t)

    async def engine_on(self) -> str:
        if self.state.engine_on:
            return "Engine already ON."
        self.state.engine_on = True
        self.state.scan_on = True
        await self.start()
        self._spawn(self._scan_loop(), "scanner")
        self._spawn(self._manage_loop(), "manager")
        self._spawn(self._report_loop(), "reporter")
        return "Engine ON (scanner + trader + management)."

    async def engine_off(self) -> str:
        self.state.engine_on = False
        self.state.autotrade = False
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        return "Engine OFF (all loops stopped, autotrade disabled)."

    # ================================================================ market data
    async def load_rules(self, symbols: list[str] | None = None) -> None:
        syms = symbols or self.state.symbols
        try:
            data = await self.client.get_trading_pairs(",".join(syms))
            for d in data or []:
                r = PairRules.from_api(d)
                self.rules[r.symbol] = r
        except BitunixAPIError as e:
            self.state.errors.append(f"load_rules: {e}")

    async def load_tiers(self, symbol: str) -> list[Tier]:
        if symbol in self.tiers:
            return self.tiers[symbol]
        try:
            data = await self.client.get_position_tiers(symbol)
            tiers = [Tier.from_api(d) for d in data or []]
            self.tiers[symbol] = tiers
            return tiers
        except BitunixAPIError as e:
            self.state.errors.append(f"load_tiers {symbol}: {e}")
            return []

    def kline_timeframes(self) -> list[str]:
        """Every interval the engine reads candles on.

        Derived from the configured timeframes, not a hardcoded list. The
        trailing-stop ATR reads 5m, so 5m is always included - otherwise that
        one read would poll REST forever while everything else used the cache.
        """
        tfs = list(self.state.timeframes)
        if "5m" not in tfs:
            tfs.append("5m")
        return tfs

    def kline_keys(self) -> list[tuple[str, str]]:
        """Every (symbol, interval) the engine actually needs."""
        out: list[tuple[str, str]] = []
        for sym in self._feed_symbols():
            for tf in self.kline_timeframes():
                if (sym, tf) not in out:
                    out.append((sym, tf))
        return out

    async def warmup_klines(self) -> int:
        """Seed the kline cache from REST once, then let WS keep it current.

        Returns the number of REST calls spent, so the banner can report it.
        """
        if self.s.market_data_source != "ws":
            return 0
        calls = 0
        for sym, tf in self.kline_keys():
            try:
                await self._rest_candles(sym, tf, REST_KLINE_MAX)
                calls += 1
            except BitunixAPIError as e:
                self.state.errors.append(f"kline warmup {sym} {tf}: {e}")
        self.kline_warmup_calls = calls
        return calls

    def kline_subscription_warnings(self) -> list[str]:
        """Intervals a strategy needs that the feed is not subscribed to.

        Said at startup instead of silently polling REST forever.
        """
        if self.s.market_data_source != "ws" or self.feed is None:
            return []
        subscribed = set(self.feed.timeframes)
        return [f"strategy interval {tf} is NOT subscribed on the WS feed "
                f"- it will poll REST"
                for tf in self.kline_timeframes() if tf not in subscribed]

    def kline_cache_report(self) -> str:
        """Kline cache state for /status and /ws."""
        c = self.kline_cache
        st = c.stats
        lines = [
            f"kline_cache=keys={len(c.keys())} hits={st.hits} misses={st.misses} "
            f"stale={st.stale_rejections} short={st.short_series} "
            f"rest_fallbacks={st.rest_fallbacks} ws_updates={st.ws_updates} "
            f"replaced={st.replaced} appended={st.appended} "
            f"rest_kline_calls={self.rest_kline_calls} warmup={self.kline_warmup_calls}"
        ]
        for sym, tf in c.keys():
            age = c.age(sym, tf)
            lines.append(
                f"  {sym} {tf}: {c.count(sym, tf)} candles src={c.source(sym, tf)} "
                f"newest_age={('%.0fs' % age) if age is not None else 'n/a'} "
                f"max={c.max_age(tf):.0f}s")
        return "\n".join(lines)

    async def get_candles(self, symbol: str, tf: str, limit: int = 300):
        """THE single candle accessor. Every candle read in the engine goes here.

        ws mode:  a fresh, long-enough, WS-fed series -> return it, zero REST.
                  stale / short / missing -> REST fallback, counted, warned.
        rest mode: straight to REST, exactly as before.
        """
        sym = (symbol or "").upper()
        want = min(limit, REST_KLINE_MAX)
        if self.s.market_data_source == "ws":
            res = self.kline_cache.try_get(sym, tf, want, min_required=want)
            if res.usable:
                return res.candles
            if res.reason:
                log.warning("kline cache miss for %s %s: %s - falling back to REST",
                            sym, tf, res.reason)
            self.kline_cache.note_rest_fallback()
        return await self._rest_candles(sym, tf, want)

    async def _rest_candles(self, symbol: str, tf: str, limit: int):
        self.rest_kline_calls += 1
        raw = await self.client.get_kline(symbol, tf, limit=limit)
        candles = to_candles(raw or [])
        if self.s.market_data_source == "ws" and candles:
            self.kline_cache.put_rest(symbol, tf, candles)
        return candles

    # ================================================================ scanning
    async def scan_symbol(self, symbol: str) -> Aggregate:
        if symbol in self._untradable:
            return Aggregate(symbol=symbol)
        per_tf: dict[str, list] = {}
        for tf in self.state.timeframes:
            try:
                candles = await self.get_candles(symbol, tf, 300)
            except BitunixAPIError as e:
                if e.code == 20015:
                    # The exchange says this futures pair cannot be traded.
                    # Retrying it every timeframe of every cycle filled the
                    # report with errors that were not failures - the symbol
                    # is simply not tradable. Learn it once and skip it.
                    self._untradable[symbol] = str(e)
                    self.state.errors.append(
                        f"skip {symbol}: not tradable ({e}) - removed from "
                        f"the scan")
                    return Aggregate(symbol=symbol)
                self.state.errors.append(f"kline {symbol} {tf}: {e}")
                continue
            if not candles:
                continue
            sigs = []
            for strat in self.strategies:
                try:
                    sig = strat.evaluate(candles, timeframe=tf, symbol=symbol)
                    sigs.append(sig)
                except Exception as e:  # noqa: BLE001
                    self.state.errors.append(f"{strat.name} {symbol} {tf}: {type(e).__name__}: {e}")
            per_tf[tf] = sigs

        agg = aggregate(
            symbol, per_tf,
            min_agreeing=self.state.min_agreeing,
            min_confidence=self.state.min_confidence,
            tf_min_confidence=self.state.tf_min_confidence,
        )
        self.last_aggregates[symbol] = agg
        self.state.scans += 1
        self.state.last_scan_ts = time.time()
        self._log_signal(agg)
        return agg

    def _log_signal(self, agg: Aggregate) -> None:
        rec = {
            "ts": time.time(), "symbol": agg.symbol, "direction": agg.direction,
            "confidence": round(agg.confidence, 2), "agreeing": agg.agreeing,
            "opposing": agg.opposing, "strength": agg.strength, "reason": agg.reason,
            "votes": [{"s": s.strategy, "tf": s.timeframe, "d": s.direction,
                       "c": round(s.confidence, 1)} for s in agg.votes_long + agg.votes_short],
        }
        with self.signals_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _universe_rank(self, sym: str, price: float, mark: float,
                       lastPrice: float | None, baseVol: float | None) -> str:
        """Classify a symbol for UNIVERSE_RANK (GAINER/LOSER/VOLUME/MOVER)."""
        if not sym or not lastPrice or not mark or not baseVol:
            return "NONE"
        if lastPrice > price:
            return "GAINER"
        if lastPrice < price:
            return "LOSER"
        if baseVol or float(baseVol) >= self.state.min_24h_volume_usdt:
            return "VOLUME"
        if baseVol and abs(baseVol - price) > 0:
            return "MOVER"
        return "NONE"

    async def _universe_symbols(self) -> list[str]:
        """Return up to universe_sizing top symbols ranked by UNIVERSE_RANK.

        Fetches every ticker from the public get_tickers endpoint, filters by
        MIN_24H_VOLUME_USD (USDT-denominated quoteVol), ranks by the chosen
        UNIVERSE_RANK method (GAINERS / LOSERS / VOLUME / MOVERS), and keeps
        the top UNIVERSE_SIZING symbols.
        """
        if not self.state.universe_rank or self.state.universe_sizing == 0:
            return []
        try:
            tickers = await self.client.get_tickers()
        except Exception:
            return []
        rows = tickers if isinstance(tickers, list) else []
        candidates = []
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            lastPrice = float(r.get("lastPrice") or 0)
            markPrice = float(r.get("markPrice") or 0)
            openPrice = float(r.get("open") or 0)
            baseVol = float(r.get("baseVol") or 0)
            quoteVol = float(r.get("quoteVol") or 0)
            candidates.append((sym, lastPrice, markPrice, openPrice, quoteVol, baseVol))

        min_vol = self.state.min_24h_volume_usdt
        filtered = [c for c in candidates if min_vol <= 0 or c[4] >= min_vol]
        method = (self.state.universe_rank or "").upper()

        if method == "GAINERS":
            ranked = [c for c in filtered if c[1] > c[3]]
            ranked.sort(key=lambda c: c[1] - c[3], reverse=True)
        elif method == "LOSERS":
            ranked = [c for c in filtered if c[1] < c[3]]
            ranked.sort(key=lambda c: c[3] - c[1], reverse=True)
        elif method == "VOLUME":
            ranked = sorted(filtered, key=lambda c: c[4], reverse=True)
        elif method == "MOVERS":
            ranked = sorted(filtered, key=lambda c: abs(c[1] - c[3]), reverse=True)
        else:
            ranked = []
        return [c[0] for c in ranked[: self.state.universe_sizing]]

    async def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.state.scan_on:
                    # SYMBOLS=AUTO: rank every tradable pair by UNIVERSE_RANK
                    # and scan the top UNIVERSE_SIZING instead of the static
                    # watchlist.
                    syms = list(self.state.symbols)
                    if self.state.universe_rank and self.state.universe_sizing:
                        auto = await self._universe_symbols()
                        if auto:
                            self._auto_symbols = auto
                            syms = auto
                        else:
                            self._auto_symbols = []
                    else:
                        self._auto_symbols = []
                    for sym in syms:
                        # per symbol: one broken symbol must not cost the
                        # others their scan in the same cycle
                        try:
                            agg = await self.scan_symbol(sym)
                            if self.state.autotrade and agg.actionable:
                                await self.try_enter(agg)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:  # noqa: BLE001
                            self.state.errors.append(
                                f"scan {sym}: {type(e).__name__}: {e}")
                            log.exception("scan error on %s", sym)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.state.errors.append(f"scan_loop: {type(e).__name__}: {e}")
                log.exception("scan loop error")
            await asyncio.sleep(max(5, self.state.scan_interval))

    # ================================================================ trading
    async def live_positions(self) -> list[dict] | None:
        """The exchange's open positions, or None when the read FAILED.

        `open_positions` cannot tell "no positions" from "the read failed" -
        it returns [] for both. That difference matters: reconciling the
        engine's position state against an empty list because the API call
        failed would throw away every live position's management state.
        """
        try:
            data = await self.client.get_pending_positions()
            return list(data or [])
        except BitunixAPIError as e:
            self.state.errors.append(f"open_positions: {e}")
            return None

    async def open_positions(self) -> list[dict]:
        return await self.live_positions() or []

    def reconcile_positions(self, live: list[dict] | None) -> list[str]:
        """Drop the engine's positions the exchange no longer has.

        The engine's `self.positions` was only ever ADDED to. A position that
        was stopped out, closed by a rung, or closed by the operator stayed in
        that dict forever: the report kept counting it, the state file kept
        growing, and the manage loop kept trying to move a stop on a position
        that no longer existed.

        The exchange is the source of truth for what is open. Anything the
        exchange does not report is gone, and the engine says so once.

        `live` is None when the exchange could not be read. That is NOT the
        same as "no positions": reconciling against a failed read would
        throw away every live position's management state.
        """
        if live is None:
            return []
        live_ids = {str(p.get("positionId") or "") for p in live}
        closed: list[str] = []
        for pid in list(self.positions):
            if pid in live_ids:
                continue
            st = self.positions.pop(pid)
            closed.append(pid)
            self._note_closed(st.symbol, "closed on the exchange")
            self._log_trade({"event": "closed_on_exchange", "ts": time.time(),
                              "symbol": st.symbol, "position_id": pid,
                              "side": st.side, "entry": st.entry, "qty": st.qty,
                              "reason": "not open on the exchange anymore"})
        if closed:
            self.persist_positions()
        return closed

    def _note_closed(self, symbol: str, why: str) -> None:
        """Remember that a symbol was just closed (anti-churn cooldown)."""
        if symbol:
            self._closed_at[symbol] = time.time()

    def _reentry_block(self, symbol: str) -> str:
        """The reason this symbol may not be re-entered yet, or ""."""
        wait = int(self.state.reentry_cooldown_sec or 0)
        if wait <= 0:
            return ""
        ts = self._closed_at.get(symbol)
        if not ts:
            return ""
        left = wait - (time.time() - ts)
        if left <= 0:
            return ""
        return (f"{symbol} was closed {int(left)}s ago - re-entry waits "
                f"{wait}s (reentry_cooldown_sec) so the same signal cannot "
                f"open and close it again in a loop")

    def _gate_timeframe(self) -> str:
        """The timeframe the trend gate watches.

        The configured gate TF when the scan set includes it; otherwise the
        BIGGEST timeframe the engine actually scans (the list is ordered
        ascending). Empty when there is nothing to watch.
        """
        tf = (self.state.trend_gate_timeframe or "").strip()
        if tf and tf in self.state.timeframes:
            return tf
        return self.state.timeframes[-1] if self.state.timeframes else ""

    def _trend_gate_blocks(self, agg: Aggregate) -> str | None:
        """Why the trend gate refuses this signal, or None to allow it.

        The gate is asymmetric by design: it does not demand agreement on
        the big TF (that would block most entries), it only refuses a
        signal the big TF is actively FIGHTING - a LONG with SHORT votes on
        the gate timeframe, or a SHORT with LONG votes there.
        """
        if not self.state.trend_gate:
            return None
        gate_tf = self._gate_timeframe()
        if not gate_tf:
            return None
        opposing = (getattr(agg, "votes_short", [])
                    if agg.direction == "LONG"
                    else getattr(agg, "votes_long", [])) or []
        fights = [v for v in opposing if getattr(v, "timeframe", "") == gate_tf]
        if not fights:
            return None
        strongest = max(fights, key=lambda v: v.confidence)
        against = "SHORT" if agg.direction == "LONG" else "LONG"
        return (f"trend gate: {agg.direction} refused - the {gate_tf} picture "
                f"fights it ({strongest.strategy} says {against} there, "
                f"conf {strongest.confidence:.0f})")

    async def try_enter(self, agg: Aggregate) -> dict:
        gate_refusal = self._trend_gate_blocks(agg)
        if gate_refusal:
            self.state.notes.append(gate_refusal)
            log.info("entry refused: %s", gate_refusal)
            return {"ok": False, "reason": gate_refusal}
        ctx, err = await self._entry_context(agg)
        if err is not None:
            return err
        assert ctx is not None
        sym, side, price, mark = ctx.sym, ctx.side, ctx.price, ctx.mark
        rules, equity, tiers = ctx.rules, ctx.equity, ctx.tiers

        err = self._resolve_leverage(ctx)
        if err is not None:
            return err
        err = self._size_position(ctx)
        if err is not None:
            return err
        err = self._apply_tier_cap(ctx)
        if err is not None:
            return err
        err = self._preflight_balance(ctx)
        if err is not None:
            return err
        plan = ctx.plan
        assert plan is not None

        await self._apply_leverage_and_margin_mode(ctx)

        order_side = "BUY" if side == "LONG" else "SELL"

        # The trigger type is read ONCE and used for both the local validation
        # below and the tpStopType/slStopType sent on the wire. Reading it twice
        # is how the two can drift apart.
        trigger_type = TRIGGER_TYPE

        # --- re-read the mark price immediately before sending and re-clamp.
        # The plan above was built from a mark read several round-trips ago
        # (account, tiers, margin mode, leverage). On a fast-moving symbol the
        # mark can cross the stop in that window, and the exchange validates
        # against the mark at the moment the request lands - not the one we
        # planned with. Re-clamping here is what makes the stop safe on the
        # wire rather than only at planning time.
        mark_now = await self.get_trigger_price(sym, last=price)
        if mark_now > 0:
            mark = ctx.mark = mark_now
        plan.sl = clamp_sl(side=side, sl=plan.sl, entry=price, mark=mark,
                           rules=rules, min_ticks=self.state.min_stop_ticks,
                           liq=plan.liq)
        plan.tp = clamp_tp(side=side, tp=plan.tp, entry=price, mark=mark,
                           rules=rules, min_ticks=self.state.min_stop_ticks)

        # --- which TP goes on the entry order.
        partial_method, rungs, tp_for_order = self._entry_exits(ctx)

        if not self._tpsl_side_ok(sym, side, price, mark, sl=plan.sl,
                                  tp=tp_for_order, rules=rules):
            return {"ok": False,
                    "reason": "TP/SL would be rejected by the exchange after the "
                              "mark price moved; order not sent"}

        body, err = await self._build_entry_body(ctx, order_side, plan,
                                                  tp_for_order, trigger_type)
        if err is not None:
            return err
        res, err = await self._send_entry_order(ctx, body)
        if err is not None:
            return err

        order_id = str(order_row(res).get("orderId") or "")
        self._log_trade({
            "event": "open", "ts": time.time(), "symbol": sym, "side": side,
            "qty": ctx.qty, "entry": price, "order_id": order_id,
            "confidence": round(ctx.agg.confidence, 2), "strength": ctx.agg.strength,
            "agreeing": ctx.agg.agreeing, "plan": plan.as_dict(),
        })
        self._log_tpsl({"event": "placed", "ts": time.time(), "symbol": sym,
                        "order_id": order_id, **plan.as_dict()})

        # register runtime state
        await self._register_filled_position(ctx, plan, partial_method, rungs,
                                             trigger_type)

        return {"ok": True, "order_id": order_id, "symbol": sym, "side": side,
                "qty": ctx.qty, "entry": price, "plan": plan.as_dict()}

    def _resolve_leverage(self, ctx: _EntryContext) -> dict | None:
        """The leverage THIS order may use, and the provisional TP/SL plan.

        The provisional plan is needed BEFORE the size: the stop distance is
        what the risk sizing divides by.
        """
        # --- leverage feasibility against the STOP, not just the balance.
        #
        # A stop must sit strictly between the liquidation price and the
        # mark-side limit. The liquidation price is derived from the ENTRY, and
        # a MARKET order fills at the last price - which can be on the far side
        # of the mark. At 100x with a 0.5% maintenance margin the liquidation
        # price lands ~0.5% below the entry, so when the mark already sits
        # 1.3% below the last price the liquidation price is ABOVE the mark and
        # there is no room for a stop at all. The engine then raised
        # "no valid stop-loss exists" on every scan, forever, for a symbol it
        # could have traded perfectly well at a lower leverage.
        #
        # The worst-case fill is the price that makes the liquidation price
        # closest to the mark: the HIGHER of last/mark for a LONG, the LOWER
        # for a SHORT. Solving the liquidation formula for L gives the highest
        # leverage at which a stop still fits.
        worst_entry = max(ctx.price, ctx.mark) if ctx.side == "LONG" else min(ctx.price, ctx.mark)

        # --- leverage for THIS order, resolved locally.
        #
        # `self.state.leverage` is the user's setting and is NEVER written here.
        # It used to be mutated in place when a stop did not fit, which meant a
        # single awkward symbol silently rewrote the leverage for every other
        # symbol and every later scan - and, worse, made a margin problem look
        # like it had been "fixed" by lowering leverage. Leverage and margin are
        # independent knobs: margin decides how much of the balance one position
        # locks, leverage only decides how much notional that margin controls.
        # The ONLY reasons to use a different leverage for one order are the two
        # the exchange itself imposes: no room for a stop, or a tier cap.
        ctx.leverage = int(self.state.leverage)
        # `notes` (routine) and `warnings` (exchange-forced leverage clamps)
        # are separate so a successful entry does not read as "errors: 1".
        ctx.notes = []
        ctx.warnings = []

        lev_tier = tier_for_leverage(ctx.tiers, ctx.leverage) or pick_tier(ctx.tiers, 0.0)
        max_lev_for_stop = max_leverage_for_stop(
            entry=worst_entry, mark=ctx.mark, side=ctx.side,
            maintenance_margin_rate=lev_tier.maintenance_margin_rate,
            rules=ctx.rules, min_ticks=self.state.min_stop_ticks)

        if max_lev_for_stop <= 0:
            msg = (f"{ctx.sym}: no stop-loss can fit at any leverage - the mark "
                   f"price {ctx.mark:.{ctx.rules.quote_precision}f} has already moved "
                   f"past the entry {ctx.price:.{ctx.rules.quote_precision}f} by more "
                   f"than the maintenance margin allows. Wait for the price to "
                   f"come back, or skip this signal.")
            self.state.errors.append(f"preflight {ctx.sym}: {msg}")
            return {"ok": False, "reason": msg}

        if ctx.leverage > max_lev_for_stop:
            err = self._auto_reduced_leverage(ctx, max_lev_for_stop)
            if err is not None:
                return err

        # The largest notional this balance can actually PAY FOR at the
        # configured leverage - margin PLUS the taker fee, with the safety
        # headroom. This is the true upper bound of the order.
        # (`equity * leverage` ignored the fee and over-stated the bound, which
        # also pushed the tier selection into a higher band than the real order.)
        max_notional = max_notional_for_balance(
            ctx.equity, ctx.leverage, self.s.taker_fee_rate,
            self.s.margin_safety_pct)

        # Provisional plan: only the stop-loss is needed here, to size the
        # order. It is built at a notional whose tier still permits the
        # configured leverage - at a high leverage the affordable maximum can
        # land in a tier whose maintenance-margin rate makes the liquidation
        # formula degenerate (liq on the wrong side of entry), which would
        # raise TpSlSideError for an order that is actually fine. The tier the
        # REAL order notional lands in is checked after sizing.
        prov_tier = tier_for_leverage(ctx.tiers, ctx.leverage)
        return self._provisional_plan(ctx, prov_tier, max_notional)

    def _auto_reduced_leverage(self, ctx: _EntryContext,
                               max_lev_for_stop: int) -> dict | None:
        """The leverage that still leaves room for a stop - or the reason not.

        Clamping to the highest leverage that still leaves room for a stop is
        the difference between "the bot never trades this symbol" and "the bot
        trades it at a sane leverage". The user's setting is never changed.
        """
        if self.s.leverage_auto_reduce:
            log.warning(
                "%s: leverage %dx leaves no room for a stop-loss between "
                "the liquidation price and the mark %s (entry %s) - "
                "using %dx for this order (the setting is unchanged)",
                ctx.sym, ctx.leverage, ctx.mark, ctx.price, max_lev_for_stop)
            ctx.warnings.append(
                f"leverage {ctx.sym}: {ctx.leverage}x leaves no room for a "
                f"stop-loss (mark {ctx.mark:.{ctx.rules.quote_precision}f}, entry "
                f"{ctx.price:.{ctx.rules.quote_precision}f}); using "
                f"{max_lev_for_stop}x for this order")
            ctx.leverage = max_lev_for_stop
            return None
        msg = (f"leverage {ctx.leverage}x leaves no room for a "
               f"stop-loss on {ctx.sym}: the stop must sit between the "
               f"liquidation price and the mark "
               f"{ctx.mark:.{ctx.rules.quote_precision}f} (entry "
               f"{ctx.price:.{ctx.rules.quote_precision}f}), and at "
               f"{ctx.leverage}x the liquidation price is already "
               f"past the mark. The highest leverage that still fits is "
               f"{max_lev_for_stop}x. Lower the leverage, or set "
               f"LEVERAGE_AUTO_REDUCE=true to let the engine clamp it.")
        self.state.errors.append(f"preflight {ctx.sym}: {msg}")
        return {"ok": False, "reason": msg}

    def _provisional_plan(self, ctx: _EntryContext, prov_tier: Any,
                           max_notional: float) -> dict | None:
        """The plan before the size is known: the stop the sizing divides by."""
        if prov_tier is None:
            # The exchange caps leverage below the setting at EVERY size. That
            # is the one documented reason to lower it, so lower it to the
            # exchange's own maximum and carry on - refusing here would leave
            # the user with a symbol that can never trade.
            max_lev = max((t.leverage for t in ctx.tiers), default=0)
            if max_lev <= 0:
                msg = (f"leverage {ctx.leverage}x is above the maximum allowed for "
                       f"{ctx.sym} at any position size, and the exchange reported "
                       f"no usable tier. Lower the leverage.")
                self.state.errors.append(f"preflight {ctx.sym}: {msg}")
                return {"ok": False, "reason": msg}
            ctx.warnings.append(
                f"leverage {ctx.sym}: {ctx.leverage}x is above the exchange maximum "
                f"{max_lev}x for this symbol; using {max_lev}x for this order")
            ctx.leverage = max_lev
            prov_tier = tier_for_leverage(ctx.tiers, ctx.leverage) or pick_tier(ctx.tiers, 0.0)

        prov_notional = max_notional
        if prov_tier.end_value != float("inf") and prov_tier.end_value > 0:
            prov_notional = min(max_notional, prov_tier.end_value * 0.9999)

        try:
            ctx.plan = plan_tpsl(
                side=ctx.side, entry=ctx.price, leverage=ctx.leverage,
                notional=prov_notional,
                strength=ctx.agg.strength, confidence=ctx.agg.confidence, tiers=ctx.tiers, rules=ctx.rules,
                liq_sl_distance_pct=self.state.liq_sl_distance_pct,
                breakeven_pct=self.state.breakeven_pct,
                trailing_trigger_roi_pct=self.state.trailing_trigger_roi_pct,
                mark=ctx.mark, min_stop_ticks=self.state.min_stop_ticks,
            )
        except TpSlSideError as e:
            self.state.errors.append(f"tpsl {ctx.sym}: {e}")
            return {"ok": False, "reason": f"cannot place a valid stop-loss: {e}"}

        ctx.qty = 0.0
        return None

    def _size_position(self, ctx: _EntryContext) -> dict | None:
        """The size of the order, in base coin."""
        # --- sizing.
        #
        # MARGIN_AMOUNT_PCT is the SIZING AUTHORITY when it is set: "use this
        # share of my balance as margin on this position". It used to be passed
        # to position_size_by_risk as an upper bound, and because the risk-based
        # size was almost always the smaller of the two the cap never bound - so
        # the margin actually locked was whatever risk sizing produced (a 10%
        # setting produced ~2% of the balance) and the setting looked like it
        # did nothing. Sizing FROM the percentage is what makes the number the
        # user set the number they see on the position.
        #
        # risk_pct is the fallback for when the knob is 0 (disabled), not a
        # silent override of it. The risk implied at the stop is reported so the
        # user can see what the margin target actually costs them.
        if self.state.margin_amount_pct > 0:
            ctx.qty = position_size_by_margin(
                ctx.equity, self.state.margin_amount_pct, ctx.price, ctx.leverage, ctx.rules,
                fee_rate=self.s.taker_fee_rate,
                safety_pct=self.s.margin_safety_pct)
            if ctx.qty > 0 and ctx.plan.sl > 0:
                implied_risk = abs(ctx.price - ctx.plan.sl) * ctx.qty
                ctx.notes.append(
                    f"size {ctx.sym}: MARGIN_AMOUNT_PCT={self.state.margin_amount_pct:g}% "
                    f"-> qty {ctx.qty} (notional {ctx.qty * ctx.price:.4f} USDT, margin "
                    f"{margin_required(ctx.qty * ctx.price, ctx.leverage):.4f} USDT at "
                    f"{ctx.leverage}x); risk at the stop is {implied_risk:.4f} USDT "
                    f"({implied_risk / ctx.equity * 100:.2f}% of balance, "
                    f"risk_pct={self.state.risk_pct:g}% is not applied while "
                    f"the margin target is set)")
        else:
            ctx.qty = position_size_by_risk(
                ctx.equity, self.state.risk_pct, ctx.price, ctx.plan.sl, ctx.leverage, ctx.rules,
                fee_rate=self.s.taker_fee_rate,
                safety_pct=self.s.margin_safety_pct)
        if ctx.qty <= 0:
            return {"ok": False, "reason": "computed qty is 0 (risk too small or below min volume)"}

        # ORDER_UNIT only changes how the size is EXPRESSED; the wire format is
        # always base-coin qty. QUANTITY (the default) is a no-op, so the
        # historical behaviour is unchanged. NOTIONAL/COST re-derive qty from
        # the risk-sized notional/margin so the two can never disagree.
        order_unit = (self.s.order_unit or "QUANTITY").upper()
        if order_unit != "QUANTITY":
            sized_notional = ctx.qty * ctx.price
            amount = unit_from_qty(ctx.qty, order_unit, ctx.price, ctx.leverage)
            ctx.qty = qty_from_unit(amount, order_unit, ctx.price, ctx.leverage, ctx.rules)
            if ctx.qty <= 0:
                return {"ok": False,
                        "reason": f"computed qty is 0 in {order_unit} units "
                                  f"(amount={amount}, notional={sized_notional})"}

        return None

    def _apply_tier_cap(self, ctx: _EntryContext) -> dict | None:
        """The exchange tier cap: lower the leverage, re-size, re-plan."""
        # --- pre-flight: the required margin must fit the real available
        # balance, and the engine's own bounds. Without this the request is
        # sent and the exchange answers 20003 Insufficient balance.
        ctx.notional = ctx.qty * ctx.price

        # The tier the REAL order notional lands in caps the leverage. Checked
        # against the actual notional, not `equity * leverage` (which lands in a
        # higher tier and produced a bogus 20003 for an order that was fine).
        #
        # When the tier does cap it, the exchange's own maximum is the answer:
        # lower the leverage to that cap, re-size and re-plan, and say so. The
        # loop is bounded because each pass can only lower the leverage, and a
        # lower leverage at a fixed margin budget means a smaller notional,
        # which lands in a tier that permits at least as much leverage.
        for _ in range(3):
            tier_msg = tier_leverage_violation(ctx.tiers, ctx.notional, ctx.leverage)
            if not tier_msg:
                break
            cap = pick_tier(ctx.tiers, ctx.notional).leverage
            if cap <= 0 or cap >= ctx.leverage:
                msg = f"{ctx.sym}: {tier_msg}"
                self.state.errors.append(f"preflight {ctx.sym}: {msg}")
                return {"ok": False, "reason": msg, "hint": "20003 Insufficient balance"}
            ctx.warnings.append(
                f"leverage {ctx.sym}: {ctx.leverage}x exceeds the exchange tier cap "
                f"{cap}x for a {ctx.notional:.2f} USDT position; using {cap}x "
                f"for this order")
            ctx.leverage = cap
            if self.state.margin_amount_pct > 0:
                ctx.qty = position_size_by_margin(
                    ctx.equity, self.state.margin_amount_pct, ctx.price, ctx.leverage, ctx.rules,
                    fee_rate=self.s.taker_fee_rate,
                    safety_pct=self.s.margin_safety_pct)
            else:
                ctx.qty = position_size_by_risk(
                    ctx.equity, self.state.risk_pct, ctx.price, ctx.plan.sl, ctx.leverage, ctx.rules,
                    fee_rate=self.s.taker_fee_rate,
                    safety_pct=self.s.margin_safety_pct)
            if ctx.qty <= 0:
                return {"ok": False,
                        "reason": "computed qty is 0 after the exchange tier "
                                  "capped the leverage"}
            ctx.notional = ctx.qty * ctx.price
            try:
                ctx.plan = plan_tpsl(
                    side=ctx.side, entry=ctx.price, leverage=ctx.leverage,
                    notional=ctx.notional, strength=ctx.agg.strength,
                    confidence=ctx.agg.confidence, tiers=ctx.tiers, rules=ctx.rules,
                    liq_sl_distance_pct=self.state.liq_sl_distance_pct,
                    breakeven_pct=self.state.breakeven_pct,
                    trailing_trigger_roi_pct=self.state.trailing_trigger_roi_pct,
                    mark=ctx.mark, min_stop_ticks=self.state.min_stop_ticks,
                )
            except TpSlSideError as e:
                self.state.errors.append(f"tpsl {ctx.sym}: {e}")
                return {"ok": False, "reason": f"cannot place a valid stop-loss: {e}"}
        else:
            msg = (f"{ctx.sym}: the exchange tier cap could not be satisfied at any "
                   f"leverage for a {ctx.notional:.2f} USDT position.")
            self.state.errors.append(f"preflight {ctx.sym}: {msg}")
            return {"ok": False, "reason": msg, "hint": "20003 Insufficient balance"}

        return None

    def _preflight_balance(self, ctx: _EntryContext) -> dict | None:
        """The margin, the balance and the engine's own bounds."""
        # A tier/leverage CLAMP is a safety decision, not a routine note: it
        # goes to `errors` so it cannot be hidden. The routine margin-budget
        # sizing note goes to `notes` instead - it used to be reported as a
        # failure, which buried the real errors behind noise AND made a
        # successful entry read as "errors: 1" in the report.
        for note in ctx.warnings:
            self.state.errors.append(note)
        for note in ctx.notes:
            self.state.notes.append(note)

        margin_real = margin_required(ctx.notional, ctx.leverage)
        # Opening a MARKET order costs the initial margin AND a taker fee, and
        # the exchange deducts BOTH from the available balance at once. Checking
        # only the margin let `margin == equity` pass locally and then fail on
        # the exchange with 20003 once the fee was added.
        fee_real = fee_for_notional(ctx.notional, self.s.taker_fee_rate)
        required_real = margin_real + fee_real
        # Reserve the configured headroom so a price move between sizing and the
        # request landing cannot push the cost back over the balance.
        usable_equity = ctx.equity * (1.0 - max(0.0, min(0.9, self.s.margin_safety_pct / 100.0)))

        if required_real > usable_equity:
            msg = (f"insufficient balance for {ctx.sym}: required {required_real:.4f} USDT "
                   f"(margin {margin_real:.4f} + fee {fee_real:.4f}) vs available "
                   f"{ctx.equity:.4f} USDT"
                   + (f" (usable {usable_equity:.4f} after "
                      f"MARGIN_SAFETY_PCT={self.s.margin_safety_pct:g}%)"
                      if self.s.margin_safety_pct else "")
                   + f" for qty={ctx.qty}, notional={ctx.notional:.4f} at "
                     f"{ctx.leverage}x. Lower the leverage, lower risk_pct, "
                     f"or add funds.")
            self.state.errors.append(f"preflight {ctx.sym}: {msg}")
            return {"ok": False, "reason": msg, "hint": "20003 Insufficient balance"}
        # MARGIN_AMOUNT_PCT: the margin this ONE order locks may not exceed the
        # configured share of the spendable balance. Sizing already targets it,
        # so this only fires when something downstream (an ORDER_UNIT
        # re-derivation, a min-volume floor) pushed the size back up. The
        # correct response is to CLAMP the size to the budget and say so - not
        # to refuse, and never to touch the leverage, which has nothing to do
        # with how much of the balance one position may lock.
        err = self._clamp_to_margin_budget(ctx, margin_real)
        if err is not None:
            return err
        margin_real = margin_required(ctx.notional, ctx.leverage)
        fee_real = fee_for_notional(ctx.notional, self.s.taker_fee_rate)
        required_real = margin_real + fee_real
        err = self._check_engine_bounds(ctx, margin_real)
        if err is not None:
            return err

        # re-plan with the real notional
        try:
            ctx.plan = plan_tpsl(
                side=ctx.side, entry=ctx.price, leverage=ctx.leverage,
                notional=ctx.qty * ctx.price, strength=ctx.agg.strength, confidence=ctx.agg.confidence,
                tiers=ctx.tiers, rules=ctx.rules,
                liq_sl_distance_pct=self.state.liq_sl_distance_pct,
                breakeven_pct=self.state.breakeven_pct,
                trailing_trigger_roi_pct=self.state.trailing_trigger_roi_pct,
                mark=ctx.mark, min_stop_ticks=self.state.min_stop_ticks,
            )
        except TpSlSideError as e:
            self.state.errors.append(f"tpsl {ctx.sym}: {e}")
            return {"ok": False, "reason": f"cannot place a valid stop-loss: {e}"}

        return None

    def _clamp_to_margin_budget(self, ctx: _EntryContext,
                                margin_real: float) -> dict | None:
        """Keep the margin this ONE order locks inside its budget.

        MARGIN_AMOUNT_PCT: the margin this ONE order locks may not exceed the
        configured share of the spendable balance. Sizing already targets it,
        so this only fires when something downstream (an ORDER_UNIT
        re-derivation, a min-volume floor) pushed the size back up. The
        correct response is to CLAMP the size to the budget and say so - not
        to refuse, and never to touch the leverage, which has nothing to do
        with how much of the balance one position may lock.
        """
        if self.state.margin_amount_pct <= 0:
            return None
        budget = margin_budget(ctx.equity, self.state.margin_amount_pct,
                               self.s.margin_safety_pct)
        if margin_real <= budget:
            return None
        max_notional_budget = max_notional_for_margin(
            budget, ctx.leverage, self.s.taker_fee_rate)
        clamped = round_step(max_notional_budget / ctx.price, ctx.rules.base_precision)
        if clamped < ctx.rules.min_trade_volume:
            msg = (f"margin {margin_real:.4f} USDT for {ctx.sym} exceeds the "
                   f"MARGIN_AMOUNT_PCT={self.state.margin_amount_pct:g}% "
                   f"budget of {budget:.4f} USDT, and clamping to the "
                   f"budget leaves {clamped} below the symbol's minimum "
                   f"volume {ctx.rules.min_trade_volume}. Raise "
                   f"MARGIN_AMOUNT_PCT or add funds.")
            self.state.errors.append(f"preflight {ctx.sym}: {msg}")
            return {"ok": False, "reason": msg}
        self.state.notes.append(
            f"margin {ctx.sym}: qty {ctx.qty} would lock {margin_real:.4f} USDT, "
            f"above the MARGIN_AMOUNT_PCT={self.state.margin_amount_pct:g}% "
            f"budget of {budget:.4f} USDT; clamped to qty {clamped} "
            f"(notional {clamped * ctx.price:.4f} USDT). Leverage is "
            f"unchanged at {ctx.leverage}x.")
        ctx.qty = clamped
        ctx.notional = ctx.qty * ctx.price
        return None

    def _check_engine_bounds(self, ctx: _EntryContext,
                             margin_real: float) -> dict | None:
        """The engine's own margin and notional bounds (MIN/MAX_*_USDT)."""
        margin_real = margin_real
        if self.s.min_margin_usdt and margin_real < self.s.min_margin_usdt:
            msg = (f"required margin {margin_real:.4f} USDT is below the configured "
                   f"MIN_MARGIN_USDT={self.s.min_margin_usdt} for {ctx.sym} "
                   f"(qty={ctx.qty}, notional={ctx.notional:.4f}).")
            self.state.errors.append(f"preflight {ctx.sym}: {msg}")
            return {"ok": False, "reason": msg}
        if self.s.max_margin_usdt and margin_real > self.s.max_margin_usdt:
            msg = (f"required margin {margin_real:.4f} USDT exceeds the configured "
                   f"MAX_MARGIN_USDT={self.s.max_margin_usdt} for {ctx.sym} "
                   f"(qty={ctx.qty}, notional={ctx.notional:.4f}).")
            self.state.errors.append(f"preflight {ctx.sym}: {msg}")
            return {"ok": False, "reason": msg}
        if self.s.max_notional_usdt and ctx.notional > self.s.max_notional_usdt:
            msg = (f"notional {ctx.notional:.4f} USDT exceeds the configured "
                   f"MAX_NOTIONAL_USDT={self.s.max_notional_usdt} for {ctx.sym} "
                   f"(qty={ctx.qty}, margin={margin_real:.4f}).")
            self.state.errors.append(f"preflight {ctx.sym}: {msg}")
            return {"ok": False, "reason": msg}
        return None

    async def _apply_leverage_and_margin_mode(self, ctx: _EntryContext) -> None:
        """Make the exchange's leverage and margin mode match this order."""
        try:
            await self.client.change_margin_mode(ctx.sym, self.state.margin_mode)
        except BitunixAPIError as e:
            if e.code not in (20006, 20009):
                self.state.errors.append(f"margin_mode {ctx.sym}: {e}")
        try:
            await self.client.change_leverage(ctx.sym, ctx.leverage)
        except BitunixAPIError as e:
            if e.code not in (20006,):
                self.state.errors.append(f"leverage {ctx.sym}: {e}")
            return
        await self._note_leverage_result(ctx)

    async def _note_leverage_result(self, ctx: _EntryContext) -> None:
        """What the exchange actually stored after the leverage write.

        The official write (`change_leverage`) is symbol-wide - it has no
        side parameter - but in hedge mode the exchange keeps LONG and
        SHORT leverage separately (`get_trading_settings` returns
        `longLeverage` and `shortLeverage`, and the UI's "Sync Long/Short"
        toggle keeps them equal). Read the symbol back after the write so
        the entry notes show whether both sides took the value.
        """
        try:
            data = await self.client.get_trading_settings(ctx.sym)
        except Exception:  # noqa: BLE001 - a note must never break an entry
            return
        rows = data if isinstance(data, list) else ([data] if data else [])
        row = next((r for r in rows if isinstance(r, dict)
                    and str(r.get("symbol", "")).upper() == ctx.sym.upper()),
                   None)
        if row is None:
            return
        long_lev = row.get("longLeverage")
        short_lev = row.get("shortLeverage")
        if long_lev is None and short_lev is None:
            return
        verdict = ("synced" if long_lev == short_lev
                   else "NOT synced - the UI's Sync Long/Short will be off")
        ctx.notes.append(
            f"leverage applied {ctx.sym}: the exchange now reports "
            f"long {long_lev}x / short {short_lev}x ({verdict})")

    async def _build_entry_body(self, ctx: _EntryContext, order_side: str,
                                plan: Any, tp_for_order: float | None,
                                trigger_type: str) -> tuple[dict | None, dict | None]:
        """The exact body that goes on the wire, after local validation."""
        try:
            body = build_order_body(
                symbol=ctx.sym, qty=ctx.qty, side=order_side,
                orderType="MARKET", reduceOnly=False,
                tpPrice=tp_for_order, tpStopType=trigger_type, tpOrderType="MARKET",
                slPrice=plan.sl, slStopType=trigger_type, slOrderType="MARKET",
                rules=ctx.rules, known_symbols=set(self.rules) or None,
                mark_price=ctx.mark, entry_price=ctx.price,
                min_stop_ticks=self.state.min_stop_ticks,
                **await self._trade_side_fields("OPEN"),
            )
        except OrderValidationError as e:
            self.state.errors.append(f"order validation {ctx.sym}: {e}")
            return None, {"ok": False, "reason": f"order validation failed: {e}"}
        return body, None

    async def _send_entry_order(self, ctx: _EntryContext,
                                 body: dict) -> tuple[Any, dict | None]:
        """Send the entry order."""
        try:
            res = await self.client.place_order(**body)
        except BitunixAPIError as e:
            log_order_body(body, e)
            self.state.errors.append(f"place_order {ctx.sym}: {e}")
            return None, {"ok": False, "reason": f"place_order failed: {e}",
                          "hint": e.hint}
        return res, None

    def _entry_exits(self, ctx: _EntryContext) -> tuple[bool, list, float | None]:
        """Which take-profit rides on the entry order, and the rungs that own it.

        With the PARTIAL method the rungs ARE the take-profit plan: each rung
        becomes its own native tpsl/place_order carrying tpQty (parked right
        after the fill, see `_place_partial_rungs`). Attaching the whole-position
        TP here would flatten the entire remainder the moment it is touched, so
        with PARTIAL the entry order carries the STOP only.
        """
        plan = ctx.plan
        partial_method = (self.state.tpsl_method or "POSITION").upper() == "PARTIAL"
        rungs: list = []
        if partial_method:
            try:
                rungs = parse_partial_ladder(self.state.partial_tp_ladder)
            except PartialLadderError as e:
                plan.notes = (plan.notes + "; " if plan.notes else "") + (
                    f"PARTIAL ladder invalid ({e}); whole-position TP attached")
                log.warning("partial ladder %r invalid: %s - using the "
                            "whole-position TP", self.state.partial_tp_ladder, e)
                partial_method = False
            else:
                if not rungs:
                    partial_method = False
                else:
                    plan.notes = (plan.notes + "; " if plan.notes else "") + (
                        "PARTIAL ladder: no whole-position TP attached at entry "
                        f"({len(rungs)} rungs own the exits)")
        tp_for_order = None if partial_method else plan.tp
        return partial_method, rungs, tp_for_order

    async def _register_filled_position(self, ctx: _EntryContext, plan: Any,
                                         partial_method: bool, rungs: list,
                                         trigger_type: str) -> None:
        """Record the filled position, and park the PARTIAL rungs on it."""
        sym = ctx.sym
        positions = await self.open_positions()
        for p in positions:
            if p.get("symbol") != sym:
                continue
            pid = str(p.get("positionId"))
            fill_entry = float(p.get("avgOpenPrice") or ctx.price)
            fill_qty = float(p.get("qty") or ctx.qty)
            self.positions[pid] = PositionState(
                position_id=pid, symbol=sym, side=ctx.side,
                entry=fill_entry,
                qty=fill_qty,
                leverage=int(p.get("leverage") or self.state.leverage),
                sl=plan.sl, tp=(0.0 if partial_method else plan.tp),
                liq=float(p.get("liqPrice") or plan.liq),
                opened_ts=time.time(),
            )
            # With the PARTIAL method the rungs are parked at entry as
            # native exchange-enforced TP orders (tpQty each), so the
            # exits survive even if this engine dies.
            if partial_method:
                await self._place_partial_rungs(
                    sym, pid, ctx.side, fill_entry, fill_qty,
                    sl_dist=abs(fill_entry - plan.sl),
                    rungs=rungs, rules=ctx.rules, trigger_type=trigger_type)
            break
        self.persist_positions()

    async def _entry_context(self, agg: Aggregate) -> tuple[_EntryContext | None, dict | None]:
        """The guards every entry needs, and the numbers it starts from.

        Returns (context, None) when the entry may proceed, or (None, error)
        with the reason it may not.
        """
        sym = agg.symbol
        if not agg.actionable:
            return None, {"ok": False, "reason": "not actionable"}

        # anti-churn: a symbol closed a moment ago is not re-entered
        cool = self._reentry_block(sym)
        if cool:
            return None, {"ok": False, "reason": cool}

        live = await self.live_positions()
        positions = live or []
        self.reconcile_positions(live)
        if len(positions) >= self.state.max_open_positions:
            return None, {"ok": False, "reason": f"max open positions ({self.state.max_open_positions}) reached"}
        if any(p.get("symbol") == sym for p in positions):
            return None, {"ok": False, "reason": f"position already open on {sym}"}

        if sym not in self.rules:
            await self.load_rules([sym])
        rules = self.rules.get(sym)
        if rules is None:
            return None, {"ok": False, "reason": f"no trading rules for {sym}"}
        if not rules.api_supported or rules.symbol_status != "OPEN":
            return None, {"ok": False, "reason": f"{sym} not tradable (status={rules.symbol_status})"}

        price = await self.get_price(sym)
        if price <= 0:
            return None, {"ok": False, "reason": f"no price for {sym}"}

        # Direction is resolved here because the mark-price fallback below
        # needs it. Using it before assignment raised UnboundLocalError on the
        # no-mark path.
        side = "LONG" if agg.direction == "LONG" else "SHORT"

        # The exchange validates TP/SL against the MARK price, not the last
        # price. Fetch it so the plan is built on the same reference the
        # exchange will use. If it is unavailable we do NOT silently fall back
        # to the last price: the last price can sit on the wrong side of the
        # mark, which is exactly how a stop passes locally and is rejected with
        # 30029. Instead the fallback is widened by the price-protection scope
        # so the stop clears a mark that could be anywhere inside that band.
        mark = await self.get_mark_price(sym)
        if mark <= 0:
            mark = self._conservative_mark(price, side, rules)
            log.warning("no mark price for %s - using a conservative reference "
                        "%s (last price %s widened by the protection scope)",
                        sym, mark, price)

        # account balance: the SPENDABLE amount, not total equity.
        # `available` already excludes the margin locked by open positions and
        # the frozen amount locked by resting orders, so it is what a new order
        # may actually spend. Sizing against total equity (available + margin +
        # unrealized) is what made the engine believe it could afford an order
        # the exchange then refused with 20003 Insufficient balance.
        try:
            acct = await self.client.get_single_account()
            a = acct[0] if isinstance(acct, list) and acct else (acct or {})
            equity = spendable_balance(a)
        except BitunixAPIError as e:
            return None, {"ok": False, "reason": f"account: {e}"}
        if equity <= 0:
            return None, {"ok": False, "reason": "no available balance"}

        tiers = await self.load_tiers(sym)
        ctx = _EntryContext(sym=sym, side=side, price=price, mark=mark,
                            equity=equity, rules=rules, tiers=tiers, agg=agg)
        return ctx, None

    async def close_position(self, symbol: str | None = None,
                             position_id: str | None = None,
                             reason: str = "user") -> dict:
        """Close a position, and RECORD WHY.

        The close log used to carry only the symbol and the result, so a log
        could not tell a reversal close from an account-threshold close from
        the operator's own command. "Why did it open and close several
        positions in a minute?" was unanswerable from the evidence - the
        reason is the one thing that separates them.
        """
        try:
            if position_id:
                res = await self.client.flash_close_position(position_id)
            else:
                res = await self.client.close_all_position(symbol)
            self._note_closed(symbol or "", reason)
            self._log_trade({"event": "close", "ts": time.time(),
                             "symbol": symbol, "position_id": position_id,
                             "reason": reason, "result": res})
            if position_id:
                st = self.positions.pop(str(position_id), None)
            self.persist_positions()
            return {"ok": True, "reason": reason, "result": res}
        except BitunixAPIError as e:
            self._log_trade({"event": "close_failed", "ts": time.time(),
                             "symbol": symbol, "position_id": position_id,
                             "reason": reason, "error": str(e)})
            return {"ok": False, "reason": str(e), "hint": e.hint}

    # ================================================================ management
    def _plan_rungs(self, rungs: list[LadderRung], qty: float,
                    rules: PairRules,
                    skipped: list[dict]) -> list[tuple[LadderRung, float, float]]:
        """The rung allocation: which rung closes how many units, at which R.

        A rung smaller than the exchange's minimum order volume cannot exist
        as its own order; it is merged into the next one. The old code merged
        as it went and DROPPED the remainder: with a 29-unit position and
        minTradeVolume 10, the rungs were 11 (40%), 10 (35%) and 7 (25%) - the
        last one was too small to place and there was no next rung to merge it
        into, so 8 units of the position were left with a stop and NO
        take-profit at all. The plan is computed first so a remainder can be
        folded into the last placeable rung instead of vanishing.
        """
        min_qty = float(rules.min_trade_volume or 0)
        plan: list[tuple[LadderRung, float, float]] = []   # (rung, share, qty)
        allocated = 0.0
        carry_share = 0.0
        for rung in rungs:
            share = rung.share + carry_share
            carry_share = 0.0
            step_qty = round_step(qty * share / 100.0, rules.base_precision)
            if min_qty > 0 and step_qty < min_qty:
                skipped.append({"share": share, "r": rung.r,
                                "reason": "below minTradeVolume - merged into "
                                          "the next rung"})
                carry_share = share
                continue
            if allocated + step_qty > qty:
                room = round_step(qty - allocated, rules.base_precision)
                if min_qty > 0 and room < min_qty:
                    skipped.append({"share": share, "r": rung.r,
                                    "reason": "would over-allocate the position"})
                    carry_share = share
                    continue
                step_qty = room
            plan.append((rung, share, step_qty))
            allocated += step_qty

        if carry_share > 0:
            # the last rung could not stand on its own: fold it into the last
            # rung that CAN, so every unit of the position keeps a take-profit
            if plan:
                rung, share, step_qty = plan[-1]
                extra = round_step(qty * carry_share / 100.0, rules.base_precision)
                merged = round_step(step_qty + extra, rules.base_precision)
                if merged > step_qty:
                    skipped.append({"share": carry_share, "r": rung.r,
                                    "reason": "below minTradeVolume - merged "
                                              "into the previous rung"})
                    plan[-1] = (rung, share + carry_share, min(merged, qty))
                else:
                    skipped.append({"share": carry_share, "r": rung.r,
                                    "reason": "below minTradeVolume - no rung "
                                              "left to merge into"})
            else:
                skipped.append({"share": carry_share, "r": rungs[-1].r,
                                "reason": "below minTradeVolume - the position "
                                          "is too small to split"})
        return plan

    async def _place_partial_rungs(self, sym: str, pid: str, side: str,
                                   entry: float, qty: float, *,
                                   sl_dist: float, rungs: list[LadderRung],
                                   rules: PairRules, trigger_type: str) -> None:
        """PARTIAL TP/SL (method 2 of 4): park each rung as a NATIVE order.

        Every rung becomes its own `/tpsl/place_order` with `tpQty` at height
        `r × sl_dist` from the entry, placed the moment the position exists.
        The exchange then enforces the whole scale-out plan on its own - the
        engine does not fire rungs, re-park them, or need a backstop order,
        because every exit lives server-side.

        A rung smaller than the exchange's minimum order volume cannot exist
        as its own order; it is MERGED into the next rung (fewer prices, same
        shares). If the position is too small to split at all, ONE
        whole-position TP at the outermost rung is attached through the
        position endpoint instead - a plan is preserved, not nothing.
        """
        if not rungs or qty <= 0 or sl_dist <= 0:
            return
        is_long = side.upper() in ("LONG", "BUY")
        placed = []
        skipped = []
        plan = self._plan_rungs(rungs, qty, rules, skipped)
        allocated = sum(step for _r, _s, step in plan)

        for rung, share, step_qty in plan:
            target = rung_target(side, entry, sl_dist, rung.r)
            try:
                res = await self.client.place_tp_sl_order(
                    sym, pid,
                    tp_price=format_price(target, rules),
                    tp_qty=format_qty(step_qty, rules),
                    tp_order_type="MARKET", tp_stop_type=trigger_type)
                placed.append({"share": share, "r": rung.r, "qty": step_qty,
                               "price": target,
                               "order_id": str(order_row(res).get("orderId") or "")})
            except (BitunixAPIError, ValueError) as e:
                skipped.append({"share": share, "r": rung.r, "reason": str(e)})
            except Exception as e:  # noqa: BLE001
                # A rung must NEVER take the scan loop down. The exchange
                # answered the first rung and the engine still died on the
                # response shape ('list' object has no attribute 'get'),
                # which left ONE take-profit on the exchange where the ladder
                # wanted three. A rung that cannot be read is skipped, the
                # rest of the ladder still goes out, and the reason is on
                # record.
                skipped.append({"share": share, "r": rung.r,
                                "reason": f"{type(e).__name__}: {e}"})

        runner_qty = max(0.0, qty - allocated)
        self._log_tpsl({
            "event": "partial_rungs_placed", "ts": time.time(),
            "symbol": sym, "position_id": pid, "side": side,
            "entry": entry, "sl_dist": round(sl_dist, 12),
            "placed": placed, "skipped": skipped,
            "runner_qty": round(runner_qty, 12),
        })
        if placed:
            return

        # Nothing could be placed (position too small to split): fall back to
        # ONE whole-position TP at the outermost rung so the trade is not left
        # with no take-profit at all.
        outer = max(r.r for r in rungs)
        fallback = rung_target(side, entry, sl_dist, outer)
        st = self.positions.get(pid)
        if st is not None:
            st.tp = fallback
        try:
            await self.client.modify_position_tp_sl_order(
                sym, pid, tp_price=format_price(fallback, rules),
                sl_price=format_price(st.sl, rules) if st and st.sl else None,
                tp_stop_type=trigger_type, sl_stop_type=trigger_type)
            self._log_tpsl({"event": "partial_rungs_fallback_tp", "ts": time.time(),
                            "symbol": sym, "position_id": pid,
                            "tp": fallback, "r": outer})
        except (BitunixAPIError, ValueError) as e:
            self.state.errors.append(f"partial fallback TP {sym}: {e}")

    async def close_all_positions(self, reason: str = "user") -> dict:
        """Close every open position - the ACCOUNT TP/SL action.

        Uses the documented `close_all_position` endpoint (market close of all
        positions). Falls back to per-position flash close if the bulk call
        fails, so a single API hiccup cannot leave the account half-flattened.

        `reason` says WHY the close happened (account_tp / account_sl / user)
        and is written to the trade log - the log is the only record of a
        close, so the reason has to be in it.
        """
        try:
            await self.client.close_all_position()
            for st in list(self.positions.values()):
                self._note_closed(st.symbol, reason)
                self._log_trade({"event": "close", "ts": time.time(),
                                 "symbol": st.symbol, "position_id": st.position_id,
                                 "reason": reason, "via": "close_all_position"})
            self.positions.clear()
            self.persist_positions()
            return {"ok": True, "via": "close_all_position", "reason": reason}
        except BitunixAPIError as e:
            log.warning("close_all_position failed (%s) - falling back to "
                        "per-position close", e)
        closed, failed = 0, 0
        for p in await self.open_positions():
            pid = str(p.get("positionId") or "")
            if not pid:
                continue
            try:
                await self.client.flash_close_position(pid)
                closed += 1
                self._note_closed(str(p.get("symbol") or ""), reason)
                self._log_trade({"event": "close", "ts": time.time(),
                                 "symbol": p.get("symbol"), "position_id": pid,
                                 "reason": reason, "via": "flash_close_position"})
            except BitunixAPIError:
                failed += 1
        self.positions.clear()
        self.persist_positions()
        return {"ok": failed == 0, "via": "flash_close_position",
                "reason": reason, "closed": closed, "failed": failed}

    def tpsl_method_report(self) -> str:
        """One honest line about the four documented TP/SL methods.

        The four are the ones the official academy article defines: Position,
        Partial, Trailing and Account TP/SL. The entry method picks the native
        exit shape (Position = one whole-position TP/SL; Partial = the
        share@R rungs, each its own native tpQty order at entry). Trailing and
        Account are software and always armed - they only run while the manage
        loop is up, which this text says out loud.
        """
        m = (self.state.tpsl_method or "POSITION").upper()
        native = (
            f"TP/SL method: PARTIAL (native rungs at entry: "
            f"{self.state.partial_tp_ladder!r}; exchange-enforced)"
            if m == "PARTIAL" else
            "TP/SL method: POSITION (native, one TP/SL closes the whole "
            "position; exchange-enforced)")
        return (
            f"{native}\n"
            f"trailing: breakeven at {self.state.breakeven_pct:g}% ROI, "
            f"ratchet from {self.state.trailing_trigger_roi_pct:g}% ROI "
            f"(software; needs the manage loop)\n"
            f"account TP/SL: {self.account_cfg().describe()} "
            f"(software; needs the manage loop)")

    def account_cfg(self) -> "AccountTpSlConfig":
        """The ACCOUNT TP/SL thresholds from the live state."""
        return AccountTpSlConfig(tp_usdt=self.state.account_tp_usdt,
                                 sl_usdt=self.state.account_sl_usdt)

    async def _manage_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.state.mid_management:
                    await self.manage_positions()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.state.errors.append(f"manage_loop: {type(e).__name__}: {e}")
            await asyncio.sleep(max(5, self.state.scan_interval))

    def _tpsl_side_ok(self, sym: str, side: str, entry: float, mark: float, *,
                      sl: float | None, tp: float | None,
                      rules: PairRules,
                      sl_profit_side: bool = False) -> bool:
        """Last gate before a managed TP/SL is sent.

        `clamp_sl`/`clamp_tp` already put the prices on the correct side; this
        re-checks the exact values that will go on the wire and refuses to send
        a request the exchange would reject with 30020-30033. A refusal is
        recorded as an error so it is visible in /status instead of silently
        dropping the update.
        """
        violations = validate_tpsl_side(
            symbol=sym, side=side, entry=entry, mark=mark, sl=sl, tp=tp,
            rules=rules, min_ticks=self.state.min_stop_ticks,
            sl_profit_side=sl_profit_side)
        if violations:
            self.state.errors.append(
                "manage TP/SL refused locally: " + "; ".join(violations))
            return False
        return True

    async def manage_positions(self) -> list[dict]:
        actions: list[dict] = []
        positions = await self.open_positions()
        live_ids = {str(p.get("positionId")) for p in positions}

        for pid in list(self.positions):
            if pid not in live_ids:
                # the position vanished (stop filled, manual close, ...) and
                # its management state is obsolete
                self.positions.pop(pid, None)

        # --- ACCOUNT TP/SL: the whole futures account is the control unit.
        closed_all = await self._manage_account_tpsl(positions)
        if closed_all is not None:
            actions.append(closed_all)
            return actions

        for p in positions:
            actions.extend(await self._manage_one_position(p))

        # Management state (breakeven_done / trailing_stop / peak_roi) must hit
        # the disk even if the process is killed a moment later.
        self.persist_positions()
        self.state.manage_last_ok = time.time()
        return actions

    def _manage_state_line(self) -> str:
        """Is the loop that moves the stops actually alive - answered plainly.

        The report used to say "breakeven armed, trailing armed - both always
        on" while the loop that does the moving sat dead (or mid_management
        was off): peak_roi frozen at 17% while live ROI was 31%, stops never
        ratcheted. "Armed" means nothing if the machine is not running; this
        line says whether it is running, and what to type when it is not.
        """
        s = self.state
        if not s.mid_management:
            return ("management loop: OFF (mid management off) - your "
                    "breakeven/trailing will NOT move. "
                    "Turn it on: mid management on")
        if not s.manage_last_ok:
            return ("management loop: NOT RUNNING yet - your exits are not "
                    "being moved. Start the engine: engine on")
        age = time.time() - s.manage_last_ok
        if age > max(60, s.scan_interval * 4):
            return (f"management loop: STALLED {age:.0f}s ago - your exits "
                    f"may not be moving. Restart: engine off, then engine on")
        return f"management loop: running (last cycle {age:.0f}s ago)"

    async def _manage_account_tpsl(self, positions: list[dict]) -> dict | None:
        """ACCOUNT TP/SL: the whole futures account is the control unit.

        When TOTAL unrealized PnL crosses the configured threshold, every
        position closes. Bitunix exposes no API for this, so it is enforced
        here in software - which means it only fires while this loop is
        running. That limitation is stated in /status, not hidden.

        Returns the action that was taken, or None when nothing fired.
        """
        acct_cfg = self.account_cfg()
        if not acct_cfg.enabled or not positions:
            return None
        total_pnl = total_unrealized_pnl(positions)
        equity = 0.0
        try:
            # Total account equity, NOT just `available`: `available` already
            # excludes the margin locked by open positions, so using it alone
            # makes an x%-of-equity threshold fire far too early.
            equity = account_equity(await self.client.get_single_account())
        except (BitunixAPIError, TypeError, ValueError):
            equity = 0.0
        decision = account_tpsl_decision(total_pnl=total_pnl, equity=equity,
                                         cfg=acct_cfg)
        if not decision.should_close_all:
            return None
        log.warning("ACCOUNT TP/SL: %s", decision.reason)
        closed = await self.close_all_positions(reason=f"account_{decision.trigger}")
        self._log_tpsl({"event": f"account_{decision.trigger}",
                        "ts": time.time(), "total_pnl": round(total_pnl, 2),
                        "threshold": decision.threshold,
                        "reason": decision.reason})
        self.persist_positions()
        return {"action": f"account_{decision.trigger}",
                "total_pnl": round(total_pnl, 2),
                "threshold": decision.threshold,
                "reason": decision.reason, "result": closed}

    def _reconcile_qty(self, st: PositionState, p: dict, sym: str, pid: str,
                       rules: PairRules) -> None:
        """Reconcile the size with the exchange before acting on it.

        Something outside this list can shrink a position between two passes:
        the stop filling, a native rung TP triggering, a manual close. Acting
        on a stale qty would send a modify/re-close bigger than the position
        (rejected) or keep managing dust that is already gone. The exchange is
        the source of truth for SIZE.
        """
        live_qty = abs(float(p.get("qty") or 0))
        if live_qty <= 0 or st.qty <= 0:
            return
        man_prec = max(0, int(rules.base_precision))
        if (round(live_qty, man_prec) == round(st.qty, man_prec)
                or math.isclose(live_qty, st.qty, rel_tol=1e-6)):
            return
        self._log_tpsl({"event": "qty_reconciled", "ts": time.time(),
                        "symbol": sym, "position_id": pid,
                        "engine_qty": st.qty, "exchange_qty": live_qty})
        st.qty = live_qty

    async def _apply_breakeven(self, st: PositionState, sym: str, pid: str,
                               side: str, entry: float, mark: float, *,
                               rules: PairRules, roi: float,
                               trigger_type: str) -> dict | None:
        """Move the stop to the entry once the ROI passes the threshold."""
        if not should_breakeven(st, roi, self.state.breakeven_pct):
            return None
        sl = clamp_sl(side=side, sl=entry, entry=entry, mark=mark,
                      rules=rules, min_ticks=self.state.min_stop_ticks,
                      liq=st.liq or None)
        if not self._tpsl_side_ok(sym, side, entry, mark, sl=sl, tp=st.tp, rules=rules):
            return None
        try:
            await self.client.modify_position_tp_sl_order(
                sym, pid, tp_price=format_price(st.tp, rules) if st.tp else None,
                sl_price=format_price(sl, rules),
                tp_stop_type=trigger_type, sl_stop_type=trigger_type)
        except BitunixAPIError as e:
            self.state.errors.append(f"breakeven {sym}: {e}")
            return None
        st.breakeven_done = True
        st.sl = sl
        self._log_tpsl({"event": "breakeven", "ts": time.time(), "symbol": sym,
                        "position_id": pid, "sl": sl, "roi": round(roi, 2)})
        return {"action": "breakeven", "symbol": sym,
                "sl": sl, "roi": round(roi, 2)}

    async def _apply_trailing(self, st: PositionState, sym: str, pid: str,
                              side: str, entry: float, mark: float, *,
                              rules: PairRules, roi: float, price: float,
                              trigger_type: str) -> dict | None:
        """Ratchet the stop behind the price once the ROI passes the trigger."""
        if not should_trail(st, roi, self.state.trailing_trigger_roi_pct):
            return None
        try:
            candles = await self.get_candles(sym, "5m", 60)
            a = atr(candles, 14)
            atr_val = a[-1] if a and a[-1] else price * 0.002
        except Exception:  # noqa: BLE001
            atr_val = price * 0.002
        new_stop = next_trailing_stop(st, price, atr_val)
        if new_stop is None:
            return None
        # The trailing ratchet only guards against loosening vs the PREVIOUS
        # trailing stop - its first candidate (price - 1.5*ATR) can fall well
        # BELOW the entry, and writing it would UNDO the breakeven protection
        # the position already earned (observed live: be=moved with the stop
        # put back 3.5% under the entry). Once the breakeven has fired, the
        # floor is exactly the stop the breakeven path writes (the entry
        # minus the exchange's minimum tick distance) - never looser.
        if st.breakeven_done:
            be_floor = clamp_sl(side=side, sl=entry, entry=entry, mark=mark,
                                rules=rules,
                                min_ticks=self.state.min_stop_ticks,
                                liq=st.liq or None)
            if be_floor is not None:
                if side.upper() in ("LONG", "BUY"):
                    new_stop = max(new_stop, be_floor)
                else:
                    new_stop = min(new_stop, be_floor)
        sl = clamp_sl(side=side, sl=new_stop, entry=entry, mark=mark,
                      rules=rules, min_ticks=self.state.min_stop_ticks,
                      liq=st.liq or None, profit_side=True)
        if not self._tpsl_side_ok(sym, side, entry, mark, sl=sl, tp=st.tp,
                                  rules=rules, sl_profit_side=True):
            return None
        try:
            await self.client.modify_position_tp_sl_order(
                sym, pid, tp_price=format_price(st.tp, rules) if st.tp else None,
                sl_price=format_price(sl, rules),
                tp_stop_type=trigger_type, sl_stop_type=trigger_type)
        except BitunixAPIError as e:
            self.state.errors.append(f"trail {sym}: {e}")
            return None
        st.trailing_stop = sl
        st.trailing_active = True
        st.sl = sl
        self._log_tpsl({"event": "trail", "ts": time.time(), "symbol": sym,
                        "position_id": pid, "sl": sl, "roi": round(roi, 2)})
        return {"action": "trail", "symbol": sym,
                "new_sl": sl, "roi": round(roi, 2)}

    async def _manage_one_position(self, p: dict) -> list[dict]:
        """Breakeven, trailing and the reversal close for ONE position."""
        actions: list[dict] = []
        if True:
            pid = str(p.get("positionId"))
            sym = p.get("symbol", "")
            side = p.get("side", "LONG")
            entry = float(p.get("avgOpenPrice") or 0)
            if entry <= 0:
                return actions
            price = await self.get_price(sym)
            if price <= 0:
                return actions
            lev = int(p.get("leverage") or self.state.leverage)
            roi = roi_pct(entry, price, side, lev)

            st = self.positions.get(pid)
            if st is None:
                st = PositionState(
                    position_id=pid, symbol=sym, side=side, entry=entry,
                    qty=float(p.get("qty") or 0), leverage=lev,
                    sl=0.0, tp=0.0, liq=float(p.get("liqPrice") or 0),
                    opened_ts=time.time(),
                )
                self.positions[pid] = st
            st.peak_roi = max(st.peak_roi, roi)

            # The exchange validates every TP/SL trigger against the MARK price
            # at the moment the request lands, so the mark must be read fresh
            # here - not reused from entry time, and never assumed to equal the
            # last price. Without this the management path sent stops that were
            # only ever checked against the entry price (30029/30031).
            mark = await self.get_trigger_price(sym, last=price)
            if mark <= 0:
                mark = price
                log.warning("no trigger price for %s - validating the managed "
                            "TP/SL against the last price %s", sym, price)
            # Cache the mark for the briefs: the exchange settles uPnL against
            # it, so it is the only price a ROI can honestly be quoted from.
            self._last_marks[sym] = mark
            trigger_type = TRIGGER_TYPE
            rules = self.rules.get(sym)
            if rules is None:
                await self.load_rules([sym])
                rules = self.rules.get(sym)
            if rules is None:
                self.state.errors.append(
                    f"manage {sym}: no trading rules, skipping TP/SL update")
                return actions

            self._reconcile_qty(st, p, sym, pid, rules)

            # 1) breakeven - always armed: move SL to entry once ROI passes
            # the threshold, with no ladder gating (the ladder machinery is
            # gone; TRAILING and ACCOUNT are also always armed below).
            action = await self._apply_breakeven(
                st, sym, pid, side, entry, mark, rules=rules, roi=roi,
                trigger_type=trigger_type)
            if action:
                actions.append(action)

            # 2) trailing
            action = await self._apply_trailing(
                st, sym, pid, side, entry, mark, rules=rules, roi=roi, price=price,
                trigger_type=trigger_type)
            if action:
                actions.append(action)

            # 3) mid-management
            agg = self.last_aggregates.get(sym)
            reversal = bool(agg and agg.actionable and agg.direction != side)
            rev_conf = agg.confidence if agg else 0.0
            action = mid_management_action(
                st, roi, reversal and self.state.reversal, rev_conf,
                self.state.reversal_confidence)
            if action == "close_reversal":
                res = await self.close_position(symbol=sym, position_id=pid,
                                                reason="reversal")
                actions.append({"action": "close_reversal", "symbol": sym,
                                "roi": round(roi, 2), "result": res})
        return actions

    # ================================================================ reporting
    #: Where the periodic report goes. The engine is a LIBRARY - it does not
    #: print. The CLI installs `print` so the operator sees the report in the
    #: terminal; the Telegram bot pushes the same text to the chat; with no
    #: sink the report is only logged.
    report_sink: Callable[[str], None] | None = None

    async def _report_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.state.report_on:
                    rep = await self.report()
                    if self.report_sink is not None:
                        self.report_sink(rep)
                    else:
                        log.info("%s", rep)
                    self.state.last_report_ts = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.state.errors.append(f"report_loop: {type(e).__name__}: {e}")
            await asyncio.sleep(max(5, self.state.report_interval))

    def exits_brief(self) -> str:
        """The exit state of every position, answered plainly.

        The operator asked "why don't you do the breakeven & trailing" and
        got a page of METHOD REFERENCE - which is a documentation answer to a
        question about their position. This is the answer to the question:
        what is armed, on which position, and has it fired yet.
        """
        s = self.state
        lines = [
            "=== the exits, right now ===",
            f"armed: breakeven at +{s.breakeven_pct:g}% ROI, "
            f"trailing from +{s.trailing_trigger_roi_pct:g}% ROI, "
            f"liq SL distance {s.liq_sl_distance_pct:g}%",
            self._manage_state_line(),
        ]
        if not self.positions:
            lines.append("no open position - nothing is being managed")
            return "\n".join(lines)
        for st in self.positions.values():
            last = self._cached_price(st.symbol)
            # Quote ROI from the MARK price: the exchange settles uPnL against
            # it and the report's ROI is derived from that uPnL. A last-price
            # ROI here would let the agent state a number the report
            # contradicts (live: 25.37% vs 22.90%).
            mark = self._last_marks.get(st.symbol) or last
            roi = (roi_pct(st.entry, mark, st.side, st.leverage)
                   if mark > 0 else 0.0)
            if st.breakeven_done:
                be = f"MOVED to the entry {st.entry:g}"
            elif s.mid_management:
                be = f"waiting (needs +{s.breakeven_pct:g}% ROI)"
            else:
                be = "OFF (mid management is off)"
            if st.trailing_active:
                trail = (f"ACTIVE at {st.trailing_stop:g}"
                         if st.trailing_stop else "ACTIVE")
            elif s.mid_management:
                trail = f"waiting (needs +{s.trailing_trigger_roi_pct:g}% ROI)"
            else:
                trail = "OFF (mid management is off)"
            lines.append(
                f"{st.symbol} {st.side} qty={st.qty:g} entry={st.entry:g} | "
                f"now {last or '?'} mark {mark:g} | ROI {roi:+.2f}% | "
                f"SL {st.sl:g} TP {st.tp:g} liq {st.liq:g}")
            lines.append(f"  breakeven: {be}")
            lines.append(f"  trailing:  {trail}"
                         + (f" (peak ROI {st.peak_roi:.2f}%)" if st.peak_roi else ""))
        return "\n".join(lines)

    def live_brief(self) -> str:
        """The engine's live state in one compact block - WHAT THE AGENT SEES.

        Without this the model answers blind. Asked "why don't you do
        breakeven and trailing" it had no idea whether they were armed, on
        which position, at what ROI - so it fell back to quoting command
        syntax, which is exactly what makes an agent feel like a bot. An
        agent that knows the operator's live position can answer the question
        in one sentence, in their language, with their numbers.

        Sync on purpose: it is built from what the engine already tracks
        (state, positions, the last scan's signals, the price cache), so it
        costs no API call and cannot fail on a flaky exchange. Anything it
        does not know is left out rather than guessed.
        """
        s = self.state
        lines: list[str] = []
        lines.append(
            f"ENGINE: engine_on={s.engine_on} scan_on={s.scan_on} "
            f"autotrade={s.autotrade} mid_management={s.mid_management} "
            f"| symbols: {','.join(s.symbols) or 'none'}")

        sizing = (f"margin target {s.margin_amount_pct:g}% of balance "
                  f"(risk_pct {s.risk_pct:g}% NOT applied)"
                  if s.margin_amount_pct > 0
                  else f"risk {s.risk_pct:g}% of equity per trade")
        lines.append(
            f"SETTINGS: leverage {s.leverage}x | margin mode {s.margin_mode} | "
            f"{sizing} | breakeven at +{s.breakeven_pct:g}% ROI | "
            f"trailing from +{s.trailing_trigger_roi_pct:g}% ROI | "
            f"liq SL distance {s.liq_sl_distance_pct:g}% | "
            f"max {s.max_open_positions} position(s) | "
            f"re-entry cooldown {s.reentry_cooldown_sec}s | "
            f"TP/SL {s.tpsl_method}"
            + (f" (ladder {s.partial_tp_ladder})" if s.partial_tp_ladder else ""))

        # --- the positions, with the exit state that decides the answer
        if self.positions:
            lines.append(f"POSITIONS ({len(self.positions)}):")
            for st in self.positions.values():
                last = self._cached_price(st.symbol)
                # MARK price, for the same reason as exits_brief: the report
                # settles ROI from the exchange's uPnL, and a last-price ROI
                # here would contradict it.
                mark = self._last_marks.get(st.symbol) or last
                roi = (roi_pct(st.entry, mark, st.side, st.leverage)
                       if mark > 0 else 0.0)
                exits = []
                if st.breakeven_done:
                    exits.append(f"breakeven ALREADY MOVED to entry {st.entry}")
                else:
                    exits.append("breakeven not reached yet")
                if st.trailing_active:
                    exits.append(f"trailing ACTIVE at {st.trailing_stop}")
                elif st.peak_roi:
                    exits.append(f"trailing not active (peak ROI {st.peak_roi:.2f}%)")
                else:
                    exits.append("trailing not active")
                lines.append(
                    f"- {st.symbol} {st.side} qty {st.qty:g} entry {st.entry} | "
                    f"now {last or '?'} mark {mark:g} | ROI {roi:+.2f}% | "
                    f"SL {st.sl} TP {st.tp} liq {st.liq} | " + " | ".join(exits))
        else:
            lines.append("POSITIONS: none open")

        # --- the last scan's signals
        if self.last_aggregates:
            sigs = []
            for sym, agg in self.last_aggregates.items():
                sigs.append(f"{sym} {agg.direction} {agg.confidence:.1f}% "
                            f"({agg.agreeing} agree)")
            lines.append("LAST SCAN SIGNALS: " + "; ".join(sigs))
        else:
            lines.append("LAST SCAN SIGNALS: none yet (no scan has completed)")

        # --- what went wrong, so the agent can explain it instead of guessing
        if s.errors:
            last = s.errors[-1]
            lines.append(f"PROBLEMS ({len(s.errors)}): last: {last}")
        return "\n".join(lines)

    def _cached_price(self, symbol: str) -> float:
        """The last known price, without an API call."""
        try:
            q = self.price_cache.peek(symbol)
        except Exception:  # noqa: BLE001 - the brief must never raise
            return 0.0
        return float(q.price) if q is not None else 0.0

    async def report(self) -> str:
        lines = [f"=== J+Rock report {time.strftime('%H:%M:%S')} ==="]
        for sym in self.state.symbols:
            try:
                price = await self.get_price(sym)
            except Exception:  # noqa: BLE001
                price = 0.0
            agg = self.last_aggregates.get(sym)
            sig = (f"{agg.direction} {agg.confidence:.1f}% ({agg.agreeing} agree)"
                   if agg else "no scan yet")
            lines.append(f"{sym}: price {price} | signal {sig}")
        live = await self.live_positions()
        positions = live or []
        self.reconcile_positions(live)
        live_ids = {str(p.get("positionId") or "") for p in positions}
        if positions:
            lines.append("-- open positions --")
            for p in positions:
                sym = p.get("symbol")
                entry = float(p.get("avgOpenPrice") or 0)
                price = await self.get_price(sym)
                # ROI is derived from the exchange's OWN unrealizedPNL and
                # margin, so roi and upnl on this line are the same number seen
                # two ways. roi_pct() reads the LAST price while the exchange
                # settles uPnL against the MARK price, so the two disagreed
                # whenever the spread was non-zero - observed live as
                # "roi=25.37% upnl=0.0478065" on margin 0.2088, which is
                # 22.89%. A line the operator cannot audit is a line that
                # reads as a lie. The margin is printed so it can be checked.
                upnl = _num(p.get("unrealizedPNL"))
                margin = _num(p.get("margin"))
                roi = (upnl / margin * 100.0) if margin > 0 else roi_pct(
                    entry, price, p.get("side", "LONG"),
                    int(p.get("leverage") or 1))
                lines.append(
                    f"  {sym} {p.get('side')} qty={p.get('qty')} entry={entry} "
                    f"px={price} roi={roi:.2f}% upnl={p.get('unrealizedPNL')} "
                    f"margin={p.get('margin')} liq={p.get('liqPrice')}")
        else:
            lines.append("no open positions")
        lines.extend(await self._report_tpsl(live_ids))
        if self.state.errors:
            lines.append(f"errors: {len(self.state.errors)} (last: {self.state.errors[-1]})")
        return "\n".join(lines)

    async def _report_tpsl(self, live_ids: set[str] | None = None) -> list[str]:
        """The TP/SL state of the account, in the report.

        The report used to stop at the signal and the position line, so an
        operator could not see whether the exits were actually armed: the
        method, the ladder, the stop, the target, the trailing state and the
        account thresholds were all invisible outside `/tpsl`. This is the
        same information, and it is what the Telegram report push carries.

        `live_ids` are the position ids the exchange still reports. Anything
        else is GONE: a rung parked for a closed position must not be
        reported as if it were still working, or the report counts a dead
        position forever.
        """
        m = (self.state.tpsl_method or "POSITION").upper()
        if m == "PARTIAL":
            method = (f"PARTIAL - native rungs at entry "
                      f"({self.state.partial_tp_ladder or 'no ladder - whole-position TP'})")
        else:
            method = "POSITION - one native TP/SL closes the whole position"
        lines = ["-- TP/SL --",
                 f"method: {method}",
                 f"software: breakeven armed at +{self.state.breakeven_pct:g}% ROI, "
                 f"trailing armed from +{self.state.trailing_trigger_roi_pct:g}% ROI "
                 f"(both always on - they fire when the position's ROI passes the "
                 f"threshold) | {self.account_cfg().describe()}",
                 self._manage_state_line()]

        # one line per managed position: what is armed, and by which method
        for st in self.positions.values():
            if live_ids is not None and st.position_id not in live_ids:
                continue
            tp = f"{st.tp:g}" if st.tp else ("rungs" if m == "PARTIAL" else "none")
            # "off" used to mean "the breakeven/trailing move has not fired
            # yet" - which reads as "the feature is disabled". These are
            # ALWAYS armed; they wait for the ROI threshold.
            if st.breakeven_done:
                be = "moved"
            elif self.state.mid_management:
                be = f"waiting(+{self.state.breakeven_pct:g}%)"
            else:
                be = "OFF(mid management off)"
            if st.trailing_active:
                trail = f"on@{st.trailing_stop:g}" if st.trailing_stop else "on"
            elif self.state.mid_management:
                trail = f"waiting(+{self.state.trailing_trigger_roi_pct:g}%)"
            else:
                trail = "OFF(mid management off)"
            lines.append(
                f"  {st.symbol} {st.side} qty={st.qty:g} entry={st.entry:g} "
                f"sl={st.sl:g} tp={tp} liq={st.liq:g} "
                f"be={be} trail={trail}"
                + (f" peak_roi={st.peak_roi:.2f}%" if st.peak_roi else ""))

        # the rungs that are actually parked on the exchange, from the log -
        # but only for a position that is STILL open
        for rec in reversed(self.tpsl_history(20)):
            if rec.get("event") != "partial_rungs_placed":
                continue
            placed = rec.get("placed") or []
            if not placed:
                continue
            pid = str(rec.get("position_id") or "")
            if live_ids is not None and pid not in live_ids:
                continue          # the position is gone - the rungs are history
            rungs = ", ".join(
                f"{p.get('share')}%@{p.get('r')}R={p.get('price')}"
                for p in placed)
            lines.append(f"  parked rungs ({rec.get('symbol')}): {rungs}")
            break
        lines.extend(self._report_closes())
        if self.state.notes:
            lines.append(f"notes: {len(self.state.notes)} "
                         f"(last: {self.state.notes[-1]})")
        return lines

    def _report_closes(self) -> list[str]:
        """The last few closes, with the reason each one happened.

        A position that opens and closes inside a minute is a fee burn, and
        the operator cannot see why from a report that only shows what is open
        NOW. Every close carries its reason in the trade log
        (`reversal` / `account_tp` / `account_sl` / `user`), so the report
        shows it instead of leaving the question to guesswork.
        """
        closes = [r for r in self.trades_history(50) if r.get("event") == "close"]
        if not closes:
            return []
        lines = ["-- recent closes --"]
        for rec in closes[-3:]:
            ts = time.strftime("%H:%M:%S", time.localtime(rec.get("ts") or 0))
            lines.append(f"  {ts} {rec.get('symbol') or '-'} "
                         f"reason={rec.get('reason') or 'unknown'}")
        return lines

    async def pnl(self) -> dict:
        positions = await self.open_positions()
        total = 0.0
        rows = []
        for p in positions:
            try:
                u = float(p.get("unrealizedPNL") or 0)
            except (TypeError, ValueError):
                u = 0.0
            total += u
            rows.append({"symbol": p.get("symbol"), "side": p.get("side"),
                         "unrealized": u, "realized": p.get("realizedPNL")})
        return {"unrealized_total": round(total, 4), "positions": rows}

    # ================================================================ history
    def _log_trade(self, rec: dict) -> None:
        with self.trades_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _log_tpsl(self, rec: dict) -> None:
        with self.tpsl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _read_jsonl(self, path: Path, n: int = 50) -> list[dict]:
        if not path.exists():
            return []
        out = []
        for ln in path.read_text(encoding="utf-8").splitlines()[-n:]:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def trades_history(self, n: int = 20) -> list[dict]:
        return self._read_jsonl(self.trades_path, n)

    def tpsl_history(self, n: int = 20) -> list[dict]:
        return self._read_jsonl(self.tpsl_path, n)

    async def positions_history(self, n: int = 20) -> list[dict]:
        try:
            data = await self.client.get_history_positions(skip=0, limit=n)
        except BitunixAPIError as e:
            self.state.errors.append(f"positions_history: {e}")
            return []
        # The official response is {"data": {"positionList": [...],
        # "total": N}} and request() returns the inner object verbatim.
        if isinstance(data, dict):
            return data.get("positionList") or []
        return data or []

    async def render_history(self, n: int = 12) -> str:
        """One readable view of everything that already happened.

        Three sources: closed positions from the exchange
        (get_history_positions), TP/SL events and trades from the local
        jsonl logs. The raw JSON is still available with the exact
        "positions history" / "trades history" / "tpsl history" commands.
        """
        lines = ["=== history ==="]
        try:
            closed = await self.positions_history(n)
        except Exception as e:  # noqa: BLE001 - a dead API must not hide the local logs
            closed = []
            lines.append(f"-- closed positions (exchange) -- API error: {e}")
        else:
            err = next((e for e in reversed(self.state.errors)
                        if e.startswith("positions_history: ")), None)
            lines.append("-- closed positions (exchange) --"
                         + (f" API error: {err.split(': ', 1)[1]}" if err else ""))
            for p in closed:
                lines.append(
                    f"{p.get('symbol')} {p.get('side')} "
                    f"{p.get('entryPrice')} -> {p.get('closePrice')} "
                    f"realized={p.get('realizedPNL')} fee={p.get('fee')} "
                    f"funding={p.get('funding')} {p.get('leverage')}x "
                    f"closed {_fmt_ms_ts(p.get('mtime'))}")
            if not closed:
                lines.append("(no closed positions on the exchange yet)")
        tpsl = self.tpsl_history(n)
        lines.append("-- TP/SL events (local log) --")
        for rec in tpsl[-n:]:
            lines.append(self._history_line(rec))
        if not tpsl:
            lines.append("(none logged yet)")
        trades = self.trades_history(n)
        lines.append("-- trades (local log) --")
        for rec in trades[-n:]:
            lines.append(self._history_line(rec))
        if not trades:
            lines.append("(none logged yet)")
        lines.append("raw JSON: positions history / trades history / tpsl history")
        return "\n".join(lines)

    @staticmethod
    def _history_line(rec: dict) -> str:
        ts = rec.get("ts")
        when = _fmt_ms_ts(ts * 1000 if isinstance(ts, (int, float)) else None)
        parts = [when, str(rec.get("event", "?"))]
        for k, v in rec.items():
            if k in ("ts", "event", "plan", "confidence", "strength",
                     "agreeing", "order_id", "position_id"):
                continue
            if isinstance(v, (str, int, float, bool)) and v != "":
                parts.append(f"{k}={v}")
        return " ".join(parts)

    # ================================================================ settings
    def store_line(self) -> str:
        """One honest line about persistence for `/status`."""
        sm = self.store.summary()
        last = (time.strftime("%H:%M:%S", time.localtime(sm["last_save"]))
                if sm["last_save"] else "never")
        extra = ""
        if sm["migrated_from"]:
            extra += f" migrated_from=v{sm['migrated_from']}"
        if sm["recovered_backup"]:
            extra += f" CORRUPT_BACKUP={Path(sm['recovered_backup']).name}"
        return (f"store: {sm['path']} schema=v{sm['version']} "
                f"settings={sm['settings'] + sm['engine_settings']} "
                f"positions={sm['positions']} "
                f"size={sm['size_bytes']}B last_save={last}{extra}")

    def settings_report(self) -> str:
        s = self.state
        return "\n".join([
            "=== engine settings ===",
            f"engine_on={s.engine_on} scan_on={s.scan_on} autotrade={s.autotrade} "
            f"report_on={s.report_on} mid_management={s.mid_management}",
            f"symbols={','.join(s.symbols)} leverage={s.leverage}x "
            f"margin={s.margin_mode} position={s.position_mode} max_open={s.max_open_positions}",
            f"risk_pct={s.risk_pct}% margin_amount_pct={s.margin_amount_pct:g}% "
            f"breakeven_pct={s.breakeven_pct}% "
            f"trailing_trigger_roi={s.trailing_trigger_roi_pct}% "
            f"liq_sl_distance={s.liq_sl_distance_pct}%",
            f"min_agreeing={s.min_agreeing} timeframes={','.join(s.timeframes)} "
            f"min_conf={s.min_confidence} tf_min_conf={s.tf_min_confidence}",
            f"scan_interval={s.scan_interval}s report_interval={s.report_interval}s "
            f"reversal={s.reversal} reversal_conf={s.reversal_confidence} "
            f"reentry_cooldown={s.reentry_cooldown_sec}s",
            f"trend_gate={s.trend_gate} tf={self._gate_timeframe() or '(none)'}",
            f"universe_rank={s.universe_rank} universe_sizing={s.universe_sizing} "
            f"min_24h_volume_usd={s.min_24h_volume_usdt:g} scans={s.scans} errors={len(s.errors)}",
            f"min_stop_ticks={s.min_stop_ticks} "
            f"taker_fee={self.s.taker_fee_rate * 100:g}% "
            f"margin_safety={self.s.margin_safety_pct:g}%",
            f"scans={s.scans} errors={len(s.errors)}",
        ])

    # ================================================================ command router
    # Prefixes that unambiguously start an engine command. Used by the CLI to
    # decide whether a bare line is engine syntax or free text for the model.
    COMMAND_PREFIXES: tuple[str, ...] = (
        "start trading", "stop trading", "autotrade", "engine on", "engine off",
        "status", "report ", "trades_history", "trades history", "tpsl_history",
        "tpsl history", "pnl", "p&l", "settings", "scan on", "scan off",
        "mid management", "reversal", "reversal_confidence", "reentry cooldown",
        "scans interval", "breakeven threshold",
        "trailing trigger", "liq sl distance", "min agreeing", "timeframes",
        "min confidence", "tf min confidence", "positions history", "close position",
        "min stop ticks", "trend gate", "history",
        "margin mode", "position mode", "symbols", "symbol ", "max open positions",
        "margin amount", "margin add", "margin reduce",
        "leverage", "risk ",
        # the PARTIAL method's knobs (POSITION has none; TRAILING/ACCOUNT are
        # always armed with their own Readouts)
        "tpsl method", "partial ladder",
        "account tp usdt", "account sl usdt",
        "universe rank", "universe sizing", "universe volume", "universe volume usd",
        "universe rank auto", "universe rank off",
        "min stop ticks",
    )

    def looks_like_command(self, line: str) -> bool:
        """True only if the line matches engine command grammar.

        Deliberately strict: a natural-language sentence must NOT be swallowed
        by the engine and answered with "Unknown engine command".

        The old check was `any(low.startswith(p) for p in COMMAND_PREFIXES)`,
        which is greedy: "leverage is too high, what should I do?" starts with
        "leverage" and was therefore treated as the `leverage` setter, so a
        normal question got "Unknown engine command" instead of an answer. A
        prefix only counts as a command when the REST of the line is the
        argument shape that command actually accepts.
        """
        low = self._normalize_command(line.strip().lower())
        if not low:
            return False
        if low in ("status", "pnl", "p&l", "trades_history", "tpsl_history",
                   "trades history", "tpsl history", "engine on", "engine off",
                   "start trading", "stop trading", "scan on", "scan off",
                   "report on", "report off", "settings", "settings trade",
                   "history"):
            return True
        for p in self.COMMAND_PREFIXES:
            if not low.startswith(p):
                continue
            rest = low[len(p):].strip()
            if self._is_command_argument(p, rest):
                return True
        return False

    #: The wording an operator actually types, mapped onto the canonical
    #: grammar. Without this, a setting they believe they changed is
    #: SILENTLY not changed - the line falls through to the model, which
    #: either fails or invents a confirmation - and the old value is still
    #: there after a restart. That reads as "everything reset".
    COMMAND_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
        # A leading "set " is a verb, not part of the grammar:
        # "Set partial ladder 25@1,25@2,25@3" is the ladder setter.
        # EXCEPT when what follows is a setter the NLU treats as DESTRUCTIVE
        # ("set leverage to 5x" keeps its confirm prompt) - leave those
        # intact so the sentence keeps flowing through NLU.
        (re.compile(r"^\s*set\s+(?!leverage\b|leverage\s+mode\b|risk\b|risk\s+"
                    r"per\s+trade\b|margin\s+amount\b|margin\s+per\s+trade\b|"
                    r"margin\s+mode\b|position\s+mode\b|close\b|autotrade\b)"),
         ""),
        (re.compile(r"^risk[ _]pct\s+(.+)$"), r"risk \1"),
        (re.compile(r"^risk per trade\s+(.+)$"), r"risk \1"),
        (re.compile(r"^margin_amount_pct\s+(.+)$"), r"margin amount \1"),
        (re.compile(r"^margin per trade\s+(.+)$"), r"margin amount \1"),
        (re.compile(r"^reversal[ _]conf(?:idence)?\s+(.+)$"), r"reversal_confidence \1"),
        (re.compile(r"^breakeven\s+([\d.]+)$"), r"breakeven threshold \1"),
        (re.compile(r"^scan interval\s+(.+)$"), r"scans interval \1"),
        (re.compile(r"^reentry[ _]cooldown\s+(.+)$"), r"reentry cooldown \1"),
        # The min-stop setter: "min stop tick 4", "min stop 4" - the old
        # constant had NO command at all, so the request fell through to the
        # model and, with the model down, produced an error dump.
        (re.compile(r"^min stop ticks?\s+([\d.]+)$"), r"min stop ticks \1"),
        (re.compile(r"^min stop\s+([\d.]+)$"), r"min stop ticks \1"),
        # The history view, in the wording people actually type. "positions
        # history" stays the raw JSON command; these forms get the readable
        # view ("position history", "history position", "trade history").
        (re.compile(r"^position history\b"), "history"),
        (re.compile(r"^history positions?\b"), "history"),
        (re.compile(r"^trade history\b"), "history"),
    )

    def _normalize_command(self, low: str) -> str:
        """Rewrite the operator's wording into the canonical grammar.

        An alias can CHAIN ("set min stop tick 4" -> "min stop tick 4" ->
        "min stop ticks 4"), so the rewrite is repeated until a pass makes
        no change - bounded, so aliases cannot ping-pong.
        """
        for _ in range(3):
            before = low
            for pattern, replacement in self.COMMAND_ALIASES:
                low = pattern.sub(replacement, low)
            if low == before:
                break
        return low

    #: Commands whose argument is a bare number (optionally with a unit word).
    _NUMERIC_ARG_COMMANDS: tuple[str, ...] = (
        "leverage", "risk", "max open positions", "scans interval",
        "report interval", "breakeven threshold", "trailing trigger",
        "liq sl distance", "min agreeing", "min confidence",
        "tf min confidence", "account tp usdt", "account sl usdt",
        "margin amount", "reversal_confidence", "reentry cooldown",
        "min stop ticks",
    )

    def _is_command_argument(self, prefix: str, rest: str) -> bool:
        """Whether `rest` is a valid argument for the command `prefix`.

        This is what separates "leverage 20" (a command) from "leverage is too
        high" (a sentence). A command with no argument is only valid for the
        prefixes that are complete on their own.
        """
        p = prefix.strip()
        if not rest:
            # Bare prefixes that are complete commands on their own.
            return p in ("status", "pnl", "p&l", "settings",
                         "margin amount", "partial ladder",
                         "trades_history", "tpsl_history", "trades history",
                         "tpsl history", "symbols", "positions history",
                         "engine on", "engine off", "scan on", "scan off",
                         "report on", "report off", "start trading",
                         "stop trading", "autotrade", "trend gate", "history")
        # A numeric command takes a number, optionally preceded by a unit word
        # ("leverage to 20", "risk pct 1.5", "min confidence 70").
        if p in self._NUMERIC_ARG_COMMANDS:
            return bool(re.search(r"-?\d+(?:\.\d+)?", rest))
        # Symbol lists: "symbols BTCUSDT,ETHUSDT" / "symbol BTCUSDT".
        if p in ("symbols", "symbol"):
            return bool(re.search(r"[A-Za-z]{2,10}USDT", rest, re.I))
        # Mode setters take a known keyword.
        if p == "margin mode":
            return rest.replace(":", "").strip().upper() in ("ISOLATION", "CROSS")
        if p == "position mode":
            return rest.replace(":", "").strip().upper().replace(" ", "_") in (
                "HEDGE", "ONE_WAY")
        if p == "tpsl method":
            return rest.split()[-1].upper() in ("POSITION", "PARTIAL")
        if p == "partial ladder":
            # "partial ladder" with no rest is a read; a setter needs a spec
            # like "40@1,35@2" (digits) or one of the reset keywords.
            return (bool(re.search(r"\d", rest))
                    or rest.split()[0].lower() in ("off", "none"))
        if p == "close position":
            return bool(re.search(r"[A-Za-z]{2,10}USDT", rest, re.I))
        if p in ("margin add", "margin reduce"):
            # "margin add BTCUSDT 100" - a symbol AND an amount are required.
            return bool(re.search(r"[A-Za-z]{2,10}USDT", rest, re.I)
                        and re.search(r"-?\d+(?:\.\d+)?", rest))
        if p == "reversal":
            return rest.strip("= ").lower() in ("true", "false", "1", "0",
                                                "on", "off", "yes", "no")
        if p == "mid management":
            return True
        if p == "trend gate":
            # bare = read; on/off; "timeframe 15m" - all valid shapes
            return True
        if p == "history":
            # bare = the view; a count ("history 20") narrows it
            return True
        if p == "timeframes":
            return bool(re.search(r"\d+[mhd]", rest, re.I))
        if p == "autotrade":
            return rest.strip().lower() in ("on", "off", "enable", "disable",
                                            "enabled", "disabled", "start", "stop")
        if p == "report":
            return rest.split()[0].lower() in ("on", "off", "interval")
        if p == "universe rank":
            return rest.strip().lower() in ("gainers", "loosers", "volume",
                                              "movers", "auto", "off")
        if p in ("universe sizing", "universe volume", "universe volume usd"):
            return bool(re.search(r"-?\d+(?:\.\d+)?", rest))
        return False

    async def command(self, line: str) -> str:
        """Route an engine command, persisting any state it changed.

        The persistence wrapper is here rather than in each of the ~25 branches
        so that a future branch cannot forget it.
        """
        try:
            return await self._command(line)
        finally:
            self._persist_if_changed()

    async def _command(self, line: str) -> str:
        """Route an engine command. Returns a human-readable result.

        The branches are grouped by domain and tried in order - the first
        group that recognises the command answers it. The order matters: a
        bare "margin" is the margin AMOUNT, "margin mode" is the mode, and
        "tpsl method" is the exit shape.
        """
        raw = line.strip()
        if not raw:
            return ""
        # The operator's wording is normalised onto the canonical grammar
        # here too, so the aliases behave the same in every branch.
        low = self._normalize_command(raw.lower())
        raw = low

        def arg(prefix: str) -> str:
            return raw[len(prefix):].strip()

        for handler in (self._cmd_trading, self._cmd_risk, self._cmd_positions,
                        self._cmd_symbols, self._cmd_tpsl):
            out = await handler(raw, low, arg)
            if out is not None:
                return out
        return f"Unknown engine command: {raw!r}. Type 'help' for the list."

    async def _cmd_trading(self, raw: str, low: str, arg) -> str | None:
        """Trading control, the report, the scan and the history."""
        s = self.state
        # --- trading control
        if low in ("start trading", "autotrade on"):
            s.autotrade = True
            if not s.engine_on:
                await self.engine_on()
            return "Autotrade ON - signals will be executed."
        if low in ("stop trading", "autotrade off"):
            s.autotrade = False
            return "Autotrade OFF - scanning continues, no new orders."
        if low == "engine off":
            return await self.engine_off()
        if low in ("engine on", "engine on (scanner, trader, autotrade)"):
            return await self.engine_on()
        if low == "status":
            return await self.status()
        if low == "report on":
            s.report_on = True
            return f"Report ON every {s.report_interval}s."
        if low == "report off":
            s.report_on = False
            return "Report OFF."
        if low.startswith("report interval"):
            try:
                s.report_interval = int(arg("report interval").split()[-1])
                return f"Report interval = {s.report_interval}s."
            except (ValueError, IndexError):
                return "Usage: report interval sec 30"
        if low in ("trades_history", "trades history"):
            return json.dumps(self.trades_history(), indent=2)
        if low in ("tpsl_history", "tpsl history"):
            return json.dumps(self.tpsl_history(), indent=2)
        if low in ("pnl", "p&l"):
            return json.dumps(await self.pnl(), indent=2)
        if low.startswith("settings trade") or low == "settings":
            return self.settings_report()
        if low == "scan on":
            s.scan_on = True
            return "Scanner ON."
        if low == "scan off":
            s.scan_on = False
            return "Scanner OFF."
        return None

    async def _cmd_risk(self, raw: str, low: str, arg) -> str | None:
        """Mid management, the reversal rules and the thresholds."""
        s = self.state
        if low.startswith("mid management"):
            s.mid_management = not s.mid_management
            return f"Mid management = {s.mid_management}."
        if low.startswith("reversal_confidence"):
            try:
                s.reversal_confidence = float(arg("reversal_confidence").strip("= "))
                return f"Reversal confidence = {s.reversal_confidence}."
            except ValueError:
                return "Usage: Reversal_confidence=70"
        if low.startswith("reentry cooldown"):
            try:
                s.reentry_cooldown_sec = int(arg("reentry cooldown").split()[-1])
                return (f"Re-entry cooldown = {s.reentry_cooldown_sec}s "
                        f"(the same symbol cannot be re-entered faster than this).")
            except (ValueError, IndexError):
                return "Usage: reentry cooldown sec 60"
        # ---- universe commands ----
        if low.startswith("universe rank"):
            val = arg("universe rank").strip().upper()
            if val in ("GAINERS", "LOSERS", "VOLUME", "MOVERS"):
                s.universe_rank = val
                return f"Universe rank = {s.universe_rank}."
            return ("Universe rank is one of: GAINERS, LOOSERS, VOLUME, MOVERS "
                    f"(current: {s.universe_rank}); AUTO turns on automatic ranking.")
        if low.startswith("universe sizing"):
            try:
                s.universe_sizing = int(arg("universe sizing").split()[-1])
            except (ValueError, IndexError):
                return "Usage: universe sizing 50"
            if not 1 <= s.universe_sizing <= 200:
                return f"Universe sizing must be 1..200 ({s.universe_sizing} rejected)."
            return f"Universe sizing = {s.universe_sizing}."
        if low.startswith("universe volume"):
            try:
                s.min_24h_volume_usdt = float(arg("universe volume").split()[-1])
            except (ValueError, IndexError):
                return "Usage: universe volume 1000000"
            return (f"Universe min 24h volume = {s.min_24h_volume_usdt:g} USDT.")
        if low.startswith("universe volume usd"):
            try:
                s.min_24h_volume_usdt = float(arg("universe volume usd").split()[-1])
            except (ValueError, IndexError):
                return "Usage: universe volume usd 1000000"
            return (f"Universe min 24h volume = {s.min_24h_volume_usdt:g} USDT.")
        if low == "universe rank auto":
            s.universe_rank = "AUTO"
            s.universe_sizing = 0
            return "Universe auto-ranking ON (top symbols by engine rank)."
        if low == "universe rank off":
            s.universe_rank = ""
            s.universe_sizing = 0
            return "Universe auto-ranking OFF."
        # ---- end universe commands ----
        if low.startswith("reversal"):
            val = arg("reversal").strip("= ").lower()
            s.reversal = val in ("true", "1", "on", "yes")
            return f"Reversal = {s.reversal}."
        if low.startswith("scans interval"):
            try:
                s.scan_interval = int(arg("scans interval").split()[-1])
                return f"Scan interval = {s.scan_interval}s."
            except (ValueError, IndexError):
                return "Usage: Scans interval sec 30"
        if low.startswith("breakeven threshold"):
            try:
                s.breakeven_pct = float(arg("breakeven threshold").split()[-1])
                return f"Breakeven threshold = {s.breakeven_pct}% ROI."
            except (ValueError, IndexError):
                return "Usage: Breakeven threshold pct 0.6"
        if low.startswith("breakeven"):
            return self._explain_breakeven()
        if low.startswith("trailing trigger"):
            try:
                s.trailing_trigger_roi_pct = float(arg("trailing trigger").split()[-1])
                return f"Trailing trigger = {s.trailing_trigger_roi_pct}% ROI."
            except (ValueError, IndexError):
                return "Usage: Trailing trigger stop ROI pct 25"
        if low.startswith("trailing"):
            return self._explain_trailing()
        if low.startswith("liq sl distance"):
            try:
                s.liq_sl_distance_pct = float(arg("liq sl distance").split()[-1])
                return f"Liq SL distance = {s.liq_sl_distance_pct}%."
            except (ValueError, IndexError):
                return "Usage: Liq sl distance pct 50"
        if low.startswith("min agreeing"):
            try:
                s.min_agreeing = int(arg("min agreeing").split()[-1])
                return f"Min agreeing strategies = {s.min_agreeing}."
            except (ValueError, IndexError):
                return "Usage: Min agreeing strategies 3"
        if low.startswith("min stop ticks"):
            try:
                n = int(float(arg("min stop ticks").split()[-1]))
            except (ValueError, IndexError):
                return "Usage: min stop ticks 4"
            if not 1 <= n <= 50:
                return ("Min stop ticks must be 1..50 "
                        f"({n} rejected; the current value is {s.min_stop_ticks}).")
            s.min_stop_ticks = n
            return (f"Min stop ticks = {n} (the stop must clear the entry "
                    f"by at least this many ticks; was {MIN_STOP_TICKS} before "
                    f"the first change).")
        if low.startswith("timeframes"):
            tfs = [t.strip() for t in arg("timeframes").replace("=", " ").split(",") if t.strip()]
            if tfs:
                s.timeframes = tfs
                return f"Timeframes = {','.join(tfs)}"
            return "Usage: Timeframes 1m,3m,5m,15m"
        if low.startswith("min confidence"):
            try:
                s.min_confidence = float(arg("min confidence").split()[-1])
                return f"Min confidence = {s.min_confidence}."
            except (ValueError, IndexError):
                return "Usage: Min Confidence 70"
        if low.startswith("tf min confidence"):
            try:
                s.tf_min_confidence = float(arg("tf min confidence").split()[-1])
                return f"TF min confidence = {s.tf_min_confidence}."
            except (ValueError, IndexError):
                return "Usage: Tf min confidence 60"
        if low.startswith("trend gate"):
            rest = arg("trend gate").strip().lower()
            if rest.startswith("timeframe"):
                tf = rest[len("timeframe"):].strip()
                if tf:
                    s.trend_gate_timeframe = tf
                    gate = self._gate_timeframe()
                    return (f"Trend gate timeframe = {tf} "
                            f"(watching {gate or 'nothing - the scan set is empty'}).")
                return "Usage: trend gate timeframe 15m"
            if rest in ("on", "off", "true", "false", "1", "0", "yes", "no"):
                s.trend_gate = rest in ("on", "true", "1", "yes")
                return (f"Trend gate = {'ON' if s.trend_gate else 'OFF'} - "
                        f"{'a signal the ' + (self._gate_timeframe() or 'big') + ' timeframe fights never enters.'
                        if s.trend_gate else 'the engine chases again, as before'}.")
            return (f"Trend gate = {'ON' if s.trend_gate else 'OFF'}  "
                    f"timeframe = {self._gate_timeframe() or '(none)'}\n"
                    f"Usage: trend gate on|off | trend gate timeframe 15m")
        return None

    def _explain_breakeven(self) -> str:
        """What breakeven is, and how to change it.

        "breakeven on" / "enable breakeven": breakeven is not a switch - it
        is ALWAYS armed and fires at the threshold. Saying so is better than
        "Unknown engine command" (which is what the user got) or pretending a
        switch was flipped.
        """
        s = self.state
        if not s.mid_management:
            return ("Breakeven is armed but MID MANAGEMENT IS OFF, so it "
                    "never fires. Turn it on with: mid management on")
        return (f"Breakeven is ALWAYS armed: the stop moves to the entry at "
                f"+{s.breakeven_pct:g}% ROI. Change the threshold with: "
                f"breakeven threshold pct {s.breakeven_pct:g}")

    def _explain_trailing(self) -> str:
        """What trailing is, and how to change it."""
        s = self.state
        if not s.mid_management:
            return ("Trailing is armed but MID MANAGEMENT IS OFF, so it "
                    "never fires. Turn it on with: mid management on")
        return (f"Trailing is ALWAYS armed: the stop ratchets from "
                f"+{s.trailing_trigger_roi_pct:g}% ROI. Change the trigger "
                f"with: trailing trigger stop ROI pct {s.trailing_trigger_roi_pct:g}")

    async def _cmd_positions(self, raw: str, low: str, arg) -> str | None:
        """The positions, the margin and the account settings."""
        s = self.state
        if low == "history" or low.startswith("history "):
            rest = arg("history").strip()
            m = re.search(r"\d+", rest)
            return await self.render_history(int(m.group()) if m else 12)
        if low.startswith("positions history"):
            return json.dumps(await self.positions_history(), indent=2)
        if low.startswith("close position"):
            sym = arg("close position").strip() or None
            return json.dumps(await self.close_position(symbol=sym, reason="user"),
                              indent=2)
        if low.startswith("margin add") or low.startswith("margin reduce"):
            return await self._margin_adjust_command(raw)
        if low.startswith("margin amount"):
            rest = arg("margin amount").strip()
            if not rest:
                # Bare "margin amount" is a READ, not a usage error: it is the
                # natural way to ask what the setting currently is.
                return (f"Margin per trade = {s.margin_amount_pct:g}% of the "
                        f"available balance (0 disables the budget).\n"
                        f"Change it with: margin amount 10")
            try:
                s.margin_amount_pct = float(rest.split()[-1])
                return (f"Margin per trade = {s.margin_amount_pct:g}% of the "
                        f"available balance (0 disables the budget).")
            except (ValueError, IndexError):
                return "Usage: margin amount 10"
        if low.startswith("margin mode"):
            val = arg("margin mode").replace(":", "").strip().upper()
            if val in ("ISOLATION", "CROSS"):
                s.margin_mode = val
                return f"Margin mode = {val} (applied on next entry)."
            return "Usage: Margin mode ISOLATION|CROSS"
        if low.startswith("position mode"):
            val = arg("position mode").replace(":", "").strip().upper().replace(" ", "_")
            if val in ("HEDGE", "ONE_WAY"):
                s.position_mode = val
                try:
                    await self.client.change_position_mode(val)
                except BitunixAPIError as e:
                    # The bot's intent changed, the exchange's did not. Drop the
                    # cache so the next order re-reads the real mode.
                    self._position_mode = ""
                    self._position_mode_ts = 0.0
                    return f"Position mode set locally to {val}, exchange said: {e}"
                self._remember_position_mode(val)
                return f"Position mode = {val}."
            return "Usage: Position mode HEDGE|ONE_WAY"
            return "Usage: Symbols BTCUSDT,ETHUSDT"
        if low.startswith("max open positions"):
            try:
                s.max_open_positions = int(arg("max open positions").split()[-1])
                return f"Max open positions = {s.max_open_positions}."
            except (ValueError, IndexError):
                return "Usage: Max open positions 3"
        if low.startswith("leverage"):
            try:
                s.leverage = int(arg("leverage").split()[-1])
                return f"Leverage = {s.leverage}x (applied on next entry)."
            except (ValueError, IndexError):
                return "Usage: leverage 10"
        # --------------------------------------------------- the TP/SL methods
        return None

    async def _cmd_symbols(self, raw: str, low: str, arg) -> str | None:
        """The symbols to scan, and their precision rules."""
        if not (low.startswith("symbols") or low.startswith("symbol ")):
            return None
        s = self.state
        val = arg("symbols") if low.startswith("symbols") else arg("symbol")
        syms: list[str] = []
        bad: list[str] = []
        for x in val.replace("=", " ").split(","):
            # "symbols: INITUSDT, ERAUSDT" kept the colon, and the engine
            # then scanned a symbol that does not exist - the exchange
            # answered [20015] on every timeframe, every cycle, and the
            # report filled up with errors that looked like real failures.
            sym = x.strip().upper().lstrip(":").strip()
            if not sym:
                continue
            if not re.fullmatch(r"[A-Z0-9]{2,15}USDT", sym):
                bad.append(x.strip())
                continue
            syms.append(sym)
        if syms:
            # The account endpoints that take a symbols filter cap it at 50
            # (documented on GET /api/v1/futures/account/trading_settings),
            # and the watchlist feeds those calls. Refusing >50 here keeps
            # every downstream symbols=... call inside the contract.
            if len(syms) > 50:
                return (f"max 50 symbols - you listed {len(syms)}. "
                        f"Trim the list: Symbols BTCUSDT,ETHUSDT,...")
            s.symbols = syms
            await self.load_rules(syms)
            msg = f"Symbols = {','.join(syms)}"
            if bad:
                msg += (f" (ignored {', '.join(bad)} - not a symbol like "
                        f"BTCUSDT)")
            return msg
        return "Usage: Symbols BTCUSDT,ETHUSDT"
        return None

    async def _cmd_tpsl(self, raw: str, low: str, arg) -> str | None:
        """The exit shape (TP/SL method, ladder) and the account thresholds."""
        s = self.state
        if low.startswith("tpsl method"):
            try:
                m = arg("tpsl method").split()[-1].upper()
            except IndexError:
                return "Usage: tpsl method POSITION|PARTIAL"
            if m not in ("POSITION", "PARTIAL"):
                return "Method must be POSITION or PARTIAL (TRAILING/ACCOUNT " \
                       "are always armed; see `settings`)."
            s.tpsl_method = m
            return self.tpsl_method_report()
        if low.startswith("account tp usdt"):
            try:
                s.account_tp_usdt = float(arg("account tp usdt").split()[-1])
                return f"Account TP = +{s.account_tp_usdt:g} USDT (0 disables)."
            except (ValueError, IndexError):
                return "Usage: account tp usdt 100"
        if low.startswith("account sl usdt"):
            try:
                s.account_sl_usdt = float(arg("account sl usdt").split()[-1])
                return f"Account SL = -{s.account_sl_usdt:g} USDT (0 disables)."
            except (ValueError, IndexError):
                return "Usage: account sl usdt 50"
        # ---------------------------------------------- the PARTIAL rung ladder
        if low.startswith("partial ladder"):
            rest = raw[len("partial ladder"):].strip()
            if not rest:
                return self.tpsl_method_report()
            low_rest = rest.lower()
            if low_rest in ("off", "none", "disable", "clear"):
                s.partial_tp_ladder = ""
                return ("PARTIAL ladder OFF - one whole-position TP rides with "
                        "the stop instead.\n" + self.tpsl_method_report())
            try:
                parse_partial_ladder(rest)
            except PartialLadderError as e:
                return f"Invalid ladder: {e}\nNothing changed."
            s.partial_tp_ladder = rest
            return "PARTIAL ladder set (exchange-enforced rungs).\n" \
                + self.tpsl_method_report()
        if low.startswith("risk"):
            try:
                s.risk_pct = float(arg("risk").split()[-1])
                return f"Risk per trade = {s.risk_pct}% of equity."
            except (ValueError, IndexError):
                return "Usage: risk 1.0"
        return None
    async def _margin_adjust_command(self, raw: str) -> str:
        """`margin add SYMBOL AMOUNT` / `margin reduce SYMBOL AMOUNT`.

        Documented in HELP and implemented in the client
        (`adjust_position_margin`) but never wired into the router, so the
        documented command answered "Unknown engine command". Isolated margin
        only: the endpoint is meaningless for a CROSS position, and the
        exchange rejects it, so it is refused locally with a clear reason
        instead of a raw API error.

        The position is looked up first so the call carries the real
        `positionId` (and side) rather than guessing - the endpoint requires
        one of them.
        """
        low = raw.strip().lower()
        reduce_ = low.startswith("margin reduce")
        rest = raw.strip()[len("margin reduce" if reduce_ else "margin add"):].strip()
        parts = rest.replace(",", " ").split()
        sym = next((p.upper() for p in parts if re.fullmatch(r"[A-Za-z]{2,10}USDT", p)), "")
        amount = next((p for p in parts if re.fullmatch(r"-?\d+(?:\.\d+)?", p)), "")
        if not sym or not amount:
            return ("Usage: margin add BTCUSDT 100 | margin reduce BTCUSDT 50\n"
                    "(isolated margin only; the amount is in USDT)")
        try:
            amt = abs(float(amount))
        except ValueError:
            return "Usage: margin add BTCUSDT 100 | margin reduce BTCUSDT 50"
        if amt <= 0:
            return "The amount must be greater than 0."

        if (self.state.margin_mode or "").upper() != "ISOLATION":
            return (f"Margin can only be adjusted on an ISOLATED position; "
                    f"margin mode is {self.state.margin_mode}. "
                    f"Set it with: margin mode ISOLATION")

        positions = await self.open_positions()
        pos = next((p for p in positions
                    if str(p.get("symbol", "")).upper() == sym), None)
        if pos is None:
            return f"No open {sym} position to adjust margin on."

        pid = str(pos.get("positionId") or "")
        side = str(pos.get("side") or "").upper() or None
        signed = -amt if reduce_ else amt
        try:
            await self.client.adjust_position_margin(
                symbol=sym, amount=str(signed), position_id=pid or None, side=side)
        except BitunixAPIError as e:
            self.state.errors.append(f"margin adjust {sym}: {e}")
            return f"Bitunix error {e.code}: {e.msg or e}"
        except ValueError as e:
            return f"Cannot adjust margin: {e}"
        verb = "Reduced" if reduce_ else "Added"
        return (f"{verb} {amt:g} USDT {'from' if reduce_ else 'to'} the {sym} "
                f"{side or ''} position (isolated margin).").replace("  ", " ")

    async def status(self) -> str:
        s = self.state
        try:
            ok, msg = await self.client.verify_credentials()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        positions = await self.open_positions()
        lines = [
            "=== status ===",
            f"engine={'ON' if s.engine_on else 'OFF'} scan={'ON' if s.scan_on else 'OFF'} "
            f"autotrade={'ON' if s.autotrade else 'OFF'} report={'ON' if s.report_on else 'OFF'}",
            f"api: {'OK' if ok else 'FAIL'} - {msg}",
            f"symbols={','.join(s.symbols)} leverage={s.leverage}x max_open={s.max_open_positions}",
            f"open positions: {len(positions)}",
            f"scans={s.scans} last_scan={time.strftime('%H:%M:%S', time.localtime(s.last_scan_ts)) if s.last_scan_ts else 'never'}",
            f"errors={len(s.errors)}" + (f" last={s.errors[-1]}" if s.errors else ""),
            self.store_line(),
        ]
        if self.status_extra:
            try:
                lines.extend(self.status_extra())
            except Exception as e:  # noqa: BLE001
                lines.append(f"status_extra: {type(e).__name__}: {e}")
        return "\n".join(lines)
