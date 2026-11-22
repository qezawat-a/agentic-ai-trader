"""Pins for universe auto-ranking.

The feature shipped unfinished, and the defects compounded:

- the commands `/help` advertised (`universe rank auto`, `universe rank
  off`, and the correct spelling `LOSERS`) did not run: the generic
  `universe rank <method>` branch caught auto/off first, `_is_command_argument`
  accepted only the misspelling `LOOSERS`, and `auto` zeroed `universe_sizing`
  - the very setting that turns ranking off;
- `UNIVERSE_RANK` was read with the float reader, so every method name fell
  back to `""` and the feature could never start from `.env`;
- GAINERS/LOSERS/MOVERS sorted by ABSOLUTE price change, so a $60 move in BTC
  outranked a +30% move in a cheap pair and the "movers" universe was really
  just the most expensive symbols.

These tests are the guard against any of that coming back.
"""
from __future__ import annotations

import asyncio

import pytest

from jrock import nlu
from jrock.agent import HELP
from jrock.config import load_settings
from jrock.trader.engine import TraderEngine
from jrock.config import Settings


def _engine(tmp_path) -> TraderEngine:
    return TraderEngine(Settings(), data_dir=tmp_path)


def _run(coro):
    return asyncio.run(coro)


def _ticker(sym: str, last: float, open_: float,
            quote_vol: float = 1_000_000.0) -> dict:
    return {
        "symbol": sym,
        "lastPrice": str(last),
        "markPrice": str(last),
        "open": str(open_),
        "baseVol": "1",
        "quoteVol": str(quote_vol),
    }


ROWS = [
    # +0.1% but a large absolute move, and the most liquid pair
    _ticker("BTCUSDT", 60060.0, 60000.0, quote_vol=9_000_000_000),
    # +30% on a cheap pair - the real gainer
    _ticker("PEPEUSDT", 0.0000013, 0.000001, quote_vol=1_000_000),
    # -5%
    _ticker("ETHUSDT", 1900.0, 2000.0, quote_vol=500_000_000),
    # +400% but below the 24h volume floor
    _ticker("ILLIQUSDT", 5.0, 1.0, quote_vol=10),
]


def _ranked(engine: TraderEngine, method: str, sizing: int = 10,
            rows: list[dict] | None = None) -> list[str]:
    engine.state.universe_rank = method
    engine.state.universe_sizing = sizing
    engine.state.min_24h_volume_usdt = 1000.0

    async def fake_get_tickers():
        return rows if rows is not None else ROWS

    engine.client.get_tickers = fake_get_tickers
    return _run(engine._universe_symbols())


# --------------------------------------------------------------- the commands

@pytest.mark.parametrize("line", [
    "universe rank GAINERS",
    "universe rank LOSERS",
    "universe rank VOLUME",
    "universe rank MOVERS",
    "universe rank auto",
    "universe rank off",
    "universe sizing 20",
    "universe volume 1000000",
])
def test_every_advertised_command_is_recognised(tmp_path, line):
    assert _engine(tmp_path).looks_like_command(line) is True


def test_losers_works_in_the_correct_spelling(tmp_path):
    e = _engine(tmp_path)
    out = _run(e.command("universe rank LOSERS"))
    assert e.state.universe_rank == "LOSERS", out
    assert "LOOSERS" not in out, out


def test_the_old_misspelling_is_still_accepted(tmp_path):
    """A command shipped in /help must not stop working; store canonical."""
    e = _engine(tmp_path)
    out = _run(e.command("universe rank LOOSERS"))
    assert e.state.universe_rank == "LOSERS", out
    assert "LOOSERS" not in out, out


def test_auto_turns_ranking_on_without_zeroing_the_size(tmp_path):
    """`auto` used to set universe_sizing = 0, which is how ranking is turned
    OFF - the command disabled the feature it claimed to enable."""
    e = _engine(tmp_path)
    e.state.universe_sizing = 20
    out = _run(e.command("universe rank auto"))
    assert e.state.universe_rank == "VOLUME", out
    assert e.state.universe_sizing == 20, out
    assert "top 20" in out, out


def test_auto_keeps_an_explicit_method(tmp_path):
    e = _engine(tmp_path)
    e.state.universe_rank = "GAINERS"
    e.state.universe_sizing = 5
    out = _run(e.command("universe rank auto"))
    assert e.state.universe_rank == "GAINERS", out


def test_auto_with_no_size_says_what_is_missing_instead_of_lying(tmp_path):
    e = _engine(tmp_path)
    e.state.universe_rank = ""
    e.state.universe_sizing = 0
    out = _run(e.command("universe rank auto"))
    assert e.state.universe_rank == "VOLUME", out
    assert "universe sizing" in out.lower(), out


