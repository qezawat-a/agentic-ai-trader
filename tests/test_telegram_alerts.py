"""Trade alerts: the bot must tell the operator when a position opens/closes.

The source is the engine's own ``data/trades.jsonl``. Using that file (instead
of a second exchange poll) means the alert and the recorded trade are the same
event, and it works when the engine runs in a different process.

No test touches the network or a real exchange.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from jrock import telegram_bot as tb


class RecordingBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, chat_id, text, *, markdown=False):
        self.sent.append((chat_id, text, markdown))
        return True


class EngineStub:
    def __init__(self, path):
        self.trades_path = path


def _app(path, chats=(42,)):
    app = tb.JRockTelegramApp(
        agent=SimpleNamespace(engine=EngineStub(path)), bot=RecordingBot())
    app.chats.update(chats)
    return app


def _append(path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _init(app) -> None:
    """First poll starts at EOF - a restart must not replay old trades."""
    asyncio.run(app._push_trade_alerts_once())
    assert app.bot.sent == []


def test_open_alert_carries_the_order_facts(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path)
    _init(app)

    _append(path, {
        "event": "open", "symbol": "BTCUSDT", "side": "LONG",
        "qty": "0.5", "entry": 60000.0, "order_id": "123",
        "confidence": 82.5, "strength": "strong", "agreeing": 4,
        "plan": {"sl": 59000.0, "tp": 62000.0},
    })
    asyncio.run(app._push_trade_alerts_once())

    (chat, text, md), = app.bot.sent
    assert chat == 42 and md is True
    assert "POSITION OPENED" in text
    assert "BTCUSDT LONG" in text
    assert "qty 0.5 @ 60000.0" in text
    assert "82.5% (strong), 4 agreeing" in text
    assert "SL 59000.0" in text and "TP 62000.0" in text
    assert "order 123" in text


def test_close_alert_carries_the_reason(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path)
    _init(app)

    _append(path, {
        "event": "close", "symbol": "ETHUSDT", "position_id": "P1",
        "reason": "account_tp", "via": "close_all_position",
    })
    asyncio.run(app._push_trade_alerts_once())

    text = app.bot.sent[-1][1]
    assert "POSITION CLOSED" in text
    assert "ETHUSDT" in text
    assert "reason: account_tp" in text
    assert "via close_all_position" in text


def test_closed_on_exchange_alert(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path)
    _init(app)

    _append(path, {
        "event": "closed_on_exchange", "symbol": "BTCUSDT", "side": "LONG",
        "qty": "0.5", "entry": 60000.0, "position_id": "P9",
    })
    asyncio.run(app._push_trade_alerts_once())

    text = app.bot.sent[-1][1]
    assert "CLOSED ON EXCHANGE" in text
    assert "only" not in text  # no invented claim about WHY it closed
    assert "TP/SL or manual close" in text
    assert "P9" in text


def test_close_failure_is_an_alert_too(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path)
    _init(app)

    _append(path, {
        "event": "close_failed", "symbol": "BTCUSDT", "position_id": "P2",
        "reason": "user", "error": "insufficient margin",
    })
    asyncio.run(app._push_trade_alerts_once())

    text = app.bot.sent[-1][1]
    assert "CLOSE FAILED" in text
    assert "insufficient margin" in text


def test_restart_does_not_replay_old_events(tmp_path):
    path = tmp_path / "trades.jsonl"
    _append(path, {"event": "open", "symbol": "OLDUSDT"})

    app = _app(path)
    _init(app)
    assert app.bot.sent == []

    _append(path, {"event": "close", "symbol": "NEWUSDT", "reason": "user"})
    asyncio.run(app._push_trade_alerts_once())

    assert len(app.bot.sent) == 1
    assert "NEWUSDT" in app.bot.sent[0][1]
    assert "OLDUSDT" not in app.bot.sent[0][1]


def test_no_chat_consumes_events_without_spamming(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path, chats=())
    _init(app)

    _append(path, {"event": "open", "symbol": "BTCUSDT", "side": "LONG"})
    asyncio.run(app._push_trade_alerts_once())
    assert app.bot.sent == []

    # The first /start must not dump the events that happened before it.
    app.chats.add(7)
    _append(path, {"event": "close", "symbol": "BTCUSDT", "reason": "user"})
    asyncio.run(app._push_trade_alerts_once())

    assert len(app.bot.sent) == 1
    assert app.bot.sent[0][0] == 7
    assert "POSITION CLOSED" in app.bot.sent[0][1]


def test_a_half_written_line_waits_for_the_rest(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path)
    _init(app)

    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "open", "symbol": "BTC')
    asyncio.run(app._push_trade_alerts_once())
    assert app.bot.sent == []

    with path.open("a", encoding="utf-8") as fh:
        fh.write('USDT", "side": "LONG"}\n')
    asyncio.run(app._push_trade_alerts_once())

    assert len(app.bot.sent) == 1
    assert "BTCUSDT" in app.bot.sent[0][1]


def test_non_alert_events_are_ignored(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path)
    _init(app)

    _append(path, {"event": "placed", "symbol": "BTCUSDT"})
    _append(path, {"event": "qty_reconciled", "symbol": "BTCUSDT"})
    asyncio.run(app._push_trade_alerts_once())
    assert app.bot.sent == []


def test_alert_text_escapes_exchange_text(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path)
    _init(app)

    _append(path, {"event": "close", "symbol": "<b>x</b>", "reason": "user"})
    asyncio.run(app._push_trade_alerts_once())

    text = app.bot.sent[-1][1]
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "<b>x</b>" not in text


def test_alerts_go_to_every_known_chat(tmp_path):
    path = tmp_path / "trades.jsonl"
    app = _app(path, chats=(7, 9))
    _init(app)

    _append(path, {"event": "open", "symbol": "BTCUSDT", "side": "SHORT"})
    asyncio.run(app._push_trade_alerts_once())

    assert [c for c, _, _ in app.bot.sent] == [7, 9]
    assert all("POSITION OPENED" in t for _, t, _ in app.bot.sent)
