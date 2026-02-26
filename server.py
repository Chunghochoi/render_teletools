import asyncio
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# ===================== CONFIG =====================
API_KEY = os.environ.get("API_KEY", "")
if not API_KEY:
    print("[WARN] API_KEY env is empty. Agent endpoints will be unprotected!")

MACHINE_TTL_SECONDS = 60
CMD_MAX = 300
LINK_MAX = 3000

# ===================== IN-MEMORY STORE =====================
commands: List[Dict[str, Any]] = []  # queue: {id, ts, cmd, count, delay}
links: List[Dict[str, Any]] = []  # history {ts, url}
known_urls = set()  # chống push trùng cùng URL
pending_urls: Deque[str] = deque()  # chờ phân phối cho máy

# phân phối theo chu kỳ: mỗi máy tối đa 1 link / 1 chu kỳ nhận link từ bot
current_cycle_id = 0
cycle_served_ips = set()

# machine theo IP
# machines[ip] = {"last_seen": float, "queue": deque[str], "last_cycle": int}
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
        machines[ip] = {"last_seen": now_ts(), "queue": deque(), "last_cycle": -1}
    else:
        machines[ip]["last_seen"] = now_ts()


def prune_inactive_machines_locked():
    ts = now_ts()
    stale = [ip for ip, info in machines.items() if (ts - float(info.get("last_seen", 0))) > MACHINE_TTL_SECONDS]
    for ip in stale:
        # trả link đang giữ (nếu có) về pending để không mất link
        q = machines[ip].get("queue") or deque()
        while q:
            pending_urls.appendleft(q.pop())
        cycle_served_ips.discard(ip)
        del machines[ip]


def distribute_links_locked():
    # Mỗi chu kỳ: mỗi máy chỉ được nhận tối đa 1 link
    active_ips = [
        ip for ip, info in machines.items() if now_ts() - float(info.get("last_seen", 0)) <= MACHINE_TTL_SECONDS
    ]
    if not active_ips or not pending_urls:
        return

    # chỉ phân phối cho máy chưa nhận link trong chu kỳ hiện tại
    # và hiện đang không có link chờ mở
    candidates = [
        ip
        for ip in sorted(active_ips)
        if ip not in cycle_served_ips and machines[ip].get("last_cycle", -1) != current_cycle_id and not machines[ip]["queue"]
    ]

    for ip in candidates:
        if not pending_urls:
            break
        machines[ip]["queue"].append(pending_urls.popleft())
        machines[ip]["last_cycle"] = current_cycle_id
        cycle_served_ips.add(ip)


ALLOWED_COMMANDS = (
    "/start",
    "/uptolinkstep2",
    "/uptolinksocical",
    "/view",
    "/checkin",
)

app = FastAPI(title="Relay Dashboard (Render Free)")