def test_off_disables_ranking_and_keeps_the_size_for_later(tmp_path):
    e = _engine(tmp_path)
    e.state.universe_rank = "GAINERS"
    e.state.universe_sizing = 20
    out = _run(e.command("universe rank off"))
    assert e.state.universe_rank == "", out
    assert e.state.universe_sizing == 20, out


def test_sizing_and_volume_still_set_their_own_fields(tmp_path):
    e = _engine(tmp_path)
    assert _run(e.command("universe sizing 33")).startswith("Universe sizing = 33")
    assert e.state.universe_sizing == 33
    assert _run(e.command("universe volume 2500000")).startswith(
        "Universe min 24h volume = 2.5e+06")
    assert e.state.min_24h_volume_usdt == 2_500_000.0


def test_the_error_message_does_not_teach_the_misspelling(tmp_path):
    out = _run(_engine(tmp_path).command("universe rank nonsense"))
    assert "LOOSERS" not in out, out
    assert "LOSERS" in out, out


def test_help_does_not_teach_the_misspelling():
    assert "LOOSERS" not in HELP
    assert "universe rank auto" in HELP


# ------------------------------------------------------------- the ranking

def test_gainers_rank_by_percent_not_absolute_price(tmp_path):
    """BTC +$60 (+0.1%) must not outrank PEPE +30%."""
    assert _ranked(_engine(tmp_path), "GAINERS") == ["PEPEUSDT", "BTCUSDT"]


def test_losers_rank_by_percent(tmp_path):
    assert _ranked(_engine(tmp_path), "LOSERS") == ["ETHUSDT"]


def test_movers_rank_by_percent(tmp_path):
    assert _ranked(_engine(tmp_path), "MOVERS") == [
        "PEPEUSDT", "ETHUSDT", "BTCUSDT"]


def test_volume_ranks_by_quote_volume(tmp_path):
    assert _ranked(_engine(tmp_path), "VOLUME") == [
        "BTCUSDT", "ETHUSDT", "PEPEUSDT"]


def test_the_volume_floor_filters_illiquid_pairs(tmp_path):
    out = _ranked(_engine(tmp_path), "MOVERS")
    assert "ILLIQUSDT" not in out, out


def test_legacy_loosers_method_still_ranks(tmp_path):
    assert _ranked(_engine(tmp_path), "LOOSERS") == ["ETHUSDT"]


def test_auto_method_ranks_by_volume(tmp_path):
    assert _ranked(_engine(tmp_path), "AUTO") == [
        "BTCUSDT", "ETHUSDT", "PEPEUSDT"]


def test_sizing_zero_and_failed_ticker_read_return_no_universe(tmp_path):
    e = _engine(tmp_path)
    assert _ranked(e, "GAINERS", sizing=0) == []

    async def boom():
        raise RuntimeError("network down")

    e.state.universe_rank = "GAINERS"
    e.state.universe_sizing = 5
    e.client.get_tickers = boom
    assert _run(e._universe_symbols()) == []


def test_a_stale_zero_price_never_becomes_a_top_mover(tmp_path):
    """A ticker with lastPrice 0 would otherwise read as -100% and top LOSERS."""
    rows = [
        _ticker("ZEROUSDT", 0.0, 1.0),
        _ticker("BTCUSDT", 60060.0, 60000.0),
    ]
    e = _engine(tmp_path)
    assert _ranked(e, "GAINERS", rows=rows) == ["BTCUSDT"]
    assert _ranked(e, "LOSERS", rows=rows) == []
    assert _ranked(e, "MOVERS", rows=rows) == ["BTCUSDT"]


# ------------------------------------------------------------------- the NLU

@pytest.mark.parametrize("text,command", [
    ("universe rank losers", "universe rank LOSERS"),
    ("universe rank LOOSERS", "universe rank LOSERS"),
    ("rank the universe by losers", "universe rank LOSERS"),
    ("rank the universe by gainers", "universe rank GAINERS"),
    # "volume" here is the method, not the volume-floor threshold
    ("universe rank volume", "universe rank VOLUME"),
    ("universe rank auto", "universe rank auto"),
    ("universe rank off", "universe rank off"),
    ("turn on universe auto ranking", "universe rank auto"),
])
def test_nlu_maps_universe_words_to_the_real_command(text, command):
    intent = nlu.parse(text)
    assert isinstance(intent, nlu.Intent), f"{text!r} -> {intent!r}"
    assert intent.command == command, (text, intent.command)


# -------------------------------------------------------------- the env knob

def test_universe_rank_env_is_read_as_a_string(monkeypatch):
    """UNIVERSE_RANK was read with float() - every method name became ''."""
    monkeypatch.setenv("UNIVERSE_RANK", "losers")
    assert load_settings().universe_rank == "LOSERS"


def test_universe_rank_env_is_optional(monkeypatch):
    monkeypatch.delenv("UNIVERSE_RANK", raising=False)
    assert load_settings().universe_rank == "VOLUME"
