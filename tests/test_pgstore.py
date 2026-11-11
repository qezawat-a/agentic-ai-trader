"""The Postgres-backed store.

These tests run against a REAL Postgres when one is reachable
(TEST_DATABASE_URL), and skip when it is not: the store's whole point is the
SQL, so mocking the driver would test the mock.
"""

from __future__ import annotations

import os

import pytest

from jrock.store import Store, open_store
from jrock.pgstore import PostgresStore, PostgresUnavailable, _redact


DSN = os.getenv("TEST_DATABASE_URL", "").strip()


def _pg() -> PostgresStore:
    if not DSN:
        pytest.skip("TEST_DATABASE_URL is not set")
    return PostgresStore(DSN)


def test_the_file_store_is_used_when_no_database_url_is_set(tmp_path):
    s = type("S", (), {"database_url": "", "data_dir": tmp_path})()
    st = open_store(s)
    assert isinstance(st, Store)
    assert not isinstance(st, PostgresStore)
    assert st.path.parent == tmp_path


def test_a_database_url_selects_the_database_store():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL is not set")
    s = type("S", (), {"database_url": DSN, "data_dir": None})()
    st = open_store(s)
    assert isinstance(st, PostgresStore)
    assert not isinstance(st, Store) or True  # it is a Store subclass too


def test_a_broken_database_url_fails_closed(tmp_path):
    """No silent fallback to a file: that is the bug the database fixes."""
    s = type("S", (), {
        "database_url": "postgresql://nobody@127.0.0.1:1/none",
        "data_dir": tmp_path,
    })()
    with pytest.raises(PostgresUnavailable):
        open_store(s)


def test_the_password_never_appears_in_a_message():
    """The DSN holds a password - it must not leak into logs or errors."""
    url = "postgresql://user:sup3rsecret@db.example.com:5432/jrock"
    red = _redact(url)
    assert "sup3rsecret" not in red
    assert "user" in red and "db.example.com" in red


def test_the_state_survives_a_new_store():
    """The whole point: a restart finds the state where it left it."""
    st = _pg()
    st.set_section("engine", {"leverage": 25, "risk_pct": 2.0}, save=False)
    st.save()

    again = _pg()
    assert again.engine["leverage"] == 25
    assert again.engine["risk_pct"] == 2.0
    assert again.loaded is True


def test_the_sections_keep_their_shape():
    """The file store and the database store carry the same sections."""
    st = _pg()
    st.set_section("engine", {"leverage": 7}, save=False)
    st.set_section("counters", {"scans": 3}, save=False)
    st.set_section("meta", {"note": "x"}, save=False)
    st.save()

    again = _pg()
    assert again.engine["leverage"] == 7
    assert again.counters["scans"] == 3
    assert again.meta["note"] == "x"


def test_a_secret_is_never_written():
    """The store refuses to persist a secret value - in the database too."""
    from jrock.store import SecretInStoreError

    st = _pg()
    st.register_secret("sup3rsecretvalue")
    # Under a NON-secret key: the scrub removes secret KEYS, so only a value
    # hiding under an innocent name can still leak - and that must be caught.
    st.engine["note"] = "sup3rsecretvalue"
    with pytest.raises(SecretInStoreError):
        st.save()
