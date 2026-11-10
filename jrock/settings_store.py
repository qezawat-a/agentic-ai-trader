"""Backwards-compatible shim over `jrock.store`.

`SettingsStore` used to live here as a bare, unversioned dict with no atomic
write and no corruption handling. All persistence now goes through
`jrock.store.Store`, which adds schema versioning, atomic writes, migration,
corrupt-file backup and a secret guard.

This module is kept so existing imports keep working; it re-exports the real
implementation rather than duplicating it, so there is exactly one code path
that touches the disk.
"""
from __future__ import annotations

import re
from pathlib import Path

from .store import (SCHEMA_VERSION, SecretInStoreError, SettingsStore, Store,
                    atomic_write_json, atomic_write_text, is_secret_key)

__all__ = [
    "Store", "SettingsStore", "SecretInStoreError", "SCHEMA_VERSION",
    "atomic_write_json", "atomic_write_text", "is_secret_key", "mirror_env",
]


def _env_value(value: str) -> str:
    """Render a value so python-dotenv reads it back as ONE statement.

    A raw multi-line value (a prompt or a soul text with newlines)
    written as ``KEY=line1\\nline2`` is not one statement: dotenv parses line 1
    as the value and then reports every following line as
    "could not parse statement starting at line N". That is the wall of
    warnings the operator saw at startup - the file was fine, the writer was
    not.

    Double quotes make the value a single statement, and dotenv decodes the
    escapes back. Backslashes and quotes are escaped first so the value
    round-trips exactly.
    """
    s = str(value)
    if "\n" in s or "\r" in s or s != s.strip() or '"' in s or "\\" in s:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    return s


def mirror_env(env_path: Path | str, key: str, value: str) -> None:
    """Write KEY=value into .env, preserving every other line.

    `.env` is a *convenience mirror* so the user can see a runtime change in
    the file they already edit. The store is the source of truth on the next
    start (store > .env > default), so a stale mirror can never win.

    The value is quoted when it needs to be (see ``_env_value``) so a
    multi-line or space-bearing value cannot corrupt the file or produce a
    parse warning on the next start.
    """
    p = Path(env_path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    rendered = f"{key}={_env_value(value)}"
    for i, ln in enumerate(lines):
        if pattern.match(ln):
            lines[i] = rendered
            break
    else:
        lines.append(rendered)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
