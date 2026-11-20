import pytest

from jrock.trader.risk import PairRules, Tier
from jrock.trader.tpsl import (PositionState, mid_management_action,
                               next_trailing_stop, plan_tpsl, should_breakeven,
                               should_trail)

RULES = PairRules("BTCUSDT", base_precision=4, quote_precision=2)
TIERS = [Tier(1, 0, 50000, 125, 0.004), Tier(2, 50000, 200000, 100, 0.005)]


def test_plan_long_sl_below_entry_tp_above():
    p = plan_tpsl(side="LONG", entry=100.0, leverage=10, notional=1000,
                  strength="normal", confidence=75, tiers=TIERS, rules=RULES,
                  sl_distance_pct=5.0)
    assert p.sl < 100 < p.tp
    assert p.liq < p.sl < 100
    assert p.rr > 0


def test_plan_short_sl_above_entry_tp_below():
    p = plan_tpsl(side="SHORT", entry=100.0, leverage=10, notional=1000,
                  strength="normal", confidence=75, tiers=TIERS, rules=RULES,
                  sl_distance_pct=5.0)
    assert p.tp < 100 < p.sl
    assert p.liq > p.sl > 100


def test_sl_is_the_configured_distance_from_entry():
    p = plan_tpsl(side="LONG", entry=100.0, leverage=10, notional=1000,
                  strength="normal", confidence=75, tiers=TIERS, rules=RULES,
                  sl_distance_pct=5.0)
    assert p.sl == pytest.approx(95.0, rel=1e-6)


def test_stronger_signal_gives_bigger_tp():
    weak = plan_tpsl(side="LONG", entry=100, leverage=10, notional=1000,
                     strength="weak", confidence=70, tiers=TIERS, rules=RULES,
                     sl_distance_pct=5.0)
    strong = plan_tpsl(side="LONG", entry=100, leverage=10, notional=1000,
                       strength="strong", confidence=90, tiers=TIERS, rules=RULES,
                       sl_distance_pct=5.0)
    assert strong.tp > weak.tp


def test_auto_sl_distance_comes_from_signal_strength():
    """`sl distance auto`: the agent picks the stop, not the operator.

    Stronger signals get a tighter stop - the entry is more reliable - and the
    TP is still scaled off that stop, so one setting drives both sides.
    """
    weak = plan_tpsl(side="LONG", entry=100.0, leverage=10, notional=1000,
                     strength="weak", confidence=65, tiers=TIERS, rules=RULES,
                     sl_distance_auto=True)
    strong = plan_tpsl(side="LONG", entry=100.0, leverage=10, notional=1000,
                       strength="strong", confidence=90, tiers=TIERS,
                       rules=RULES, sl_distance_auto=True)
    assert weak.sl == pytest.approx(97.0, rel=1e-6)      # 3% stop
    assert strong.sl == pytest.approx(98.5, rel=1e-6)    # 1.5% stop
    assert strong.sl > weak.sl                           # tighter when stronger
    # a stop exists, so the TP the R-multiple derives from it exists too
    assert weak.tp > 100 and strong.tp > 100


def test_auto_wins_over_a_stale_manual_distance():
    """A leftover sl_distance_pct must not keep deciding after `sl distance auto`.

    Otherwise the operator types the command, sees an acknowledgement, and the
    old number silently still owns the stop.
    """
    p = plan_tpsl(side="LONG", entry=100.0, leverage=10, notional=1000,
                  strength="strong", confidence=90, tiers=TIERS, rules=RULES,
                  sl_distance_pct=35.0, sl_distance_auto=True)
    assert p.sl == pytest.approx(98.5, rel=1e-6)         # 1.5%, not 35%


def test_auto_with_no_signal_still_places_no_stop():
    """`none` has no strength to derive from, so nothing is invented."""
    p = plan_tpsl(side="LONG", entry=100.0, leverage=10, notional=1000,
                  strength="none", confidence=0, tiers=TIERS, rules=RULES,
                  sl_distance_auto=True)
    assert p.sl == 0.0
    assert p.tp == 0.0


def test_manual_distance_is_unchanged_when_auto_is_off():
    p = plan_tpsl(side="LONG", entry=100.0, leverage=10, notional=1000,
                  strength="normal", confidence=75, tiers=TIERS, rules=RULES,
                  sl_distance_pct=5.0, sl_distance_auto=False)
    assert p.sl == pytest.approx(95.0, rel=1e-6)


def test_higher_tier_raises_mmr():
    p1 = plan_tpsl(side="LONG", entry=100, leverage=10, notional=1000,
                   strength="normal", confidence=75, tiers=TIERS, rules=RULES)
    p2 = plan_tpsl(side="LONG", entry=100, leverage=10, notional=100000,
                   strength="normal", confidence=75, tiers=TIERS, rules=RULES)
    assert p2.mmr > p1.mmr
    assert p2.tier_level == 2


def test_breakeven_trigger():
    st = PositionState("1", "BTCUSDT", "LONG", 100, 1, 10, 95, 110, 90)
    assert not should_breakeven(st, 0.5, 0.6)
    assert should_breakeven(st, 0.7, 0.6)
    st.breakeven_done = True
    assert not should_breakeven(st, 5.0, 0.6)


def test_trailing_trigger():
    st = PositionState("1", "BTCUSDT", "LONG", 100, 1, 10, 95, 110, 90)
    assert not should_trail(st, 20, 25)
    assert should_trail(st, 30, 25)


def test_trailing_stop_ratchets_up_only():
    st = PositionState("1", "BTCUSDT", "LONG", 100, 1, 10, 95, 110, 90)
    s1 = next_trailing_stop(st, 110, 1.0)
    assert s1 is not None
    st.trailing_stop = s1
    s2 = next_trailing_stop(st, 111, 1.0)
    assert s2 is not None and s2 > s1
    # price falls -> no loosening
    assert next_trailing_stop(st, 105, 1.0) is None


def test_trailing_stop_short_ratchets_down():
    st = PositionState("1", "BTCUSDT", "SHORT", 100, 1, 10, 105, 90, 110)
    s1 = next_trailing_stop(st, 90, 1.0)
    assert s1 is not None
    st.trailing_stop = s1
    s2 = next_trailing_stop(st, 89, 1.0)
    assert s2 is not None and s2 < s1


def test_mid_management_reversal_closes():
    st = PositionState("1", "BTCUSDT", "LONG", 100, 1, 10, 95, 110, 90)
    assert mid_management_action(st, 10, True, 80, 70) == "close_reversal"


def test_mid_management_tightens_a_big_winner():
    st = PositionState("1", "BTCUSDT", "LONG", 100, 1, 10, 95, 110, 90)
    # the scale-out shares live in the native PARTIAL rungs now; a +60% ROI
    # position is only tightened here
    assert mid_management_action(st, 60, False, 0, 70) == "tighten"


def test_mid_management_hold():
    st = PositionState("1", "BTCUSDT", "LONG", 100, 1, 10, 95, 110, 90)
    assert mid_management_action(st, 5, False, 0, 70) == "hold"
