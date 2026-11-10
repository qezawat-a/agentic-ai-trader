"""Self-repair loop: pytest -> review -> fix -> push to GitHub main.

The engine can watch the market; this module watches the CODE. One cycle:

    1. run the test suite (pytest -q tests)
    2. GREEN  - nothing to repair. Committed-but-unpushed self-heal fixes, if
                any, are pushed. Pre-existing dirty state that self-heal did
                not create is left strictly alone.
    3. RED    - collect the failing tests, send the evidence to the LLM
                (``agent.chat``) with a strict "return JSON only" prompt that
                asks for exact-string replacements per file, apply them with
                an exact match (a fix whose anchor text is missing is skipped,
                never guessed), re-run the tests:
                  red again   - revert every file the cycle touched, log, stop
                  green       - keep the fixes; commit ONLY those files and
                                push to origin main (SELF_HEAL_AUTO_PUSH)

Guardrails (deliberately blunt):
    * NEVER touches .env / secrets / anything outside jrock/ and tests/.
    * At most MAX_FILES_PER_CYCLE files per cycle - a model that wants to
      rewrite nine files in one pass is doing something wrong.
    * Never forces a push, never deletes history; the push is a plain
      fast-forward commit on the current branch, refused if that branch is
      not main.
    * Every cycle is appended to data/self_heal_log.jsonl.

Run:
    python -m jrock.self_heal --once   # one cycle now
    python -m jrock.self_heal --loop   # background loop (SELF_HEAL_INTERVAL_HOURS)
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("jrock.self_heal")

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "data" / "self_heal_log.jsonl"

#: Files the self-heal loop must never read-write or commit.
PROTECTED_NAMES = {".env", ".env.local", ".env.production", ".env.private"}
#: A fix may only edit code: the package and its tests.
ALLOWED_PREFIXES = ("jrock/", "tests/")
MAX_FILES_PER_CYCLE = 5
PYTEST_TIMEOUT = 600


@dataclass
class Cycle:
    """One self-heal cycle's outcome, exactly what is written to the log."""

    ts: float = field(default_factory=time.time)
    phase: str = "start"          # start | pytest-green | pytest-red | fixed | reverted
    tests: str = ""               # pytest tail
    analysis: str = ""
    fixes_applied: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    commit: str = ""
    pushed: bool = False
    note: str = ""

    def log_row(self) -> dict:
        return {
            "ts": self.ts,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
            "phase": self.phase,
            "note": self.note,
            "analysis": self.analysis[:2000],
            "fixes_applied": self.fixes_applied,
            "files": self.files,
            "commit": self.commit,
            "pushed": self.pushed,
        }


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--no-pager", *args], cwd=ROOT, check=check,
        capture_output=True, text=True, timeout=120)


