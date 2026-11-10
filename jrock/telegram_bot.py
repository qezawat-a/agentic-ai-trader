"""Telegram front-end for the J+Rock trading engine.

A chat replacement for the local web dashboard: the same read-only view of
positions, open orders, PnL and engine state, delivered in a Telegram chat
instead of a browser tab at 127.0.0.1:8080.

Design notes
------------
* **No token in code.** The bot token is read from ``TELEGRAM_BOT_TOKEN``. A
  missing token is a clear startup error, never a silent no-op.
* **No second implementation of the data.** Everything here calls the same
  ``build_state`` the dashboard uses (and the same ``agent.handle`` command
  router), so the numbers cannot drift between the two front-ends. That is
  also what keeps this module deletable if the dashboard ever goes away.
* **Read-only plus the existing command surface.** The bot does not invent
  new trading actions: ``/orders`` and friends go through ``agent.handle``,
  so validation, clamping and persistence stay the engine's job.
* **Long polling, no webhook.** Runs anywhere including a phone on Termux -
  no public URL, no TLS, no port to expose.

Run:
    export TELEGRAM_BOT_TOKEN=...
    python -m jrock.telegram_bot
"""
from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import time
from typing import Any

import aiohttp

log = logging.getLogger("jrock.telegram_bot")

API_BASE = "https://api.telegram.org"
#: Telegram's hard limit for one message body.
MAX_MESSAGE = 4000

#: The command list published with ``setMyCommands``. Telegram renders these
#: behind the Menu button in the BOTTOM-LEFT corner of the chat, so the operator
#: sees what the bot can do without typing ``/help`` first. Order matters: it is
#: the order Telegram displays them in.
COMMANDS: list[tuple[str, str]] = [
    ("status", "engine + account + positions"),
    ("positions", "open positions"),
    ("orders", "pending orders"),
    ("pnl", "account and PnL"),
    ("engine", "engine flags"),
    ("ladder", "TP ladder state"),
    ("help", "this message"),
]

