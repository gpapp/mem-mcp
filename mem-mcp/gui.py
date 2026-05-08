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
import logging
import secrets
from datetime import datetime, timedelta

import memory as mem
from fastapi import Request, HTTPException, FastAPI
from fastapi.responses import Response, JSONResponse, HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape

SESSION_SECRET = os.getenv("MEM_SESSION_SECRET", secrets.token_hex(32))
web_app = FastAPI(title="Memory Vault GUI")
web_app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="mem_session")

templates = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(["html", "xml"])
)

# Helper functions must be defined BEFORE middleware that uses them
def _check_session_auth(request: Request) -> tuple[str, str] | None:
    session_user = request.session.get("user")
    session_pass = request.session.get("pass")
    if session_user and session_pass:
        return session_user, session_pass
    
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ")[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" in decoded:
                return decoded.split(":", 1)
        except Exception: pass
    return None

# Auth guard middleware - protect /gui routes
@web_app.middleware("http")
async def auth_guard(request: Request, call_next):
    if request.url.path.startswith("/gui") or request.url.path.startswith("/api"):
        creds = _check_session_auth(request)
        if not creds:
            if request.url.path.startswith("/api/auth") or request.url.path in ["/api/ping", "/"]:
                pass
            elif request.url.path.startswith("/api/") and not request.url.path.startswith("/api/auth"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            else:
                return RedirectResponse(url=mem.BASE_URL or "/", status_code=302)
    return await call_next(request)

# Suppress noisy uvicorn access logs for the root path (MCP heartbeats)
class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return msg.find("GET / ") == -1 and msg.find("GET /api/ping") == -1

logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

@web_app.middleware("http")
async def log_gui_requests(request: Request, call_next):
    # Log only interesting GUI/API requests
    if request.url.path not in ["/", "/api/ping", "/favicon.ico"]:
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
    title: Optional[str] = None
    category: str = "General"
    tags: Optional[str] = ""


class MemoryUpdate(BaseModel):
    text: str
    title: Optional[str] = None
    category: str = "General"
    tags: Optional[str] = ""

class MemoryLink(BaseModel):
    sourceId: str
    targetId: str
    relType: str


class DiaryCreate(BaseModel):
    content: str
    date: Optional[str] = None


# ---------------------------------------------------------------------------
# User extraction (from request, not MCP context)
# ---------------------------------------------------------------------------

def _user(request: Request) -> str:
    session_user = request.session.get("user")
    if session_user:
        print(f"[GUI] API Request path: {request.url.path} (User: {session_user} [session])")
        return session_user
    user = mem.extract_user_from_headers(dict(request.headers))
    print(f"[GUI] API Request path: {request.url.path} (User: {user})")
    return user

# ---------------------------------------------------------------------------
# Login / Logout Routes
# ---------------------------------------------------------------------------

class LoginForm(BaseModel):
    username: str
    password: str


async def _set_session(request: Request, user: str, password: str):
    request.session["user"] = user
    request.session["pass"] = password
    request.session["expires"] = (datetime.now() + timedelta(hours=24)).isoformat()


@web_app.post("/api/auth/login")
async def api_login(request: Request, response: Response, form: LoginForm):
    await _set_session(request, form.username, form.password)
    return {"status": "ok", "user": form.username}


@web_app.post("/api/auth/logout")
async def api_logout(request: Request, response: Response):
    request.session.clear()
    return {"status": "ok"}


@web_app.get("/api/auth/check")
async def api_auth_check(request: Request):
    creds = _check_session_auth(request)
    if creds:
        user, _pass = creds
        return {"authenticated": True, "user": user}
    return {"authenticated": False}


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

def _require_auth(request: Request):
    creds = _check_session_auth(request)
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return creds[0]


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
        doc_id = await mem.db_add_memory(body.text, body.category, _user(request), metadata, title=body.title)
        return {"id": doc_id, "text": body.text, "title": body.title, "category": body.category.strip().capitalize(), "metadata": metadata}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.put("/api/memories/{memory_id}", response_class=JSONResponse)
async def api_update_memory(memory_id: str, request: Request, body: MemoryUpdate):
    try:
        metadata = {"tags": [t.strip() for t in body.tags.split(",") if t.strip()]} if body.tags else {}
        found = await mem.db_update_memory(memory_id, body.title, body.text, body.category, _user(request), metadata)
        if not found:
            raise HTTPException(status_code=404, detail="Memory not found or access denied.")
        return {"id": memory_id, "title": body.title, "text": body.text, "category": body.category.strip().capitalize(), "metadata": metadata}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

@web_app.post("/api/memories/link", response_class=JSONResponse, status_code=201)
async def api_link_memory(request: Request, body: MemoryLink):
    try:
        await mem.db_link_facts(body.sourceId, body.targetId, body.relType, {}, _user(request))
        return {"status": "linked"}
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


@web_app.get("/api/graph", response_class=JSONResponse)
async def api_get_graph(request: Request):
    try:
        return mem.db_get_graph(_user(request))
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
    # Try session auth first, then Basic Auth header
    auth_user, auth_pass, auth_b64 = "unknown", "********", ""
    
    session_user = request.session.get("user")
    session_pass = request.session.get("pass")
    if session_user and session_pass:
        auth_user, auth_pass = session_user, session_pass
    else:
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
    # If BASE_URL is https://hass.securemail.hu/mcp, we want the mcp_url to be https://hass.securemail.hu/mcp/mcp
    mcp_url = f"{mem.BASE_URL}/mcp"
        
    return {
        "AUTH_USER": auth_user, 
        "AUTH_PASS": auth_pass, 
        "AUTH_BASE64": auth_b64,
        "MCP_URL": mcp_url
    }


@web_app.get("/favicon.svg", response_class=Response)
async def get_favicon():
    path = os.path.join(os.path.dirname(__file__), "templates", "favicon.svg")
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type="image/svg+xml")


def _render(name: str, **ctx):
    return templates.get_template(f"{name}.html").render(**ctx)


@web_app.get("/", response_class=HTMLResponse)
async def get_landing(request: Request):
    creds = _check_session_auth(request)
    ctx = _get_auth_context(request)
    base_url = mem.BASE_URL or "/"
    ctx["BASE_URL"] = base_url
    ctx["authenticated"] = bool(creds)
    html = _render("landing", **ctx)
    return HTMLResponse(content=html)


@web_app.get("/api/download/mcp-bridge.mjs", response_class=Response)
async def download_mcp_bridge(request: Request):
    ctx = _get_auth_context(request)
    ctx["BASE_URL"] = mem.BASE_URL or "/"
    js_content = templates.get_template("mcp-bridge.mjs").render(**ctx)
    return Response(
        content=js_content,
        media_type="application/javascript",
        headers={"Content-Disposition": 'attachment; filename="mcp-bridge.mjs"'}
    )

@web_app.get("/gui", response_class=HTMLResponse)
async def get_gui(request: Request):
    creds = _check_session_auth(request)
    if not creds:
        return RedirectResponse(url=mem.BASE_URL or "/", status_code=302)
    ctx = _get_auth_context(request)
    ctx["BASE_URL"] = mem.BASE_URL or "/"
    html = _render("dashboard", **ctx)
    return HTMLResponse(content=html)
