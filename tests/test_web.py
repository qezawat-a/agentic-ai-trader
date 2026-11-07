"""Tests for the web dashboard controller (jrock/web.py)."""
from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.web import make_app

TOKEN = "test-token"


def _agent(tmp_path) -> JRockAgent:
    s = Settings()
    s.data_dir  # noqa: B018  (ensures dir exists)
    a = JRockAgent(s)
    a.engine.data_dir = tmp_path
    return a


def _run(coro):
    return asyncio.run(coro)


async def _with_client(tmp_path, fn):
    agent = _agent(tmp_path)
    app = make_app(agent, TOKEN)
    # Skip the startup hook: it calls the network (model probe, Bitunix auth).
    app.on_startup.clear()
    app.on_cleanup.clear()
    async with TestClient(TestServer(app)) as client:
        return await fn(client)


def test_command_rejects_missing_token(tmp_path):
    async def go(client):
        r = await client.post("/api/command", json={"line": "help"})
        assert r.status == 401
    _run(_with_client(tmp_path, go))


def test_command_rejects_wrong_token(tmp_path):
    async def go(client):
        r = await client.post("/api/command", json={"line": "help"},
                              headers={"X-Auth-Token": "nope"})
        assert r.status == 401
    _run(_with_client(tmp_path, go))


def test_command_routes_help_through_agent(tmp_path):
    async def go(client):
        r = await client.post("/api/command", json={"line": "/help"},
                              headers={"X-Auth-Token": TOKEN})
        assert r.status == 200
        body = await r.json()
        assert "J+Rock commands" in body["output"]
    _run(_with_client(tmp_path, go))


def test_command_rejects_empty_line(tmp_path):
    async def go(client):
        r = await client.post("/api/command", json={"line": "   "},
                              headers={"X-Auth-Token": TOKEN})
        assert r.status == 400
    _run(_with_client(tmp_path, go))


def test_command_rejects_non_json(tmp_path):
    async def go(client):
        r = await client.post("/api/command", data="not json",
                              headers={"X-Auth-Token": TOKEN})
        assert r.status == 400
    _run(_with_client(tmp_path, go))


def test_status_requires_token_and_reports_switches(tmp_path):
    async def go(client):
        assert (await client.get("/api/status")).status == 401
        r = await client.get("/api/status", headers={"X-Auth-Token": TOKEN})
        assert r.status == 200
        body = await r.json()
        assert body["autotrade"] is False
        assert body["engine_on"] is False
        assert "leverage" in body and "symbols" in body
    _run(_with_client(tmp_path, go))


def test_report_requires_token(tmp_path):
    async def go(client):
        assert (await client.get("/api/report")).status == 401
    _run(_with_client(tmp_path, go))


def test_report_returns_latest_text(tmp_path):
    async def go(client):
        agent = client.app["agent"]
        agent.engine.last_report = "=== J+Rock report 12:00:00 ==="
        r = await client.get("/api/report", headers={"X-Auth-Token": TOKEN})
        assert r.status == 200
        body = await r.json()
        assert body["report"] == "=== J+Rock report 12:00:00 ==="
        assert body["report_on"] is False
    _run(_with_client(tmp_path, go))


def test_mid_management_trigger_runs_one_pass_and_keeps_toggle(tmp_path, monkeypatch):
    """`mid management trigger` runs manage_positions once; it must not flip the toggle."""
    async def go():
        agent = _agent(tmp_path)
        calls = []

        async def fake_manage_positions():
            calls.append(1)
            return []

        monkeypatch.setattr(agent.engine, "manage_positions", fake_manage_positions)
        app = make_app(agent, TOKEN)
        app.on_startup.clear()
        app.on_cleanup.clear()
        async with TestClient(TestServer(app)) as client:
            headers = {"X-Auth-Token": TOKEN}

            # Bare command flips the toggle: the first flip reports the new state.
            r = await client.post("/api/command", json={"line": "mid management"},
                                  headers=headers)
            first = (await r.json())["output"]
            assert "Mid management = " in first

            # The trigger runs one pass and must not report a toggle change.
            r = await client.post("/api/command",
                                  json={"line": "mid management trigger"},
                                  headers=headers)
            assert r.status == 200
            out = (await r.json())["output"]
            assert "Mid management pass done" in out
            assert "Mid management = " not in out
            assert len(calls) == 1

            # A second bare command must flip back, proving the trigger left state alone.
            r = await client.post("/api/command", json={"line": "mid management"},
                                  headers=headers)
            second = (await r.json())["output"]
            assert first != second
    _run(go())


def test_index_requires_token(tmp_path):
    async def go(client):
        assert (await client.get("/")).status == 401
        r = await client.get(f"/?token={TOKEN}")
        assert r.status == 200
        assert "J+Rock dashboard" in await r.text()
    _run(_with_client(tmp_path, go))