#: A persistent reply keyboard, also bottom-left. ``setMyCommands`` only shows
#: the commands in the Menu sheet; this puts the four most-used ones one tap
#: away, permanently, without the operator opening anything.
REPLY_KEYBOARD: dict[str, Any] = {
    "keyboard": [
        [{"text": "/status"}, {"text": "/positions"}],
        [{"text": "/orders"}, {"text": "/pnl"}],
        [{"text": "/ladder"}, {"text": "/help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "J+Rock - tap a command or type",
}


class TelegramError(RuntimeError):
    """The Bot API refused a call. Carries the description Telegram sent."""


# --------------------------------------------------------------------- client
class TelegramBot:
    """Minimal Bot API client - sendMessage / getUpdates only.

    Deliberately dependency-free (no python-telegram-bot) so the trading
    environment does not grow a large async framework for two endpoints.
    """

    def __init__(self, token: str, session: aiohttp.ClientSession):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is empty")
        self._token = token
        self._session = session
        self._base = f"{API_BASE}/bot{token}"

    async def call(self, method: str, **params: Any) -> Any:
        url = f"{self._base}/{method}"
        async with self._session.post(url, json=params,
                                      timeout=aiohttp.ClientTimeout(total=60)) as r:
            try:
                payload = await r.json()
            except Exception as e:  # noqa: BLE001
                raise TelegramError(f"{method}: non-JSON reply ({e})") from None
        if not payload.get("ok"):
            # NEVER include the URL - it contains the token.
            raise TelegramError(
                f"{method}: {payload.get('description') or 'unknown error'} "
                f"(code {payload.get('error_code')})")
        return payload.get("result")

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def poll(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message"])

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> Any:
        """Publish the command list Telegram shows in the bottom-left Menu.

        This is what makes the commands appear WITHOUT the operator typing
        ``/help`` first: Telegram renders them behind the Menu button that sits
        in the bottom-left corner of the chat, and the client caches them, so
        they are there on the next time the chat is opened.
        """
        return await self.call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands])

    async def send(self, chat_id: int | str, text: str,
                   *, markdown: bool = False, keyboard: bool = True) -> Any:
        for i, chunk in enumerate(_split(text, MAX_MESSAGE)):
            params: dict[str, Any] = {
                "chat_id": chat_id, "text": chunk,
                "parse_mode": "HTML" if markdown else None,
                "disable_web_page_preview": True,
            }
            # The reply keyboard rides on the LAST chunk only, so a long report
            # does not stack one keyboard per message.
            if keyboard and i == len(_split(text, MAX_MESSAGE)) - 1:
                params["reply_markup"] = REPLY_KEYBOARD
            await self.call("sendMessage", **params)
        return True


# ------------------------------------------------------------------- rendering
def _split(text: str, limit: int) -> list[str]:
    """Split on line boundaries so a long report stays readable."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit:
            if cur:
                out.append(cur)
            cur = ""
            while len(line) > limit:      # a single pathological line
                out.append(line[:limit])
                line = line[limit:]
        cur += line
    if cur:
        out.append(cur)
    return out


def _e(value: Any) -> str:
    """Escape for Telegram HTML mode - message content is untrusted text."""
    return html.escape("" if value is None else str(value), quote=False)


def _money(value: Any, places: int = 2) -> str:
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return "?"


def _signed(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{v:+,.2f}"


def render_positions(state: dict) -> str:
    rows = state.get("positions") or []
    if not rows:
        return "<b>Positions</b>\nno open positions"
    lines = [f"<b>Positions</b> ({len(rows)})"]
    for p in rows:
        pnl = _signed(p.get("unrealized"))
        lines.append(
            f"\n<b>{_e(p.get('symbol'))}</b> {_e(p.get('side'))}\n"
            f"  qty {_e(p.get('qty'))} @ {_e(p.get('entry'))}  (mark {_e(p.get('mark'))})\n"
            f"  uPnL <b>{pnl}</b>  ROI {_e(p.get('roi'))}%  lev {_e(p.get('leverage'))}x\n"
            f"  liq {_e(p.get('liq'))}  mode {_e(p.get('margin_mode'))}")
    return "\n".join(lines)


def render_orders(state: dict) -> str:
    rows = state.get("orders") or []
    if not rows:
        return "<b>Open orders</b>\nno pending orders"
    lines = [f"<b>Open orders</b> ({len(rows)})"]
    for o in rows:
        lines.append(
            f"\n<b>{_e(o.get('symbol'))}</b> {_e(o.get('side'))} {_e(o.get('type'))}\n"
            f"  qty {_e(o.get('qty'))}  price {_e(o.get('price'))}\n"
            f"  status {_e(o.get('status'))}  id {_e(o.get('id'))}")
    return "\n".join(lines)


def render_pnl(state: dict) -> str:
    a = state.get("account") or {}
    return (
        "<b>PnL / account</b>\n"
        f"equity      <b>{_money(a.get('equity'))}</b>\n"
        f"available   {_money(a.get('available'))}\n"
        f"margin used {_money(a.get('margin'))}\n"
        f"unrealized  <b>{_signed(a.get('unrealized'))}</b>\n"
        f"realized    {_signed(a.get('realized'))}\n"
        f"open        {_e(a.get('open_positions'))} positions, "
        f"{_e(a.get('open_orders'))} orders")


def render_engine(state: dict) -> str:
    e = state.get("engine") or {}
    on = lambda b: "on" if b else "off"          # noqa: E731
    return (
        "<b>Engine</b>\n"
        f"engine {on(e.get('engine_on'))}  autotrade {on(e.get('autotrade'))}  "
        f"scan {on(e.get('scan_on'))}\n"
        f"symbols {_e(', '.join(e.get('symbols') or []))}\n"
        f"leverage {_e(e.get('leverage'))}x  tpsl {_e(e.get('tpsl_method'))}\n"
        f"ladder runner {_e(e.get('tp_ladder_runner'))} "
        f"basis {_e(e.get('tp_ladder_trigger_basis'))}\n"
        f"scans {_e(e.get('scans'))}  errors {_e(e.get('errors'))}\n"
        + (f"<code>{_e(e.get('last_error'))}</code>" if e.get("last_error") else "")
    )


def render_summary(state: dict) -> str:
    """The one-screen view: what the dashboard opens on."""
    return "\n\n".join([
        f"<b>J+Rock</b>  updated {_e(state.get('updated_at'))}",
        render_engine(state),
        render_pnl(state),
        render_positions(state),
    ])


#: Topics the Menu exposes one level deep: ``/help engine`` etc. These are
#: exactly the keys the CLI router accepts after ``/help``, so the two surfaces
#: (Telegram menu and CLI help) stay aligned.
HELP_TOPICS: dict[str, str] = {
    "engine": "engine + account + positions (same as /status)",
    "positions": "open positions",
    "orders": "pending orders",
    "pnl": "account and PnL",
    "ladder": "TP ladder state",
}


def _strip_code(text: str) -> str:
    """Strip one layer of Telegram/CLI code fence around a command.

    ``/help engine`` rendered as a code block arrives as
    `````/help engine````` or `` ` ``` /help engine ``` ``` ` `` - the router
    split on whitespace and took the backticks with the command, so it fell
    through to the agent. Stripping a fencing layer (backticks, or the triple
    `` ``` `` band) makes the same tap work whether Telegram renders it as a
    button or as a code snippet.
    """
    s = text.strip()
    # Triple fence first, then a single backtick. Each is stripped at most
    # once, and only when it actually wraps the whole string - a bare
    # ``/help`` must not lose a character.
    for wrap in ("```", "`"):
        if len(s) > 2 * len(wrap) and s.startswith(wrap) and s.endswith(wrap):
            s = s[len(wrap): -len(wrap)].strip()
    return s


def _split_command(text: str) -> tuple[str, str]:
    """Return (command, rest) for a Telegram message.

    Handles ``/status``, ``/status@Bot``, ``/status extra args`` and code
    fenced variants (``` /status ```) - everything a real operator taps in a
    chat. Unknown text is returned as ("", text) so the caller can fall back
    to the engine router, which is exactly what the CLI accepts.
    """
    s = _strip_code(text)
    if not s.startswith("/"):
        return "", text
    first, _, rest = s.partition(" ")
    cmd = first.lstrip("/").lower().split("@")[0]
    return cmd, rest.strip()


def render_help() -> str:
    """The help text, generated from ``COMMANDS`` and ``HELP_TOPICS``.

    Built from the same lists that are published to Telegram, so the Menu sheet
    and this message can never disagree about what the bot accepts. ``/help
    <topic>`` opens one of the topic pages below; ``/help`` alone lists both
    the commands and the topics.
    """
    width = max(len(c) for c, _ in COMMANDS) + 2
    lines = [f"/{c.ljust(width)}{d}" for c, d in COMMANDS]
    topics = "  ".join(f"/{t}" for t in HELP_TOPICS)
    return (
        "<b>J+Rock bot</b>\n"
        + "\n".join(lines)
        + f"\n\ntopics: {topics}\n"
          "(e.g. <code>/help engine</code> for one page)\n\n"
          "Any other text is passed to the engine command router, so the same "
          "phrases the CLI accepts also work here."
    )


#: Kept as a module constant for callers that imported it before ``render_help``
#: existed; it is now derived rather than hand-maintained.
HELP = render_help()


# ------------------------------------------------------------------ the bot app
class JRockTelegramApp:
    """Routes Telegram messages to the dashboard state or the agent router."""

    def __init__(self, agent, bot: TelegramBot):
        self.agent = agent
        self.bot = bot

    async def state(self) -> dict:
        # Imported lazily so this module can be imported without aiohttp.web
        # side effects and so a future dashboard removal is a one-line change.
        from .web import build_state
        return await build_state(self.agent)

    async def handle(self, chat_id: int | str, text: str) -> None:
        cmd, rest = _split_command(text)

        # Every reply carries the persistent keyboard, so the commands stay in
        # the bottom-left corner of the chat without the operator asking for
        # them. `/start` and `/help` are the entry points that matter most.
        try:
            if cmd in ("start", "help"):
                # `/help engine` is a real page, not a fall-through to the
                # engine router: the operator tapped a topic and expects help
                # about it, not a status dump.
                topic = rest.lower().split()[0] if rest else ""
                if topic in HELP_TOPICS:
                    await self.bot.send(
                        chat_id,
                        f"<b>/help {topic}</b>\n{_e(HELP_TOPICS[topic])}\n\n"
                        f"run <code>/{topic}</code> to see it now",
                        markdown=True)
                elif topic:
                    await self.bot.send(
                        chat_id,
                        f"<b>no help page for {_e(topic)}</b>\n"
                        f"topics: {_e('  '.join('/' + t for t in HELP_TOPICS))}",
                        markdown=True)
                else:
                    await self.bot.send(chat_id, render_help(), markdown=True)
            elif cmd in ("status", "s"):
                await self.bot.send(chat_id, render_summary(await self.state()),
                                    markdown=True)
            elif cmd in ("positions", "pos"):
                await self.bot.send(chat_id, render_positions(await self.state()),
                                    markdown=True)
            elif cmd == "orders":
                # One source: the dashboard readout, which is already normalised
                # through order_rows(). The previous if/else had identical
                # branches, so it only looked like it chose between two.
                await self.bot.send(chat_id, render_orders(await self.state()),
                                    markdown=True)
            elif cmd == "pnl":
                await self.bot.send(chat_id, render_pnl(await self.state()),
                                    markdown=True)
            elif cmd == "engine":
                await self.bot.send(chat_id, render_engine(await self.state()),
                                    markdown=True)
            elif cmd in ("ladder", "tpsl"):
                out = await self.agent.handle(cmd)
                await self.bot.send(chat_id, _as_html(out), markdown=True)
            else:
                # Bare text, or a command this front-end does not own: hand the
                # ORIGINAL text to the engine router, which is the same surface
                # the CLI exposes. `rest` is deliberately not used here - the
                # router needs the whole line ("turn the engine on").
                out = await self.agent.handle(text)
                await self.bot.send(chat_id, _as_html(out), markdown=True)
        except TelegramError as e:
            log.error("telegram send failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("handling %r failed", text[:80])
            try:
                await self.bot.send(
                    chat_id, f"<b>error</b>\n<code>{_e(type(e).__name__)}: "
                             f"{_e(e)}</code>", markdown=True)
            except TelegramError:
                pass

    async def run(self, *, once: bool = False) -> None:
        me = await self.bot.get_me()
        log.info("connected as @%s", me.get("username"))
        # Publish the command list once at startup. Telegram caches it per bot,
        # so this is what makes the Menu button in the bottom-left corner show
        # the commands on the operator's next visit - no /help needed first.
        try:
            await self.bot.set_my_commands(COMMANDS)
            log.info("published %d commands to the Menu", len(COMMANDS))
        except TelegramError as e:
            # A failed menu publish must not stop the bot: the commands still
            # work when typed, and the reply keyboard still appears.
            log.warning("setMyCommands failed (menu will be empty): %s", e)
        print(f"Telegram bot @{me.get('username')} is listening "
              f"(chat id is shown per message; Ctrl+C to stop)", flush=True)
        offset: int | None = None
        while True:
            try:
                updates = await self.bot.poll(offset)
            except TelegramError as e:
                log.warning("poll failed: %s", e)
                await asyncio.sleep(3)
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("network: %s", e)
                await asyncio.sleep(3)
                continue
            for u in updates:
                offset = u.get("update_id", 0) + 1
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                text = msg.get("text")
                if not text or "id" not in chat:
                    continue
                log.info("chat=%s from=%s: %s", chat["id"],
                         (msg.get("from") or {}).get("username"), text[:80])
                await self.handle(chat["id"], text)
            if once:
                return


def _as_html(text: str) -> str:
    """Engine output is plain text - escape it, keep it monospaced."""
    return f"<pre>{_e(text)}</pre>"


# ------------------------------------------------------------------------ entry
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m jrock.telegram_bot",
        description="Telegram front-end for the J+Rock trading engine.")
    p.add_argument("--once", action="store_true",
                   help="poll once and exit (handy for a smoke test)")
    p.add_argument("--chat-id", default=None,
                   help="send one status message to this chat id and exit")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    return p


async def _amain(args) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set.\n"
              "Create a bot with @BotFather, then:\n"
              "  export TELEGRAM_BOT_TOKEN=123456:ABC...\n"
              "(see .env.example)", flush=True)
        return 2

    from .agent import JRockAgent

    async with aiohttp.ClientSession() as session:
        bot = TelegramBot(token, session)
        app = JRockTelegramApp(JRockAgent(), bot)
        if args.chat_id:
            await bot.send(args.chat_id, render_summary(await app.state()),
                           markdown=True)
            return 0
        await app.run(once=args.once)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
