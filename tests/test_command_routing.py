"""Regression tests for the command-bot behaviour fix.

The reported symptom: the agent did nothing unless given an explicit command,
and in ordinary chat it did not understand the user. Two defects produced it:

1. **Greedy command-prefix matching.** `TraderEngine.looks_like_command` was
   `any(low.startswith(p) for p in COMMAND_PREFIXES)`, so a natural sentence
   that merely STARTED with a command word was swallowed by the engine and
   answered with "Unknown engine command" instead of being understood. The
   canonical case: "leverage is too high, what should I do?" starts with
   "leverage" and was treated as the `leverage` setter.

2. **A single-word guard that dropped greetings.** `nlu.parse` returned None
   for any input shorter than two words, so "hi", "thanks", "thx" ... matched
   no rule and the agent answered nothing at all.

Both are pinned here, together with the requirement that REAL commands still
work exactly as before.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jrock import nlu
from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.trader.engine import TraderEngine


def _engine(tmp_path: Path) -> TraderEngine:
    return TraderEngine(Settings(), data_dir=tmp_path)


def _agent(tmp_path: Path) -> JRockAgent:
    s = Settings()
    s.data_dir  # noqa: B018  (ensures dir exists)
    a = JRockAgent(s)
    a.engine.data_dir = tmp_path
    return a


def _run(coro):
    return asyncio.run(coro)


# --- 1. natural sentences are NOT engine commands ---------------------------

#: Sentences that merely start with (or contain) a command word. None of them
#: is a command; each must fall through to the NLU layer / the model.
NOT_COMMANDS = [
    "leverage is too high, what should I do?",
    "risk is too high for me",
    "symbols are not working",
    "settings look wrong",
    "status of my account",
    "report is not working",
    "scan seems stuck",
    "engine is not responding",
    "margin mode looks wrong",
    "position mode is confusing",
    "timeframes are too short",
    "min confidence is too low",
    "trailing trigger is too tight",
    "breakeven threshold seems high",
    "ladder is not what I expected",
    "close position please",          # a request, not the `close position X` cmd
    "symbols please",
    "leverage please",
]


@pytest.mark.parametrize("line", NOT_COMMANDS)
def test_natural_sentence_is_not_an_engine_command(tmp_path, line):
    assert _engine(tmp_path).looks_like_command(line) is False


# --- 2. real commands still work --------------------------------------------

REAL_COMMANDS = [
    "status",
    "pnl",
    "settings",
    "settings trade",
    "leverage 10",
    "leverage to 20",
    "risk 1.5",
    "risk pct 2",
    "max open positions 3",
    "scans interval 60",
    "report interval 30",
    "breakeven threshold 0.5",
    "trailing trigger 1.0",
    "liq sl distance 50",
    "min agreeing 3",
    "min confidence 80",
    "tf min confidence 60",
    "symbols BTCUSDT,ETHUSDT",
    "symbol BTCUSDT",
    "margin mode ISOLATION",
    "margin mode CROSS",
    "position mode HEDGE",
    "position mode ONE_WAY",
    "tpsl method POSITION",
    "tpsl method PARTIAL",
    "partial ladder 40@1,35@2,25@3",
    "partial ladder off",
    "close position BTCUSDT",
    "engine on",
    "engine off",
    "scan on",
    "scan off",
    "report on",
    "report off",
    "start trading",
    "stop trading",
    "autotrade on",
    "autotrade off",
    "timeframes 1m,5m,15m",
    "reversal true",
    "reversal false",
    "universe rank GAINERS",
    "universe sizing 50",
    "universe volume 1000000",
    "universe rank auto",
    "universe rank off",
]


@pytest.mark.parametrize("line", REAL_COMMANDS)
def test_real_command_is_still_a_command(tmp_path, line):
    assert _engine(tmp_path).looks_like_command(line) is True


def test_help_is_not_an_engine_command(tmp_path):
    """`help` is an AGENT command, not an engine one - the engine must not
    claim it, or the agent's own help would be shadowed."""
    assert _engine(tmp_path).looks_like_command("help") is False


# --- 3. the NLU layer understands ordinary conversation ---------------------

#: (sentence, expected intent name) - each must resolve WITHOUT a command.
UNDERSTOOD = [
    ("leverage is too high, what should I do?", "leverage_read"),
    ("risk is too high for me", "risk_read"),
    ("symbols are not working", "symbols"),
    ("settings look wrong", "settings"),
    ("status of my account", "status"),
    ("tell me about my account", "status"),
    ("is the engine running", "status"),
    ("how much money do I have", "balance"),
    ("what is my buying power", "balance"),
    ("can you show me my balance", "balance"),
    ("i want to see my open trades", "positions"),
    ("any open positions", "positions"),
    ("how are my trades doing", "pnl"),
    ("how much have i made", "pnl"),
    ("whats my pnl", "pnl"),
    ("do i have any orders", "orders"),
    ("whats the price of btc", "price"),
    ("what do you think about ETHUSDT", "price"),
    ("whats the funding on BTCUSDT", "funding"),
    ("show me the tpsl", "tpsl_read"),
    ("what is my leverage", "leverage_read"),
    ("show me my risk setting", "settings"),
    ("explain taker fee rate", "explain_setting"),
    ("what is taker fee rate", "explain_setting"),
    ("why did my order fail", "why_failed"),
    ("help me out", "help"),
    ("i need help", "help"),
    ("what can you do for me", "help"),
    ("turn off the bot", "autotrade_off"),
    ("pause trading", "autotrade_off"),
    ("stop everything", "autotrade_off"),
    ("resume trading", "autotrade_on"),
    ("hi there", "smalltalk"),
    ("whats going on", "smalltalk"),
    ("good morning", "smalltalk"),
    ("are you there", "smalltalk"),
]


