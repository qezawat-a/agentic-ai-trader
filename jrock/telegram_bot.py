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

    async def send(self, chat_id: int | str, text: str,
                   *, markdown: bool = False) -> Any:
        for chunk in _split(text, MAX_MESSAGE):
            await self.call("sendMessage", chat_id=chat_id, text=chunk,
                            parse_mode="HTML" if markdown else None,
                            disable_web_page_preview=True)
        return True

    async def poll(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message"])


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


HELP = (
    "<b>J+Rock bot</b>\n"
    "/status   engine + account + positions\n"
    "/positions  open positions\n"
    "/orders   pending orders\n"
    "/pnl      account and PnL\n"
    "/engine   engine flags\n"
    "/ladder   TP ladder state\n"
    "/help     this message\n\n"
    "Any other text is passed to the engine command router, so the same "
    "phrases the CLI accepts also work here."
)


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
        cmd = text.strip().split()[0].lower().lstrip("/") if text.strip() else ""
        cmd = cmd.split("@")[0]          # "/status@MyBot" in groups

        try:
            if cmd in ("start", "help"):
                await self.bot.send(chat_id, HELP, markdown=True)
            elif cmd in ("status", "s"):
                await self.bot.send(chat_id, render_summary(await self.state()),
                                    markdown=True)
            elif cmd in ("positions", "pos"):
                await self.bot.send(chat_id, render_positions(await self.state()),
                                    markdown=True)
            elif cmd == "orders":
                # Two sources on purpose: the dashboard readout gives the
                # structured rows, and the agent command is what the operator
                # would type in the CLI. Prefer the structured one.
                st = await self.state()
                if st.get("orders"):
                    await self.bot.send(chat_id, render_orders(st), markdown=True)
                else:
                    await self.bot.send(chat_id, render_orders(st), markdown=True)
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
