"""Postgres-backed persistence.

WHY THIS EXISTS
---------------
The file store (`data/state.json`) lives INSIDE the working copy. A redeploy
that recreates the checkout - a fresh clone, a container without a volume, a
`rm -rf` of the repo - takes the state with it, and the agent comes back
looking like it "reset everything". The operator hit exactly that.

A database is not in the checkout, so the state survives a redeploy. This
module keeps the SAME interface as the file `Store` (`get`/`set`/`section`/
`set_section`/`save`/`summary`), so the engine and the agent do not care
which one they got.

DESIGN
------
- One row, one JSONB document per section. The shape is identical to the
  file store, so migrating back and forth is trivial.
- The driver is psycopg 3 (sync). The store's `save()` is called from sync
  code paths; a sync driver keeps that honest. It is an OPTIONAL dependency:
  without DATABASE_URL nothing here is imported.
- Fail closed. If DATABASE_URL is set but the database cannot be reached, the
  agent refuses to start instead of quietly writing to a file that will be
  lost again - a silent fallback is exactly the failure this module exists
  to remove.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .store import SCHEMA_VERSION, SecretInStoreError, Store, is_secret_key

#: The DDL. One row, id=1, holds the whole state.
DDL = """
CREATE TABLE IF NOT EXISTS jrock_state (
    id         SMALLINT PRIMARY KEY,
    version    INTEGER           NOT NULL,
    updated_ts DOUBLE PRECISION  NOT NULL,
    settings   JSONB             NOT NULL DEFAULT '{}'::jsonb,
    counters   JSONB             NOT NULL DEFAULT '{}'::jsonb,
    engine     JSONB             NOT NULL DEFAULT '{}'::jsonb,
    positions  JSONB             NOT NULL DEFAULT '{}'::jsonb,
    meta       JSONB             NOT NULL DEFAULT '{}'::jsonb
)
"""


class PostgresUnavailable(RuntimeError):
    """DATABASE_URL is set but the database cannot be used."""


def _redact(url: str) -> str:
    """A URL without the password - safe to log or to put in an error."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if not parts.netloc or "@" not in parts.netloc:
            return url
        userinfo, _, host = parts.netloc.rpartition("@")
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{host}" if ":" in userinfo else f"{user}@{host}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:  # noqa: BLE001 - redaction must never raise
        return "***"


class PostgresStore(Store):
    """The file store's twin, backed by Postgres.

    Same interface, same in-memory semantics: the sections are held in memory
    and `save()` writes the whole document. A `save()` is a single UPSERT, so
    a crash mid-write cannot leave half a state behind - the same guarantee
    the file store gets from its atomic write.
    """

    def __init__(self, dsn: str, *, connect_timeout: int = 10) -> None:
        try:
            import psycopg  # type: ignore
        except ImportError as e:  # pragma: no cover - depends on the host
            raise PostgresUnavailable(
                "DATABASE_URL is set but psycopg is not installed. "
                "Install it with: pip install 'psycopg[binary]'"
            ) from e
        self._psycopg = psycopg
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        # The file store's base class wants a path; this store has none.
        from pathlib import Path

        self.path = Path(_redact(dsn))
        self.version = SCHEMA_VERSION
        self.settings: dict[str, Any] = {}
        self.counters: dict[str, Any] = {}
        self.engine: dict[str, Any] = {}
        self.positions: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}
        self._secrets: list[str] = []
        self.loaded = False
        self.loaded_from = _redact(dsn)
        self.migrated_from: int | None = None
        self.recovered_backup = ""
        self.saves = 0
        self.last_save_ts = 0.0
        self.register_secrets = True
        self._load()

    # ------------------------------------------------------------------- io
    def _connect(self):
        try:
            return self._psycopg.connect(
                self.dsn, connect_timeout=self.connect_timeout,
                autocommit=True,
            )
        except Exception as e:  # noqa: BLE001 - every driver error means "no db"
            raise PostgresUnavailable(
                f"cannot reach the database at {_redact(self.dsn)}: "
                f"{type(e).__name__}: {e}"
            ) from e

    def _load(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute(
                    "SELECT version, updated_ts, settings, counters, engine, "
                    "positions, meta FROM jrock_state WHERE id = 1")
                row = cur.fetchone()
                if row is None:
                    # First start: create the row so the next save is an UPSERT
                    # of an existing row and the table state is never empty.
                    cur.execute(
                        "INSERT INTO jrock_state (id, version, updated_ts) "
                        "VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING",
                        (SCHEMA_VERSION, time.time()))
                    self.last_save_ts = time.time()
                    return
                version, updated_ts, settings, counters, engine, positions, meta = row
                self.version = int(version or SCHEMA_VERSION)
                self.settings = _as_dict(settings)
                self.counters = _as_dict(counters)
                self.engine = _as_dict(engine)
                self.positions = _as_dict(positions)
                self.meta = _as_dict(meta)
                self.last_save_ts = float(updated_ts or 0.0)
                self.loaded = True
                if self.version < SCHEMA_VERSION:
                    self.migrated_from = self.version
                    self.version = SCHEMA_VERSION
                    self.save()

    def save(self) -> None:
        payload = self._scrub(self._payload())
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jrock_state "
                    "(id, version, updated_ts, settings, counters, engine, "
                    " positions, meta) VALUES (1, %s, %s, %s::jsonb, %s::jsonb, "
                    " %s::jsonb, %s::jsonb, %s::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version, "
                    "updated_ts = EXCLUDED.updated_ts, settings = EXCLUDED.settings, "
                    "counters = EXCLUDED.counters, engine = EXCLUDED.engine, "
                    "positions = EXCLUDED.positions, meta = EXCLUDED.meta",
                    (payload["version"], payload["updated_ts"],
                     json.dumps(payload["settings"], default=str),
                     json.dumps(payload["counters"], default=str),
                     json.dumps(payload["engine"], default=str),
                     json.dumps(payload["positions"], default=str),
                     json.dumps(payload["meta"], default=str)))
        self.saves += 1
        self.last_save_ts = payload["updated_ts"]

    # ---------------------------------------------------------------- report
    def summary(self) -> dict[str, Any]:
        return {
            "kind": "database",
            "path": _redact(self.dsn),
            "version": self.version,
            "settings": len(self.settings),
            "engine_settings": len(self.engine),
            "positions": len(self.positions),
            "last_save": self.last_save_ts,
            "saves": self.saves,
            "size_bytes": 0,
            "loaded": self.loaded,
            "migrated_from": self.migrated_from,
            "recovered_backup": self.recovered_backup,
        }

    def size_bytes(self) -> int:
        return 0


def _as_dict(value: Any) -> dict[str, Any]:
    """psycopg hands back JSONB as a dict already; a string is possible too."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


__all__ = ["PostgresStore", "PostgresUnavailable", "DDL", "is_secret_key",
           "SecretInStoreError"]
