"""The `.env` parse wall: one warning, a redacted report, a safe repair.

python-dotenv logs one warning per unparseable line, so a pasted block turns
start-up into dozens of identical lines that hide the real output. The loader
now prints one message; `scripts/check_env.py` names the lines and can fix the
unambiguous cases.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from jrock import config

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_env.py"


def _env(tmp_path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def _run(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECKER), "--env", str(path),
                           *args], capture_output=True, text=True, cwd=ROOT)


# ------------------------------------------------------------------- loader
def test_many_bad_lines_become_one_warning(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("GOOD_ONE", raising=False)
    body = "GOOD_ONE=1\n" + "\n".join(
        f"pasted block {i} of text" for i in range(20)) + "\n"
    env = _env(tmp_path, body)

    with caplog.at_level(logging.WARNING, logger="jrock.config"):
        config._load_env(env)

    assert len(config.ENV_PARSE_WARNINGS) == 20
    ours = [r for r in caplog.records if r.name == "jrock.config"]
    assert len(ours) == 1, [r.getMessage() for r in ours]
    msg = ours[0].getMessage()
    assert "20 lines" in msg and "lines 2," in msg
    assert "check_env.py" in msg, "the message must say how to find them"
    # The wall is gone: python-dotenv's own logger stayed silent.
    assert not [r for r in caplog.records if r.name == "dotenv.main"]


def test_a_clean_env_is_silent(tmp_path, caplog):
    body = ('A=1\nB=two words\n# comment\n\nC="x\\ny"\n'
            'SOUL="will be quoted,\nstays one value."\n')
    env = _env(tmp_path, body)
    with caplog.at_level(logging.WARNING, logger="jrock.config"):
        config._load_env(env)
    assert config.ENV_PARSE_WARNINGS == []
    assert not [r for r in caplog.records if r.name == "jrock.config"]


def test_an_unterminated_quote_is_flagged(tmp_path):
    from dotenv import dotenv_values

    env = _env(tmp_path, "A=1\nBROKEN=\"value\n")

    report = _run(env, "--json")
    assert report.returncode == 1, report.stdout
    kinds = {(p["line"], p["kind"])
             for p in json.loads(report.stdout)["problems"]}
    assert (2, "unparsed") in kinds
    assert (2, "unparsed") in kinds

    # --comment silences it: python-dotenv then loads the file clean.
    out = _run(env, "--fix", "--comment")
    assert out.returncode == 0, out.stdout
    assert dotenv_values(env) == {"A": "1"}


# ------------------------------------------------------------------ checker
def test_checker_names_the_lines_and_never_prints_a_value(tmp_path):
    env = _env(tmp_path, "GOOD=1\nSECRET_KEY: sk-live-abc123\njunk prose here\n")

    out = _run(env)

    assert out.returncode == 1, out.stdout
    assert "SECRET_KEY" in out.stdout
    assert "sk-live-abc123" not in out.stdout, "a value must never be echoed"
    assert "line    2" in out.stdout or "line 2" in out.stdout
    assert "values are never printed" in out.stdout


def test_checker_exits_zero_when_clean(tmp_path):
    env = _env(tmp_path, "A=1\nB=two words\n")
    out = _run(env)
    assert out.returncode == 0, out.stdout
    assert "0 lines ignored" in out.stdout


def test_fix_rewrites_the_yaml_habit(tmp_path):
    from dotenv import dotenv_values

    env = _env(tmp_path, "A=1\nB: two words\nC=3\n")
    out = _run(env, "--fix")

    assert out.returncode == 0, out.stdout
    assert dotenv_values(env) == {"A": "1", "B": "two words", "C": "3"}
    assert Path(str(env) + ".bak").exists(), "a backup is mandatory"


def test_fix_alone_does_not_guess_about_a_multiline_value(tmp_path):
    """Folding a pasted block into the previous key would corrupt it."""
    from dotenv import dotenv_values

    env = _env(tmp_path, "SOUL=line one\nline2\n")
    out = _run(env, "--fix")

    assert out.returncode == 1, "line2 is still ignored and must be reported"
    assert dotenv_values(env)["SOUL"] == "line one"


def test_fix_with_merge_rejoins_a_multiline_value(tmp_path):
    from dotenv import dotenv_values

    env = _env(tmp_path, "A=1\nSOUL=line one\nline2\nC=3\n")
    out = _run(env, "--fix", "--merge")

    assert out.returncode == 0, out.stdout
    parsed = dotenv_values(env)
    assert parsed["SOUL"] == "line one\nline2"
    assert parsed["A"] == "1" and parsed["C"] == "3"
    assert "now one quoted value" in out.stdout


def test_merge_refuses_a_pasted_json_block(tmp_path):
    env = _env(tmp_path, 'A=1\n{"a": 1}\n{"b": 2}\n')
    out = _run(env, "--fix", "--merge")
    assert out.returncode == 1
    assert '{"a": 1}' in env.read_text(encoding="utf-8"), "text is preserved"


def test_comment_silences_what_cannot_be_fixed(tmp_path):
    env = _env(tmp_path, 'A=1\n{"a": 1}\n{"b": 2}\n')
    out = _run(env, "--fix", "--comment")

    assert out.returncode == 0, out.stdout
    body = env.read_text(encoding="utf-8")
    assert "# {\"a\": 1}" in body, "the text is kept, just disabled"
    assert "A=1" in body
    assert "commented out 2 lines" in out.stdout