@pytest.mark.parametrize("line,name", UNDERSTOOD)
def test_ordinary_sentence_resolves_to_an_intent(line, name):
    r = nlu.parse(line)
    assert isinstance(r, nlu.Intent), f"{line!r} -> {r!r}"
    assert r.name == name, f"{line!r} -> {r.name}, expected {name}"


# --- 4. a single-word greeting is understood, not dropped -------------------

@pytest.mark.parametrize("word", ["hi", "hello", "hey", "thanks", "thx", "ty",
                                  "cheers", "yo"])
def test_single_word_greeting_is_understood(word):
    r = nlu.parse(word)
    assert isinstance(r, nlu.Intent), f"{word!r} -> {r!r}"
    assert r.name == "smalltalk"


def test_single_word_non_greeting_still_falls_through():
    """The guard must still let a bare word that is NOT a greeting fall through
    to the existing bare-word table / the model."""
    assert nlu.parse("BTCUSDT") is None
    assert nlu.parse("xyzzy") is None


# --- 5. end to end: a greeting gets a real reply, not silence ---------------

def test_greeting_gets_a_reply_not_silence(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("hi"))
    assert out.strip(), "a greeting must never produce an empty reply"
    assert "listening" in out.lower() or "help" in out.lower()


def test_natural_question_does_not_say_unknown_command(tmp_path):
    """The exact reported failure: a normal sentence answered with
    'Unknown engine command'."""
    a = _agent(tmp_path)
    out = _run(a.handle("leverage is too high, what should I do?"))
    assert "unknown engine command" not in out.lower()


def test_real_command_still_routes_to_the_engine(tmp_path):
    a = _agent(tmp_path)
    out = _run(a.handle("leverage 10"))
    assert "unknown engine command" not in out.lower()


# ==========================================================================
# The switches the operator types must not ask for a confirmation
# ==========================================================================

def test_typed_switches_run_without_a_confirmation_prompt(tmp_path):
    """"auto accept on" and "autotrade on" are switches, not orders.

    "auto accept on" (without the word "edit") used to fall through to the
    MODEL, which answered with a question of its own and then asked again for
    the "yes" - an endless loop for a one-word toggle.
    """
    a = _agent(tmp_path)
    out = _run(a.handle("auto accept on"))
    assert "auto accept edit = True" in out, out
    assert a.perms.auto_accept_edit is True

    out = _run(a.handle("autotrade on"))
    assert "Autotrade ON" in out, out
    assert a.engine.state.autotrade is True
    assert a._pending_action is None, "no confirmation may be pending"

    out = _run(a.handle("auto accept off"))
    assert "auto accept edit = False" in out, out


def test_a_yes_after_the_prompt_runs_the_action(tmp_path):
    """The confirm loop was the complaint: "yes" must resolve the prompt.

    The typed command runs directly; the SENTENCE asks once, and the answer
    "yes do it" must resolve it. Before, only an exact "yes" worked, so the
    agent answered "still waiting" and the user said yes again, forever.
    """
    a = _agent(tmp_path)
    out = _run(a.handle("close my BTCUSDT position"))
    assert "About to:" in out, out
    assert a._pending_action is not None
    out = _run(a.handle("yes do it"))
    assert "Still waiting" not in out, out
    assert a._pending_action is None


def test_reset_clears_the_conversation_memory_only(tmp_path):
    """/reset is in the Menu, so it must do something.

    The conversation memory (the turns the model is given as context) is
    cleared; the long-term memory (/memory) is a different store and is NOT
    touched.
    """
    a = _agent(tmp_path)
    a._history.append(("user", "hello"))
    a._history.append(("assistant", "hi"))
    out = _run(a.handle("/reset"))
    assert "conversation memory cleared" in out, out
    assert a._history == []


def test_settings_show_the_margin_target_and_the_cooldown(tmp_path):
    """The setting that decides the size must be on screen.

    MARGIN_AMOUNT_PCT is the sizing AUTHORITY when it is set (risk_pct is then
    ignored) - so /settings has to show it, and say which one is in force.
    The re-entry cooldown is new and belongs there too.
    """
    a = _agent(tmp_path)
    a.engine.state.margin_amount_pct = 35.0
    a.engine.state.risk_pct = 0.0
    out = _run(a.handle("/settings"))
    assert "margin per trade: 35% of the available balance" in out, out
    assert "not applied while the margin target is set" in out, out
    assert "re-entry cooldown:" in out, out

    # with the margin target off, risk is the sizing authority
    a.engine.state.margin_amount_pct = 0.0
    a.engine.state.risk_pct = 1.5
    out = _run(a.handle("/settings"))
    assert "margin per trade: off" in out, out
    assert "risk per trade: 1.5%" in out, out


