"""Tests for the movers ranking and the /autofix self-fix pipeline.

The self-fix loop has two unbreakable gates that these tests pin:
  1. nothing is committed while pytest still FAILs;
  2. every proposed patch goes through the permission-gated string
     replacement (Tools.patch), never a blind file overwrite.
"""
from __future__ import annotations

import asyncio

import jrock.agent as jrock_agent_ns
import pytest

from jrock.agent import (JRockAgent, MOVERS_DEFAULT_MODE, MOVERS_DEFAULT_ROWS,
                         rank_movers)
from jrock.config import Settings


def root():
    """The agent's ROOT read AT CALL TIME.

    tests/conftest.py repoints BOTH jrock.agent.ROOT and jrock.config.ROOT
    at each test's tmp_path, but it does so after test modules were
    imported - so `from jrock.config import ROOT` is frozen at the real
    repo path and DISAGREES with everything the agent (and Tools) resolve
    at runtime. Every scratch path in this file must go through this
    helper, or the scope guard and Tools.patch will compare different
    directories.
    """
    return jrock_agent_ns.ROOT


def _agent(tmp_path, **kw) -> JRockAgent:
    s = Settings(auto_accept_edit=True, **kw)
    a = JRockAgent(s)
    a.engine.data_dir = tmp_path
    return a


def _run(coro):
    return asyncio.run(coro)


TICKERS = [
    {"symbol": "BTCUSDT", "lastPrice": "110", "open": "100", "quoteVol": "1000000"},
    {"symbol": "ETHUSDT", "lastPrice": "90", "open": "100", "quoteVol": "500000"},
    {"symbol": "DOGEUSDT", "lastPrice": "0.5", "open": "0.5", "quoteVol": "3000000"},
    {"symbol": "XRPUSDT", "lastPrice": "120", "open": "100", "quoteVol": "900000"},
]

# ------------------------------------------------------------------ movers


def test_rank_movers_movers_is_biggest_move_either_direction():
    rows = rank_movers(TICKERS, "MOVERS")
    assert rows[0]["symbol"] == "XRPUSDT"          # +20% - the biggest move overall
    assert rows[1]["symbol"] == "BTCUSDT"          # +10%
    assert rows[2]["symbol"] == "ETHUSDT"          # -10%, same magnitude, opposite sign
    assert abs(rows[1]["chg"] + rows[2]["chg"]) < 1e-9


def test_rank_movers_gainers_only_positive():
    rows = rank_movers(TICKERS, "GAINERS")
    assert [x["symbol"] for x in rows] == ["BTCUSDT", "XRPUSDT"]
    assert all(x["chg"] > 0 for x in rows)


def test_rank_movers_losers_only_negative():
    rows = rank_movers(TICKERS, "LOSERS")
    assert [x["symbol"] for x in rows] == ["ETHUSDT"]
    assert rows[0]["chg"] < 0


def test_rank_movers_volume_is_liquidity_first():
    rows = rank_movers(TICKERS, "VOLUME")
    assert rows[0]["symbol"] == "DOGEUSDT"          # 3M despite 0% move
    assert rows[-1]["symbol"] == "ETHUSDT"          # thinnest


def test_rank_movers_drops_rows_without_a_price():
    rows = rank_movers([{"symbol": "BAD", "lastPrice": "0", "open": "0",
                         "quoteVol": "1"}], "VOLUME")
    assert rows == []


def test_movers_command_ranks_and_offers_a_universe(tmp_path):
    a = _agent(tmp_path)

    async def fake_tickers(symbols=None):
        return TICKERS

    a.engine.client = type("C", (), {"get_tickers": staticmethod(fake_tickers)})()
    out = _run(a.handle("movers gainers 2"))
    assert "BTCUSDT" in out and "XRPUSDT" in out
    assert "ETHUSDT" not in out                     # filtered: not a gainer
    assert "+10.00%" in out
    # the universe line can be pasted straight into the engine
    assert "symbols BTCUSDT,XRPUSDT" in out


def test_movers_command_default_mode_and_rows(tmp_path):
    a = _agent(tmp_path)
    assert MOVERS_DEFAULT_MODE == "MOVERS"
    assert MOVERS_DEFAULT_ROWS == 10


# ---------------------------------------------------------------- autofix

FAIL_BLOCK = """
### CHANGE
FILE: {path}
OLD: <<<OLD
old-value = 1
>>>OLD
NEW: <<<NEW
old-value = 2
>>>NEW
"""


class FakeInternal:
    """Stands in for Tools.internal: records commands, returns canned results."""

    def __init__(self, pytest1_ok, pytest2_ok):
        self.pytest1_ok = pytest1_ok
        self.pytest2_ok = pytest2_ok
        self.calls: list[str] = []

    def __call__(self, command, timeout=120):
        self.calls.append(command)
        ok = True
        output = ""
        if "pytest" in command:
            ok = self.pytest1_ok if len(self.calls) == 1 else self.pytest2_ok
            output = "3 failed, 100 passed" if not ok else "103 passed"
        return type("R", (), {"ok": ok, "output": output})()


def _autofix_data_scratch() -> str:
    """A patch target INSIDE the repo but gitignored (data/), so no real
    commit picks it up; the suite cleans it up afterwards."""
    import uuid
    return f"data/autofix_scratch_{uuid.uuid4().hex[:8]}.py"


