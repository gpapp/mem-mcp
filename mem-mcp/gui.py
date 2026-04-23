"""
gui.py – FastAPI web application for the Memory Vault dashboard.

Provides:
  - GET  /gui              → interactive dashboard SPA
  - GET  /api/memories     → list all memories for the current user
  - POST /api/memories     → create a new memory
  - PUT  /api/memories/{id} → update an existing memory
  - DELETE /api/memories/{id} → delete a memory
  - GET  /api/diary        → list all diary entries
  - POST /api/diary        → create / update a diary entry
  - GET  /api/categories   → list distinct category names

All REST responses are JSON.  The GUI uses fetch() with relative URLs so
it works behind any reverse proxy regardless of base path.

User identity is extracted from the incoming request's Basic-Auth header
or common proxy headers – identical logic to the MCP server.
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

import memory as mem

web_app = FastAPI(title="Memory Vault GUI")

# Allow both /path and /path/ for all routes
from fastapi.routing import APIRoute
def toggle_strict_slashes(app: FastAPI):
    for route in app.routes:
        if isinstance(route, APIRoute):
            route.path_strict_slashes = False

@web_app.on_event("startup")
async def startup_event():
    toggle_strict_slashes(web_app)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class MemoryCreate(BaseModel):
    text: str
    category: str = "General"


class MemoryUpdate(BaseModel):
    text: str
    category: str = "General"


class DiaryCreate(BaseModel):
    content: str
    date: Optional[str] = None


# ---------------------------------------------------------------------------
# User extraction (from request, not MCP context)
# ---------------------------------------------------------------------------

def _user(request: Request) -> str:
    return mem.extract_user_from_headers(dict(request.headers))


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# REST API (All routes under /api)
# ---------------------------------------------------------------------------

@web_app.get("/api/ping")
async def api_ping():
    return {"status": "ok", "version": "1.2", "base_url": mem.BASE_URL}

@web_app.get("/api/memories", response_class=JSONResponse)
async def api_list_memories(request: Request):
    try:
        return mem.db_list_memories(_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/memories", response_class=JSONResponse, status_code=201)
async def api_create_memory(request: Request, body: MemoryCreate):
    try:
        doc_id = await mem.db_add_memory(body.text, body.category, _user(request))
        return {"id": doc_id, "text": body.text, "category": body.category.strip().capitalize()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.put("/api/memories/{memory_id}", response_class=JSONResponse)
async def api_update_memory(memory_id: str, request: Request, body: MemoryUpdate):
    try:
        found = await mem.db_update_memory(memory_id, body.text, body.category, _user(request))
        if not found:
            raise HTTPException(status_code=404, detail="Memory not found or access denied.")
        return {"id": memory_id, "text": body.text, "category": body.category.strip().capitalize()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.delete("/api/memories/{memory_id}", response_class=JSONResponse)
async def api_delete_memory(memory_id: str, request: Request):
    try:
        await mem.db_delete_memory(memory_id, _user(request))
        return {"deleted": memory_id}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/categories", response_class=JSONResponse)
async def api_list_categories(request: Request):
    try:
        return mem.db_list_categories(_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/diary", response_class=JSONResponse)
async def api_list_diary(request: Request):
    try:
        return mem.db_list_diary(_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/diary", response_class=JSONResponse, status_code=201)
async def api_save_diary(request: Request, body: DiaryCreate):
    try:
        entry_date = await mem.db_save_diary(body.content, _user(request), body.date)
        return {"date": entry_date, "content": body.content}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ---------------------------------------------------------------------------
# GUI – single-page application (all URLs are relative)
# ---------------------------------------------------------------------------

_GUI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Memory Vault</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    :root {
      --primary: #4f46e5; --primary-light: #eef2ff; --primary-dark: #3730a3;
      --danger: #ef4444; --danger-light: #fef2f2;
      --success: #10b981;
      --bg: #f8fafc; --surface: #ffffff;
      --border: #e5e7eb; --text: #111827; --muted: #6b7280;
      --radius: 10px; --shadow: 0 1px 3px rgba(0,0,0,.08);
    }
    *, *::before, *::after { box-sizing: border-box; }
    body { margin: 0; font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); }

    /* ── Nav ── */
    nav {
      background: var(--primary); color: #fff;
      padding: .9rem 1.5rem; display: flex; align-items: center;
      justify-content: space-between; position: sticky; top: 0; z-index: 100;
      box-shadow: 0 2px 8px rgba(79,70,229,.35);
    }
    nav h1 { margin: 0; font-size: 1.15rem; font-weight: 700; letter-spacing: -.3px; }
    #user-badge { font-size: .82rem; opacity: .9; background: rgba(255,255,255,.15);
                  padding: .2rem .7rem; border-radius: 99px; }

    /* ── Tabs ── */
    .tabs { display: flex; gap: .25rem; padding: 1rem 1.5rem .25rem;
            border-bottom: 2px solid var(--border); background: var(--surface); }
    .tab-btn { padding: .5rem 1.1rem; border: none; background: none; cursor: pointer;
               font: 500 .9rem 'Inter', sans-serif; color: var(--muted); border-radius: 6px 6px 0 0;
               transition: all .15s; }
    .tab-btn.active { color: var(--primary); background: var(--primary-light);
                      border-bottom: 2px solid var(--primary); margin-bottom: -2px; }
    .tab-btn:hover:not(.active) { background: var(--bg); }

    /* ── Layout ── */
    .page { display: none; padding: 1.5rem; max-width: 1100px; margin: 0 auto; }
    .page.active { display: block; }

    /* ── Cards & forms ── */
    .card { background: var(--surface); border: 1px solid var(--border);
            border-radius: var(--radius); padding: 1.25rem; box-shadow: var(--shadow); }
    .card + .card { margin-top: 1rem; }
    input, textarea, select {
      width: 100%; padding: .55rem .8rem; border: 1px solid var(--border);
      border-radius: 7px; font: .9rem 'Inter', sans-serif; color: var(--text);
      background: var(--bg); transition: border .15s;
    }
    input:focus, textarea:focus, select:focus {
      outline: none; border-color: var(--primary); background: #fff;
    }
    textarea { resize: vertical; min-height: 100px; }
    .form-row { display: flex; gap: .75rem; align-items: flex-end; flex-wrap: wrap; }
    .form-row > * { flex: 1 1 160px; }
    label { display: block; font-size: .8rem; font-weight: 600; color: var(--muted);
            margin-bottom: .3rem; text-transform: uppercase; letter-spacing: .5px; }

    /* ── Buttons ── */
    .btn { padding: .5rem 1rem; border: none; border-radius: 7px; cursor: pointer;
           font: 600 .85rem 'Inter', sans-serif; transition: all .15s; white-space: nowrap; }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:hover { background: var(--primary-dark); }
    .btn-ghost { background: var(--bg); color: var(--muted); border: 1px solid var(--border); }
    .btn-ghost:hover { background: var(--border); }
    .btn-danger { background: var(--danger-light); color: var(--danger); }
    .btn-danger:hover { background: #fca5a5; }
    .btn-sm { padding: .3rem .65rem; font-size: .78rem; }

    /* ── Memory list ── */
    .category-header {
      font-size: .75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;
      color: var(--primary); padding: 1.1rem 0 .4rem; border-bottom: 1px solid var(--primary-light);
      margin-bottom: .5rem;
    }
    .memory-item {
      display: flex; align-items: flex-start; gap: .75rem;
      background: var(--surface); border: 1px solid var(--border);
      border-left: 3px solid var(--primary); border-radius: var(--radius);
      padding: .75rem 1rem; margin-bottom: .5rem;
      transition: box-shadow .15s;
    }
    .memory-item:hover { box-shadow: 0 2px 8px rgba(79,70,229,.1); }
    .memory-text { flex: 1; font-size: .9rem; line-height: 1.5; }
    .memory-meta { font-size: .75rem; color: var(--muted); margin-top: .2rem; }
    .memory-actions { display: flex; gap: .4rem; flex-shrink: 0; }

    /* ── Edit form (inline) ── */
    .edit-form { margin-top: .5rem; display: flex; gap: .5rem; flex-wrap: wrap; }
    .edit-form input, .edit-form select { flex: 1 1 160px; }

    /* ── Diary ── */
    .diary-entry {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 1.5rem; margin-bottom: 1.25rem;
      box-shadow: var(--shadow);
    }
    .diary-date-badge {
      display: inline-block; background: var(--primary-light); color: var(--primary);
      padding: .25rem .85rem; border-radius: 99px; font-weight: 600; font-size: .85rem;
      margin-bottom: 1rem;
    }
    .markdown-body { background: transparent !important; color: var(--text); }

    /* ── Empty state ── */
    .empty { color: var(--muted); font-size: .9rem; padding: 2rem 0; text-align: center; }

    /* ── Toast ── */
    #toast {
      position: fixed; bottom: 1.5rem; right: 1.5rem;
      background: #1f2937; color: #fff; padding: .65rem 1.1rem;
      border-radius: 8px; font-size: .85rem; box-shadow: 0 4px 12px rgba(0,0,0,.2);
      opacity: 0; transform: translateY(8px); transition: all .25s; pointer-events: none; z-index: 999;
    }
    #toast.show { opacity: 1; transform: translateY(0); }
  </style>
</head>
<body>

<nav>
  <h1>🧠 Memory Vault</h1>
  <span id="user-badge">Loading…</span>
</nav>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('memories')">📝 Memories</button>
  <button class="tab-btn" onclick="switchTab('diary')">📖 Diary</button>
</div>

<!-- ═══ MEMORIES PAGE ═══ -->
<div id="page-memories" class="page active">
  <div class="card" style="margin-bottom:1.25rem;">
    <strong style="display:block;margin-bottom:.75rem;font-size:.95rem;">Add a new memory</strong>
    <div class="form-row">
      <div>
        <label for="new-text">Fact / Memory</label>
        <input id="new-text" placeholder="e.g. I prefer dark mode editors">
      </div>
      <div style="flex:0 1 200px;">
        <label for="new-category">Category</label>
        <input id="new-category" placeholder="e.g. Preferences" value="General">
      </div>
      <div style="flex:0 1 auto;padding-bottom:1px;">
        <label>&nbsp;</label>
        <button class="btn btn-primary" onclick="addMemory()">＋ Add</button>
      </div>
    </div>
  </div>

  <div id="memories-list"></div>
</div>

<!-- ═══ DIARY PAGE ═══ -->
<div id="page-diary" class="page">
  <div class="card" style="margin-bottom:1.25rem;">
    <strong style="display:block;margin-bottom:.75rem;font-size:.95rem;">Write a diary entry</strong>
    <div style="margin-bottom:.75rem;">
      <label for="diary-date">Date (YYYY-MM-DD, blank = today)</label>
      <input id="diary-date" placeholder="2025-01-15" style="max-width:200px;">
    </div>
    <label for="diary-content">Content (Markdown supported)</label>
    <textarea id="diary-content" placeholder="Today I…"></textarea>
    <div style="margin-top:.75rem;text-align:right;">
      <button class="btn btn-primary" onclick="saveDiary()">💾 Save Entry</button>
    </div>
  </div>

  <div id="diary-list"></div>
</div>

<div id="toast"></div>

<script>
  // ── API helpers ──────────────────────────────────────────────────────────
  const apiBase = "{{BASE_URL}}/api";

  const api = {
    get:    (url)       => fetch(`${apiBase}/${url}`).then(r => r.ok ? r.json() : Promise.reject(r)),
    post:   (url, body) => fetch(`${apiBase}/${url}`, {method:'POST',  headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r => r.ok ? r.json() : Promise.reject(r)),
    put:    (url, body) => fetch(`${apiBase}/${url}`, {method:'PUT',   headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r => r.ok ? r.json() : Promise.reject(r)),
    delete: (url)       => fetch(`${apiBase}/${url}`, {method:'DELETE'}).then(r => r.ok ? r.json() : Promise.reject(r)),
  };

  // ── State ─────────────────────────────────────────────────────────────────
  let memories = [];

  // ── Toast ─────────────────────────────────────────────────────────────────
  function toast(msg, ms=2500) {
    const el = document.getElementById('toast');
    el.textContent = msg; el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), ms);
  }

  // ── Tabs ──────────────────────────────────────────────────────────────────
  function switchTab(tab) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('page-' + tab).classList.add('active');
    document.querySelectorAll('.tab-btn').forEach(b => {
      if (b.textContent.toLowerCase().includes(tab === 'memories' ? 'memor' : 'diary'))
        b.classList.add('active');
    });
    if (tab === 'diary') loadDiary();
  }

  // ── Memories ──────────────────────────────────────────────────────────────
  async function loadMemories() {
    try {
      memories = await api.get('memories');
      renderMemories();
    } catch(e) { document.getElementById('memories-list').innerHTML = '<p class="empty">⚠️ Could not load memories.</p>'; }
  }

  function renderMemories() {
    const el = document.getElementById('memories-list');
    if (!memories.length) { el.innerHTML = '<p class="empty">No memories yet.</p>'; return; }

    // Group by category
    const groups = {};
    memories.forEach(m => { (groups[m.category] = groups[m.category] || []).push(m); });

    el.innerHTML = Object.keys(groups).sort().map(cat => `
      <div class="category-header">${cat}</div>
      ${groups[cat].map(m => renderMemoryItem(m)).join('')}
    `).join('');
  }

  function renderMemoryItem(m) {
    const ts = m.timestamp ? m.timestamp.slice(0,10) : '';
    return `
    <div class="memory-item" id="mem-${m.id}">
      <div class="memory-text">
        <div id="mem-text-${m.id}">${escHtml(m.text)}</div>
        <div class="memory-meta">${ts ? '📅 '+ts : ''} <span style="opacity:.5;font-size:.7rem;">${m.id.slice(0,8)}…</span></div>
        <div class="edit-form" id="edit-form-${m.id}" style="display:none;">
          <input id="edit-text-${m.id}" value="${escHtml(m.text)}">
          <input id="edit-cat-${m.id}" value="${escHtml(m.category)}" style="flex:0 1 160px;">
          <button class="btn btn-primary btn-sm" onclick="saveEdit('${m.id}')">Save</button>
          <button class="btn btn-ghost btn-sm" onclick="cancelEdit('${m.id}')">Cancel</button>
        </div>
      </div>
      <div class="memory-actions">
        <button class="btn btn-ghost btn-sm" id="edit-btn-${m.id}" onclick="startEdit('${m.id}')">✏️</button>
        <button class="btn btn-danger btn-sm" onclick="deleteMemory('${m.id}')">🗑️</button>
      </div>
    </div>`;
  }

  function startEdit(id) {
    document.getElementById('edit-form-' + id).style.display = 'flex';
    document.getElementById('edit-btn-' + id).style.display = 'none';
    document.getElementById('edit-text-' + id).focus();
  }
  function cancelEdit(id) {
    document.getElementById('edit-form-' + id).style.display = 'none';
    document.getElementById('edit-btn-' + id).style.display = '';
  }

  async function saveEdit(id) {
    const text = document.getElementById('edit-text-' + id).value.trim();
    const cat  = document.getElementById('edit-cat-' + id).value.trim() || 'General';
    if (!text) return;
    try {
      const updated = await api.put('memories/' + id, {text, category: cat});
      const m = memories.find(x => x.id === id);
      if (m) { m.text = updated.text; m.category = updated.category; }
      renderMemories();
      toast('✅ Memory updated');
    } catch(e) { toast('❌ Update failed'); }
  }

  async function addMemory() {
    const text = document.getElementById('new-text').value.trim();
    const cat  = document.getElementById('new-category').value.trim() || 'General';
    if (!text) return;
    try {
      const m = await api.post('memories', {text, category: cat});
      m.timestamp = new Date().toISOString();
      memories.push(m);
      renderMemories();
      document.getElementById('new-text').value = '';
      toast('✅ Memory saved');
    } catch(e) { toast('❌ Could not save memory'); }
  }

  async function deleteMemory(id) {
    if (!confirm('Delete this memory?')) return;
    try {
      await api.delete('memories/' + id);
      memories = memories.filter(m => m.id !== id);
      renderMemories();
      toast('🗑️ Memory deleted');
    } catch(e) { toast('❌ Delete failed'); }
  }

  // ── Diary ─────────────────────────────────────────────────────────────────
  async function loadDiary() {
    const el = document.getElementById('diary-list');
    el.innerHTML = '<p class="empty">Loading…</p>';
    try {
      const entries = await api.get('diary');
      if (!entries.length) { el.innerHTML = '<p class="empty">No diary entries yet.</p>'; return; }
      el.innerHTML = entries.map(d => `
        <div class="diary-entry">
          <span class="diary-date-badge">📅 ${d.date}</span>
          <div class="markdown-body">${marked.parse(d.content || '')}</div>
        </div>`).join('');
    } catch(e) { el.innerHTML = '<p class="empty">⚠️ Could not load diary.</p>'; }
  }

  async function saveDiary() {
    const content = document.getElementById('diary-content').value.trim();
    const date    = document.getElementById('diary-date').value.trim() || null;
    if (!content) return;
    try {
      await api.post('diary', {content, date});
      document.getElementById('diary-content').value = '';
      document.getElementById('diary-date').value = '';
      toast('📖 Diary entry saved');
      if (document.getElementById('page-diary').classList.contains('active')) loadDiary();
    } catch(e) { toast('❌ Could not save diary entry'); }
  }

  // ── Utilities ─────────────────────────────────────────────────────────────
  function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  (async () => {
    try {
      const info = await api.get('whoami');
      document.getElementById('user-badge').textContent = '👤 ' + info.user;
    } catch(_) { document.getElementById('user-badge').textContent = ''; }
    loadMemories();
  })();
</script>
</body>
</html>
"""


@web_app.get("/api/whoami", response_class=JSONResponse)
async def api_whoami(request: Request):
    return {"user": _user(request)}


@web_app.get("/gui", response_class=HTMLResponse)
@web_app.get("/mcp/gui", response_class=HTMLResponse)
async def get_gui():
    # Inject BASE_URL into the HTML
    html = _GUI_HTML.replace("{{BASE_URL}}", mem.BASE_URL)
    return HTMLResponse(content=html)
