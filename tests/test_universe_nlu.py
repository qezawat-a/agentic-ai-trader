"""Natural-language routing for the universe commands.

The universe layer is only useful if a normal sentence reaches it. These tests
pin the phrasings a user actually types, and - just as important - that the
existing command strings still route identically (no regression).
"""
from __future__ import annotations

import pytest

from jrock.nlu import Clarification, parse


def _cmd(text: str) -> str | None:
    r = parse(text)
    if r is None or isinstance(r, Clarification):
        return None
    return r.command


# ------------------------------------------------------------- show
@pytest.mark.parametrize("text", [
    "show me the universe",
    "what is the universe",
    "which coins are you scanning",
    "what symbols are you trading",
    "show the universe",
])
def test_show_universe_phrasings(text):
    assert _cmd(text) == "universe"


# ------------------------------------------------------------- rank
@pytest.mark.parametrize("text,mode", [
    ("rank by gainers", "GAINERS"),
    ("rank by losers", "LOSERS"),
    ("rank by movers", "MOVERS"),
    ("rank by volume", "VOLUME"),
    ("sort by gainers", "GAINERS"),
    ("gainers ranking", "GAINERS"),
    ("set the universe rank to movers", "MOVERS"),
])
def test_rank_phrasings(text, mode):
    assert _cmd(text) == f"universe rank {mode}"


# ------------------------------------------------------------- size
@pytest.mark.parametrize("text,n", [
    ("set universe size to 10", 10),
    ("universe size 25", 25),
    ("how many pairs in the universe, set it to 5", 5),
])
def test_size_phrasings(text, n):
    assert _cmd(text) == f"universe size {n}"


# ------------------------------------------------------------- min volume
def test_min_volume_understands_millions():
    """'20 million' must be 20_000_000, not 20 - a 20 USDT floor is no floor."""
    assert _cmd("only scan coins with at least 20 million volume") == \
        "universe min volume 20000000.0"


def test_min_volume_understands_plain_numbers():
    assert _cmd("minimum volume 5000000") == "universe min volume 5000000.0"


# ------------------------------------------------------------- auto / off
@pytest.mark.parametrize("text", [
    "turn on auto universe",
    "use all pairs",
    "scan all pairs",
    "universe auto",
    "use the full pair list",
])
def test_auto_phrasings(text):
    assert _cmd(text) == "universe auto"


@pytest.mark.parametrize("text", [
    "universe off",
    "turn off the universe",
    "stop the universe",
    "use manual symbols",
])
def test_off_phrasings(text):
    assert _cmd(text) == "universe off"


# ------------------------------------------------------------- safety
def test_universe_auto_is_destructive_and_must_be_confirmed():
    r = parse("turn on auto universe")
    assert r is not None and r.destructive is True


def test_universe_off_is_not_destructive():
    r = parse("universe off")
    assert r is not None and r.destructive is False


def test_show_universe_is_not_destructive():
    r = parse("show me the universe")
    assert r is not None and r.destructive is False


# ------------------------------------------------------------- no regression
@pytest.mark.parametrize("text,expected", [
    ("show me my trading settings", "settings trade"),
    ("what is my balance", "balance"),
    ("show my positions", "positions"),
    ("what are my open orders", "orders"),
    ("turn off autotrade", "stop trading"),
    ("set leverage to 5x", "leverage 5"),
])
def test_existing_intents_still_route(text, expected):
    assert _cmd(text) == expected


def test_a_plain_symbol_list_question_is_not_hijacked_by_universe():
    """'show me the symbols' is a symbol list, not the universe."""
    assert _cmd("show me the symbols") == "symbols"


def test_unrelated_sentence_still_falls_through_to_the_model():
    assert parse("what is the weather in tokyo") is None
