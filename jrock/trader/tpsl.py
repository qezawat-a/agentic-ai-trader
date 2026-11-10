"""Agent-decided TP/SL.

The user sets only TWO numbers:
    breakeven_pct            - move SL to entry once ROI >= this %
    trailing_trigger_roi_pct - start trailing once ROI >= this %

Everything else is derived by the agent from signal strength:
    * SL distance is anchored to the liquidation price: the SL sits
      `liq_sl_distance_pct` (default 50%) of the way from entry to liq.
    * TP is a multiple of the SL distance, scaled by confidence/strength:
        weak 1.2R, normal 1.8R, strong 2.5R, extreme 3.2R
      and capped so the implied ROI stays sane.
"""
from __future__ import annotations

import re

from dataclasses import dataclass

from .risk import (PairRules, Tier, TpSlSideError, clamp_sl, clamp_tp,
                   liquidation_price, min_stop_distance, pick_tier,
                   round_price, sl_from_liq_distance)

R_MULTIPLE = {"none": 1.0, "weak": 1.2, "normal": 1.8, "strong": 2.5, "extreme": 3.2}


@dataclass
class TpSlPlan:
    side: str
    entry: float
    sl: float
    tp: float
    liq: float
    sl_roi_pct: float
    tp_roi_pct: float
    rr: float
    strength: str
    breakeven_pct: float
    trailing_trigger_roi_pct: float
    tier_level: int
    mmr: float
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "side": self.side, "entry": self.entry, "sl": self.sl, "tp": self.tp,
            "liq": self.liq, "sl_roi_pct": round(self.sl_roi_pct, 2),
            "tp_roi_pct": round(self.tp_roi_pct, 2), "rr": round(self.rr, 2),
            "strength": self.strength, "breakeven_pct": self.breakeven_pct,
            "trailing_trigger_roi_pct": self.trailing_trigger_roi_pct,
            "tier_level": self.tier_level, "mmr": self.mmr, "notes": self.notes,
        }


def _check_stop_side(*, side: str, liq: float, sl: float, entry: float, ref: float,
                     d: float, rules: PairRules) -> None:
    """Refuse when the stop does not fit between the liq price and the mark side.

    At high leverage with a coarse tick there may be no room at all; refuse
    with the numbers instead of emitting an invalid stop.
    """
    if side.upper() in ("LONG", "BUY"):
        if not (liq < sl < min(entry, ref)):
            raise TpSlSideError(
                f"{rules.symbol} LONG: no valid stop-loss exists. The stop must "
                f"sit between the liquidation price {liq:.{rules.quote_precision}f} "
                f"and {min(entry, ref):.{rules.quote_precision}f} "
                f"(entry={entry:.{rules.quote_precision}f}, "
                f"mark={ref:.{rules.quote_precision}f}), keeping "
                f"{d:.{rules.quote_precision}f} clear of both. "
                f"Lower the leverage or widen the tick.")
    else:
        if not (max(entry, ref) < sl < liq):
            raise TpSlSideError(
                f"{rules.symbol} SHORT: no valid stop-loss exists. The stop must "
                f"sit between {max(entry, ref):.{rules.quote_precision}f} and the "
                f"liquidation price {liq:.{rules.quote_precision}f} "
                f"(entry={entry:.{rules.quote_precision}f}, "
                f"mark={ref:.{rules.quote_precision}f}), keeping "
                f"{d:.{rules.quote_precision}f} clear of both. "
                f"Lower the leverage or widen the tick.")


def _plan_tp(*, side: str, entry: float, ref: float, sl_dist: float,
             strength: str, confidence: float, is_long: bool, rules: PairRules,
             min_stop_ticks: int) -> float:
    """The take-profit price: a multiple of the stop distance, clamped."""
    mult = R_MULTIPLE.get(strength, 1.8)
    mult *= 0.85 + (max(0.0, min(100.0, confidence)) / 100.0) * 0.3

    tp_dist = sl_dist * mult
    tp = entry + tp_dist if is_long else entry - tp_dist
    tp = round_price(tp, rules.quote_precision)

    # --- clamp the TP to the correct side of BOTH entry and mark.
    tp = clamp_tp(side=side, tp=tp, entry=entry, mark=ref, rules=rules,
                  min_ticks=min_stop_ticks)
    return tp


