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
import json
import asyncio # Added for asyncio.wait_for
import subprocess
from datetime import datetime, timedelta
from typing import Optional

import memory as mem
from fastapi import Request, HTTPException, FastAPI
from fastapi.responses import Response, JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from memory import SESSION_SECRET, SESSION_MAX_AGE # Import from memory.py
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sse_starlette.sse import EventSourceResponse
web_app = FastAPI(title="Memory Vault GUI")
web_app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="mem_session", max_age=SESSION_MAX_AGE, same_site="lax", https_only=False)

HTPASSWD_PATH = os.getenv("HTPASSWD_PATH", os.path.join(os.path.dirname(__file__), "htpasswd"))

def _verify_htpasswd(username: str, password: str) -> bool:
    try:
        if not os.path.exists(HTPASSWD_PATH):
            logging.warning(f"htpasswd file not found: {HTPASSWD_PATH}")
            return False
        result = subprocess.run(
            ["htpasswd", "-vb", HTPASSWD_PATH, username, password],
            capture_output=True, text=True
        )
        result_code = result.returncode
        logging.info(f"htpasswd verify: {username} -> {result_code == 0}")
        return result_code == 0
    except Exception as e:
        logging.warning(f"htpasswd verification failed: {e}")
        return False

templates = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(["html", "xml"])
)

# Helper functions must be defined BEFORE middleware that uses them
def _check_session_auth(request: Request) -> str | None:
    """Returns username if authenticated, else None."""
    session_user = request.session.get("user")
    session_pass = request.session.get("pass")
    if session_user and session_pass:
        return session_user
    
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ")[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" in decoded:
                return decoded.split(":", 1)[0]
        except Exception: pass
    return None

# Auth guard middleware - protect /gui routes
@web_app.middleware("http")
async def auth_guard(request: Request, call_next):
    if request.url.path.startswith("/gui") or request.url.path.startswith("/api"):
        user = _check_session_auth(request)
        if user:
            request.state.user = user # Set user in request state
        else:
            # Allow specific unauthenticated paths
            if request.url.path.startswith("/api/auth") or request.url.path in ["/api/ping", "/"] or request.url.path == "/api/events":
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
    name: Optional[str] = None
    category: str = "General"
    tags: Optional[str] = ""

    class Config:
        extra = "allow"


class MemoryUpdate(BaseModel):
    text: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[str] = None

    class Config:
        extra = "allow"


class MemoryLink(BaseModel):
    sourceId: str
    targetId: str
    relType: str = "KNOWS"


# ---------------------------------------------------------------------------
# User extraction (from request, not MCP context)
# ---------------------------------------------------------------------------

def _user(request: Request) -> str:
    session_user = request.session.get("user")
    if session_user:
        return session_user
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user
    user = mem.extract_user_from_headers(dict(request.headers))
    return user

def _require_user(request: Request) -> str:
    user = _user(request)
    if user == "anonymous" or not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


@web_app.put("/api/memories/{memory_id}", response_class=JSONResponse)
async def api_update_memory(memory_id: str, request: Request, body: MemoryUpdate):
    try:
        # Build metadata from tags and any extra fields
        all_fields = body.dict()
        metadata = {"tags": [t.strip() for t in (all_fields.pop("tags", "") or "").split(",") if t.strip()]}
        for std_key in ("text", "name", "category", "tags"):
            all_fields.pop(std_key, None)
        for k, v in all_fields.items():
            if v is not None and v != "":
                metadata[k] = v
        found = await mem.db_update_memory(memory_id, body.name, body.text, body.category, _require_user(request), metadata)
        if not found:
            raise HTTPException(status_code=404, detail="Memory not found or access denied.")
        return {"id": memory_id, "name": body.name, "text": body.text, "category": (body.category.strip().capitalize() if body.category else "General"), "metadata": metadata}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/memories", response_class=JSONResponse)