def test_autofix_nothing_to_fix_when_green_and_silent(tmp_path):
    a = _agent(tmp_path)
    a._collect_bug_evidence = lambda: "## pytest tests -q  (PASS)\nok"
    out = _run(a.handle("autofix"))
    assert "nothing to fix" in out


def test_autofix_requires_a_model_before_touching_anything(tmp_path):
    a = _agent(tmp_path)
    a._collect_bug_evidence = lambda: (
        "## live engine errors\n- boom\n## pytest tests -q  (FAIL)\n1 failed")
    assert not a.s.api_key                          # the gate is the missing key
    out = _run(a.handle("autofix"))
    assert "model patch step unavailable" in out


def test_autofix_fail_keeps_everything_uncommitted(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    scratch = _autofix_data_scratch()
    (root() / scratch).write_text("old-value = 1\n")
    try:
        # evidence runs are stubbed, so the FIRST pytest FakeInternal sees
        # IS the retest - it must fail for this test's premise.
        fake_internal = FakeInternal(False, False)
        monkeypatch.setattr(a.tools, "internal", fake_internal)

        async def fake_complete(system: str, user: str) -> str:
            return FAIL_BLOCK.format(path=scratch)

        a._complete_raw = fake_complete
        a._collect_bug_evidence = lambda: (
            "## live engine errors\n- boom\n## pytest tests -q  (FAIL)\n1 failed")
        out = _run(a.handle("autofix"))
        assert "retest: FAIL" in out
        assert "NOTHING was committed" in out
        # NOT A SINGLE git command ran: no add, no commit, no push
        assert not [c for c in fake_internal.calls if c.startswith("git ")]
    finally:
        (root() / scratch).unlink(missing_ok=True)


def test_autofix_pass_commits_and_pushes_once(tmp_path, monkeypatch):
    """The one thing the user asked for: analyze -> fix -> test -> push."""
    a = _agent(tmp_path)
    scratch = _autofix_data_scratch()
    (root() / scratch).write_text("old-value = 1\n")
    a.engine.state.errors.append("boom in sizing")
    try:
        # evidence runs are stubbed, so the FIRST pytest FakeInternal sees
        # IS the retest - it must pass so the loop commits and pushes.
        fake_internal = FakeInternal(True, True)
        monkeypatch.setattr(a.tools, "internal", fake_internal)

        async def fake_complete(system: str, user: str) -> str:
            return FAIL_BLOCK.format(path=scratch)

        a._complete_raw = fake_complete
        a._collect_bug_evidence = lambda: (
            "## live engine errors\n- boom\n## pytest tests -q  (FAIL)\n1 failed")
        out = _run(a.handle("autofix"))
        assert "applied" in out
        assert "retest: PASS" in out
        assert "committed and pushed" in out
        assert (root() / scratch).read_text().strip() == "old-value = 2"
        git_calls = [c for c in fake_internal.calls if c.startswith("git ")]
        assert any(c.startswith("git add") for c in git_calls)
        assert any(c.startswith("git commit") for c in git_calls)
        assert sum(1 for c in git_calls if c.startswith("git push")) == 1
    finally:
        (root() / scratch).unlink(missing_ok=True)


def test_autofix_no_applied_patch_never_commits(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    # A parent-relative path escapes the tmp sandbox the same way it would
    # escape the real repo: the scope guard must reject it.
    bad = "../autofix_escape_target.py"
    fake_internal = FakeInternal(False, True)
    monkeypatch.setattr(a.tools, "internal", fake_internal)

    async def fake_complete(system: str, user: str) -> str:
        return FAIL_BLOCK.format(path=bad)

    a._complete_raw = fake_complete
    a._collect_bug_evidence = lambda: "## pytest tests -q  (FAIL)\n1 failed"
    out = _run(a.handle("autofix"))
    assert "nothing was modified" in out
    assert not [c for c in fake_internal.calls if c.startswith("git ")]


def test_apply_change_blocks_reports_each_status(tmp_path):
    a = _agent(tmp_path)
    a.perms.auto_accept_edit = True
    inside = root() / "data" / "autofix_status_scratch.py"
    inside.write_text("keep\n")
    try:
        report = a._apply_change_blocks(
            "### CHANGE\nFILE: no-marks-here\n### CHANGE\n"
            f"FILE: {inside}\nOLD: <<<OLD\nkeep\n>>>OLD\nNEW: <<<NEW\nkept\n>>>NEW\n")
        assert len(report) == 2
        assert report[0]["status"] == "malformed"
        assert report[1]["status"] == "applied"
        assert inside.read_text() == "kept\n"
    finally:
        inside.unlink(missing_ok=True)


def test_autofix_patches_are_scope_gated(tmp_path, monkeypatch):
    """A FILE: pointing outside the repo must not be followed."""
    a = _agent(tmp_path)
    a.perms.auto_accept_edit = True
    report = a._apply_change_blocks(
        "### CHANGE\nFILE: ../autofix_escape_target.py\nOLD: <<<OLD\nx\n>>>OLD\n"
        "NEW: <<<NEW\ny\n>>>NEW\n")
    assert report and "rejected" in report[0]["status"]
