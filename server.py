import asyncio
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

API_KEY = os.environ.get("API_KEY", "")
if not API_KEY:
    print("[WARN] API_KEY env is empty. Agent endpoints will be unprotected!")

MACHINE_TTL_SECONDS = 60
CMD_MAX = 300
LINK_MAX = 3000

BOT_OPTIONS = {
    "rin": {
        "label": "@rinmoney_bot",
        "commands": [
            ("/start", "/start"),
            ("/uptolinkstep2", "Uptolink 2 Steps"),
            ("/uptolinksocical", "Uptolink Social"),
            ("/view", "/view"),
            ("/checkin", "/checkin"),
        ],
    },
    "crypto": {
        "label": "@CryptoLinkForEarnBot",
        "commands": [
            ("/uptolinkstep2", "Uptolink 2 Steps"),
            ("/uptolinkstep3", "Uptolink 3 Steps"),
        ],
    },
}

ALLOWED_COMMANDS = {
    "/start",
    "/uptolinkstep2",
    "/uptolinkstep3",
    "/uptolinksocical",
    "/view",
    "/checkin",
}

commands: List[Dict[str, Any]] = []
links: List[Dict[str, Any]] = []
known_urls = set()
pending_unassigned: Deque[str] = deque()
machines: Dict[str, Dict[str, Any]] = {}

cmd_lock = asyncio.Lock()
link_lock = asyncio.Lock()


def now_ts() -> float:
    return time.time()


def fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def require_agent(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized (agent)")


def get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return (request.client.host if request.client else "unknown").strip() or "unknown"


def ensure_machine(ip: str):
    if ip not in machines:
        machines[ip] = {"last_seen": now_ts(), "queue": deque()}
    else:
        machines[ip]["last_seen"] = now_ts()


def active_ips_locked() -> List[str]:
    ts = now_ts()
    return [ip for ip, info in machines.items() if (ts - float(info.get("last_seen", 0))) <= MACHINE_TTL_SECONDS]


def prune_inactive_machines_locked():
    ts = now_ts()
    stale = [ip for ip, info in machines.items() if (ts - float(info.get("last_seen", 0))) > MACHINE_TTL_SECONDS]
    for ip in stale:
        q = machines[ip].get("queue") or deque()
        while q:
            pending_unassigned.appendleft(q.pop())
        del machines[ip]


def assign_all_pending_locked():
    active_ips = sorted(active_ips_locked())
    if not active_ips or not pending_unassigned:
        return

    idx = 0
    while pending_unassigned:
        ip = active_ips[idx % len(active_ips)]
        machines[ip]["queue"].append(pending_unassigned.popleft())
        idx += 1


def render_bot_buttons() -> str:
    out = []
    for bot_key, bot in BOT_OPTIONS.items():
        btns = []
        for cmd, label in bot["commands"]:
            btns.append(f'<button class="cmd" onclick="sendCmd(\'{cmd}\')">{label}</button>')
        out.append(
            f'''<div class="botPanel" data-bot="{bot_key}">
<h3>{bot['label']}</h3>
<div class="cmdGrid">{''.join(btns)}</div>
</div>'''
        )
    return "".join(out)


def page() -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Uptolink Liquid Glass Dashboard</title>
  <style>
    :root {{
      --bg: radial-gradient(1200px 700px at 10% 0%, #1f2a54 0%, #0a1023 45%, #070b18 100%);
      --glass: rgba(255,255,255,.09);
      --glass-2: rgba(255,255,255,.06);
      --stroke: rgba(255,255,255,.18);
      --txt: #e9eeff;
      --muted: #9db0e6;
      --accent: #7aa2ff;
      --ok: #44d19a;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; min-height:100vh; background:var(--bg); color:var(--txt); font-family: Inter,Segoe UI,Arial,sans-serif; }}
    .wrap {{ max-width: 1280px; margin: 18px auto; padding: 0 14px; display:grid; gap:14px; }}
    .glass {{
      background: linear-gradient(145deg, var(--glass), var(--glass-2));
      border: 1px solid var(--stroke);
      border-radius: 18px;
      backdrop-filter: blur(18px) saturate(140%);
      -webkit-backdrop-filter: blur(18px) saturate(140%);
      box-shadow: 0 14px 45px rgba(0,0,0,.35);
    }}
    .top {{ padding: 14px; display:grid; gap:12px; }}
    .topline {{ display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; align-items:center; }}
    .title {{ font-size:20px; font-weight:700; display:flex; gap:8px; align-items:center; }}
    .pill {{ border:1px solid var(--stroke); padding:5px 10px; border-radius:999px; font-size:12px; color:var(--muted); background: rgba(255,255,255,.05); }}

    .controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:end; }}
    .field label {{ display:block; font-size:12px; color:var(--muted); margin-bottom:5px; }}
    .field input {{ width:140px; background:rgba(0,0,0,.22); color:var(--txt); border:1px solid var(--stroke); border-radius:10px; padding:9px; }}

    .btn {{ border:1px solid var(--stroke); border-radius:12px; padding:9px 12px; color:var(--txt); background:rgba(255,255,255,.08); cursor:pointer; font-weight:600; }}
    .btn:hover {{ background:rgba(255,255,255,.14); }}
    .btn.danger {{ background: rgba(255,80,120,.24); }}
    .btn.ok {{ background: rgba(68,209,154,.25); }}

    .botSwitch {{ padding: 10px; display:flex; gap:8px; flex-wrap:wrap; }}
    .botTab {{ padding:8px 12px; border-radius:999px; border:1px solid var(--stroke); cursor:pointer; color:var(--muted); background:rgba(255,255,255,.04); }}
    .botTab.active {{ color:var(--txt); background:rgba(122,162,255,.28); border-color: rgba(122,162,255,.6); }}

    .botPanels {{ padding: 0 10px 10px; display:grid; gap:10px; }}
    .botPanel {{ display:none; border:1px solid var(--stroke); border-radius:14px; padding:10px; background:rgba(255,255,255,.04); }}
    .botPanel.active {{ display:block; }}
    .botPanel h3 {{ margin:0 0 8px 0; font-size:14px; color:var(--accent); }}
    .cmdGrid {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .cmd {{ border:1px solid var(--stroke); border-radius:10px; padding:8px 11px; background:rgba(255,255,255,.09); color:var(--txt); cursor:pointer; }}

    .links {{ padding: 12px; display:grid; gap:10px; min-height: 58vh; }}
    .toolbar {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    .search {{ flex:1; min-width:250px; max-width:460px; background:rgba(0,0,0,.22); color:var(--txt); border:1px solid var(--stroke); border-radius:10px; padding:9px; }}
    #links {{ overflow:auto; max-height: 52vh; border:1px solid var(--stroke); border-radius:12px; padding:10px; background:rgba(0,0,0,.18); }}
    .row {{ display:flex; gap:10px; align-items:center; padding:8px 0; border-bottom:1px solid rgba(255,255,255,.09); }}
    .time {{ width:70px; font-size:12px; color:var(--muted); }}
    .url {{ flex:1; word-break:break-all; color:#9dc1ff; text-decoration:none; }}
  </style>
</head>
<body>
<div class="wrap">
  <div class="glass top">
    <div class="topline">
      <div class="title">Liquid Glass Dashboard <span class="pill" id="status">loading...</span></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <span class="pill" id="machines">0 machines</span>
        <span class="pill" id="activeBotLabel">bot: -</span>
      </div>
    </div>

    <div class="controls">
      <div class="field"><label>Số lần gửi</label><input id="count" type="number" min="1" max="200" value="1" /></div>
      <div class="field"><label>Delay (giây)</label><input id="delay" type="number" min="0" step="0.1" value="5" /></div>
      <button class="btn" onclick="refreshAll()">Refresh</button>
      <button class="btn danger" onclick="clearLinks()">Clear links</button>
      <button id="autoBtn" class="btn" onclick="toggleAutoOpen()">Auto-open: OFF</button>
    </div>

    <div class="botSwitch" id="botSwitch"></div>
    <div class="botPanels">{render_bot_buttons()}</div>
  </div>

  <div class="glass links">
    <div class="topline">
      <div class="title" style="font-size:16px;">Received links</div>
      <span class="pill" id="countLinks">0 links</span>
    </div>

    <div class="toolbar">
      <input class="search" id="q" placeholder="Search in links..." oninput="renderLinks(false)" />
      <button class="btn" onclick="copyAll()">Copy all</button>
      <button class="btn" onclick="openAll()">Open all</button>
    </div>

    <div id="links">(loading...)</div>
  </div>
</div>

<script>
const BOT_OPTIONS = {str({k:{'label':v['label']} for k,v in BOT_OPTIONS.items()}).replace("'", '"')};
let activeBot = localStorage.getItem('activeBot') || 'rin';
let autoOpenEnabled = localStorage.getItem('autoOpenEnabled') === '1';
let cachedLinks = [];
let prevLen = 0;

function initBotSwitch() {{
  const wrap = document.getElementById('botSwitch');
  wrap.innerHTML = '';
  for (const [key, meta] of Object.entries(BOT_OPTIONS)) {{
    const tab = document.createElement('button');
    tab.className = 'botTab' + (key === activeBot ? ' active':'');
    tab.textContent = meta.label;
    tab.onclick = () => setActiveBot(key);
    wrap.appendChild(tab);
  }}
  setActiveBot(activeBot, true);
}}

function setActiveBot(key, silent=false) {{
  if (!BOT_OPTIONS[key]) key='rin';
  activeBot = key;
  localStorage.setItem('activeBot', key);
  document.querySelectorAll('.botTab').forEach((el, idx) => {{
    const k = Object.keys(BOT_OPTIONS)[idx];
    el.classList.toggle('active', k === key);
  }});
  document.querySelectorAll('.botPanel').forEach((el) => {{
    el.classList.toggle('active', el.dataset.bot === key);
  }});
  document.getElementById('activeBotLabel').textContent = 'bot: ' + BOT_OPTIONS[key].label;
  if (!silent) refreshAll();
}}

function updateAutoButton() {{
  const b = document.getElementById('autoBtn');
  b.textContent = 'Auto-open: ' + (autoOpenEnabled ? 'ON' : 'OFF');
  b.classList.toggle('ok', autoOpenEnabled);
}}

function toggleAutoOpen() {{
  autoOpenEnabled = !autoOpenEnabled;
  localStorage.setItem('autoOpenEnabled', autoOpenEnabled ? '1' : '0');
  updateAutoButton();
}}

async function getStatus() {{
  try {{
    const r = await fetch('/api/status');
    const j = await r.json();
    document.getElementById('status').textContent = j.ok ? 'ok' : 'not ready';
    document.getElementById('machines').textContent = `${{j.machine_count || 0}} machines`;
  }} catch (e) {{
    document.getElementById('status').textContent = 'offline';
  }}
}}

function readParams() {{
  return {{
    count: parseInt(document.getElementById('count').value || '1', 10),
    delay: parseFloat(document.getElementById('delay').value || '5')
  }};
}}

async function sendCmd(cmd) {{
  const p = readParams();
  const r = await fetch('/api/command', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ cmd, bot: activeBot, count: p.count, delay: p.delay }})
  }});
  const j = await r.json().catch(() => ({{}}));
  if (!r.ok) alert(j.detail || j.error || 'failed');
}}

async function refreshLinks() {{
  const r = await fetch('/api/links');
  const j = await r.json();
  cachedLinks = j.links || [];
  document.getElementById('countLinks').textContent = `${{cachedLinks.length}} links`;
  const added = cachedLinks.length > prevLen;
  prevLen = cachedLinks.length;
  renderLinks(added);
}}

function escapeHtml(s) {{
  return (s || '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
}}

function renderLinks(forceScrollBottom) {{
  const box = document.getElementById('links');
  const isNearBottom = (box.scrollHeight - box.scrollTop - box.clientHeight) < 48;
  const q = (document.getElementById('q').value || '').trim().toLowerCase();
  const arr = q ? cachedLinks.filter(x => (x.url || '').toLowerCase().includes(q)) : cachedLinks;

  if (!arr.length) {{
    box.innerHTML = '(no links yet)';
    return;
  }}

  box.innerHTML = arr.map(x => `
    <div class="row">
      <span class="time">[${{escapeHtml(x.time)}}]</span>
      <a class="url" href="${{escapeHtml(x.url)}}" target="_blank" rel="noopener noreferrer">${{escapeHtml(x.url)}}</a>
      <button class="btn" style="padding:6px 9px" onclick="copyText('${{encodeURIComponent(x.url)}}')">Copy</button>
      <button class="btn" style="padding:6px 9px" onclick="window.open('${{escapeHtml(x.url)}}', '_blank', 'noopener')">Open</button>
    </div>
  `).join('');

  if (forceScrollBottom || isNearBottom) {{
    box.scrollTop = box.scrollHeight;
  }}
}}

async function copyText(encoded) {{
  const text = decodeURIComponent(encoded);
  try {{ await navigator.clipboard.writeText(text); }}
  catch (e) {{
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
  }}
}}

async function copyAll() {{
  if (!cachedLinks.length) return;
  await navigator.clipboard.writeText(cachedLinks.map(x => x.url).join('\\n'));
}}

async function openAll() {{
  for (const x of cachedLinks) {{
    window.open(x.url, '_blank', 'noopener');
    await new Promise(res => setTimeout(res, 250));
  }}
}}

async function clearLinks() {{
  const r = await fetch('/api/links/clear', {{ method: 'POST' }});
  const j = await r.json().catch(() => ({{}}));
  if (!r.ok) alert(j.detail || j.error || 'failed');
  await refreshLinks();
}}

async function machinePing() {{
  try {{ await fetch('/api/machine/ping', {{ method: 'POST' }}); }} catch (e) {{}}
}}

async function machineFetchAndOpen() {{
  if (!autoOpenEnabled) return;
  try {{
    const r = await fetch('/api/machine/next', {{ method: 'POST' }});
    const j = await r.json();
    if (!r.ok || !j.ok || !j.url) return;
    const w = window.open(j.url, '_blank', 'noopener');
    if (!w) console.warn('Popup blocked. Please allow popups for this site.');
  }} catch (e) {{}}
}}

async function refreshAll() {{
  await getStatus();
  await refreshLinks();
}}

async function boot() {{
  initBotSwitch();
  updateAutoButton();
  await refreshAll();
  setInterval(refreshLinks, 1400);
  setInterval(getStatus, 3000);
  setInterval(machinePing, 2000);
  setInterval(machineFetchAndOpen, 1500);
}}
boot();
</script>
</body>
</html>
"""


app = FastAPI(title="Relay Dashboard (Render Free)")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    ip = get_client_ip(request)
    async with link_lock:
        ensure_machine(ip)
        prune_inactive_machines_locked()
        assign_all_pending_locked()
    return HTMLResponse(page())


@app.get("/api/status")
async def status():
    async with link_lock:
        prune_inactive_machines_locked()
        machine_count = len(machines)
        pending_count = len(pending_unassigned)
    return JSONResponse(
        {
            "ok": True,
            "allowed_commands": sorted(ALLOWED_COMMANDS),
            "bots": BOT_OPTIONS,
            "machine_count": machine_count,
            "pending_count": pending_count,
        }
    )


@app.post("/api/command")
async def post_command(payload: Dict[str, Any]):
    cmd = str(payload.get("cmd", "")).strip()
    bot = str(payload.get("bot", "rin")).strip().lower()
    count = int(payload.get("count", 1))
    delay = float(payload.get("delay", 5))

    if bot not in BOT_OPTIONS:
        return JSONResponse({"ok": False, "error": f"Unsupported bot: {bot}"}, status_code=400)
    if cmd not in ALLOWED_COMMANDS:
        return JSONResponse({"ok": False, "error": f"Unsupported cmd: {cmd}"}, status_code=400)
    if cmd not in {c for c, _ in BOT_OPTIONS[bot]["commands"]}:
        return JSONResponse({"ok": False, "error": f"Command {cmd} not allowed for bot {bot}"}, status_code=400)
    if not (1 <= count <= 200):
        return JSONResponse({"ok": False, "error": "count must be 1..200"}, status_code=400)
    if delay < 0:
        return JSONResponse({"ok": False, "error": "delay must be >= 0"}, status_code=400)

    async with cmd_lock:
        commands.append({"id": int(now_ts() * 1000), "ts": now_ts(), "bot": bot, "cmd": cmd, "count": count, "delay": delay})
        if len(commands) > CMD_MAX:
            del commands[: len(commands) - CMD_MAX]

    return JSONResponse({"ok": True})


@app.get("/api/links")
async def get_links():
    async with link_lock:
        out = [{"time": fmt_time(x["ts"]), "url": x["url"]} for x in links]
    return JSONResponse({"ok": True, "links": out})


@app.post("/api/links/clear")
async def clear_links():
    async with link_lock:
        links.clear()
        known_urls.clear()
        pending_unassigned.clear()
        for machine in machines.values():
            machine["queue"].clear()
    return JSONResponse({"ok": True})


@app.post("/api/machine/ping")
async def machine_ping(request: Request):
    ip = get_client_ip(request)
    async with link_lock:
        ensure_machine(ip)
        prune_inactive_machines_locked()
        assign_all_pending_locked()
        return JSONResponse({"ok": True, "ip": ip, "machine_count": len(machines)})


@app.post("/api/machine/next")
async def machine_next(request: Request):
    ip = get_client_ip(request)
    async with link_lock:
        ensure_machine(ip)
        prune_inactive_machines_locked()
        assign_all_pending_locked()
        q: Deque[str] = machines[ip]["queue"]
        url = q.popleft() if q else None
    return JSONResponse({"ok": True, "url": url, "ip": ip})


@app.post("/api/pull")
async def pull_command(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    require_agent(x_api_key)
    async with cmd_lock:
        if not commands:
            return JSONResponse({"ok": True, "command": None})
        cmd = commands.pop(0)
    return JSONResponse({"ok": True, "command": cmd})


@app.post("/api/push_links")
async def push_links(payload: Dict[str, Any], x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    require_agent(x_api_key)
    arr = payload.get("links", [])
    if not isinstance(arr, list):
        return JSONResponse({"ok": False, "error": "links must be a list"}, status_code=400)

    accepted = 0
    async with link_lock:
        for raw in arr:
            if not isinstance(raw, str):
                continue
            url = raw.strip()
            if not url or url in known_urls:
                continue
            known_urls.add(url)
            links.append({"ts": now_ts(), "url": url})
            pending_unassigned.append(url)
            accepted += 1

        if len(links) > LINK_MAX:
            extra = len(links) - LINK_MAX
            removed = links[:extra]
            del links[:extra]
            for item in removed:
                known_urls.discard(item.get("url"))

        prune_inactive_machines_locked()
        assign_all_pending_locked()

    return JSONResponse({"ok": True, "accepted": accepted})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")