# ===================== UI PAGE =====================
def page() -> str:
    return r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Uptolink Dashboard (Relay)</title>
  <style>
    body { font-family: Arial, sans-serif; background:#0b1020; color:#e7ecff; margin:0; height:100vh; }
    .wrap { max-width: 1200px; margin: 16px auto; padding: 0 12px; height: calc(100vh - 32px); display:flex; flex-direction: column; gap: 12px; }
    .card { background:#111a33; border:1px solid #23305e; border-radius:14px; padding:14px; box-shadow: 0 10px 24px rgba(0,0,0,.25); }
    h1 { margin:0; font-size: 18px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#0b1020; border:1px solid #2a3b76; font-size:12px; opacity:.95; }
    .row { display:flex; gap:10px; flex-wrap: wrap; align-items:end; margin-top: 10px; }
    label { font-size: 12px; opacity: .9; display:block; margin-bottom:6px; }
    input { background:#0b1020; color:#e7ecff; border:1px solid #2a3b76; border-radius:10px; padding:10px; width: 160px; }
    button { background:#2b58ff; color:white; border:0; border-radius:12px; padding:10px 14px; cursor:pointer; font-weight:700; }
    button.secondary { background:#243055; }
    button.danger { background:#ff3b5c; }
    .btns { display:flex; gap:10px; flex-wrap: wrap; }
    .hint { font-size:12px; opacity:.85; margin-top: 6px; line-height:1.4; }

    .linksCard { flex: 1 1 auto; display:flex; flex-direction: column; min-height: 0; }
    .toolbar { display:flex; gap:10px; align-items:center; flex-wrap: wrap; margin-top: 10px; }
    .search { flex: 1 1 280px; max-width: 420px; }
    .linksBox { margin-top: 12px; background:#0b1020; border:1px solid #2a3b76; border-radius:12px; padding:12px; overflow:auto; min-height: 0; flex: 1 1 auto; }
    .linkrow { display:flex; gap:10px; align-items:center; padding: 8px 0; border-bottom: 1px solid rgba(42,59,118,.45); }
    .time { opacity:.75; font-size:12px; width:70px; flex:0 0 auto; }
    .url { flex: 1 1 auto; word-break: break-all; color:#8fb2ff; text-decoration:none; }
    .mini { padding:8px 10px; border-radius:10px; font-weight:700; }
    .topline { display:flex; justify-content: space-between; gap:12px; flex-wrap:wrap; align-items:center; }
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="topline">
      <h1>
        Uptolink Dashboard (Relay)
        <span class="pill" id="status">loading...</span>
        <span class="pill" id="machines">0 machines</span>
      </h1>
      <span class="pill">Auto phân phối theo IP máy đang online</span>
    </div>

    <div class="row">
      <div>
        <label>Số lần gửi</label>
        <input id="count" type="number" min="1" max="200" value="1" />
      </div>
      <div>
        <label>Delay (giây)</label>
        <input id="delay" type="number" min="0" step="0.1" value="5" />
      </div>

      <div class="btns">
        <button onclick="sendCmd('/start')">/start</button>
        <button onclick="sendCmd('/uptolinkstep2')">/uptolinkstep2</button>
        <button onclick="sendCmd('/uptolinksocical')">/uptolinksocical</button>
        <button class="secondary" onclick="sendCmd('/view')">/view</button>
        <button class="secondary" onclick="sendCmd('/checkin')">/checkin</button>

        <button class="secondary" onclick="refreshAll()">Refresh</button>
        <button class="danger" onclick="clearLinks()">Clear links</button>
        <button id="autoBtn" class="secondary" onclick="toggleAutoOpen()">Auto-open: OFF</button>
      </div>
    </div>

    <div class="hint">
      Mỗi IP mở trang này được xem là 1 máy. Khi có links mới, server phân phối mỗi máy tối đa 1 link/đợt, không trùng.
    </div>
  </div>

  <div class="card linksCard">
    <div class="topline">
      <h1 style="font-size:16px;">Uptolink links</h1>
      <span class="pill" id="countLinks">0 links</span>
    </div>

    <div class="toolbar">
      <input class="search" id="q" placeholder="Search in links..." oninput="renderLinks()" />
      <button class="secondary" onclick="copyAll()">Copy all</button>
      <button class="secondary" onclick="openAll()">Open all</button>
    </div>

    <div class="linksBox" id="links">(loading...)</div>
  </div>
</div>

<script>
let cachedLinks = [];
let autoOpenEnabled = localStorage.getItem('autoOpenEnabled') === '1';

async function getStatus() {
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    document.getElementById('status').textContent = j.ok ? 'ok' : 'not ready';
    document.getElementById('machines').textContent = `${j.machine_count || 0} machines`;
  } catch (e) {
    document.getElementById('status').textContent = 'offline';
  }
}

function updateAutoButton() {
  const b = document.getElementById('autoBtn');
  if (!b) return;
  b.textContent = 'Auto-open: ' + (autoOpenEnabled ? 'ON' : 'OFF');
  b.style.background = autoOpenEnabled ? '#0f9d58' : '#243055';
}

function toggleAutoOpen() {
  autoOpenEnabled = !autoOpenEnabled;
  localStorage.setItem('autoOpenEnabled', autoOpenEnabled ? '1' : '0');
  updateAutoButton();
}

function readParams() {
  const count = parseInt(document.getElementById('count').value || '1', 10);
  const delay = parseFloat(document.getElementById('delay').value || '5');
  return { count, delay };
}

async function sendCmd(cmd) {
  const p = readParams();
  const r = await fetch('/api/command', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ cmd, count: p.count, delay: p.delay })
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) alert(j.detail || j.error || 'failed');
}

async function refreshLinks() {
  const r = await fetch('/api/links');
  const j = await r.json();
  cachedLinks = (j.links || []);
  document.getElementById('countLinks').textContent = cachedLinks.length + ' links';
  renderLinks();
}

function escapeHtml(s) {
  return (s || '')
    .replaceAll('&','&amp;')
    .replaceAll('<','&lt;')
    .replaceAll('>','&gt;')
    .replaceAll('"','&quot;')
    .replaceAll("'",'&#39;');
}

function renderLinks() {
  const box = document.getElementById('links');
  const q = (document.getElementById('q').value || '').trim().toLowerCase();
  const arr = q ? cachedLinks.filter(x => (x.url||'').toLowerCase().includes(q)) : cachedLinks;

  if (arr.length === 0) { box.innerHTML = '(no links yet)'; return; }

  box.innerHTML = arr.map((x) => {
    const rawUrl = x.url || '';
    const safeUrl = escapeHtml(rawUrl);
    const safeTime = escapeHtml(x.time);
    const encodedUrl = encodeURIComponent(rawUrl);
    return `
      <div class="linkrow">
        <span class="time">[${safeTime}]</span>
        <a class="url" href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeUrl}</a>
        <button class="mini secondary" onclick="copyText(decodeURIComponent('${encodedUrl}'))">Copy</button>
        <button class="mini" onclick="window.open(decodeURIComponent('${encodedUrl}'), '_blank', 'noopener')">Open</button>
      </div>
    `;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
  }
}

async function clearLinks() {
  const r = await fetch('/api/links/clear', { method: 'POST' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) alert(j.detail || j.error || 'failed');
  await refreshLinks();
}

async function copyAll() {
  if (!cachedLinks.length) return;
  await copyText(cachedLinks.map(x => x.url).join('\n'));
}

async function openAll() {
  for (const x of cachedLinks) {
    window.open(x.url, '_blank', 'noopener');
    await new Promise(res => setTimeout(res, 250));
  }
}

async function machinePing() {
  try {
    await fetch('/api/machine/ping', { method: 'POST' });
  } catch (e) {}
}

async function machineFetchAndOpen() {
  if (!autoOpenEnabled) return;

  try {
    const r = await fetch('/api/machine/next', { method: 'POST' });
    const j = await r.json();
    if (!r.ok || !j.ok || !j.url) return;

    const w = window.open(j.url, '_blank', 'noopener');
    if (!w) {
      console.warn('Popup blocked. Hãy cho phép popups cho site này để tự động mở tab.');
    }
  } catch (e) {}
}

async function refreshAll() {
  await getStatus();
  await refreshLinks();
}

async function boot() {
  updateAutoButton();
  await refreshAll();

  setInterval(refreshLinks, 1500);
  setInterval(getStatus, 3000);
  setInterval(machinePing, 2000);
  setInterval(machineFetchAndOpen, 1500);
}
boot();
</script>
</body>
</html>
"""


# ===================== ROUTES (PUBLIC WEB) =====================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    ip = get_client_ip(request)
    async with link_lock:
        ensure_machine(ip)
        prune_inactive_machines_locked()
        distribute_links_locked()
    return HTMLResponse(page())


@app.get("/api/status")
async def status():
    async with link_lock:
        prune_inactive_machines_locked()
        machine_count = len(machines)
        pending_count = len(pending_urls)
    return JSONResponse({
        "ok": True,
        "allowed_commands": list(ALLOWED_COMMANDS),
        "machine_count": machine_count,
        "pending_count": pending_count,
        "cycle_id": current_cycle_id,
    })


@app.post("/api/command")
async def post_command(payload: Dict[str, Any]):
    cmd = str(payload.get("cmd", "")).strip()
    count = int(payload.get("count", 1))
    delay = float(payload.get("delay", 5))

    if cmd not in ALLOWED_COMMANDS:
        return JSONResponse(
            {"ok": False, "error": f"Unsupported cmd. Allowed: {ALLOWED_COMMANDS}"},
            status_code=400,
        )
    if not (1 <= count <= 200):
        return JSONResponse({"ok": False, "error": "count must be 1..200"}, status_code=400)
    if delay < 0:
        return JSONResponse({"ok": False, "error": "delay must be >= 0"}, status_code=400)

    async with cmd_lock:
        commands.append({"id": int(now_ts() * 1000), "ts": now_ts(), "cmd": cmd, "count": count, "delay": delay})
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
        pending_urls.clear()
        cycle_served_ips.clear()
        for machine in machines.values():
            machine["queue"].clear()
            machine["last_cycle"] = -1
    return JSONResponse({"ok": True})


@app.post("/api/machine/ping")
async def machine_ping(request: Request):
    ip = get_client_ip(request)
    async with link_lock:
        ensure_machine(ip)
        prune_inactive_machines_locked()
        distribute_links_locked()
        return JSONResponse({"ok": True, "ip": ip, "machine_count": len(machines)})


@app.post("/api/machine/next")
async def machine_next(request: Request):
    ip = get_client_ip(request)
    async with link_lock:
        ensure_machine(ip)
        prune_inactive_machines_locked()
        distribute_links_locked()

        q: Deque[str] = machines[ip]["queue"]
        url = q.popleft() if q else None

    return JSONResponse({"ok": True, "url": url, "ip": ip})


# ===================== ROUTES (AGENT ONLY) =====================
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
        prev_pending = len(pending_urls)
        for raw in arr:
            if not isinstance(raw, str):
                continue
            url = raw.strip()
            if not url or url in known_urls:
                continue

            known_urls.add(url)
            links.append({"ts": now_ts(), "url": url})
            pending_urls.append(url)
            accepted += 1

        if len(links) > LINK_MAX:
            extra = len(links) - LINK_MAX
            removed = links[:extra]
            del links[:extra]
            for item in removed:
                known_urls.discard(item.get("url"))

        # Nếu có link mới đi vào pending thì mở chu kỳ phân phối mới
        if len(pending_urls) > prev_pending:
            global current_cycle_id
            current_cycle_id += 1
            cycle_served_ips.clear()
            for machine in machines.values():
                machine["last_cycle"] = -1

        prune_inactive_machines_locked()
        distribute_links_locked()

    return JSONResponse({"ok": True, "accepted": accepted, "cycle_id": current_cycle_id})


# ===================== MAIN =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")