def _roi_pct(p: float, entry: float, side: str, leverage: int) -> float:
    """The ROI in % that price p realizes against the entry."""
    raw = (p - entry) / entry if side.upper() in ("LONG", "BUY") else (entry - p) / entry
    return raw * leverage * 100.0


def plan_tpsl(*, side: str, entry: float, leverage: int, notional: float,
              strength: str, confidence: float, tiers: list[Tier],
              rules: PairRules, liq_sl_distance_pct: float = 50.0,
              breakeven_pct: float = 0.6,
              trailing_trigger_roi_pct: float = 25.0,
              mark: float | None = None,
              min_stop_ticks: int = 3) -> TpSlPlan:
    """Build a TP/SL plan that is guaranteed to sit on the correct side.

    `mark` is the exchange mark price. The exchange validates the trigger
    against BOTH the mark price and the average entry price, so a stop that is
    only correct relative to `entry` can still be rejected (30029/30031) when
    the two differ. When `mark` is supplied the stop is clamped to clear the
    worse of the two by at least `min_stop_ticks` ticks.
    """
    tier = pick_tier(tiers, notional)
    mmr = tier.maintenance_margin_rate
    liq = liquidation_price(entry, leverage, side, mmr)

    is_long = side.upper() in ("LONG", "BUY")
    d = min_stop_distance(rules, min_stop_ticks)
    ref = mark if (mark is not None and mark > 0) else entry

    sl = sl_from_liq_distance(entry, liq, side, liq_sl_distance_pct)
    sl = round_price(sl, rules.quote_precision)

    # --- clamp the SL to the correct side of BOTH entry and mark.
    # A high-leverage stop can land within a tick of the mark price; rounding
    # then collapses it onto the mark and the exchange rejects the order.
    # clamp_sl is shared with the running-engine path (breakeven/trailing) so
    # there is exactly one implementation of this rule.
    sl = clamp_sl(side=side, sl=sl, entry=entry, mark=ref, rules=rules,
                  min_ticks=min_stop_ticks, liq=liq)

    # --- feasibility: the stop must fit BETWEEN the liquidation price and the
    # mark-side limit.
    _check_stop_side(side=side, liq=liq, sl=sl, entry=entry, ref=ref, d=d,
                     rules=rules)

    sl_dist = abs(entry - sl)
    tp = _plan_tp(side=side, entry=entry, ref=ref, sl_dist=sl_dist,
                  strength=strength, confidence=confidence, is_long=is_long,
                  rules=rules, min_stop_ticks=min_stop_ticks)

    sl_roi = _roi_pct(sl, entry, side, leverage)
    tp_roi = _roi_pct(tp, entry, side, leverage)
    rr = abs(tp_roi / sl_roi) if sl_roi else 0.0

    notes = []
    if sl_roi and abs(sl_roi) > 60:
        notes.append(f"SL ROI {sl_roi:.1f}% is wide - consider lower leverage")
    if tp_roi > 400:
        notes.append("TP ROI very high; may take long to fill")

    return TpSlPlan(
        side=side.upper(), entry=entry, sl=sl, tp=tp, liq=liq,
        sl_roi_pct=sl_roi, tp_roi_pct=tp_roi, rr=rr, strength=strength,
        breakeven_pct=breakeven_pct,
        trailing_trigger_roi_pct=trailing_trigger_roi_pct,
        tier_level=tier.level, mmr=mmr, notes="; ".join(notes),
    )


@dataclass
class PositionState:
    position_id: str
    symbol: str
    side: str
    entry: float
    qty: float
    leverage: int
    sl: float
    tp: float
    liq: float
    breakeven_done: bool = False
    trailing_active: bool = False
    trailing_stop: float | None = None
    opened_ts: float = 0.0
    peak_roi: float = 0.0


