#!/usr/bin/env python3
"""Find the `.env` lines python-dotenv ignores, and optionally repair them.

python-dotenv understands exactly one `KEY=value` per line. Three things break
that and are all common in a hand-edited file:

  * `KEY: value` (YAML habit) - the line is dropped and the setting silently
    goes missing;
  * a value pasted across several lines - only the first line is kept, the rest
    becomes junk keys or a parse warning;
  * a pasted block (JSON, prose, a log) - one warning per line at every start.

`jrock/config.py` now prints that as a single warning. This script says WHICH
lines, and fixes what can be fixed without guessing:

    python scripts/check_env.py              # report, values never printed
    python scripts/check_env.py --fix        # KEY: value  ->  KEY=value
    python scripts/check_env.py --fix --merge  # also rejoin a multi-line value
    python scripts/check_env.py --comment    # comment out the rest, so the
                                             # warnings stop
    python scripts/check_env.py --json       # machine-readable report

A backup (`<file>.bak`) is written before any change. Exit code is 1 when a
line would still be ignored, so it can gate a restart.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jrock.settings_store import _env_value  # noqa: E402
from jrock.store import atomic_write_text  # noqa: E402

import logging  # noqa: E402

import dotenv  # noqa: E402

BINDING = re.compile(r"^\s*(?:export\s+)?([^=#\s]+)\s*=(.*)$")
COLON = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
#: A line with no `=` and no blank: the usual shape of a continuation of the
#: previous value. An all-caps token is excluded - that is a broken KEY line,
#: not a value.
ORPHAN = re.compile(r"^[^\s#=]+$")
LOOKS_LIKE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: `--merge` refuses anything that looks like pasted JSON or a quoted block:
#: folding that into the previous value would corrupt it.
NOT_A_CONTINUATION = set('{}[]"')


def _dotenv_breaks(text: str) -> set[int]:
    """Lines python-dotenv itself refuses - the ground truth for 'ignored'.

    The heuristics below judge single lines; the real parser works on the
    whole file (a quoted value may span lines for example). Whatever it warns
    about is what start-up would report, so those lines win the argument.
    """
    import io

    messages: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("dotenv.main")
    capture = Capture()
    logger.addHandler(capture)
    try:
        dotenv.dotenv_values(stream=io.StringIO(text))
    finally:
        logger.removeHandler(capture)
    warned: set[int] = set()
    for m in messages:
        hit = re.search(r"starting at line (\d+)", m)
        if hit:
            warned.add(int(hit.group(1)))
    return warned


def _unescaped(value: str, quote: str) -> int:
    """Number of unescaped `quote` characters in a value."""
    n, i = 0, 0
    while i < len(value):
        if value[i] == "\\":
            i += 2
            continue
        if value[i] == quote:
            n += 1
        i += 1
    return n



@dataclass
class Problem:
    line: int
    kind: str          # "unparsed" | "colon" | "orphan"
    key: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return {"line": self.line, "kind": self.kind, "key": self.key,
                "note": self.note}


def scan(text: str) -> list[Problem]:
    """Classify every line: what python-dotenv keeps, and what it ignores."""
    lines = text.splitlines()
    warned = _dotenv_breaks(text)

    problems: list[Problem] = []
    last_key = ""
    open_quote = ""          # a quoted value may span lines (dotenv allows it)
    for i, raw in enumerate(lines, start=1):
        if open_quote:
            # Inside a quoted value: it closes at the first unescaped quote.
            if _unescaped(raw, open_quote):
                open_quote = ""
            continue
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        if i in warned:
            colon = COLON.match(raw)
            if colon:
                problems.append(Problem(i, "colon", colon.group(1),
                                        "rewrite as KEY=value"))
                last_key = colon.group(1)
            else:
                problems.append(Problem(i, "unparsed", "",
                                        "not KEY=value: delete, quote or "
                                        "comment it"))
            continue
        binding = BINDING.match(raw)
        if binding:
            last_key = binding.group(1)
            value = binding.group(2)
            first = value.lstrip()[:1]
            if first in ('"', "'") and _unescaped(value.lstrip(), first) % 2:
                open_quote = first
            continue
        colon = COLON.match(raw)
        if colon:
            problems.append(Problem(i, "colon", colon.group(1),
                                    "rewrite as KEY=value"))
            last_key = colon.group(1)
            continue
        if body == "--" and last_key:
            continue
        if ORPHAN.match(body) and last_key and not LOOKS_LIKE_KEY.match(body):
            problems.append(Problem(i, "orphan", last_key,
                                    "second line of that value?"))
            continue
        problems.append(Problem(i, "unparsed", "",
                                "not KEY=value: delete, quote or comment it"))
    return problems


def repair(lines: list[str], merge: bool = False) -> tuple[list[str], list[str]]:
    """Fix the unambiguous cases. Returns (new_lines, notes).

    `KEY: value` always becomes `KEY=value`. With `merge`, a run of
    continuation-looking lines is folded back into the value it belongs to and
    the whole value is quoted, so dotenv reads it as ONE statement. Everything
    else is left byte-for-byte as it was: guessing what a pasted block meant is
    how a config gets corrupted, and `.env` holds API keys.
    """
    out: list[str] = []
    notes: list[str] = []
    pending: dict | None = None

    def flush() -> None:
        nonlocal pending
        if pending and pending["merged"]:
            out[pending["idx"]] = f"{pending['key']}={_env_value(pending['val'])}"
            notes.append(f"line {pending['line']}: {pending['key']} now one "
                         f"quoted value ({pending['merged']} line"
                         f"{'' if pending['merged'] == 1 else 's'} merged)")
        pending = None

    for i, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            flush()
            out.append(raw)
            continue
        binding = BINDING.match(raw)
        if binding:
            flush()
            out.append(raw)
            pending = {"idx": len(out) - 1, "line": i,
                       "key": binding.group(1),
                       "val": binding.group(2).strip(), "merged": 0}
            continue
        colon = COLON.match(raw)
        if colon:
            flush()
            out.append(f"{colon.group(1)}={_env_value(colon.group(2).strip())}")
            notes.append(f"line {i}: {colon.group(1)} rewritten from "
                         f"'KEY: value' to 'KEY=value'")
            continue
        if (merge and pending and ORPHAN.match(text)
                and not LOOKS_LIKE_KEY.match(text)
                and not (set(text) & NOT_A_CONTINUATION)):
            pending["val"] += "\n" + text
            pending["merged"] += 1
            continue
        flush()
        out.append(raw)
    flush()
    return out, notes


def comment_out(lines: list[str], problems: list[Problem]) -> tuple[list[str], int]:
    """Prefix the unfixable lines with '# ' so dotenv stops warning about them."""
    bad = {p.line for p in problems if p.kind == "unparsed"}
    out, n = [], 0
    for i, raw in enumerate(lines, start=1):
        if i in bad and raw.strip():
            out.append("# " + raw)
            n += 1
        else:
            out.append(raw)
    return out, n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Report .env lines python-dotenv ignores.")
    ap.add_argument("--env", default=str(ROOT / ".env"),
                    help="path to the .env file (default: repo root)")
    ap.add_argument("--fix", action="store_true",
                    help="rewrite KEY:value lines, writing <file>.bak first")
    ap.add_argument("--merge", action="store_true",
                    help="with --fix: also rejoin a multi-line value into one "
                         "quoted statement")
    ap.add_argument("--comment", action="store_true",
                    help="comment out the lines that cannot be repaired")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args(argv)

    path = Path(args.env)
    if not path.exists():
        print(f"{path}: no such file - nothing to check")
        return 0

    lines = path.read_text(encoding="utf-8").splitlines()
    problems = scan(path.read_text(encoding="utf-8"))

    def backup() -> None:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

    fixes: list[str] = []
    if args.fix and problems:
        fixed, fixes = repair(lines, merge=args.merge)
        if fixed != lines:
            backup()
            atomic_write_text(path, "\n".join(fixed) + "\n")
            lines = fixed
            problems = scan("\n".join(lines))

    commented = 0
    if args.comment and problems:
        body, commented = comment_out(lines, problems)
        if commented:
            if not args.fix:
                backup()
            atomic_write_text(path, "\n".join(body) + "\n")
            lines = body
            problems = scan("\n".join(lines))

    values = sum(1 for ln in lines
                 if ln.strip() and not ln.lstrip().startswith("#")
                 and BINDING.match(ln))
    report = {"file": str(path), "values": values,
              "problems": [p.as_dict() for p in problems],
              "fixes": fixes, "commented": commented}

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{path}: {values} settings, "
              f"{len(problems)} line{'' if len(problems) == 1 else 's'} ignored")
        for f in fixes:
            print(f"  fixed: {f}")
        if commented:
            print(f"  commented out {commented} line"
                  f"{'' if commented == 1 else 's'}")
        for p in problems:
            if p.kind == "colon":
                print(f"  line {p.line:>4}  'KEY: value' syntax   {p.key}")
            elif p.kind == "orphan":
                print(f"  line {p.line:>4}  may continue {p.key} "
                      f"(needs quotes)")
            else:
                print(f"  line {p.line:>4}  not KEY=value")
        if problems:
            print("  values are never printed here - open the file at those "
                  "line numbers")
            print("  next: --fix (--merge for multi-line values), then "
                  "--comment for the rest")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