# Every field of EngineState that the operator can set, and the text in
# /settings that must carry it. A field that is NOT in this table is a field
# nobody can see - and a new field breaks the test until it is added here.
SETTINGS_VISIBLE = {
    "engine_on": "engine_on=",
    "scan_on": "scan_on=",
    "autotrade": "autotrade=",
    "report_on": "report_on=",
    "mid_management": "mid_management=",
    "scan_interval": "scan interval:",
    "report_interval": "report interval:",
    "reentry_cooldown_sec": "re-entry cooldown:",
    "reversal": "reversal:",
    "reversal_confidence": "reversal confidence:",
    "symbols": "symbols:",
    "leverage": "leverage:",
    "margin_mode": "margin mode:",
    "position_mode": "position mode:",
    "max_open_positions": "max open positions:",
    "risk_pct": "risk per trade:",
    "margin_amount_pct": "margin per trade:",
    "breakeven_pct": "breakeven:",
    "trailing_trigger_roi_pct": "trailing trigger:",
    "liq_sl_distance_pct": "liq SL distance:",
    "min_agreeing": "min agreeing strategies:",
    "timeframes": "timeframes:",
    "min_confidence": "min confidence:",
    "tf_min_confidence": "tf min confidence:",
    "trend_gate": "trend gate:",
    "trend_gate_timeframe": "trend gate timeframe:",
    "tpsl_method": "tpsl method:",
    "partial_tp_ladder": "partial ladder:",
    "account_tp_usdt": "account TP/SL",
    "account_sl_usdt": "account TP/SL",
    "min_stop_ticks": "min stop ",
    "universe_rank": "universe rank:",
    "universe_sizing": "universe sizing:",
    "min_24h_volume_usdt": "min 24h volume:",
}

# The fields that are engine internals, not operator settings.
SETTINGS_INTERNAL = {
    "last_scan_ts", "last_report_ts", "scans", "errors", "notes",
    "manage_last_ok",
}


def test_settings_shows_every_settable_field(tmp_path):
    """A setting the operator cannot see is a setting they cannot verify.

    This is the gate that keeps /settings complete: when a new field is added
    to EngineState, this test fails until the field is either shown or
    explicitly listed as an internal.
    """
    from jrock.trader.engine import EngineState

    fields = {f.name for f in EngineState.__dataclass_fields__.values()}
    uncovered = fields - set(SETTINGS_VISIBLE) - SETTINGS_INTERNAL
    assert not uncovered, f"new EngineState fields are not shown in /settings: {sorted(uncovered)}"
    stale = set(SETTINGS_VISIBLE) - fields
    assert not stale, f"SETTINGS_VISIBLE names a field that no longer exists: {sorted(stale)}"

    a = _agent(tmp_path)
    out = _run(a.handle("/settings"))
    for field, needle in SETTINGS_VISIBLE.items():
        assert needle in out, f"{field} is not visible in /settings (looked for {needle!r})"


# ---------------------------------------------------------------- the aliases
# The wording an operator actually types. Each one used to fall through to
# the model (which failed, or invented a confirmation) and leave the setting
# unchanged - so the operator saw the old value and thought it had reset.
SETTING_ALIASES = [
    ("risk_pct 2.0",        "risk_pct", 2.0),
    ("risk pct 2.0",        "risk_pct", 2.0),
    ("risk per trade 2.0",  "risk_pct", 2.0),
    ("margin_amount_pct 35", "margin_amount_pct", 35.0),
    ("margin per trade 35", "margin_amount_pct", 35.0),
    ("reversal confidence 85", "reversal_confidence", 85.0),
    ("reversal_confidence 85", "reversal_confidence", 85.0),
    ("breakeven 12",        "breakeven_pct", 12.0),
    ("scan interval 15",    "scan_interval", 15),
    ("reentry cooldown 120", "reentry_cooldown_sec", 120),
    ("min stop tick 4",     "min_stop_ticks", 4),
    ("min stop ticks 6",    "min_stop_ticks", 6),
    ("min stop 4",          "min_stop_ticks", 4),
    ("set min stop tick 4", "min_stop_ticks", 4),
]


@pytest.mark.parametrize("line,field,value", SETTING_ALIASES)
def test_a_setting_alias_reaches_the_engine(tmp_path, line, field, value):
    """An alias must set the value, not fall through to the model."""
    a = _agent(tmp_path)
    out = _run(a.handle(line))
    assert "model" not in out.lower()[:80], f"{line} went to the model: {out}"
    assert getattr(a.engine.state, field) == value, (line, out)


def test_the_aliases_do_not_swallow_a_question(tmp_path):
    """The alias grammar must stay narrow: a question is still a question."""
    a = _agent(tmp_path)
    for q in ("is the leverage too high?", "what does margin per trade mean?",
              "should I set reversal confidence to 90?"):
        assert not a.engine.looks_like_command(q), q
