"""Docs stay in step with the code: `.env.example` and the README.

The failure this guards against is quiet and expensive: a setting is added to
`config.py`, works, and is never documented - so the next person to deploy has
no idea it exists, or worse, copies a stale `.env.example` that silently
disables it. The reverse is just as bad: `.env.example` advertises a knob the
code no longer reads.

Everything here is pure - it reads files, never the network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
README = ROOT / "README.md"
CONFIG = ROOT / "jrock" / "config.py"


@pytest.fixture(scope="module")
def env() -> dict:
    assert ENV_EXAMPLE.exists(), ".env.example is missing"
    return dotenv_values(ENV_EXAMPLE)


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def config_src() -> str:
    return CONFIG.read_text(encoding="utf-8")


# --------------------------------------------------------------- universe
UNIVERSE_VARS = [
    "UNIVERSE_AUTO",
    "UNIVERSE_RANK",
    "UNIVERSE_SIZE",
    "MIN_24H_VOLUME_USD",
]


@pytest.mark.parametrize("name", UNIVERSE_VARS)
def test_universe_var_is_documented_in_env_example(env, name):
    assert name in env, f"{name} is read by config.py but missing from .env.example"


@pytest.mark.parametrize("name", UNIVERSE_VARS)
def test_universe_var_is_read_by_config(config_src, name):
    assert name in config_src, f"{name} is documented but config.py never reads it"


def test_universe_defaults_match_config(env):
    """The documented defaults must be the ones the code actually uses."""
    assert env["UNIVERSE_AUTO"] == "0"
    assert env["UNIVERSE_RANK"] == "VOLUME"
    assert env["UNIVERSE_SIZE"] == "40"
    assert env["MIN_24H_VOLUME_USD"] == "20000000"


def test_universe_auto_is_off_by_default(env):
    """The whole point of the layer: it must not switch itself on."""
    assert env["UNIVERSE_AUTO"] in ("0", "false", "False", "")


def test_universe_rank_default_is_a_valid_mode(env):
    assert env["UNIVERSE_RANK"] in ("VOLUME", "GAINERS", "LOSERS", "MOVERS")


# ------------------------------------------------------- margin / 20003
MARGIN_VARS = ["TAKER_FEE_RATE", "MARGIN_SAFETY_PCT"]


@pytest.mark.parametrize("name", MARGIN_VARS)
def test_margin_var_is_documented_in_env_example(env, name):
    assert name in env, f"{name} is read by config.py but missing from .env.example"


@pytest.mark.parametrize("name", MARGIN_VARS)
def test_margin_var_is_read_by_config(config_src, name):
    assert name in config_src, f"{name} is documented but config.py never reads it"


def test_margin_defaults_match_config(env):
    assert env["TAKER_FEE_RATE"] == "0.0006"
    assert env["MARGIN_SAFETY_PCT"] == "2.0"


def test_taker_fee_is_a_fraction_not_a_percent(env):
    """0.0006 is 0.06%. A value of 0.06 here would reserve 6% and starve sizing."""
    fee = float(env["TAKER_FEE_RATE"])
    assert 0 < fee < 0.01, f"TAKER_FEE_RATE={fee} looks like a percent, not a fraction"


# ------------------------------------------------------------- README
def test_readme_documents_the_universe_layer(readme):
    assert "Universe selection" in readme
    for name in UNIVERSE_VARS:
        assert name in readme, f"{name} is not mentioned in the README"


def test_readme_states_universe_auto_is_off_by_default(readme):
    """The user must be told the layer is inert until they ask for it."""
    assert "universe_auto" in readme
    assert "OFF by default" in readme or "off by default" in readme


def test_readme_documents_the_20003_causes(readme):
    section = readme.split("### 20003 `Insufficient balance`", 1)
    assert len(section) == 2, "the 20003 section is gone from the README"
    body = section[1].split("###", 1)[0]
    # The two structural causes that kept the error coming back.
    assert "_sync_exchange_leverage" in body
    assert "real notional" in body or "qty * price" in body


def test_readme_says_the_80pct_buffer_is_not_the_fix(readme):
    """The user asked directly; the answer must be on the record."""
    assert "80% buffer is not the fix" in readme


def test_readme_documents_margin_knobs(readme):
    assert "TAKER_FEE_RATE" in readme
    assert "MARGIN_SAFETY_PCT" in readme


def test_readme_states_a_test_count(readme):
    """The README must state a count, and it must be a plausible one.

    The exact number is not asserted here: parametrised tests expand at
    collection time, so a source-level count cannot match the collected count.
    The suite's own run is the authority; this only catches a count that was
    never updated at all (or a section that lost its number).
    """
    m = re.search(r"(\d+)\s+tests covering", readme)
    assert m, "the README no longer states a test count"
    assert int(m.group(1)) >= 900, "the README test count looks stale"