async def api_list_memories(request: Request):
    """List all memories for the current user."""
    try:
        return mem.db_list_memories(_require_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/memories/{memory_id}", response_class=JSONResponse)
async def api_get_memory(memory_id: str, request: Request):
    """Fetch a single memory enriched with links and metadata."""
    user_id = _require_user(request)
    # We use db_list_memories and filter to reuse the complex enrichment logic
    # which handles Neo4j link aggregation and property cleaning.
    all_m = mem.db_list_memories(user_id)
    m = next((x for x in all_m if x["id"] == memory_id), None)
    if not m:
        raise HTTPException(status_code=404, detail="Memory not found or access denied.")
    return m

@web_app.get("/api/diary/search", response_class=JSONResponse)
async def api_search_diary(request: Request, q: str = "", limit: int = 10, top_p: float = 0.4):
    """Search diary entries using vector similarity. Falls back to listing all if q is empty."""
    user_id = _require_user(request)
    try:
        if not q.strip():
            return mem.db_list_diary(user_id)
        return mem.db_search_diary(q.strip(), user_id, limit=limit, top_p=top_p)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/diary/{entry_id}", response_class=JSONResponse)
async def api_get_diary_entry(entry_id: str, request: Request):
    """Fetch a single diary entry by ID."""
    user_id = _require_user(request)
    # Reuse db_list_diary to ensure the structure (mentions, dates) is consistent.
    all_e = mem.db_list_diary(user_id)
    e = next((x for x in all_e if x["id"] == entry_id), None)
    if not e:
        raise HTTPException(status_code=404, detail="Diary entry not found or access denied.")
    return e


@web_app.delete("/api/diary/{entry_id}", response_class=JSONResponse)
async def api_delete_diary_entry(entry_id: str, request: Request):
    """Delete a single diary entry by ID."""
    try:
        deleted = await mem.db_delete_diary(entry_id, _require_user(request))
        if not deleted:
            raise HTTPException(status_code=404, detail="Diary entry not found or access denied.")
        return {"deleted": entry_id}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


class DiaryCreate(BaseModel):
    content: str
    name: str
    id: Optional[str] = None
    timestamp: str

class DiaryLink(BaseModel):
    factId: str


@web_app.put("/api/diary/{entry_id}", response_class=JSONResponse)
async def api_update_diary_entry(entry_id: str, request: Request, body: DiaryCreate):
    """Update a diary entry's content, name, and/or timestamp."""
    try:
        user_id = _require_user(request)
        ok = await mem.db_update_diary(entry_id, user_id, content=body.content, name=body.name, timestamp=body.timestamp)
        if not ok:
            raise HTTPException(status_code=404, detail="Diary entry not found or access denied.")
        return {"id": entry_id, "content": body.content, "name": body.name, "timestamp": body.timestamp}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/diary/{entry_id}/link", response_class=JSONResponse, status_code=201)
async def api_link_diary_mention(entry_id: str, request: Request, body: DiaryLink):
    """Link a diary entry to a fact via MENTIONS."""
    try:
        await mem.db_link_diary_mention(entry_id, body.factId, _require_user(request))
        return {"status": "linked"}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.delete("/api/diary/{entry_id}/link/{fact_id}", response_class=JSONResponse)
async def api_unlink_diary_mention(entry_id: str, fact_id: str, request: Request):
    """Remove a MENTIONS link from a diary entry to a fact."""
    try:
        await mem.db_unlink_diary_mention(entry_id, fact_id, _require_user(request))
        return {"status": "unlinked"}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/memories", response_class=JSONResponse, status_code=201)
async def api_create_memory(request: Request, body: MemoryCreate):
    try:
        metadata = {"tags": [t.strip() for t in body.tags.split(",") if t.strip()]} if body.tags else {}
        doc_id = await mem.db_add_memory(body.text, body.category, _require_user(request), metadata, name=body.name)
        return {"id": doc_id, "text": body.text, "name": body.name, "category": body.category.strip().capitalize(), "metadata": metadata}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/memories/link", response_class=JSONResponse, status_code=201)
async def api_link_memory(request: Request, body: MemoryLink):
    try:
        await mem.db_link_facts(body.sourceId, body.targetId, body.relType, {}, _require_user(request))
        return {"status": "linked"}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


class MemoryUnlink(BaseModel):
    sourceId: str
    targetId: str
    relType: Optional[str] = None


@web_app.delete("/api/memories/link", response_class=JSONResponse)
async def api_unlink_memory(request: Request, body: MemoryUnlink):
    try:
        await mem.db_unlink_facts(body.sourceId, body.targetId, body.relType or "", _require_user(request))
        return {"status": "unlinked"}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.delete("/api/memories/{memory_id}", response_class=JSONResponse)
async def api_delete_memory(memory_id: str, request: Request):
    try:
        await mem.db_delete_memory(memory_id, _require_user(request))
        return {"deleted": memory_id}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/categories", response_class=JSONResponse)
async def api_list_categories(request: Request):
    try:
        return mem.db_list_categories(_require_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/diary", response_class=JSONResponse)
async def api_list_diary(request: Request):
    try:
        return mem.db_list_diary(_require_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/insights", response_class=JSONResponse)
async def api_get_insights(request: Request):
    try:
        return mem.db_find_patterns(_require_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/graph", response_class=JSONResponse)
async def api_get_graph(request: Request):
    try:
        return mem.db_get_graph(_require_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/graph/neighbors/{fact_id}", response_class=JSONResponse)
async def api_get_neighbors(request: Request, fact_id: str):
    try:
        return mem.db_get_neighborhood(fact_id, depth=1, rel_types=None, user_id=_require_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/graph/focus/{fact_id}", response_class=JSONResponse)
async def api_focus_graph(request: Request, fact_id: str):
    try:
        user_id = _require_user(request)
        fact = mem.db_get_fact_by_id(fact_id, user_id)
        if not fact:
            raise HTTPException(status_code=404, detail="Fact not found")
        
        neighbors = mem.db_get_neighborhood(fact_id, depth=1, rel_types=None, user_id=user_id)
        
        # Group connections by relationship type
        connections_by_type = {}
        edges = []
        nodes = []
        
        # Add center node
        center = {
            "id": fact["id"],
            "name": fact.get("name", fact["text"][:50]),
            "text": fact["text"],
            "category": fact.get("category", "General")
        }
        
        # Track seen node IDs to avoid duplicates
        seen_nodes = {fact["id"]}
        
        for neighbor in neighbors:
            nid = neighbor["id"]
            if nid not in seen_nodes:
                seen_nodes.add(nid)
                nodes.append({
                    "id": neighbor["id"],
                    "name": neighbor.get("name", "")[:50] or neighbor.get("text", "")[:50],
                    "category": neighbor.get("category", "General")
                })
        
        return {
            "center": center,
            "nodes": nodes,
            "edges": edges,
            "neighbors": [{"id": n["id"], "name": n.get("name", "")[:50] or n.get("text", "")[:50], "category": n.get("category", "General")} for n in neighbors]
        }
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.get("/api/graph/connections/{fact_id}", response_class=JSONResponse)
async def api_get_connections(request: Request, fact_id: str):
    try:
        return mem.db_get_connections_by_type(fact_id, _require_user(request))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@web_app.post("/api/diary", response_class=JSONResponse, status_code=201)
async def api_save_diary(request: Request, body: DiaryCreate):
    try:
        user_id = _require_user(request)

        # If the entry exists (id provided) but the timestamp was changed,
        # we must delete the old entry because the ID is derived from the timestamp.
        if body.id:
            new_id = mem._diary_id(user_id, body.timestamp)
            if body.id != new_id:
                await mem.db_delete_diary(body.id, user_id)

        entry_ts = await mem.db_save_diary(body.content, user_id, body.timestamp, body.name)
        return {"timestamp": entry_ts, "content": body.content, "name": body.name}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

@web_app.get("/api/whoami", response_class=JSONResponse)
async def api_whoami(request: Request):
    return {"user": _require_user(request)}


@web_app.get("/api/events")
async def sse_events(request: Request):
    user_id = _require_user(request) # Ensure user is authenticated for SSE stream
    # Create a private queue for this specific connection
    queue = asyncio.Queue()
    mem.db_subscribers.append(queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Wait for an event with a timeout to periodically check for disconnects
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    # Only send events belonging to this specific user
                    if event["user_id"] == user_id:
                        yield {
                            "event": event["event_type"],
                            "data": json.dumps(event["payload"])
                        }
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    mem.logger.error(f"Error in SSE generator: {e}")
                    await asyncio.sleep(1)
        finally:
            if queue in mem.db_subscribers:
                mem.db_subscribers.remove(queue)
            mem.logger.info(f"SSE stream for user {user_id} closed.")

    return EventSourceResponse(event_generator())

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
        auth_b64 = base64.b64encode(f"{auth_user}:{auth_pass}".encode()).decode()
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
