""""Position history" must show the PAST, not answer with the present.

The operator typed "Position history" and the bot answered "no open
positions" - the singular/reversed phrasing fell through to the open
positions intent, and the real histories (closed positions from the
exchange, the local TP/SL and trade logs) were only reachable with the
exact phrase "positions history" - as a raw JSON dump.

Now:
  - "position history", "history position", "trade history" -> the
    readable history view (exchange positions + tpsl.jsonl + trades.jsonl)
  - "/positions history" (what the model itself suggests) -> the same view
  - "positions history" (exact) stays the raw JSON command
  - get_history_positions unwraps the official {"positionList": [...]}
    envelope instead of dumping it raw
"""
from __future__ import annotations

import asyncio
import json

from jrock.agent import JRockAgent
from jrock.config import Settings
from jrock.nlu import parse

CLOSED = {
    "positionList": [
        {"positionId": "p1", "symbol": "INITUSDT", "side": "LONG",
         "entryPrice": "0.23528", "closePrice": "0.23000",
         "realizedPNL": "-0.45", "fee": "0.02", "funding": "-0.01",
         "leverage": 25, "ctime": 1760000000000, "mtime": 1760001000000},
    ],
    "total": 1,
}


class HistoryClient:
    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error

    async def get_history_positions(self, **kwargs):
        if self._error is not None:
            raise self._error
        return self._data

    async def get_pending_positions(self):
        return []


class Boom(Exception):
    pass


def _agent(tmp_path, client=None) -> JRockAgent:
    a = JRockAgent(Settings(bitunix_api_key="k", bitunix_api_secret="s"))
    a.engine.data_dir = tmp_path
    # The jsonl paths are derived in __init__; re-point them at the tmp dir.
    a.engine.trades_path = tmp_path / "trades.jsonl"
    a.engine.tpsl_path = tmp_path / "tpsl.jsonl"
    if client is not None:
        a.engine.client = client  # type: ignore[assignment]
    return a


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ nlu
def test_position_history_phrases_route_to_history_intent():
    intent = __import__("jrock.nlu", fromlist=["Intent"]).Intent
    for text in ("position history", "history position", "positions history",
                 "show my history", "my trade history", "previous positions",
                 "تاریخچه پوزیشن ها"):
        parsed = parse(text)
        assert isinstance(parsed, intent), text
        assert parsed.command == "history", text


def test_open_positions_question_still_routes_to_positions():
    intent = parse("show my positions")
    assert intent.command == "positions"


# ------------------------------------------------------- engine rendering
def test_render_history_formats_exchange_positions(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    out = _run(a.engine.render_history())
    assert "INITUSDT LONG 0.23528 -> 0.23000" in out
    assert "realized=-0.45" in out
    assert "25x" in out
    assert '{"positionList"' not in out


def test_render_history_says_when_the_exchange_has_nothing(tmp_path):
    a = _agent(tmp_path, HistoryClient({"positionList": [], "total": 0}))
    out = _run(a.engine.render_history())
    assert "no closed positions on the exchange yet" in out


def test_positions_history_unwraps_the_position_list(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    rows = _run(a.engine.positions_history())
    assert rows and rows[0]["symbol"] == "INITUSDT"


def test_render_history_includes_local_logs(tmp_path):
    a = _agent(tmp_path, HistoryClient({"positionList": [], "total": 0}))
    (tmp_path / "trades.jsonl").write_text(
        json.dumps({"event": "open", "ts": 1760000000.0, "symbol": "ONEUSDT",
                    "side": "LONG", "qty": 12, "entry": 0.19}) + "\n", encoding="utf-8")
    (tmp_path / "tpsl.jsonl").write_text(
        json.dumps({"event": "placed", "ts": 1760000001.0, "symbol": "ONEUSDT",
                    "method": "PARTIAL"}) + "\n", encoding="utf-8")
    out = _run(a.engine.render_history())
    assert "-- TP/SL events (local log) --" in out
    assert "placed" in out and "method=PARTIAL" in out
    assert "-- trades (local log) --" in out
    assert "open" in out and "ONEUSDT" in out and "entry=0.19" in out


def test_render_history_survives_an_api_error(tmp_path):
    a = _agent(tmp_path, HistoryClient(error=Boom("boom")))
    out = _run(a.engine.render_history())
    assert "API error" in out
    assert "-- trades (local log) --" in out


def test_engine_command_history_and_aliases(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    bare = _run(a.engine.command("history"))
    assert "INITUSDT" in bare
    assert _run(a.engine.command("position history")) == bare
    assert _run(a.engine.command("history position")) == bare
    assert a.engine.looks_like_command("history")


def test_positions_history_json_command_is_unchanged(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    out = _run(a.engine.command("positions history"))
    assert json.loads(out)[0]["symbol"] == "INITUSDT"


# ------------------------------------------------------------ agent routing
def test_agent_slash_positions_history_routes_to_history(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    out = _run(a.handle("/positions history"))
    assert "INITUSDT" in out


def test_agent_bare_history_routes_to_the_view(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    out = _run(a.handle("history"))
    assert "INITUSDT" in out


def test_agent_plain_position_history_routes_to_the_view(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    out = _run(a.handle("position history"))
    assert "INITUSDT" in out


def test_agent_persian_history_word_routes_to_the_view(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    out = _run(a.handle("تاریخچه"))
    assert "INITUSDT" in out


def test_agent_positions_without_history_still_shows_current(tmp_path):
    a = _agent(tmp_path, HistoryClient(CLOSED))
    out = _run(a.handle("/positions"))
    assert out == "no open positions"
