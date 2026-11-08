"""Order-unit conversion, liquidation price, tiered risk limits, position sizing.

Bitunix futures notes (from the official docs + support article):
  * `qty` in place_order is the BASE-COIN amount (e.g. BTC for BTCUSDT), not contracts.
  * `minTradeVolume` / `maxMarketOrderVolume` are base-coin amounts.
  * `basePrecision` = max decimals for qty; `quotePrecision` = max decimals for price.
  * Liquidation (isolated, one-way):
        liq = entry * (1 -/+ 1/leverage + maintenanceMarginRate)   (long / short)
    i.e. long:  liq = entry * (1 - 1/L + mmr)
         short: liq = entry * (1 + 1/L - mmr)
  * Tiered risk: the maintenance margin rate depends on the position notional tier
    (get_position_tiers). Higher notional -> higher mmr -> liquidation closer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal


@dataclass
class PairRules:
    symbol: str
    min_trade_volume: float = 0.0
    max_market_order_volume: float = 0.0
    max_limit_order_volume: float = 0.0
    base_precision: int = 4
    quote_precision: int = 2
    min_leverage: int = 1
    max_leverage: int = 125
    default_leverage: int = 20
    price_protect_scope: float = 0.02
    symbol_status: str = "OPEN"
    api_supported: bool = True

    @classmethod
    def from_api(cls, d: dict) -> "PairRules":
        def f(k, default=0.0):
            v = d.get(k, default)
            if v is None or v == "":
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        def i(k, default=0):
            v = d.get(k, default)
            if v is None or v == "":
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        return cls(
            symbol=d.get("symbol", ""),
            min_trade_volume=f("minTradeVolume"),
            max_market_order_volume=f("maxMarketOrderVolume"),
            max_limit_order_volume=f("maxLimitOrderVolume"),
            base_precision=i("basePrecision", 4),
            quote_precision=i("quotePrecision", 2),
            min_leverage=i("minLeverage", 1),
            max_leverage=i("maxLeverage", 125),
            default_leverage=i("defaultLeverage", 20),
            price_protect_scope=f("priceProtectScope", 0.02),
            symbol_status=d.get("symbolStatus", "OPEN"),
            api_supported=bool(d.get("isApiSupported", True)),
        )


@dataclass
class Tier:
    level: int
    start_value: float
    end_value: float
    leverage: int
    maintenance_margin_rate: float

    @classmethod
    def from_api(cls, d: dict) -> "Tier":
        return cls(
            level=int(d.get("level", 1)),
            start_value=float(d.get("startValue", 0) or 0),
            end_value=float(d.get("endValue", 0) or 0),
            leverage=int(d.get("leverage", 1) or 1),
            maintenance_margin_rate=float(d.get("maintenanceMarginRate", 0.004) or 0.004),
        )


def round_step(value: float, precision: int) -> float:
    """Round DOWN to the allowed precision (never over-size an order)."""
    if precision < 0:
        precision = 0
    factor = 10 ** precision
    return int(value * factor) / factor


def round_price(price: float, precision: int) -> float:
    """Round to `precision` decimals, half-up.

    Deliberately NOT Python's built-in `round()`, which is banker's rounding:
    round(0.0047845, 6) == 0.004784 but round(0.0047855, 6) == 0.004786, so the
    same distance from the mark can round in either direction. For a price that
    must stay on one side of the mark that is a coin flip, and it is how a stop
    that was valid when planned lands on the wrong side when sent.
    """
    p = max(0, int(precision))
    q = Decimal(1).scaleb(-p)
    return float(Decimal(repr(float(price))).quantize(q, rounding=ROUND_HALF_UP))


def floor_price(price: float, precision: int) -> float:
    """Round DOWN to the tick. Use for anything that must stay BELOW a level."""
    p = max(0, int(precision))
    q = Decimal(1).scaleb(-p)
    return float(Decimal(repr(float(price))).quantize(q, rounding=ROUND_FLOOR))


def ceil_price(price: float, precision: int) -> float:
    """Round UP to the tick. Use for anything that must stay ABOVE a level."""
    p = max(0, int(precision))
    q = Decimal(1).scaleb(-p)
    return float(Decimal(repr(float(price))).quantize(q, rounding=ROUND_CEILING))


def format_qty(qty: float, rules: "PairRules") -> str:
    """Format a base-coin qty to the symbol's basePrecision as a string.

    basePrecision == 0 means integer qty (e.g. AKEUSDT). round_step already
    floors to the precision; this renders it without a trailing '.0'.
    """
    p = max(0, int(rules.base_precision))
    if p == 0:
        return str(int(round_step(qty, 0)))
    return f"{round_step(qty, p):.{p}f}"


def format_price(price: float, rules: "PairRules") -> str:
    """Format a price to the symbol's quotePrecision as a string.

    Uses half-up rounding, matching `round_price`. A price that has already been
    clamped to the correct side of the mark must not be re-rounded the other way
    here - that would undo the clamp after it was validated.
    """
    p = max(0, int(rules.quote_precision))
    if p == 0:
        return str(int(round_price(price, 0)))
    return f"{round_price(price, p):.{p}f}"


def qty_from_notional(notional_usd: float, price: float, rules: PairRules) -> float:
    """Base-coin qty for a given USD notional."""
    if price <= 0:
        return 0.0
    return round_step(notional_usd / price, rules.base_precision)


def notional_from_qty(qty: float, price: float) -> float:
    return qty * price


def margin_required(notional: float, leverage: int) -> float:
    return notional / max(1, leverage)


#: Bitunix USDT-M futures taker fee at VIP 0, as a fraction of notional.
#: A MARKET order is always a taker order, so this is charged on ENTRY and is
#: deducted from the available balance at the same moment the margin is locked.
#: Ignoring it is what produced 20003: the engine sized the order so that
#: margin == available balance exactly, the local pre-flight passed (it only
#: compared margin), and the exchange then wanted margin + fee and refused.
TAKER_FEE_RATE = 0.0006


def fee_for_notional(notional: float, fee_rate: float = TAKER_FEE_RATE) -> float:
    """Entry fee charged on a MARKET order of `notional` USDT."""
    return max(0.0, notional) * max(0.0, fee_rate)


def max_notional_for_balance(equity: float, leverage: int,
                             fee_rate: float = TAKER_FEE_RATE,
                             safety_pct: float = 0.0) -> float:
    """Largest notional whose margin PLUS entry fee still fits `equity`.

    Opening a position costs ``notional/leverage`` of margin and
    ``notional*fee_rate`` of taker fee, both taken from the available balance::

        notional/leverage + notional*fee_rate <= equity
        notional <= equity / (1/leverage + fee_rate)

    `safety_pct` reserves extra headroom (percent of equity) so a price move
    between sizing and the request landing cannot push the cost back over the
    balance. Without the fee term this returns ``equity*leverage``, which is
    exactly the value that made margin == equity and triggered 20003.
    """
    if equity <= 0 or leverage <= 0:
        return 0.0
    lev = max(1, int(leverage))
    usable = equity * (1.0 - max(0.0, min(0.9, safety_pct / 100.0)))
    denom = 1.0 / lev + max(0.0, fee_rate)
    if denom <= 0:
        return 0.0
    return usable / denom


def required_cost(notional: float, leverage: int,
                  fee_rate: float = TAKER_FEE_RATE) -> float:
    """Total balance an order consumes: initial margin + entry fee."""
    return margin_required(notional, leverage) + fee_for_notional(notional, fee_rate)


#: The three order units Bitunix documents in
#: "Explanation of the Order Units in Futures Trading":
#:   QUANTITY - base-coin quantity (the `qty` field of place_order)
#:   NOTIONAL - position value in USDT
#:   COST     - margin paid in USDT (initial margin, before fees)
ORDER_UNITS = ("QUANTITY", "NOTIONAL", "COST")


def qty_from_unit(amount: float, unit: str, price: float, leverage: int,
                  rules: PairRules) -> float:
    """Convert an order size expressed in one of the three documented units
    into the base-coin quantity that place_order's `qty` field requires.

        QUANTITY  qty = amount
        NOTIONAL  qty = amount / price
        COST      qty = amount * leverage / price

    The wire format is always base-coin qty; the unit only changes how the
    caller specifies the size. An unknown unit raises rather than guessing.
    """
    u = (unit or "QUANTITY").strip().upper()
    if u not in ORDER_UNITS:
        raise ValueError(
            f"unknown order unit {unit!r}. Allowed: {', '.join(ORDER_UNITS)}.")
    if price <= 0:
        return 0.0
    if u == "QUANTITY":
        raw = amount
    elif u == "NOTIONAL":
        raw = amount / price
    else:  # COST
        raw = amount * max(1, leverage) / price
    return round_step(raw, rules.base_precision)


def unit_from_qty(qty: float, unit: str, price: float, leverage: int) -> float:
    """Inverse of qty_from_unit: express a base-coin qty in the given unit."""
    u = (unit or "QUANTITY").strip().upper()
    if u not in ORDER_UNITS:
        raise ValueError(
            f"unknown order unit {unit!r}. Allowed: {', '.join(ORDER_UNITS)}.")
    notional = qty * price
    if u == "QUANTITY":
        return qty
    if u == "NOTIONAL":
        return notional
    return margin_required(notional, leverage)


def pick_tier(tiers: list[Tier], notional: float) -> Tier:
    """Tier whose [startValue, endValue) contains the notional."""
    if not tiers:
        return Tier(1, 0.0, float("inf"), 125, 0.004)
    ordered = sorted(tiers, key=lambda t: t.start_value)
    for t in ordered:
        if t.start_value <= notional < t.end_value:
            return t
    return ordered[-1]


def liquidation_price(entry: float, leverage: int, side: str,
                      maintenance_margin_rate: float) -> float:
    """Isolated-margin liquidation estimate (Bitunix formula).

    Long  : liq = entry * (1 - 1/L + mmr)
    Short : liq = entry * (1 + 1/L - mmr)

    At ``L >= 1/mmr`` the maintenance requirement swallows the entire margin
    and the formula puts liquidation AT or THROUGH the entry price - a LONG
    would report a liq price ABOVE entry, which is not a price at all. The JS
    engine returns ``entry`` in that case so the caller can see the position is
    unopenable at that leverage and refuse it. Returning the raw value instead
    produced a nonsense liq price that then failed the TP/SL feasibility check
    with a confusing "no valid stop-loss exists" message rather than naming the
    real cause (leverage too high for this pair's MMR).
    """
    L = max(1, leverage)
    mmr = max(0.0, maintenance_margin_rate)
    if side.upper() in ("LONG", "BUY"):
        p = entry * (1 - 1 / L + mmr)
        if p >= entry:
            return entry
    else:
        p = entry * (1 + 1 / L - mmr)
        if p <= entry:
            return entry
    return max(p, 0.0)


def liq_unsafe(entry: float, leverage: int, side: str,
               maintenance_margin_rate: float) -> bool:
    """True when liquidation sits at/through entry, i.e. the trade cannot open.

    Mirrors the JS ``liqUnsafe`` flag: at this leverage the exchange closes the
    position before any stop could fire, so the caller must refuse rather than
    size a position that can only end in liquidation.
    """
    if entry <= 0:
        return True
    liq = liquidation_price(entry, leverage, side, maintenance_margin_rate)
    if side.upper() in ("LONG", "BUY"):
        return liq >= entry
    return liq <= entry


def mmr_for(max_leverage: int) -> float:
    """Maintenance-margin-rate FALLBACK when the tier lookup is unavailable.

    The authoritative source is ``get_position_tiers`` (see ``pick_tier``).
    This heuristic only runs when that call fails (network, brand-new listing).

    Ported from the JS ``mmrFor``: the old ``1/maxLev`` made
    ``liqPrice == entry`` at any leverage where ``1/lev >= 1 - mmr``, which
    blocked EVERY trade at 50x+ with a false ``liqUnsafe``. Capped at 1% so high
    leverage does not falsely trip the guard while staying conservative - it
    over-states MMR on every pair measured (BTC real tier-1 is 0.30%, this
    returns 0.50%), which places liquidation closer to entry than reality. That
    is the safe direction to be wrong in.
    """
    try:
        max_lev = float(max_leverage)
    except (TypeError, ValueError):
        return 0.005
    if not (max_lev > 0):
        return 0.005
    return max(0.004, min(min(1.0 / max_lev, 0.01), 0.05))


def max_safe_leverage(entry: float, sl_dist: float,
                      maintenance_margin_rate: float = 0.005,
                      buffer: float = 0.5) -> int:
    """Highest leverage at which this stop still fits inside liquidation.

    Solves ``|entry - liq| >= sl_dist / (1 - buffer)`` for leverage using the
    published liq formula ``|entry - liq| = entry * (1/lev - mmr)``::

        lev = 1 / (sl_dist / (1 - buffer) / entry + mmr)

    ``buffer`` is the safety gap kept between the stop and liquidation, as a
    fraction of the entry->liq distance (the engine's ``liq_sl_distance_pct``).
    Informational: the engine does NOT lower the configured leverage to fit the
    stop - it pulls the stop inside liquidation instead. This is what the
    engine reports as the ceiling, and what ``liq_unsafe`` refuses on.
    """
    if entry <= 0 or sl_dist <= 0:
        return 1
    b = max(0.05, min(0.9, float(buffer)))
    needed = float(sl_dist) / (1.0 - b)
    frac = needed / float(entry)
    denom = frac + max(0.0, float(maintenance_margin_rate))
    if denom <= 0:
        return 1
    return max(1, int(1.0 / denom))


def sl_from_liq_distance(entry: float, liq: float, side: str,
                         distance_pct: float = 50.0) -> float:
    """Stop loss placed `distance_pct` of the way from entry toward liq price.

    distance_pct = 50 means the SL sits halfway between entry and liquidation,
    i.e. it triggers at ~50% of the ROI that would be lost at liquidation.
    """
    frac = max(1.0, min(99.0, distance_pct)) / 100.0
    if side.upper() in ("LONG", "BUY"):
        return entry - (entry - liq) * frac
    return entry + (liq - entry) * frac


def roi_pct(entry: float, price: float, side: str, leverage: int) -> float:
    if entry <= 0:
        return 0.0
    raw = (price - entry) / entry if side.upper() in ("LONG", "BUY") else (entry - price) / entry
    return raw * leverage * 100.0


def min_order_notional(price: float, min_trade_volume: float,
                       precision: int = 4) -> float:
    """Smallest order value (USDT) the exchange will accept for a symbol.

    ``minTradeVolume`` is a BASE-COIN amount, so the notional is
    ``minTradeVolume * price``. The minimum is rounded UP to ``precision``
    digits so that, after ``round_step`` floors a qty to the same precision,
    the order still clears the exchange's minimum. Ported from the JS
    ``minOrderNotional``.
    """
    if price <= 0:
        return 0.0
    p = max(0, int(precision))
    f = 10 ** p
    min_qty = math.ceil(max(0.0, min_trade_volume) * f) / f
    return min_qty * price


def min_margin_for_min_order(price: float, min_trade_volume: float,
                             leverage: int, precision: int = 4) -> float:
    """Margin (USDT) needed so that qty = minTradeVolume at this leverage.

    ``qty = margin * leverage / price``  =>  ``margin = minTradeVolume * price / leverage``.
    Ported from the JS ``minMarginForMinOrder``. Used to explain WHY an order
    was refused (the minimum is bigger than the balance allows) instead of
    reporting a bare "qty is 0".
    """
    return min_order_notional(price, min_trade_volume, precision) / max(1, leverage)


def position_size_by_risk(equity: float, risk_pct: float, entry: float,
                          stop: float, leverage: int, rules: PairRules,
                          fee_rate: float = TAKER_FEE_RATE,
                          safety_pct: float = 0.0,
                          slots_left: int = 1) -> float:
    """Size so that hitting the stop loses ~risk_pct of equity.

    The size is capped by what the balance can actually PAY FOR, not by
    ``equity * leverage``. Opening a MARKET order costs margin AND a taker fee,
    so the cap is ``max_notional_for_balance`` - using ``equity * leverage``
    made margin equal the whole balance, the local pre-flight passed, and the
    exchange rejected the order with 20003 once it added the fee.

    ``slots_left`` is the number of open-position slots still free. The JS
    engine caps each order's margin at ``available / slotsLeft`` so the book
    can never commit the whole balance to the first of several positions; the
    same cap is applied here. It defaults to 1 (no cap) so a caller that does
    not track slots keeps the previous behaviour exactly.
    """
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or entry <= 0:
        return 0.0
    risk_usd = equity * (risk_pct / 100.0)
    qty = risk_usd / stop_dist
    max_notional = max_notional_for_balance(equity, leverage, fee_rate, safety_pct)
    # Never let one order commit more than its share of the balance.
    slots = max(1, int(slots_left or 1))
    if slots > 1:
        max_notional = min(max_notional, max_notional_for_balance(
            equity / slots, leverage, fee_rate, safety_pct))
    qty = min(qty, max_notional / entry)
    qty = round_step(qty, rules.base_precision)
    if qty < rules.min_trade_volume:
        return 0.0
    if rules.max_market_order_volume and qty > rules.max_market_order_volume:
        qty = round_step(rules.max_market_order_volume, rules.base_precision)
    return qty


class TpSlSideError(ValueError):
    """Raised when a TP/SL price cannot be placed on the correct side.

    Bitunix rejects these with 30020-30033 (wrong side of mark/last price) and
    30024/30025 (wrong side of the liquidation price). We refuse locally with
    the numbers instead of sending a request we know will be rejected.
    """


def tick_size(rules: "PairRules") -> float:
    """Smallest representable price step for the symbol (quotePrecision)."""
    return 10.0 ** (-max(0, int(rules.quote_precision)))


def min_stop_distance(rules: "PairRules", min_ticks: int = 3) -> float:
    """Minimum distance a TP/SL must keep from the reference price.

    Without this, a high-leverage stop can round onto (or past) the mark price
    and the exchange rejects it with 30029/30031. `min_ticks` is configurable
    via TP_SL_MIN_STOP_TICKS.
    """
    return tick_size(rules) * max(1, int(min_ticks))


def tpsl_bounds(*, side: str, entry: float, mark: float,
                rules: "PairRules", min_ticks: int = 3) -> tuple[float, float]:
    """(lowest allowed SL, highest allowed SL) for the direction.

    The exchange compares the trigger against BOTH the mark price and the
    average entry price, so the stop must clear the worse of the two:

        LONG : sl < min(entry, mark) - min_dist
        SHORT: sl > max(entry, mark) + min_dist
    """
    d = min_stop_distance(rules, min_ticks)
    if side.upper() in ("LONG", "BUY"):
        return 0.0, min(entry, mark) - d
    return max(entry, mark) + d, float("inf")


def clamp_sl(*, side: str, sl: float, entry: float, mark: float,
             rules: "PairRules", min_ticks: int = 3,
             liq: float | None = None) -> float:
    """Force a stop-loss onto the correct side of BOTH entry and mark.

    This is THE single place a stop-loss price is made safe, and every path that
    sends an SL must go through it - the entry plan (`plan_tpsl`) and the
    running-engine updates (breakeven, trailing) alike. The previous fix only
    covered `plan_tpsl`, so the management path kept sending stops that were
    never checked against the mark price at all.

    Rounding is direction-aware: a LONG stop is floored and a SHORT stop is
    ceiled, so rounding can only ever move the stop FURTHER from the mark. Plain
    nearest-rounding can move it back across the mark by up to half a tick,
    which is exactly the 30029/30031 rejection.
    """
    p = max(0, int(rules.quote_precision))
    d = min_stop_distance(rules, min_ticks)
    tick = tick_size(rules)
    is_long = side.upper() in ("LONG", "BUY")

    # The exchange requires the stop to be STRICTLY beyond the limit, so the
    # clamp must land strictly beyond it too - a stop that sits exactly on the
    # limit is still rejected (30023/30029).
    #
    # The liquidation guard only applies when `liq` is on the side a real
    # liquidation would be (below entry for a LONG, above it for a SHORT). A
    # liq price on the wrong side is stale or belongs to the other direction;
    # honouring it would push the stop across the mark and cause the very
    # rejection this function exists to prevent.
    if is_long:
        limit = min(entry, mark) - d
        out = floor_price(min(sl, limit), p)
        if out >= limit:
            out = floor_price(limit - tick, p)
        if liq is not None and 0 < liq < entry and out <= liq:
            out = ceil_price(liq + d, p)
        return out

    limit = max(entry, mark) + d
    out = ceil_price(max(sl, limit), p)
    if out <= limit:
        out = ceil_price(limit + tick, p)
    if liq is not None and liq > entry and out >= liq:
        out = floor_price(liq - d, p)
    return out


def clamp_tp(*, side: str, tp: float, entry: float, mark: float,
             rules: "PairRules", min_ticks: int = 3) -> float:
    """Force a take-profit onto the correct side of BOTH entry and mark.

    Mirror of `clamp_sl`: a LONG TP is ceiled and a SHORT TP is floored, so
    rounding only ever moves the target further from the mark.
    """
    p = max(0, int(rules.quote_precision))
    d = min_stop_distance(rules, min_ticks)
    tick = tick_size(rules)
    if side.upper() in ("LONG", "BUY"):
        limit = max(entry, mark) + d
        out = ceil_price(max(tp, limit), p)
        if out <= limit:
            out = ceil_price(limit + tick, p)
        return out
    limit = min(entry, mark) - d
    out = floor_price(min(tp, limit), p)
    if out >= limit:
        out = floor_price(limit - tick, p)
    return out


def validate_tpsl_side(*, symbol: str, side: str, entry: float, mark: float,
                       sl: float | None, tp: float | None,
                       rules: "PairRules", min_ticks: int = 3,
                       liq: float | None = None) -> list[str]:
    """Return a list of human-readable violations (empty == valid).

    Covers the documented exchange rules:
        30020/30021  TP on the wrong side of mark
        30022/30023  SL on the wrong side of mark
        30026-30033  TP/SL on the wrong side of last price
        30024/30025  SL on the wrong side of the liquidation price
    """
    is_long = side.upper() in ("LONG", "BUY")
    d = min_stop_distance(rules, min_ticks)
    out: list[str] = []

    # A non-positive price means "not set" (PositionState defaults sl/tp to 0.0
    # for a position the engine did not open itself). Treating 0.0 as a real
    # price would flag every such position as a violation.
    if sl is not None and sl <= 0:
        sl = None
    if tp is not None and tp <= 0:
        tp = None

    if sl is not None:
        if is_long:
            limit = min(entry, mark) - d
            if sl >= limit:
                out.append(
                    f"SL {sl:.{rules.quote_precision}f} must be BELOW "
                    f"{limit:.{rules.quote_precision}f} for a LONG "
                    f"(entry={entry:.{rules.quote_precision}f}, "
                    f"mark={mark:.{rules.quote_precision}f}, "
                    f"min distance={d:.{rules.quote_precision}f})")
            if liq is not None and sl <= liq:
                out.append(
                    f"SL {sl:.{rules.quote_precision}f} must be ABOVE the "
                    f"liquidation price {liq:.{rules.quote_precision}f} for a LONG")
        else:
            limit = max(entry, mark) + d
            if sl <= limit:
                out.append(
                    f"SL {sl:.{rules.quote_precision}f} must be ABOVE "
                    f"{limit:.{rules.quote_precision}f} for a SHORT "
                    f"(entry={entry:.{rules.quote_precision}f}, "
                    f"mark={mark:.{rules.quote_precision}f}, "
                    f"min distance={d:.{rules.quote_precision}f})")
            if liq is not None and sl >= liq:
                out.append(
                    f"SL {sl:.{rules.quote_precision}f} must be BELOW the "
                    f"liquidation price {liq:.{rules.quote_precision}f} for a SHORT")

    if tp is not None:
        if is_long:
            limit = max(entry, mark) + d
            if tp <= limit:
                out.append(
                    f"TP {tp:.{rules.quote_precision}f} must be ABOVE "
                    f"{limit:.{rules.quote_precision}f} for a LONG "
                    f"(entry={entry:.{rules.quote_precision}f}, "
                    f"mark={mark:.{rules.quote_precision}f})")
        else:
            limit = min(entry, mark) - d
            if tp >= limit:
                out.append(
                    f"TP {tp:.{rules.quote_precision}f} must be BELOW "
                    f"{limit:.{rules.quote_precision}f} for a SHORT "
                    f"(entry={entry:.{rules.quote_precision}f}, "
                    f"mark={mark:.{rules.quote_precision}f})")

    return [f"{symbol} {side.upper()}: {m}" for m in out]


def validate_price_protection(price: float, mark: float, side: str,
                              scope: float) -> tuple[bool, str]:
    """Bitunix rejects orders outside the price-protection band."""
    if mark <= 0:
        return True, "ok"
    lo = mark * (1 - scope)
    hi = mark * (1 + scope)
    if not (lo <= price <= hi):
        return False, f"price {price} outside protection band [{lo:.6f}, {hi:.6f}]"
    return True, "ok"
