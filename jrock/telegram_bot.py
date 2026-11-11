"""Telegram front-end for the J+Rock trading engine.

A chat command surface for the engine: a read-only view of positions, open
orders, PnL and engine state, delivered in a Telegram chat (the web dashboard
was removed; this bot and the CLI are the two front-ends).

Design notes
------------
* **No token in code.** The bot token is read from ``TELEGRAM_BOT_TOKEN``. A
  missing token is a clear startup error, never a silent no-op.
* **No second implementation of the data.** Everything here calls the same
  ``build_state`` snapshot in ``jrock.state`` (and the same ``agent.handle``
  command router), so the numbers cannot drift between the two front-ends.
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
import contextlib
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
#:
#: The list carries EVERY command the bot routes - the trading engine first
#: (that is what the operator uses every minute), then the agent itself. A
#: command that is not in the Menu sheet is a command the operator has to
#: remember, which is the thing a Menu is for.
COMMANDS: list[tuple[str, str]] = [
    # --- the trading engine
    ("status", "engine + account + positions"),
    ("positions", "open positions"),
    ("history", "closed positions + TP/SL + trades"),
    ("orders", "pending orders"),
    ("pnl", "account and PnL"),
    ("report", "generate and send one engine report now"),
    ("tpsl", "TP/SL method + rung state"),
    ("exits", "breakeven + trailing, per position, right now"),
    ("balance", "balance and margin use"),
    ("price", "last / mark price for the symbols"),
    ("engine", "engine flags"),
    ("leverage", "leverage and margin mode per symbol"),
    ("margin", "margin mode (ISOLATION|CROSS) per symbol"),
    ("settings", "account trading settings"),
    ("symbols", "tradable symbols"),
    ("funding", "current funding rate"),
    ("ws", "WebSocket state, market data source, live test"),
    ("help", "this message"),
    # --- the agent itself
    ("models", "list the models on BASE_URL"),
    ("set_model", "pin the active model (/set_model <id>)"),
    ("connect_provider", "point J-Rock at a provider"),
    ("think", "one-shot reasoning (no tools)"),
    ("memory", "long-term memory (/memory add|list|search|clear)"),
    ("add_soul", "append to the soul"),
    ("soul", "show the soul"),
    ("add_skill", "teach a skill"),
    ("skill", "load / list skills"),
    ("add_mcp", "register an MCP server"),
    ("mcp", "use MCP tools"),
    ("team_spawn", "Architect+Engineer+Reviewer mini-team"),
    ("check_ai", "LLM health probe"),
    ("check_system", "deep system + engine diagnostics"),
    ("code_review", "review its own code"),
    ("bug_fix", "propose and apply a fix"),
    ("verify", "run the tests and the import check"),
    ("simplify", "simplify a source file"),
    ("selfheal", "the self-repair loop (pytest -> review -> fix)"),
    ("diagnose", "full diagnostic bundle"),
    ("reset", "clear the conversation memory"),
]

#: The reply keyboard is GONE. The operator asked for the six buttons under
#: the input field to be removed: the Menu sheet (every command, published
#: above) is the one surface, and a second set of buttons under the typing
#: field only crowded the chat.
#:
#: Telegram keeps a keyboard until it is replaced, so the bot removes it once
#: at startup with `remove_keyboard: True`.
REMOVE_KEYBOARD: dict[str, Any] = {"remove_keyboard": True}


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
        """Send a message.

        The reply keyboard is GONE. Every message carries
        `remove_keyboard: true`, which is what actually takes the old six
        buttons off the input field - Telegram keeps a keyboard until it is
        replaced, and this replaces it with nothing. The Menu sheet
        (every command, see COMMANDS) is the one command surface.
        """
        for chunk in _split(text, MAX_MESSAGE):
            params: dict[str, Any] = {
                "chat_id": chat_id, "text": chunk,
                "parse_mode": "HTML" if markdown else None,
                "disable_web_page_preview": True,
            }
            if keyboard:
                params["reply_markup"] = REMOVE_KEYBOARD
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
        f"partial ladder {_e(e.get('partial_tp_ladder') or '-')} "
        f"(exchange-enforced)\n"
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
    "tpsl": "TP/SL method + PARTIAL ladder state",
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
def _allowed_chats_from_env() -> set[int]:
    """TELEGRAM_ALLOWED_CHATS as chat ids. Empty = answer any chat."""
    raw = os.getenv("TELEGRAM_ALLOWED_CHATS", "").strip()
    return {int(t) for t in raw.replace(",", " ").split() if t.isdigit()}


#: Floor for the report cadence, so a mistyped interval cannot turn the bot
#: into a Telegram flood.
REPORT_MIN_INTERVAL = 5


class JRockTelegramApp:
    """Routes Telegram messages to the dashboard state or the agent router.

    Also the REPORT PUSH channel: when the engine has ``report on``, the loop
    in ``report_push_loop`` generates the same report the engine prints to
    stdout and sends it to every chat that has messaged the bot, each
    ``report_interval`` seconds. Reports only ever go to known chats -
    a chat that never wrote to the bot never gets one.
    """

    def __init__(self, agent, bot: TelegramBot):
        self.agent = agent
        self.bot = bot
        #: Chats that have talked to the bot this run - the report fan-out
        #: list. TELEGRAM_ALLOWED_CHATS (empty = any) gates ANSWERS; the push
        #: list is every allowed chat that has actually drawn a reply.
        self.chats: set[int | str] = set()
        self.allowed_chats = _allowed_chats_from_env()

    def _chat_allowed(self, chat_id: int | str) -> bool:
        return not self.allowed_chats or chat_id in self.allowed_chats

    async def state(self) -> dict:
        # Imported lazily so the snapshot machinery (network calls + readouts)
        # only loads with the rest of the agent.
        from .state import build_state
        return await build_state(self.agent)

    async def handle(self, chat_id: int | str, text: str) -> None:
        # Remember the chat so the report push knows WHO to talk to. The
        # allowlist gate already ran in run(); handle() itself is the fan-out
        # source of truth for "chats the operator actually talked to".
        first_contact = chat_id not in self.chats
        self.chats.add(chat_id)
        cmd, rest = _split_command(text)

        # The first message after a restart gets the state banner: what
        # survived, and - crucially - that engine_on/autotrade are NOT
        # auto-resumed. Without this the operator sees a "reset" agent and
        # has no way to tell a deliberate safety choice from a lost state.
        if first_contact:
            try:
                banner = self.agent.state_banner()
            except Exception as e:  # noqa: BLE001 - the banner must never
                log.debug("state banner unavailable: %s", e)   # break the chat
                banner = ""
            if banner:
                await self.bot.send(chat_id, f"<b>J+Rock restarted</b>\n{_e(banner)}",
                                    markdown=True)

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
            elif cmd == "exits":
                out = await self.agent.handle("/exits")
                await self.bot.send(chat_id, _e(out))
            elif cmd in ("tpsl", "balance", "price", "history"):
                # Read-only agent commands: reuse the CLI routing verbatim
                # (arguments included, e.g. "/tpsl pending BTCUSDT").
                out = await self.agent.handle(f"{cmd}{(' ' + rest) if rest else ''}")
                await self.bot.send(chat_id, _as_html(out), markdown=True)
            elif cmd == "report":
                # A synchronous one-shot of the same report the engine's
                # report loop generates - straight from the engine so it is
                # the real thing, not a cached summary.
                rep = await self.agent.engine.report()
                await self.bot.send(chat_id, _as_html(rep), markdown=True)
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

    # ------------------------------------------------------- report -> telegram
    def _report_interval(self) -> float:
        """Engine's report_interval with a flood-proof floor."""
        eng = getattr(self.agent, "engine", None)
        try:
            return max(REPORT_MIN_INTERVAL, float(eng.state.report_interval))
        except Exception:  # noqa: BLE001 - missing engine/attr = safe default
            return 30.0

    async def _push_report_once(self) -> bool:
        """Generate one report and send it to every known chat.

        Returns whether anything was pushed. The three OFF conditions are
        deliberate: report off (the operator never opted into pushes), no
        chats known yet (nobody has talked to the bot this run), and no
        engine (degenerate app used in tests). Unconditional sends would be
        the spam channel nobody asked for.
        """
        eng = getattr(self.agent, "engine", None)
        if eng is None or not getattr(eng.state, "report_on", False):
            return False
        if not self.chats:
            return False
        rep = await eng.report()
        body = _as_html(rep)
        for chat_id in sorted(self.chats, key=str):
            await self.bot.send(chat_id, body, markdown=True)
        eng.state.last_report_ts = time.time()
        return True

    async def report_push_loop(self) -> None:
        """The scheduled twin of the chat's /report button.

        One report pass every report_interval, forever. All failures are
        logged and absorbed: a flaky Bitunix read or a blocked send must
        never kill the pusher - the next pass just tries again.
        """
        while True:
            try:
                await self._push_report_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("telegram report push failed")
            await asyncio.sleep(self._report_interval())

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
            # work when typed.
            log.warning("setMyCommands failed (menu will be empty): %s", e)
        print(f"Telegram bot @{me.get('username')} is listening "
              f"(chat id is shown per message; Ctrl+C to stop)", flush=True)
        if self.allowed_chats:
            print(f"answering only chats in TELEGRAM_ALLOWED_CHATS: "
                  f"{sorted(self.allowed_chats)}", flush=True)
        else:
            print("TELEGRAM_ALLOWED_CHATS is empty: answering ANY chat "
                  "that writes to the bot", flush=True)
        pusher = asyncio.create_task(self.report_push_loop(),
                                     name="telegram-report-push")
        offset: int | None = None
        try:
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
                    if not self._chat_allowed(chat["id"]):
                        log.info("chat=%s not in TELEGRAM_ALLOWED_CHATS - "
                                 "ignored", chat["id"])
                        continue
                    log.info("chat=%s from=%s: %s", chat["id"],
                             (msg.get("from") or {}).get("username"), text[:80])
                    await self.handle(chat["id"], text)
                if once:
                    return
        finally:
            pusher.cancel()
            # Awaiting a cancelled task raises CancelledError (a BaseException,
            # deliberately not an Exception) - suppress exactly that.
            with contextlib.suppress(asyncio.CancelledError):
                await pusher


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