# ------------------------------------------------------------ PARTIAL TP/SL
# The PARTIAL method is the scale-out ladder from the official article
# "Bitunix Futures Positions: Guide to Four TP/SL Methods": several triggers,
# each closing a share of the position. Each rung is a NATIVE
# /tpsl/place_order carrying tpQty, parked at entry - so the exchange enforces
# every rung even if this engine is dead, and no manage-loop bookkeeping
# (fired levels, backstop re-parking) exists at all. The stop is never
# laddered: scaling out of a loser is just being wrong more slowly.

@dataclass(frozen=True)
class LadderRung:
    """One rung of the partial ladder: `share` % of the position at `r` * risk."""

    share: float
    r: float


def parse_partial_ladder(spec: str) -> list[LadderRung]:
    """Parse the share@R ladder spec, e.g. "40@1,35@2,25@3".

    One knob, one meaning: each pair is `share@R` - close `share`% of the
    position when price runs R times the entry-to-stop distance. Rules
    (mirroring the documented format exactly):
      * share > 0 and share <= 100
      * R > 0
      * shares sum to <= 100; whatever is left is the runner
      * R must increase left to right (rung 2 is farther than rung 1)
    Empty/blank spec = no ladder (the caller falls back to the POSITION method
    or to no TP at all when there is no signal-strength plan).
    """
    raw = (spec or "").strip()
    if not raw:
        return []
    rungs: list[LadderRung] = []
    for part in raw.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*@\s*(\d+(?:\.\d+)?)", part)
        if not m:
            raise PartialLadderError(
                f'bad ladder rung "{part}" - expected "share@R", e.g. "30@1.5"')
        share, r = float(m.group(1)), float(m.group(2))
        if not (0 < share <= 100):
            raise PartialLadderError(f"ladder share must be 0-100, got {share}")
        if not (r > 0):
            raise PartialLadderError(f"ladder R multiple must be > 0, got {r}")
        rungs.append(LadderRung(share=share, r=r))
    total = sum(x.share for x in rungs)
    if total > 100.0001:
        raise PartialLadderError(
            f"ladder shares sum to {total:g}%, must be <= 100")
    for prev, cur in zip(rungs, rungs[1:]):
        if cur.r <= prev.r:
            raise PartialLadderError(
                f"ladder targets must increase: {prev.r}R then {cur.r}R")
    return rungs


class PartialLadderError(ValueError):
    """The partial ladder spec is malformed."""


def rung_target(side: str, entry: float, sl_dist: float, r: float) -> float:
    """The absolute price of a rung at R × the entry-to-stop distance."""
    is_long = side.upper() in ("LONG", "BUY")
    return entry + sl_dist * r if is_long else entry - sl_dist * r


def should_breakeven(state: PositionState, roi: float, breakeven_pct: float) -> bool:
    return (not state.breakeven_done) and roi >= breakeven_pct


def should_trail(state: PositionState, roi: float, trigger_roi_pct: float) -> bool:
    return roi >= trigger_roi_pct


def next_trailing_stop(state: PositionState, price: float, atr_value: float,
                       min_step_pct: float = 0.15) -> float | None:
    """Ratchet the trailing stop; never loosen it."""
    if atr_value <= 0:
        atr_value = price * 0.002
    if state.side.upper() in ("LONG", "BUY"):
        candidate = price - atr_value * 1.5
        if state.trailing_stop is None or candidate > state.trailing_stop * (1 + min_step_pct / 100):
            return candidate
    else:
        candidate = price + atr_value * 1.5
        if state.trailing_stop is None or candidate < state.trailing_stop * (1 - min_step_pct / 100):
            return candidate
    return None


def mid_management_action(state: PositionState, roi: float, reversal: bool,
                          reversal_confidence: float,
                          min_reversal_conf: float = 70.0) -> str:
    """Returns: "hold" | "close_reversal" | "tighten"."""
    if reversal and reversal_confidence >= min_reversal_conf:
        return "close_reversal"
    if roi >= 30:
        return "tighten"
    return "hold"
