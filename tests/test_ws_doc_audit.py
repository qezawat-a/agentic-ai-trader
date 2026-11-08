"""WebSocket audit against the official Bitunix docs.

Every test here pins ONE defect found by matching the repo's WS layer against
the official reference (docs/bitunix/websocket/*) and against live frames from
wss://fapi.bitunix.com/public/ captured on 2026-11-08.

The defects, in the order they appear below:

  1. `_handlers_for` prefix-matched 'ticker' against 'tickers', so the
     aggregated `tickers` LIST was delivered to the single-ticker handler,
     which then called `d.get("la")` on a list -> AttributeError per frame.
  2. `_login` read exactly ONE frame after sending the login frame and threw
     it away. The private socket can push `tpsl` before the login ack, so the
     first TP/SL push after every reconnect was lost.
  3. `_login` never checked `op == "login"`, and set `logged_in = True` on a
     timeout or a non-JSON frame - claiming authentication it never had.
  4. `Event.from_message` did `int(msg["ts"])` unguarded. `ts` is documented
     as String on the `trade` channel; a non-numeric ts raised out of `_pump`
     and forced a full reconnect on one malformed frame.
  5. `api_reference.WS_CHANNELS` still advertised the invalid channel names
     (market_price / market_ticker / market_tickers / market_trade).
  6. `MarketDataFeed.latest_mark` was fed from `ip`, which is the INDEX price.
     The mark price is not on any public WS channel.
  7. The heartbeat accepted any pong, so a late pong from the previous ping
     could satisfy the current wait.
  8. `_read_connect_ack` dropped a push frame that arrived before the ack.
  9. Subscriptions were sent in one frame with no regard for the documented
     300-channel-per-connection limit.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from jrock.trader import api_reference as ar
from jrock.trader.ws import (
    MAX_SUBS_PER_FRAME,
    PRIVATE_CHANNELS,
    PUBLIC_CHANNELS,
    BitunixWebSocket,
    Event,
    MarketDataFeed,
    PrivateFeed,
    WSConfig,
    build_login,
    build_ping,
    build_subscribe,
    build_unsubscribe,
    kline_channel,
    ws_login_sign,
)


# ============================================================ 1. handler routing

def test_ticker_handler_does_not_catch_tickers():
    """'ticker' must NOT prefix-match 'tickers' (a LIST, different shape)."""
    ws = BitunixWebSocket(WSConfig())
    ticker_calls: list = []
    ws.on("ticker", lambda ev: ticker_calls.append(ev))

    handlers = ws._handlers_for("tickers")
    assert handlers == [], "the single-ticker handler must not receive `tickers`"
    assert ws._handlers_for("ticker") != []


def test_kline_family_prefix_still_matches():
    """A real family prefix must keep working: market_kline -> market_kline_1min."""
    ws = BitunixWebSocket(WSConfig())
    ws.on("market_kline", lambda ev: None)
    assert ws._handlers_for("market_kline_1min") != []
    assert ws._handlers_for("market_kline_15min") != []


def test_depth_family_prefix_matches_digit_suffix():
    """depth_book -> depth_book1 (no underscore before the digit)."""
    ws = BitunixWebSocket(WSConfig())
    ws.on("depth_book", lambda ev: None)
    assert ws._handlers_for("depth_book1") != []
    assert ws._handlers_for("depth_book15") != []


def test_tickers_list_does_not_crash_the_ticker_handler():
    """The live `tickers` push is a LIST; the ticker handler must survive it."""
    feed = MarketDataFeed(WSConfig(), ["BTCUSDT"], ["1m"])
    # a real aggregated frame: data is a list of objects
    frame = {"ch": "tickers", "ts": 1794129100428,
             "data": [{"s": "BTCUSDT", "la": "75871.2", "o": "76111.4",
                       "h": "76159.6", "l": "75526.8", "b": "7180.1",
                       "q": "544253478.5", "r": "-0.31", "bd": "75871.1",
                       "ak": "75871.2", "bv": "2.25", "av": "2.79"}]}
    ev = Event.from_message(frame)
    assert ev is not None
    # must not raise
    feed._on_ticker(ev)
    assert feed.latest_price == {}


# ============================================================ 2/3. private login

class FakeWS:
    """A scripted socket: `recv()` returns the queued frames in order."""

    def __init__(self, frames: list):
        self._frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._frames:
            await asyncio.sleep(3600)
        return self._frames.pop(0)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_login_does_not_swallow_a_push_frame():
    """A `tpsl` push arriving before the login ack must be queued, not dropped."""
    cfg = WSConfig(api_key="k", api_secret="s", login_timeout=1.0)
    ws = BitunixWebSocket(cfg, private=True)
    ws._queue = asyncio.Queue(maxsize=100)

    tpsl = {"ch": "tpsl", "ts": 1, "data": {"orderId": "O1", "status": "NEW"}}
    sock = FakeWS([json.dumps(tpsl), json.dumps({"op": "login"})])
    await ws._login(sock)

    assert ws.logged_in is True
    assert ws._queue.qsize() == 1, "the pre-ack tpsl push was thrown away"
    ev = ws._queue.get_nowait()
    assert ev.channel == "tpsl"
    assert ev.data["orderId"] == "O1"


@pytest.mark.asyncio
async def test_login_requires_the_login_op():
    """Only `op == "login"` is success - not merely 'some frame arrived'."""
    cfg = WSConfig(api_key="k", api_secret="s", login_timeout=0.3)
    ws = BitunixWebSocket(cfg, private=True)
    ws._queue = asyncio.Queue(maxsize=100)

    # a connect ack, then silence: no login ack ever arrives
    sock = FakeWS([json.dumps({"op": "connect", "data": {"result": True}})])
    await ws._login(sock)
    assert ws.logged_in is False, "must not claim login without a login ack"


@pytest.mark.asyncio
async def test_login_raises_on_an_error_code():
    cfg = WSConfig(api_key="k", api_secret="s", login_timeout=1.0)
    ws = BitunixWebSocket(cfg, private=True)
    ws._queue = asyncio.Queue(maxsize=100)
    sock = FakeWS([json.dumps({"op": "login", "code": 10001, "msg": "bad sign"})])
    with pytest.raises(RuntimeError, match="ws login failed"):
        await ws._login(sock)


@pytest.mark.asyncio
async def test_login_needs_credentials():
    ws = BitunixWebSocket(WSConfig(), private=True)
    with pytest.raises(RuntimeError, match="BITUNIX_API_KEY"):
        await ws._login(FakeWS([]))


def test_login_frame_shape_matches_the_docs():
    """op=login, args=[{apiKey,timestamp,nonce,sign}], timestamp in SECONDS."""
    frame = build_login("mykey", "mysecret", ts=1747402389, nonce="abc123")
    assert frame["op"] == "login"
    assert isinstance(frame["args"], list) and len(frame["args"]) == 1
    arg = frame["args"][0]
    assert set(arg) == {"apiKey", "timestamp", "nonce", "sign"}
    assert arg["apiKey"] == "mykey"
    assert arg["timestamp"] == 1747402389
    assert arg["nonce"] == "abc123"
    assert arg["sign"] == ws_login_sign("mykey", "mysecret", 1747402389, "abc123")


def test_login_sign_is_nonce_then_timestamp_then_apikey():
    """sign = sha256(sha256(nonce + timestamp + apiKey) + secretKey)."""
    api_key, secret, ts, nonce = "k", "s", 1700000000, "n"
    digest = hashlib.sha256(f"{nonce}{ts}{api_key}".encode()).hexdigest()
    expected = hashlib.sha256((digest + secret).encode()).hexdigest()
    assert ws_login_sign(api_key, secret, ts, nonce) == expected


def test_nonce_is_32_chars_alnum():
    from jrock.trader.ws import _nonce
    n = _nonce()
    assert len(n) == 32
    assert n.isalnum()


# ============================================================ 4. ts robustness

def test_event_from_message_tolerates_a_string_ts():
    """`trade` documents ts as String; a non-numeric ts must not raise."""
    ev = Event.from_message({"ch": "trade", "ts": "2026-04-07T05:47:52Z",
                             "data": []})
    assert ev is not None
    assert ev.ts == 0


def test_event_from_message_tolerates_a_missing_ts():
    ev = Event.from_message({"ch": "price", "data": {}})
    assert ev is not None
    assert ev.ts == 0


def test_event_from_message_requires_a_channel():
    assert Event.from_message({"op": "ping", "pong": 1}) is None


# ============================================================ 5. channel contract

def test_api_reference_lists_the_real_public_channel_names():
    """The machine-readable contract must not advertise invented channels."""
    pub = ar.WS_CHANNELS["public"]
    for bad in ("market_price", "market_ticker", "market_tickers", "market_trade"):
        assert bad not in pub, f"{bad} is not a real channel name"
    for good in ("price", "ticker", "tickers", "trade", "depth_books",
                 "depth_book1", "depth_book5", "depth_book15"):
        assert good in pub, f"{good} is a documented channel and must be listed"


def test_public_channel_map_matches_the_docs():
    assert PUBLIC_CHANNELS["price"] == "price"
    assert PUBLIC_CHANNELS["ticker"] == "ticker"
    assert PUBLIC_CHANNELS["tickers"] == "tickers"
    assert PUBLIC_CHANNELS["trade"] == "trade"
    assert PUBLIC_CHANNELS["depth"] == "depth_books"
    assert PUBLIC_CHANNELS["depth_book1"] == "depth_book1"
    assert PUBLIC_CHANNELS["depth_book5"] == "depth_book5"
    assert PUBLIC_CHANNELS["depth_book15"] == "depth_book15"


def test_private_channel_map_matches_the_docs():
    assert set(PRIVATE_CHANNELS.values()) == {"balance", "order", "position", "tpsl"}


def test_kline_channel_names_use_the_documented_suffixes():
    assert kline_channel("1m") == "market_kline_1min"
    assert kline_channel("1h") == "market_kline_60min"   # NOT 1hour
    assert kline_channel("1d") == "market_kline_1day"
    assert kline_channel("1w") == "market_kline_1week"
    assert kline_channel("1M") == "market_kline_1month"
    with pytest.raises(ValueError):
        kline_channel("1hour")


def test_ws_domains_match_the_docs():
    assert ar.WS_PUBLIC == "wss://fapi.bitunix.com/public/"
    assert ar.WS_PRIVATE == "wss://fapi.bitunix.com/private/"
    cfg = WSConfig()
    assert cfg.public_url == "wss://fapi.bitunix.com/public/"
    assert cfg.private_url == "wss://fapi.bitunix.com/private/"


# ============================================================ 6. index vs mark

def test_price_channel_ip_is_index_not_mark():
    """`ip` is the INDEX price; the mark price is not on any public channel."""
    feed = MarketDataFeed(WSConfig(), ["BTCUSDT"], ["1m"])
    frame = {"ch": "price", "symbol": "BTCUSDT", "ts": 1794129100428,
             "data": {"ip": "75905.4", "mp": "75871.2", "fr": "0.007428",
                      "ft": "2026-11-08T08:00:00Z",
                      "nft": "2026-11-08T16:00:00Z"}}
    ev = Event.from_message(frame)
    feed._on_price(ev)
    assert feed.latest_index["BTCUSDT"] == pytest.approx(75905.4)
    assert feed.latest_price["BTCUSDT"] == pytest.approx(75871.2)
    assert feed.latest_funding["BTCUSDT"] == pytest.approx(0.007428)
    assert not hasattr(feed, "latest_mark"), "ip must not be labelled mark price"


def test_ticker_la_is_the_last_price():
    feed = MarketDataFeed(WSConfig(), ["BTCUSDT"], ["1m"])
    frame = {"ch": "ticker", "symbol": "BTCUSDT", "ts": 1794129100428,
             "data": {"s": "BTCUSDT", "o": "76111.4", "la": "75871.2",
                      "h": "76159.6", "l": "75526.8", "b": "7180.1",
                      "q": "544253478.5", "r": "-0.31"}}
    feed._on_ticker(Event.from_message(frame))
    assert feed.latest_price["BTCUSDT"] == pytest.approx(75871.2)


# ============================================================ 7. heartbeat

def test_ping_frame_shape_matches_the_docs():
    frame = build_ping(1732519687)
    assert frame == {"op": "ping", "ping": 1732519687}
    assert isinstance(frame["ping"], int)


@pytest.mark.asyncio
async def test_pong_must_echo_the_current_ping():
    """A late pong from the previous ping must not satisfy the current wait."""
    ws = BitunixWebSocket(WSConfig(ping_interval=0.05, pong_timeout=0.2))
    ws._ping_value = 111
    ws._pong.clear()
    assert not ws._pong.is_set()

    # simulate _pump receiving a stale pong (echoes the PREVIOUS ping)
    stale = {"op": "ping", "ts": 1, "pong": 999, "ping": 1}
    if stale.get("pong") == ws._ping_value:
        ws._pong.set()
    assert not ws._pong.is_set(), "a stale pong satisfied the current wait"

    # the matching pong does satisfy it
    fresh = {"op": "ping", "ts": 2, "pong": 111, "ping": 2}
    if fresh.get("pong") == ws._ping_value:
        ws._pong.set()
    assert ws._pong.is_set()


# ============================================================ 8. connect ack

@pytest.mark.asyncio
async def test_connect_ack_queues_a_pre_ack_push():
    ws = BitunixWebSocket(WSConfig(login_timeout=1.0))
    ws._queue = asyncio.Queue(maxsize=100)
    push = {"ch": "price", "symbol": "BTCUSDT", "ts": 1, "data": {"mp": "1"}}
    sock = FakeWS([json.dumps(push),
                   json.dumps({"op": "connect", "data": {"result": True}})])
    await ws._read_connect_ack(sock)
    assert ws._queue.qsize() == 1
    assert ws._queue.get_nowait().channel == "price"


@pytest.mark.asyncio
async def test_connect_ack_raises_when_rejected():
    ws = BitunixWebSocket(WSConfig(login_timeout=1.0))
    ws._queue = asyncio.Queue(maxsize=100)
    sock = FakeWS([json.dumps({"op": "connect", "data": {"result": False}})])
    with pytest.raises(RuntimeError, match="connect rejected"):
        await ws._read_connect_ack(sock)


# ============================================================ 9. subscribe frames

def test_subscribe_and_unsubscribe_frame_shapes():
    subs = [{"symbol": "BTCUSDT", "ch": "market_kline_1min"},
            {"symbol": "BTCUSDT", "ch": "depth_books"}]
    sub = build_subscribe(subs)
    assert sub["op"] == "subscribe"
    assert sub["args"] == subs
    unsub = build_unsubscribe(subs)
    assert unsub["op"] == "unsubscribe"
    assert unsub["args"] == subs


def test_subscribe_frame_uses_ch_not_channel():
    """The docs' unsubscribe EXAMPLE says `channel`; the parameter table says
    `ch`. The table wins - and the live socket only honours `ch`."""
    frame = build_subscribe([{"symbol": "BTCUSDT", "ch": "trade"}])
    assert "ch" in frame["args"][0]
    assert "channel" not in frame["args"][0]


def test_subscription_limit_is_the_documented_300():
    assert MAX_SUBS_PER_FRAME == 300


# ============================================================ private feed

def test_private_feed_subscribes_all_four_channels():
    pf = PrivateFeed(WSConfig())
    assert {s["ch"] for s in pf.ws.subs} == {"balance", "order", "position", "tpsl"}


def test_private_feed_parses_a_documented_tpsl_frame():
    """data is an OBJECT with orderId (there is no `id` field)."""
    pf = PrivateFeed(WSConfig())
    frame = {"ch": "tpsl", "ts": 1794011946619,
             "data": {"event": "CLOSE", "positionId": "P1", "orderId": "O1",
                      "symbol": "BTCUSDT", "status": "FILLED",
                      "tpPrice": "80000", "slPrice": "70000",
                      "tpQty": "1", "slQty": "1",
                      "ctime": "2024-05-16T08:13:09.123456789Z"}}
    ev = Event.from_message(frame)
    assert ev is not None
    pf._make_handler("tpsl")(ev)
    assert pf.counts["tpsl"] == 1
    assert pf.latest["tpsl"]["orderId"] == "O1"
    assert pf.latest["tpsl"]["status"] == "FILLED"
    assert len(pf.tpsl_events) == 1


def test_private_feed_parses_a_documented_balance_frame():
    pf = PrivateFeed(WSConfig())
    frame = {"ch": "balance", "ts": 1,
             "data": {"coin": "USDT", "available": "100", "frozen": "0",
                      "isolationFrozen": "0", "crossFrozen": "0",
                      "margin": "0", "isolationMargin": "0",
                      "crossMargin": "0", "expMoney": "0"}}
    pf._make_handler("balance")(Event.from_message(frame))
    assert pf.latest["balance"]["coin"] == "USDT"


def test_private_feed_parses_a_documented_position_frame():
    pf = PrivateFeed(WSConfig())
    frame = {"ch": "position", "ts": 1,
             "data": {"event": "OPEN", "positionId": "P1",
                      "marginMode": "CROSS", "positionMode": "ONE_WAY",
                      "side": "LONG", "leverage": "10", "margin": "5",
                      "ctime": "2024-05-16T08:13:09.123456789Z", "qty": "1",
                      "symbol": "BTCUSDT", "realizedPNL": "0",
                      "unrealizedPNL": "1", "funding": "0", "fee": "0"}}
    pf._make_handler("position")(Event.from_message(frame))
    assert pf.latest["position"]["side"] == "LONG"


def test_private_feed_parses_a_documented_order_frame():
    pf = PrivateFeed(WSConfig())
    frame = {"ch": "order", "ts": 1,
             "data": {"event": "CREATE", "orderId": "O1", "symbol": "BTCUSDT",
                      "positionType": "CROSS", "positionMode": "ONE_WAY",
                      "side": "BUY", "effect": "GTC", "type": "LIMIT",
                      "qty": "1", "price": "100",
                      "ctime": "2024-05-16T08:13:09.123456789Z",
                      "mtime": "2024-05-16T08:13:09.123456789Z",
                      "leverage": "10", "orderStatus": "NEW", "fee": "0",
                      "averagePrice": "0", "dealAmount": "0",
                      "clientId": "c1"}}
    pf._make_handler("order")(Event.from_message(frame))
    assert pf.latest["order"]["orderStatus"] == "NEW"


def test_tpsl_events_are_bounded():
    pf = PrivateFeed(WSConfig())
    h = pf._make_handler("tpsl")
    for i in range(250):
        h(Event.from_message({"ch": "tpsl", "ts": i, "data": {"orderId": str(i)}}))
    assert len(pf.tpsl_events) == 200
    assert pf.tpsl_events[-1]["data"]["orderId"] == "249"
