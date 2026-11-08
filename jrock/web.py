"""J+Rock web dashboard: a browser controller in place of the CLI.

    python -m jrock.web                      # http://127.0.0.1:8080/?token=...
    WEB_PORT=8090 WEB_TOKEN=secret python -m jrock.web

Every line the dashboard sends goes through the same JRockAgent.handle() the
CLI uses, so routing, yes/no confirmations and permissions are unchanged.

Security: binds to 127.0.0.1 by default and requires a token on every API
call (header X-Auth-Token). Without WEB_TOKEN a random token is generated and
printed at startup. Do not bind to a public interface without a reverse proxy
and TLS.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import socket
import sys

from aiohttp import web

from .agent import JRockAgent
from .config import load_settings

log = logging.getLogger("jrock.web")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_BODY_BYTES = 64 * 1024

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>J+Rock dashboard</title>
<style>
:root{--bg:#04070c;--panel:#070b12;--line:#12202e;--ink:#c9e7f2;--dim:#5b7484;
      --cyan:#29e6ff;--green:#3dffa8;--amber:#ffc24b;--red:#ff5470}
html,body{background:var(--bg);color:var(--ink);margin:0;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
::selection{background:rgba(41,230,255,.25)}
*::-webkit-scrollbar{width:8px;height:8px}
*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{background:#15293a;border-radius:8px}
*::-webkit-scrollbar-thumb:hover{background:#1e3a52}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:50;opacity:.5;
  background:repeating-linear-gradient(to bottom,transparent 0,transparent 2px,
  rgba(0,0,0,.14) 3px,rgba(0,0,0,.14) 4px)}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(1200px 500px at 15% -10%,rgba(41,230,255,.07),transparent 60%),
  radial-gradient(900px 420px at 90% 110%,rgba(61,255,168,.05),transparent 60%)}
header,.app{position:relative;z-index:1}
header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;
  padding:10px 14px;border-bottom:1px solid var(--line);background:rgba(7,11,18,.9)}
.app{display:grid;grid-template-columns:320px 1fr;gap:10px;padding:10px;align-items:start}
@media (max-width:820px){.app{grid-template-columns:1fr}}
.col{display:flex;flex-direction:column;gap:10px;min-width:0}
.panel{background:linear-gradient(180deg,rgba(13,20,30,.9),rgba(7,11,18,.95));
  border:1px solid var(--line);border-radius:10px;padding:10px}
.h{font-size:10px;text-transform:uppercase;letter-spacing:.18em;color:#4a6376;margin-bottom:8px;
  display:flex;justify-content:space-between;align-items:center;gap:6px}
.glow-cyan{color:var(--cyan);text-shadow:0 0 12px rgba(41,230,255,.45);font-weight:600;letter-spacing:.06em}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:6px;
  padding:3px 8px;font-size:11px;letter-spacing:.04em;background:rgba(10,16,25,.8);white-space:nowrap}
.chip.live{border-color:rgba(255,84,112,.5);color:var(--red)}
.chip.sym{color:#bfeaff}
.dot{width:7px;height:7px;border-radius:99px;display:inline-block;background:#33455a}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.btn{border:1px solid var(--line);background:rgba(13,21,32,.9);padding:6px 10px;border-radius:6px;
  font:inherit;font-size:11px;letter-spacing:.05em;transition:all .15s ease;cursor:pointer;
  color:var(--ink);white-space:nowrap}
.btn:hover:not(:disabled){border-color:rgba(41,230,255,.5);color:var(--cyan)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-primary{border-color:rgba(41,230,255,.55);color:var(--cyan);background:rgba(41,230,255,.08)}
.btn-danger{border-color:rgba(255,84,112,.5);color:var(--red);background:rgba(255,84,112,.07)}
.btn-green{border-color:rgba(61,255,168,.45);color:var(--green);background:rgba(61,255,168,.07)}
.btn-small{padding:4px 8px;font-size:10px}
.btn-wide{width:100%}
.tog{display:flex;align-items:center;justify-content:space-between;gap:6px;text-align:left}
.st-on{color:var(--green)} .st-off{color:var(--dim)}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.input{background:rgba(6,10,16,.9);border:1px solid var(--line);border-radius:6px;padding:6px 9px;
  font:inherit;font-size:12px;color:var(--ink);outline:none;min-width:0}
.input:focus{border-color:rgba(41,230,255,.5)}
.grow{flex:1}
.qrow{display:flex;align-items:center;gap:6px;font-size:10px;color:var(--dim)}
.qrow span{width:110px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis}
.qin{width:70px;text-align:center;padding:4px 6px}
.code{background:#050a10;border:1px solid var(--line);border-radius:8px;padding:10px 12px;
  font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word;margin:0;
  max-height:40vh;overflow:auto}
.confirm{display:none;margin-top:8px;border:1px solid rgba(255,84,112,.35);border-radius:8px;padding:8px;
  background:rgba(255,84,112,.05)}
.confirm.show{display:block}
.note{color:var(--dim);font-size:11px}
</style>
</head>
<body>
<header>
  <div class="glow-cyan">J+ROCK · trader engine</div>
  <div class="row">
    <span class="chip"><span id="dotEngine" class="dot"></span><span id="engineTxt">engine off</span></span>
    <span class="chip" id="autoChip">autotrade off</span>
  </div>
</header>

<div class="app">
  <div class="col">
    <div class="panel">
      <div class="h"><span>controls</span><span id="scanChip" class="note"></span></div>
      <div class="grid2">
        <button class="btn act tog" id="tgEngine">ENGINE</button>
        <button class="btn act tog" id="tgScan">SCAN</button>
        <button class="btn act tog" id="tgAuto">AUTOTRADE</button>
        <button class="btn act tog" id="tgReport">REPORT</button>
        <button class="btn act tog" id="tgMid">MID-MGMT</button>
        <button class="btn act tog" id="tgRev">REVERSAL</button>
      </div>
      <div class="row" style="margin-top:6px">
        <button class="btn act btn-small" data-line="mid management trigger">run mid mgmt now</button>
      </div>
      <div class="note" style="margin-top:6px">AUTOTRADE ON asks for a yes/no before it runs. MID-MGMT toggles the background loop; "run mid mgmt now" runs one pass (breakeven, trailing, partial, reversal) on open positions.</div>
    </div>

    <div class="panel">
      <div class="h"><span>universe</span><span id="uniChip" class="note"></span></div>
      <div class="row">
        <button class="btn act btn-small" data-line="universe">show</button>
        <button class="btn act btn-small btn-green" data-confirm="universe auto">auto on</button>
        <button class="btn act btn-small" data-confirm="universe off">auto off</button>
      </div>
      <div class="row" style="margin-top:6px">
        <button class="btn act btn-small" data-confirm="universe rank VOLUME">VOLUME</button>
        <button class="btn act btn-small" data-confirm="universe rank GAINERS">GAINERS</button>
        <button class="btn act btn-small" data-confirm="universe rank LOSERS">LOSERS</button>
        <button class="btn act btn-small" data-confirm="universe rank MOVERS">MOVERS</button>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
        <div class="qrow">
          <span>size (pairs)</span>
          <input class="input qin" id="uniSize" inputmode="numeric" autocomplete="off">
          <button class="btn act btn-small" id="uniSizeBtn">set</button>
        </div>
        <div class="qrow">
          <span>min 24h vol $</span>
          <input class="input qin" id="uniVol" inputmode="numeric" autocomplete="off" style="width:110px">
          <button class="btn act btn-small" id="uniVolBtn">set</button>
        </div>
      </div>
      <div class="note" style="margin-top:6px">Universe changes which symbols the engine trades, so each change asks you to confirm first. "show" only reads.</div>
    </div>

    <div class="panel">
      <div class="h"><span>watchlist</span></div>
      <div class="row" id="symbols"></div>
      <div class="row" style="margin-top:8px"><button class="btn act btn-small" data-line="price">prices</button></div>
    </div>

    <div class="panel">
      <div class="h"><span>account</span></div>
      <div class="grid2">
        <button class="btn act" data-line="balance">balance</button>
        <button class="btn act" data-line="pnl">pnl</button>
        <button class="btn act" data-line="positions">positions</button>
        <button class="btn act" data-line="orders">orders</button>
      </div>
    </div>

    <div class="panel">
      <div class="h"><span>close position</span></div>
      <div class="row">
        <input class="input grow" id="closeSym" placeholder="BTCUSDT" autocomplete="off">
        <button class="btn act btn-danger btn-small" id="closeBtn">close</button>
      </div>
      <div class="note" style="margin-top:6px">Closing asks for a yes/no before it runs.</div>
    </div>
  </div>

  <div class="col">
    <div class="panel">
      <div class="h"><span>quick settings</span><span id="levChip" class="note"></span></div>
      <div id="qsets" style="display:flex;flex-direction:column;gap:6px"></div>
      <div class="row" style="margin-top:8px">
        <span class="chip" id="mChip">margin –</span>
        <span class="chip" id="pChip">position –</span>
        <span class="chip" id="maxChip">max –</span>
      </div>
    </div>

    <div class="panel">
      <div class="h"><span>command or question</span></div>
      <form id="form" class="row">
        <input class="input grow" id="line" placeholder="e.g. balance, close my AKEUSDT position, why did the last order fail" autocomplete="off">
        <button class="btn act btn-primary" type="submit">run</button>
      </form>
      <div id="confirm" class="confirm">
        <div class="note">Waiting for your answer:</div>
        <div class="row" style="margin-top:6px">
          <button class="btn act btn-danger" id="yes">Yes, confirm</button>
          <button class="btn act" id="no">No, cancel</button>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="h"><span>read-only</span></div>
      <div class="row">
        <button class="btn act btn-small" data-line="status">status</button>
        <button class="btn act btn-small" data-line="settings">settings</button>
        <button class="btn act btn-small" data-line="symbols">symbols</button>
        <button class="btn act btn-small" data-line="funding">funding</button>
        <button class="btn act btn-small" data-line="leverage">leverage</button>
        <button class="btn act btn-small" data-line="margin">margin</button>
        <button class="btn act btn-small" data-line="tpsl">tpsl</button>
        <button class="btn act btn-small" data-line="ladder">ladder</button>
        <button class="btn act btn-small" data-line="help">help</button>
      </div>
    </div>

    <div class="panel">
      <div class="h"><span>engine report</span><span class="note">refreshes every 15s</span></div>
      <pre class="code" id="report">No report yet. Turn it on with REPORT.</pre>
    </div>

    <div class="panel">
      <div class="h"><span>engine log</span></div>
      <pre class="code" id="out">Ready.</pre>
    </div>
  </div>
</div>

<script>
const token = new URLSearchParams(location.search).get("token") || "";
const AUTH = {"X-Auth-Token": token};
const $ = (id) => document.getElementById(id);
const out = $("out");
let busy = false;
let S = null;

const QS = [
  ["risk %/trade", "risk_pct", (v) => "risk " + v],
  ["scan interval s", "scan_interval", (v) => "scans interval " + v],
  ["report interval s", "report_interval", (v) => "report interval sec " + v],
  ["breakeven %", "breakeven_pct", (v) => "breakeven threshold " + v],
  ["reversal conf", "reversal_confidence", (v) => "reversal_confidence=" + v],
  ["leverage", "leverage", (v) => "set leverage to " + v + "x"],
];

async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: AUTH}, opts || {}));
  let body = {};
  try { body = await r.json(); } catch (e) { /* non-JSON error page */ }
  return {ok: r.ok, status: r.status, body: body};
}

function setBusy(on) {
  busy = on;
  document.querySelectorAll("button.act").forEach((b) => { b.disabled = on; });
}

// The server answers a destructive request with a yes/no prompt. Show the
// Yes/No buttons only while that prompt is pending.
function setPending(text) {
  $("confirm").classList.toggle("show", /'yes'.*'no'|yes\\/no|reply 'yes'/i.test(text || ""));
}

async function run(line) {
  if (busy) return;
  setBusy(true);
  out.textContent = "> " + line + "\\n...";
  try {
    const r = await api("/api/command", {
      method: "POST",
      headers: Object.assign({"Content-Type": "application/json"}, AUTH),
      body: JSON.stringify({line: line})
    });
    if (r.ok) {
      out.textContent = "> " + line + "\\n" + r.body.output;
      setPending(r.body.output);
    } else {
      out.textContent = "HTTP " + r.status + ": " + (r.body.error || "request failed");
    }
  } catch (e) {
    out.textContent = "Request failed: " + e;
  } finally {
    setBusy(false);
  }
  refreshStatus();
}

function paintToggle(btn, label, on) {
  btn.classList.toggle("btn-green", on);
  btn.textContent = "";
  const name = document.createElement("span");
  name.textContent = label;
  const state = document.createElement("span");
  state.className = on ? "st-on" : "st-off";
  state.textContent = on ? "ON" : "off";
  btn.append(name, state);
}

function paintSymbols(list) {
  const box = $("symbols");
  box.textContent = "";
  for (const sym of list) {
    const c = document.createElement("span");
    c.className = "chip sym";
    c.textContent = sym.replace("USDT", "");
    box.appendChild(c);
  }
}

function buildQuickSettings() {
  const box = $("qsets");
  for (const [label, key, make] of QS) {
    const row = document.createElement("div");
    row.className = "qrow";
    const name = document.createElement("span");
    name.textContent = label;
    const inp = document.createElement("input");
    inp.className = "input qin";
    inp.dataset.key = key;
    const apply = () => { const v = inp.value.trim(); if (v) run(make(v)); };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
    const set = document.createElement("button");
    set.className = "btn act btn-small";
    set.textContent = "set";
    set.addEventListener("click", apply);
    row.append(name, inp, set);
    box.appendChild(row);
  }
}

function fillQuickSettings() {
  document.querySelectorAll("#qsets input").forEach((inp) => {
    if (document.activeElement !== inp && S && S[inp.dataset.key] !== undefined) {
      inp.value = S[inp.dataset.key];
    }
  });
}

// Universe changes alter the symbol set, so they need an explicit OK in the
// browser. Bare commands do not pass through the server's yes/no prompt.
function runConfirmed(line) {
  if (busy) return;
  if (!window.confirm("Change the trading universe?\\n\\n" + line +
      "\\n\\nThis changes which symbols the engine scans and trades.")) return;
  run(line);
}

function runUniverseNumber(input, prefix) {
  const v = input.value.trim();
  if (!/^\\d+$/.test(v) || Number(v) <= 0) {
    out.textContent = "Enter a positive whole number first.";
    return;
  }
  runConfirmed(prefix + v);
}

function paintUniverse(s) {
  const parts = [
    s.universe_auto ? "auto" : "fixed list",
    s.universe_rank,
    s.universe_size != null ? "top " + s.universe_size : null,
    s.min_24h_volume_usd != null ? "min $" + s.min_24h_volume_usd : null,
  ].filter((p) => p !== null && p !== undefined && p !== "");
  $("uniChip").textContent = parts.length ? parts.join(" · ") : "universe –";
  for (const [id, key] of [["uniSize", "universe_size"], ["uniVol", "min_24h_volume_usd"]]) {
    const inp = $(id);
    if (document.activeElement !== inp && s[key] != null) inp.value = s[key];
  }
}

async function refreshStatus() {
  try {
    const r = await api("/api/status");
    if (!r.ok) return;
    S = r.body;
    paintToggle($("tgEngine"), "ENGINE", S.engine_on);
    paintToggle($("tgScan"), "SCAN", S.scan_on);
    paintToggle($("tgAuto"), "AUTOTRADE", S.autotrade);
    paintToggle($("tgReport"), "REPORT", S.report_on);
    paintToggle($("tgMid"), "MID-MGMT", S.mid_management);
    paintToggle($("tgRev"), "REVERSAL", S.reversal);
    $("dotEngine").className = "dot" + (S.engine_on ? " on" : "");
    $("engineTxt").textContent = S.engine_on ? "engine on" : "engine off";
    $("autoChip").textContent = S.autotrade ? "autotrade ON" : "autotrade off";
    $("autoChip").classList.toggle("live", !!S.autotrade);
    $("scanChip").textContent = "scan every " + S.scan_interval + "s";
    $("levChip").textContent = S.leverage + "x";
    $("mChip").textContent = "margin " + S.margin_mode;
    $("pChip").textContent = "position " + S.position_mode;
    $("maxChip").textContent = "max " + S.max_open_positions + " pos";
    paintSymbols(S.symbols || []);
    paintUniverse(S);
    fillQuickSettings();
  } catch (e) { /* keep the last status on screen */ }
}

async function refreshReport() {
  try {
    const r = await api("/api/report");
    if (!r.ok) return;
    const j = r.body;
    if (j.report) {
      $("report").textContent = j.report;
    } else if (!j.report_on) {
      $("report").textContent = "Report is off. Turn it on with REPORT.";
    } else {
      $("report").textContent = "Report is on; the first report has not been produced yet.";
    }
  } catch (e) { /* keep the last report on screen */ }
}

$("tgEngine").addEventListener("click", () => S && run(S.engine_on ? "engine off" : "engine on"));
$("tgScan").addEventListener("click", () => S && run(S.scan_on ? "scan off" : "scan on"));
$("tgAuto").addEventListener("click", () => S && run(S.autotrade ? "turn off autotrade" : "turn on autotrade"));
$("tgReport").addEventListener("click", () => S && run(S.report_on ? "report off" : "report on"));
$("tgMid").addEventListener("click", () => run("mid management"));
$("tgRev").addEventListener("click", () => S && run("reversal=" + (S.reversal ? "off" : "on")));
$("closeBtn").addEventListener("click", () => {
  const sym = $("closeSym").value.trim().toUpperCase();
  if (sym) run("close my " + sym + " position");
});
$("yes").addEventListener("click", () => run("yes"));
$("no").addEventListener("click", () => run("no"));
document.querySelectorAll("[data-line]").forEach((b) =>
  b.addEventListener("click", () => run(b.dataset.line)));
document.querySelectorAll("[data-confirm]").forEach((b) =>
  b.addEventListener("click", () => runConfirmed(b.dataset.confirm)));
$("uniSizeBtn").addEventListener("click", () => runUniverseNumber($("uniSize"), "universe size "));
$("uniVolBtn").addEventListener("click", () => runUniverseNumber($("uniVol"), "universe min volume "));
$("uniSize").addEventListener("keydown", (e) => { if (e.key === "Enter") runUniverseNumber($("uniSize"), "universe size "); });
$("uniVol").addEventListener("keydown", (e) => { if (e.key === "Enter") runUniverseNumber($("uniVol"), "universe min volume "); });
$("form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("line");
  const line = input.value.trim();
  if (line) run(line);
});

buildQuickSettings();
refreshStatus();
refreshReport();
setInterval(refreshStatus, 5000);
setInterval(refreshReport, 15000);
</script>
</body>
</html>
"""


