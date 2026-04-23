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

_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Memory Vault | Home</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --primary: #4f46e5; --bg: #f8fafc; --surface: #ffffff;
      --border: #e5e7eb; --text: #111827; --muted: #6b7280;
      --radius: 12px;
    }
    body { margin: 0; font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }
    .container { max-width: 800px; margin: 0 auto; padding: 4rem 1.5rem; }
    
    header { text-align: center; margin-bottom: 4rem; }
    h1 { font-size: 2.5rem; font-weight: 800; margin-bottom: 1rem; letter-spacing: -1px; }
    .badge { background: #eef2ff; color: var(--primary); padding: .4rem 1rem; border-radius: 99px; font-weight: 600; font-size: .9rem; }
    
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 2rem; box-shadow: 0 4px 12px rgba(0,0,0,.03); margin-bottom: 2rem; }
    h2 { font-size: 1.25rem; font-weight: 700; margin-top: 0; display: flex; align-items: center; gap: .5rem; }
    
    pre { background: #1f2937; color: #f3f4f6; padding: 1.25rem; border-radius: 8px; overflow-x: auto; font-size: .9rem; }
    code { font-family: 'JetBrains Mono', 'Fira Code', monospace; }
    
    .btn { display: inline-block; background: var(--primary); color: #fff; padding: .8rem 2rem; border-radius: 8px; text-decoration: none; font-weight: 600; transition: transform .15s; }
    .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(79,70,229,.3); }
    
    .step { margin-bottom: 1.5rem; }
    .step-num { display: inline-block; width: 24px; height: 24px; background: var(--primary); color: #fff; text-align: center; line-height: 24px; border-radius: 50%; font-size: .8rem; font-weight: 700; margin-right: .5rem; }
    
    footer { text-align: center; color: var(--muted); font-size: .9rem; margin-top: 4rem; }
    .url-tag { color: var(--primary); font-weight: 700; background: #eef2ff; padding: .1rem .4rem; border-radius: 4px; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <span class="badge">Production Ready</span>
      <h1>🧠 Memory Vault</h1>
      <p>A unified knowledge graph and vector store for your personal memories.</p>
      <div style="margin-top: 2rem;">
        <a href="{{BASE_URL}}/gui" class="btn">Go to Dashboard →</a>
      </div>
    </header>

    <div class="card">
      <h2>🔌 MCP Endpoint Setup</h2>
      <p>Connect your AI assistant (Claude Desktop, etc.) to your Memory Vault via the Model Context Protocol.</p>
      
      <div class="step">
        <p><span class="step-num">1</span> Your Endpoint URL is:</p>
        <p><code class="url-tag">{{BASE_URL}}/mcp</code></p>
      </div>

      <div class="step">
        <p><span class="step-num">2</span> <strong>Claude Desktop Config:</strong></p>
        <p>Add this snippet to your <code>claude_desktop_config.json</code>:</p>
        <pre><code>{
  "mcpServers": {
    "memory-vault": {
      "url": "{{AUTH_USER}}:{{AUTH_PASS}}@{{BASE_URL}}/mcp"
    }
  }
}</code></pre>
      </div>

      <div class="step">
        <p><span class="step-num">3</span> <strong>Authentication:</strong></p>
        <p>The server uses Basic Auth. Use these credentials in your client:</p>
        <div style="background: var(--bg); padding: 1rem; border-radius: 8px; border: 1px dashed var(--primary);">
          <p style="margin:0;"><strong>User:</strong> <code>{{AUTH_USER}}</code></p>
          <p style="margin:.5rem 0 0 0;"><strong>Pass:</strong> <code>{{AUTH_PASS}}</code></p>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>✨ Key Features</h2>
      <ul style="padding-left: 1.2rem;">
        <li><strong>Vector Search</strong>: Semantic retrieval of facts and diary entries.</li>
        <li><strong>Knowledge Graph</strong>: Automatic linking of entities in Neo4j.</li>
        <li><strong>Diary History</strong>: Time-based personal journal with Markdown support.</li>
        <li><strong>Multi-User</strong>: Isolated vaults based on proxy headers.</li>
      </ul>
    </div>

    <footer>
      Powered by FastMCP, FastAPI, Qdrant, and Neo4j.
    </footer>
  </div>
</body>
</html>
"""

web_app = FastAPI(title="Memory Vault GUI")

@web_app.middleware("http")
async def log_gui_requests(request: Request, call_next):
    print(f"[GUI] {request.method} {request.url.path}")
    return await call_next(request)

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
    user = mem.extract_user_from_headers(dict(request.headers))
    print(f"[GUI] API Request path: {request.url.path} (User: {user})")
    return user


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

    /* ── Diary Layout ── */
    .diary-layout { display: flex; gap: 1.5rem; height: calc(100vh - 180px); min-height: 500px; }
    .diary-sidebar { width: 220px; flex-shrink: 0; background: var(--surface); border: 1px solid var(--border); 
                     border-radius: var(--radius); overflow-y: auto; display: flex; flex-direction: column; }
    .diary-sidebar-header { padding: 1rem; border-bottom: 1px solid var(--border); font-weight: 700; font-size: .85rem; 
                            text-transform: uppercase; color: var(--muted); display: flex; justify-content: space-between; align-items: center; }
    .diary-date-item { padding: .75rem 1rem; cursor: pointer; border-bottom: 1px solid var(--bg); transition: all .15s; 
                       font-size: .9rem; color: var(--text); font-weight: 500; }
    .diary-date-item:hover { background: var(--bg); color: var(--primary); }
    .diary-date-item.active { background: var(--primary-light); color: var(--primary); border-right: 3px solid var(--primary); }
    
    .diary-main { flex: 1; display: flex; flex-direction: column; gap: 1rem; overflow-y: auto; text-align: left; }
    .diary-view-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); 
                       padding: 2rem; box-shadow: var(--shadow); flex-grow: 1; text-align: left; }
    .diary-editor-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); 
                         padding: 1.25rem; box-shadow: var(--shadow); text-align: left; }
    .markdown-body { background: transparent !important; color: var(--text); text-align: left; }
    .markdown-body * { text-align: left; }
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
  <div class="diary-layout">
    <div class="diary-sidebar">
      <div class="diary-sidebar-header">
        <span>History</span>
        <button class="btn btn-primary btn-sm" onclick="showDiaryEditor()">＋ New</button>
      </div>
      <div id="diary-dates-list"></div>
    </div>
    
    <div class="diary-main">
      <!-- Editor View -->
      <div id="diary-editor" class="diary-editor-card" style="display:none;">
        <strong style="display:block;margin-bottom:.75rem;font-size:.95rem;">Write a diary entry</strong>
        <div style="margin-bottom:.75rem;">
          <label for="diary-date">Date (YYYY-MM-DD, blank = today)</label>
          <input id="diary-date" placeholder="2025-01-15" style="max-width:200px;">
        </div>
        <label for="diary-content">Content (Markdown supported)</label>
        <textarea id="diary-content" placeholder="Today I…"></textarea>
        <div style="margin-top:.75rem; display: flex; justify-content: space-between;">
          <button class="btn btn-ghost" onclick="cancelDiaryEdit()">Cancel</button>
          <button class="btn btn-primary" onclick="saveDiary()">💾 Save Entry</button>
        </div>
      </div>

      <!-- Display View -->
      <div id="diary-viewer" class="diary-view-card">
        <div id="diary-view-content" class="empty">Select an entry from the sidebar or create a new one.</div>
      </div>
    </div>
  </div>
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
  let diaryEntries = [];
  let activeDiaryDate = null;

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
    const datesEl = document.getElementById('diary-dates-list');
    datesEl.innerHTML = '<p class="empty">Loading…</p>';
    try {
      diaryEntries = await api.get('diary');
      renderDiarySidebar();
      if (diaryEntries.length > 0 && !activeDiaryDate) {
        selectDiaryEntry(diaryEntries[0].date);
      }
    } catch(e) { datesEl.innerHTML = '<p class="empty">⚠️ Error loading diary.</p>'; }
  }

  function renderDiarySidebar() {
    const el = document.getElementById('diary-dates-list');
    if (!diaryEntries.length) { el.innerHTML = '<p class="empty">No entries.</p>'; return; }
    
    // Sort dates descending
    const sorted = [...diaryEntries].sort((a,b) => b.date.localeCompare(a.date));
    
    el.innerHTML = sorted.map(d => `
      <div class="diary-date-item ${d.date === activeDiaryDate ? 'active' : ''}" onclick="selectDiaryEntry('${d.date}')">
        📅 ${d.date}
      </div>`).join('');
  }

  function selectDiaryEntry(date) {
    activeDiaryDate = date;
    const entry = diaryEntries.find(d => d.date === date);
    renderDiarySidebar();
    
    document.getElementById('diary-editor').style.display = 'none';
    document.getElementById('diary-viewer').style.display = 'block';
    
    const viewEl = document.getElementById('diary-view-content');
    if (entry) {
      viewEl.innerHTML = `
        <span class="diary-date-badge">📅 ${entry.date}</span>
        <div class="markdown-body">${marked.parse(entry.content || '')}</div>
        <div style="margin-top: 2rem; text-align: right;">
          <button class="btn btn-ghost btn-sm" onclick="editCurrentEntry()">✏️ Edit</button>
        </div>
      `;
    } else {
      viewEl.innerHTML = '<p class="empty">Select an entry.</p>';
    }
  }

  function showDiaryEditor() {
    activeDiaryDate = null;
    renderDiarySidebar();
    document.getElementById('diary-viewer').style.display = 'none';
    document.getElementById('diary-editor').style.display = 'block';
    document.getElementById('diary-date').value = new Date().toISOString().slice(0,10);
    document.getElementById('diary-content').value = '';
    document.getElementById('diary-content').focus();
  }

  function cancelDiaryEdit() {
    if (diaryEntries.length > 0) {
      selectDiaryEntry(diaryEntries[0].date);
    } else {
      document.getElementById('diary-editor').style.display = 'none';
      document.getElementById('diary-view-content').innerHTML = '<p class="empty">No entries.</p>';
    }
  }

  function editCurrentEntry() {
    const entry = diaryEntries.find(d => d.date === activeDiaryDate);
    if (!entry) return;
    
    document.getElementById('diary-viewer').style.display = 'none';
    document.getElementById('diary-editor').style.display = 'block';
    document.getElementById('diary-date').value = entry.date;
    document.getElementById('diary-content').value = entry.content;
    document.getElementById('diary-content').focus();
  }

  async function saveDiary() {
    const content = document.getElementById('diary-content').value.trim();
    const date    = document.getElementById('diary-date').value.trim() || null;
    if (!content) return;
    try {
      await api.post('diary', {content, date});
      toast('📖 Diary entry saved');
      await loadDiary();
      if (date) selectDiaryEntry(date);
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


@web_app.get("/", response_class=HTMLResponse)
async def get_landing(request: Request):
    # Try to extract Basic Auth info from headers for easier setup
    auth_user = "unknown"
    auth_pass = "********"
    
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            import base64
            encoded = auth_header.split(" ")[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" in decoded:
                auth_user, auth_pass = decoded.split(":", 1)
        except Exception:
            pass

    # Inject BASE_URL and Auth into the HTML
    html = _LANDING_HTML.replace("{{BASE_URL}}", mem.BASE_URL)
    html = html.replace("{{AUTH_USER}}", auth_user)
    html = html.replace("{{AUTH_PASS}}", auth_pass)
    
    return HTMLResponse(content=html)


@web_app.get("/gui", response_class=HTMLResponse)
@web_app.get("/mcp/gui", response_class=HTMLResponse)
async def get_gui():
    # Inject BASE_URL into the HTML
    html = _GUI_HTML.replace("{{BASE_URL}}", mem.BASE_URL)
    return HTMLResponse(content=html)
