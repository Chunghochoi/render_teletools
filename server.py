import os
import time
import asyncio
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# ===================== CONFIG =====================
# Chỉ AGENT cần API_KEY (để pull/push). WEB KHÔNG CẦN.
API_KEY = os.environ.get("API_KEY", "")  # set trên Render
if not API_KEY:
    print("[WARN] API_KEY env is empty. Agent endpoints will be unprotected!")

# In-memory storage (Render free sleep/restart => mất dữ liệu)
commands: List[Dict[str, Any]] = []  # queue: {id, ts, cmd, count, delay}
links: List[Dict[str, Any]] = []     # {ts, url}

cmd_lock = asyncio.Lock()
link_lock = asyncio.Lock()

CMD_MAX = 300
LINK_MAX = 3000


def now_ts() -> float:
    return time.time()


def fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def require_agent(x_api_key: Optional[str]):
    # Nếu bạn set API_KEY => bắt buộc đúng key
    # Nếu bạn không set API_KEY => ai cũng gọi được (không khuyến nghị)
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized (agent)")


# ✅ Danh sách lệnh hợp lệ (đã sửa /uptolinksocical + thêm /view /checkin)
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
        <span class="pill">Public UI (no API key)</span>
      </h1>
      <span class="pill">Render free: RAM only (restart/sleep = mất links)</span>
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
      </div>
    </div>

    <div class="hint">
      Bấm nút → agent trên máy bạn poll lệnh → gửi bot → lọc link uptolink → đẩy link lên đây.
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

async function getStatus() {
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    document.getElementById('status').textContent = j.ok ? 'ok' : 'not ready';
  } catch (e) {
    document.getElementById('status').textContent = 'offline';
  }
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
    .replaceAll("'","&#39;");
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

async function refreshAll() {
  await getStatus();
  await refreshLinks();
}

async function boot() {
  await refreshAll();
  setInterval(refreshLinks, 1500);
  setInterval(getStatus, 5000);
}
boot();
</script>
</body>
</html>
"""


# ===================== ROUTES (PUBLIC WEB) =====================
@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(page())


@app.get("/api/status")
async def status():
    return JSONResponse({"ok": True, "allowed_commands": list(ALLOWED_COMMANDS)})


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
    return JSONResponse({"ok": True})


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

    async with link_lock:
        for url in arr:
            if isinstance(url, str) and url:
                links.append({"ts": now_ts(), "url": url})
        if len(links) > LINK_MAX:
            del links[: len(links) - LINK_MAX]

    return JSONResponse({"ok": True})


# ===================== MAIN =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