def _authorized(request: web.Request) -> bool:
    supplied = request.headers.get("X-Auth-Token") or request.query.get("token", "")
    expected = request.app["token"]
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


async def index(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=401, content_type="text/plain",
                            text="Unauthorized. Open the URL printed at startup (it contains ?token=...).")
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def command(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": 'expected JSON {"line": "..."}'}, status=400)
    line = body.get("line") if isinstance(body, dict) else None
    if not isinstance(line, str) or not line.strip():
        return web.json_response({"error": 'expected a non-empty "line"'}, status=400)

    agent: JRockAgent = request.app["agent"]
    # The agent is stateful and the CLI runs one command at a time; keep that.
    async with request.app["lock"]:
        try:
            output = await agent.handle(line)
        except Exception as e:  # noqa: BLE001
            output = f"ERROR: {type(e).__name__}: {e}"
    return web.json_response({"output": output})


async def status(request: web.Request) -> web.Response:
    """Engine switches and settings for the toggles. Read-only."""
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    st = request.app["agent"].engine.state
    # Universe fields come from the universe layer. Read them defensively so
    # this dashboard still starts on a checkout that does not have it yet.
    s = request.app["agent"].s
    return web.json_response({
        "universe_auto": bool(getattr(s, "universe_auto", False)),
        "universe_rank": getattr(s, "universe_rank", None),
        "universe_size": getattr(s, "universe_size", None),
        "min_24h_volume_usd": getattr(s, "min_24h_volume_usd", None),
        "engine_on": st.engine_on,
        "scan_on": st.scan_on,
        "autotrade": st.autotrade,
        "report_on": st.report_on,
        "mid_management": st.mid_management,
        "reversal": st.reversal,
        "report_interval": st.report_interval,
        "scan_interval": st.scan_interval,
        "symbols": list(st.symbols),
        "leverage": st.leverage,
        "margin_mode": st.margin_mode,
        "position_mode": st.position_mode,
        "max_open_positions": st.max_open_positions,
        "risk_pct": st.risk_pct,
        "breakeven_pct": st.breakeven_pct,
        "reversal_confidence": st.reversal_confidence,
        "scans": st.scans,
        "errors": len(st.errors),
    })


async def report(request: web.Request) -> web.Response:
    """Latest periodic report text. Read-only: it does not run a new report."""
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    engine = request.app["agent"].engine
    return web.json_response({
        "report_on": engine.state.report_on,
        "report": engine.last_report,
        "last_report_ts": engine.state.last_report_ts,
    })


def make_app(agent: JRockAgent, token: str) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app["agent"] = agent
    app["token"] = token
    app["lock"] = asyncio.Lock()

    async def on_startup(app: web.Application) -> None:
        print(await agent.startup())

    async def on_cleanup(app: web.Application) -> None:
        await agent.engine.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_post("/api/command", command)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/report", report)
    return app


def port_in_use(host: str, port: int) -> bool:
    """True if we cannot bind host:port. Checked before the agent starts, so a
    busy port does not leave the trading feed running with no dashboard."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jrock-web", description="J+Rock web dashboard")
    ap.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT") or 8080))
    ap.add_argument("--env", help="path to .env")
    args = ap.parse_args(argv)

    if port_in_use(args.host, args.port):
        print(f"ERROR: {args.host}:{args.port} is already in use. Another dashboard "
              "may still be running, or another app holds the port. Stop it, or set "
              "WEB_PORT to a free port (e.g. WEB_PORT=8090 python -m jrock.web).",
              file=sys.stderr)
        return 1

    # Hex only: no I/l or 0/O confusion when copying from a terminal.
    token = os.getenv("WEB_TOKEN") or secrets.token_hex(16)
    if args.host not in LOOPBACK_HOSTS:
        print(f"WARNING: binding to {args.host}, which is reachable from the network. "
              "Use TLS and a reverse proxy.", file=sys.stderr)

    agent = JRockAgent(load_settings(args.env))
    print(f"J+Rock dashboard: http://{args.host}:{args.port}/?token={token}")
    web.run_app(make_app(agent, token), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