def _run(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def _pytest_tail(out: str, limit: int = 4000) -> str:
    return out[-limit:]


def _is_allowed(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if not rel.startswith(ALLOWED_PREFIXES):
        return False
    if Path(rel).name in PROTECTED_NAMES:
        return False
    try:
        (ROOT / rel).resolve().relative_to(ROOT)
    except ValueError:
        return False
    return True


class SelfHealer:
    """Runs one repair cycle, or forever on a background thread."""

    def __init__(self, agent, *, auto_push: bool = True,
                 max_files: int = MAX_FILES_PER_CYCLE):
        self.agent = agent
        self.auto_push = auto_push
        self.max_files = max_files
        self._stop = threading.Event()

    # ------------------------------------------------------------ public API
    def run_once(self) -> Cycle:
        """One full cycle: test -> review -> fix -> (re)test -> push."""
        cyc = Cycle()
        green, tail = self._run_tests()
        cyc.tests = _pytest_tail(tail)

        if green:
            cyc.phase = "pytest-green"
            if self._dirty():
                cyc.note = ("tests green; uncommitted changes exist that "
                            "self-heal did not make - left untouched")
            self._append_log(cyc)
            return cyc

        cyc.phase = "pytest-red"
        # Give the tests a second chance before spending an LLM call: one
        # flaky failure is not a code bug.
        tailed_reason = _flaky_hint(cyc.tests)
        if tailed_reason:
            cyc.note = f"retried once ({tailed_reason})"
            green, cyc.tests = self._run_tests()
            cyc.tests = _pytest_tail(cyc.tests)
            if green:
                cyc.phase = "pytest-green-retry"
                self._append_log(cyc)
                return cyc

        analysis, fixes = self._review(cyc.tests)
        cyc.analysis = analysis
        if not fixes:
            cyc.phase = "pytest-red"
            cyc.note = "review produced no applicable fixes; nothing changed"
            self._append_log(cyc)
            return cyc

        changed = self._apply_fixes(fixes)
        cyc.files = changed
        if not changed:
            cyc.note = "no fix applied (anchors missing or files not allowed)"
            self._append_log(cyc)
            return cyc
        cyc.fixes_applied = [f"{x.file}" for x in fixes if x.applied]
        cyc.note = f"applied {len(changed)} file(s); re-running tests"

        green2, tests2 = self._run_tests()
        cyc.tests = _pytest_tail(tests2)
        if not green2:
            self._revert(cyc.files)
            cyc.phase = "reverted"
            cyc.note = "tests still red after fixes - ALL changes reverted"
            self._append_log(cyc)
            return cyc

        cyc.phase = "fixed"
        cyc.note = cyc.note or "fixes kept: tests green"
        if self.auto_push:
            msg = (f"fix(selfheal): {analysis.splitlines()[0][:60] if analysis else 'repair'}"
                   if analysis else "fix(selfheal): self-repair")
            cyc.commit = self._commit_and_push(changed, msg)
            cyc.pushed = bool(cyc.commit)
            if cyc.commit:
                cyc.note = f"tests green; committed and pushed {cyc.commit[:10]}"
            else:
                cyc.note = "tests green; fixes kept but NOT pushed (push refused)"
        self._append_log(cyc)
        return cyc

    def describe(self) -> str:
        """The last few cycles, for /selfheal status."""
        if not LOG_FILE.exists():
            return "selfheal: no cycles logged yet"
        rows = _read_log()[-5:]
        return "selfheal recent cycles:\n" + "\n".join(
            f"  {r.get('iso', '?')} {r.get('phase')} "
            f"{r.get('note', '')[:80]} files={len(r.get('files') or [])}"
            for r in reversed(rows))

    def run_forever(self, interval_hours: float) -> None:
        """Blocking loop for the CLI/background thread. interval 0 = off."""
        if interval_hours <= 0:
            log.info("self-heal interval is 0 - loop disabled")
            return
        period = interval_hours * 3600.0
        while not self._stop.is_set():
            started = time.time()
            try:
                c = self.run_once()
                log.info("selfheal cycle: phase=%s note=%s", c.phase, c.note)
            except Exception as e:  # noqa: BLE001 - the loop must survive bugs in the loop
                log.exception("selfheal cycle crashed: %s", e)
            waited = max(300.0, period - (time.time() - started))
            self._stop.wait(waited)

    def start_background(self, interval_hours: float) -> threading.Thread | None:
        """Spawn the daemon loop once. Returns None when disabled/already on."""
        if interval_hours <= 0:
            return None
        if getattr(self, "_thread", None) and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self.run_forever, kwargs={"interval_hours": interval_hours},
            name="self-heal", daemon=True)
        self._thread.start()
        return self._thread

    def stop_background(self) -> None:
        self._stop.set()

    def _append_log(self, cyc: "Cycle") -> None:
        """Append one cycle to data/self_heal_log.jsonl. Never raises."""
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(cyc.log_row(), ensure_ascii=False) + "\n")
        except OSError:
            log.exception("selfheal: could not write %s", LOG_FILE)

    # -------------------------------------------------------------- internals
    def _run_tests(self) -> tuple[bool, str]:
        r = _run(sys.executable, "-m", "pytest", "-q", "tests",
                 timeout=PYTEST_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out

    def _dirty(self) -> list[str]:
        try:
            return [ln[3:] for ln in _git("status", "--porcelain").stdout.splitlines()
                    if ln.strip()]
        except Exception:  # noqa: BLE001
            return []

    def _review(self, tests: str) -> tuple[str, list["Fix"]]:
        """Ask the LLM for concrete, exact-string fixes.

        The prompt is deliberately narrow: the model may not invent diffs,
        reformat files, or write prose. What comes back is parsed as JSON.
        """
        if self.agent is None:
            return "no LLM available", []
        failing = _failing_files(tests)
        excerpts = {}
        for rel in failing[:6]:
            p = ROOT / rel
            try:
                src = p.read_text(encoding="utf-8")
            except OSError:
                continue
            excerpts[rel] = src[-6000:]
        if not excerpts:
            failing = sorted(self._dirty())[:6]
            for rel in failing:
                p = ROOT / rel
                if not _is_allowed(rel) or not p.is_file():
                    continue
                try:
                    excerpts[rel] = p.read_text(encoding="utf-8")[-6000:]
                except OSError:
                    continue

        prompt = (
            "You are the self-repair reviewer of this repo. The test suite is "
            "failing. Diagnose the bug from the evidence and return STRICT JSON "
            "(no markdown fence, no prose before or after) shaped exactly like:\n"
            '{\n  "analysis": "one short paragraph: root cause",\n'
            '  "fixes": [\n    {"file": "jrock/path.py",\n'
            '     "old": "exact text to find, verbatim, unique in the file",\n'
            '     "new": "replacement text"}\n  ]\n}\n'
            "RULES:\n"
            "* 'old' MUST appear in the named file exactly once and byte-exactly.\n"
            "* Only edit code inside jrock/ and tests/. NEVER .env or data/.\n"
            f"* At most {self.max_files} files.\n"
            "* Minimal change: fix the bug, do not refactor, do not delete "
            "behaviour, do not wire removed knobs back in.\n"
            "* If you cannot determine the repair confidently, return "
            '{"analysis": "...", "fixes": []} - an empty fixes list is a '
            "valid, honest answer.\n\n"
            "FAILING TEST OUTPUT (tail):\n" + tests[-2500:] + "\n\n"
            + "\n\n".join(f"--- {k} (tail) ---\n{v}" for k, v in excerpts.items())
        )
        try:
            reply = _run_async(self.agent.chat(prompt))
        except Exception as e:  # noqa: BLE001
            return f"LLM call failed: {type(e).__name__}: {e}", []
        return _parse_json(reply)

    def _apply_fixes(self, fixes: list["Fix"]) -> list[str]:
        changed: list[str] = []
        for fx in fixes:
            # Cap on DISTINCT files, not on fixes - several fixes may target
            # one file, but a cycle that rewrites half the repo is wrong.
            if len(set(changed)) >= self.max_files:
                break
            if not _is_allowed(fx.file):
                log.warning("selfheal: refusing %s (outside jrock/|tests)",
                            fx.file)
                fx.applied = False
                continue
            p = ROOT / fx.file
            try:
                src = p.read_text(encoding="utf-8")
            except OSError:
                fx.applied = False
                continue
            n = src.count(fx.old)
            if n != 1:
                log.warning("selfheal: anchor in %s matched %d times - skipped",
                            fx.file, n)
                fx.applied = False
                continue
            p.write_text(src.replace(fx.old, fx.new, 1), encoding="utf-8")
            fx.applied = True
            if fx.file not in changed:
                changed.append(fx.file)
        return changed

    def _revert(self, files: list[str]) -> None:
        for rel in files:
            try:
                _git("checkout", "--", rel)
            except subprocess.CalledProcessError:
                log.exception("selfheal: could not revert %s", rel)

    def _commit_and_push(self, files: list[str], msg: str) -> str:
        try:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if branch != "main":
                log.warning("selfheal: branch is %r, not main - not pushing", branch)
                return ""
            _git("add", "--", *files)
            cp = _git("commit", "-m", msg, check=False)
            if cp.returncode != 0:
                log.warning("selfheal: commit refused: %s", cp.stderr.strip()[:200])
                return ""
            sha = _git("rev-parse", "HEAD").stdout.strip()[:12]
            push = _git("push", "origin", "main", check=False)
            if push.returncode != 0:
                log.warning("selfheal: push failed: %s", push.stderr.strip()[:200])
                return sha
            return sha
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("selfheal: git failed: %s", e)
            return ""


@dataclass
class Fix:
    """One exact-string replacement proposed by the reviewer."""
    file: str
    old: str
    new: str
    applied: bool = False


# --------------------------------------------------------------------- helpers
def _flaky_hint(tests: str) -> bool:
    """Names that suggest a flaky/network test rather than a code bug."""
    return bool(re.search(r"Timeout|timeout|connection|Connection|EOF|wss?://",
                          tests)) and "BitunixAPIError" not in tests


def _failing_files(tests: str) -> list[str]:
    """Extract jrock/ or tests/ paths from pytest output."""
    names: list[str] = []
    for m in re.finditer(r"(?:^|[\s(])((?:jrock|tests)/[\w/.]+\.py)", tests):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _parse_json(reply: str) -> tuple[str, list[Fix]]:
    try:
        start = reply.index("{")
        end = reply.rindex("}") + 1
        data = json.loads(reply[start:end])
    except (ValueError, TypeError):
        return f"review did not return JSON: {reply[:200]}", []
    analysis = str(data.get("analysis") or "").strip()
    fixes: list[Fix] = []
    for item in data.get("fixes") or []:
        if not isinstance(item, dict):
            continue
        f, old, new = item.get("file"), item.get("old"), item.get("new")
        if not (isinstance(f, str) and isinstance(old, str) and isinstance(new, str)):
            continue
        if old == new:
            continue
        fixes.append(Fix(file=f.replace("\\", "/"), old=old, new=new))
    return analysis, fixes


def _run_async(coro):
    """Run agent.chat (async) from a sync context safely."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # We are inside an event loop (e.g. agent thread) - run in a fresh loop
    # on a worker thread.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _read_log() -> list[dict]:
    try:
        rows = []
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    except (OSError, ValueError):
        return []


# -------------------------------------------------------------------- the loop
def build_from_agent(agent) -> "SelfHealer":
    s = agent.s
    return SelfHealer(agent, auto_push=s.self_heal_auto_push)


# ------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s self-heal %(levelname)s %(message)s")
    argv = argv if argv is not None else sys.argv[1:]
    once = "--once" in argv
    loop = "--loop" in argv
    if not (once or loop):
        print(__doc__)
        return 2

    from jrock.agent import JRockAgent
    from jrock.config import load_settings

    agent = JRockAgent(load_settings())
    healer = SelfHealer(agent, auto_push=agent.s.self_heal_auto_push)

    if loop:
        interval = agent.s.self_heal_interval_hours
        if interval <= 0:
            print("SELF_HEAL_INTERVAL_HOURS is 0 - background loop disabled.")
            return 0
        print(f"self-heal loop starting (every {interval:g}h, "
              f"auto_push={agent.s.self_heal_auto_push}). Ctrl-C to stop.")
        healer.run_forever(interval)
        return 0

    cyc = healer.run_once()
    print(f"selfheal: phase={cyc.phase} note={cyc.note}")
    if cyc.files:
        print("files:", ", ".join(cyc.files))
    if cyc.commit:
        print("commit:", cyc.commit[:12], "pushed:", cyc.pushed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
