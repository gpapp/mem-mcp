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

import os
import base64
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

import memory as mem

def _render_template(name: str, **context) -> str:
    path = os.path.join(os.path.dirname(__file__), "templates", f"{name}.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    for k, v in context.items():
        html = html.replace(f"{{{{{k}}}}}", str(v))
    return html

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
    tags: Optional[str] = ""


class MemoryUpdate(BaseModel):
    text: str
    category: str = "General"
    tags: Optional[str] = ""


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

@web_app.get("/api/ping")
async def api_ping():
    return {"status": "ok", "version": "1.3", "base_url": mem.BASE_URL}

@web_app.get("/api/memories", response_class=JSONResponse)
async def api_list_memories(request: Request):
    try:
        return mem.db_list_memories(_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/memories", response_class=JSONResponse, status_code=201)
async def api_create_memory(request: Request, body: MemoryCreate):
    try:
        metadata = {"tags": [t.strip() for t in body.tags.split(",") if t.strip()]} if body.tags else {}
        doc_id = await mem.db_add_memory(body.text, body.category, _user(request), metadata)
        return {"id": doc_id, "text": body.text, "category": body.category.strip().capitalize(), "metadata": metadata}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.put("/api/memories/{memory_id}", response_class=JSONResponse)
async def api_update_memory(memory_id: str, request: Request, body: MemoryUpdate):
    try:
        metadata = {"tags": [t.strip() for t in body.tags.split(",") if t.strip()]} if body.tags else {}
        found = await mem.db_update_memory(memory_id, body.text, body.category, _user(request), metadata)
        if not found:
            raise HTTPException(status_code=404, detail="Memory not found or access denied.")
        return {"id": memory_id, "text": body.text, "category": body.category.strip().capitalize(), "metadata": metadata}
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


@web_app.get("/api/insights", response_class=JSONResponse)
async def api_get_insights(request: Request):
    try:
        return mem.db_find_patterns(_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/diary", response_class=JSONResponse, status_code=201)
async def api_save_diary(request: Request, body: DiaryCreate):
    try:
        entry_date = await mem.db_save_diary(body.content, _user(request), body.date)
        return {"date": entry_date, "content": body.content}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

@web_app.get("/api/whoami", response_class=JSONResponse)
async def api_whoami(request: Request):
    return {"user": _user(request)}


# ---------------------------------------------------------------------------
# HTML Routes
# ---------------------------------------------------------------------------

def _get_auth_context(request: Request):
    auth_user, auth_pass, auth_b64 = "unknown", "********", ""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ")[1]
            auth_b64 = encoded
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" in decoded:
                auth_user, auth_pass = decoded.split(":", 1)
        except Exception: pass
    
    # Intelligently calculate MCP_URL
    # If BASE_URL already ends in /mcp, don't append it again
    mcp_url = f"{mem.BASE_URL}/mcp"
    if mem.BASE_URL.endswith("/mcp"):
        mcp_url = mem.BASE_URL
        
    return {
        "AUTH_USER": auth_user, 
        "AUTH_PASS": auth_pass, 
        "AUTH_BASE64": auth_b64,
        "MCP_URL": mcp_url
    }


@web_app.get("/", response_class=HTMLResponse)
async def get_landing(request: Request):
    ctx = _get_auth_context(request)
    html = _render_template("landing", BASE_URL=mem.BASE_URL, **ctx)
    return HTMLResponse(content=html)


@web_app.get("/gui", response_class=HTMLResponse)
async def get_gui(request: Request):
    ctx = _get_auth_context(request)
    html = _render_template("dashboard", BASE_URL=mem.BASE_URL, **ctx)
    return HTMLResponse(content=html)
