"""Guardrail tests for the self-repair loop.

The loop edits CODE and pushes it, so the things that must hold are the
guardrails, not the model's judgement:

  * it may only touch `jrock/` and `tests/` - never `.env`, never `data/`;
  * a fix is applied only when its anchor matches EXACTLY once (a missing or
    duplicated anchor is skipped, never guessed);
  * every cycle is logged, and a green run writes a `pytest-green` row;
  * `_parse_json` rejects prose/markdown chatter and honestly returns no fixes.
"""
from __future__ import annotations

import json

import pytest

import jrock.self_heal as sh
from jrock.self_heal import Cycle, Fix, SelfHealer


# ------------------------------------------------------------------ guardrails
@pytest.mark.parametrize("rel,ok", [
    ("jrock/engine.py", True),
    ("tests/test_x.py", True),
    (".env", False),
    (".env.local", False),
    ("data/state.json", False),
    ("README.md", False),
    ("../outside.py", False),
    ("jrock/../.env", False),
])
def test_only_code_paths_are_allowed(rel, ok):
    assert sh._is_allowed(rel) is ok


# --------------------------------------------------------------- exact anchors
def _healer(tmp_path, monkeypatch) -> SelfHealer:
    monkeypatch.setattr(sh, "ROOT", tmp_path)
    monkeypatch.setattr(sh, "LOG_FILE", tmp_path / "data" / "self_heal_log.jsonl")
    return SelfHealer(agent=None, auto_push=False)


def test_apply_fixes_requires_a_unique_anchor(tmp_path, monkeypatch):
    h = _healer(tmp_path, monkeypatch)
    (tmp_path / "jrock").mkdir()
    (tmp_path / "tests").mkdir()
    p = tmp_path / "jrock" / "x.py"
    p.write_text("a = 1\nb = 2\nb = 2\n", encoding="utf-8")

    applied = h._apply_fixes([
        Fix("jrock/x.py", "a = 1", "a = 9"),      # unique -> applied
        Fix("jrock/x.py", "b = 2", "b = 3"),      # 2 matches -> skipped
        Fix("jrock/x.py", "missing", "x"),        # no match -> skipped
        Fix(".env", "K=V", "K=W"),                # protected -> skipped
    ])
    assert applied == ["jrock/x.py"]
    src = p.read_text(encoding="utf-8")
    assert "a = 9" in src
    assert src.count("b = 2") == 2, "a duplicated anchor must not be touched"


def test_apply_fixes_caps_the_number_of_files(tmp_path, monkeypatch):
    h = _healer(tmp_path, monkeypatch)
    h.max_files = 2
    (tmp_path / "jrock").mkdir()
    fixes = []
    for i in range(4):
        p = tmp_path / "jrock" / f"f{i}.py"
        p.write_text("x = 1\n", encoding="utf-8")
        fixes.append(Fix(f"jrock/f{i}.py", "x = 1", "x = 2"))
    applied = h._apply_fixes(fixes)
    assert len(applied) == 2
    untouched = [p for p in (tmp_path / "jrock").glob("f*.py")
                 if "x = 1" in p.read_text(encoding="utf-8")]
    assert len(untouched) == 2


# ------------------------------------------------------------------- the log
def test_a_green_cycle_is_logged_and_changes_nothing(tmp_path, monkeypatch):
    h = _healer(tmp_path, monkeypatch)
    monkeypatch.setattr(h, "_run_tests", lambda: (True, "1058 passed"))
    monkeypatch.setattr(h, "_dirty", lambda: [])

    cyc = h.run_once()

    assert cyc.phase == "pytest-green"
    rows = [json.loads(ln) for ln in sh.LOG_FILE.read_text().splitlines()]
    assert rows[-1]["phase"] == "pytest-green"
    assert rows[-1]["pushed"] is False


def test_a_red_cycle_with_no_fixes_does_not_touch_anything(tmp_path, monkeypatch):
    h = _healer(tmp_path, monkeypatch)
    monkeypatch.setattr(h, "_run_tests", lambda: (False, "1 failed"))
    monkeypatch.setattr(h, "_review", lambda tail: ("cannot tell", []))
    monkeypatch.setattr(sh, "_flaky_hint", lambda tail: False)

    cyc = h.run_once()

    assert cyc.phase == "pytest-red"
    assert "no applicable fixes" in cyc.note
    assert cyc.files == []


def test_describe_reads_the_log(tmp_path, monkeypatch):
    h = _healer(tmp_path, monkeypatch)
    sh.LOG_FILE.parent.mkdir(parents=True)
    sh.LOG_FILE.write_text(json.dumps({"iso": "2026-01-01T00:00:00",
                                       "phase": "fixed", "note": "ok",
                                       "files": ["jrock/x.py"]}) + "\n")
    out = h.describe()
    assert "fixed" in out and "jrock/x.py" not in out  # only counts files
    assert "files=1" in out


# -------------------------------------------------------------- JSON contract
def test_parse_json_accepts_strict_json_with_chatter():
    analysis, fixes = sh._parse_json(
        'sure! {"analysis": "boom", "fixes": [{"file": "jrock/a.py", '
        '"old": "x", "new": "y"}]} hope that helps')
    assert analysis == "boom"
    assert [f.file for f in fixes] == ["jrock/a.py"]


def test_parse_json_rejects_prose_and_no_op_fixes():
    analysis, fixes = sh._parse_json("I think the bug is in engine.py")
    assert fixes == [] and "did not return JSON" in analysis

    _, fixes = sh._parse_json(
        '{"analysis": "x", "fixes": [{"file": "jrock/a.py", '
        '"old": "same", "new": "same"}]}')
    assert fixes == [], "an old==new fix changes nothing and must be dropped"
