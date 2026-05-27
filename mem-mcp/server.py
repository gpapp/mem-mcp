"""
server.py  - Entry point for the Memory Vault.

Starts a unified FastAPI server that handles:
  • MCP server  → /mcp
  • Web GUI     → /gui
  • REST API    → /api

All services run on port 8080 by default.
"""

import os
import uvicorn
import memory as mem

from mcp_tools import mcp
from gui import web_app
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from memory import SESSION_SECRET, SESSION_MAX_AGE
from starlette.middleware import Middleware as StarletteMiddleware
mcp_cors = StarletteMiddleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_methods=["*"], 
    allow_headers=["*"],
    allow_credentials=True
)
# Enable CORS for the unified server
web_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Merge MCP into the Web GUI app
# ---------------------------------------------------------------------------

# Session middleware for MCP app to ensure session is available for MCP calls
mcp_session_middleware = StarletteMiddleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="mem_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=False
)
mcp_app = mcp.http_app(transport="http", path="/mcp", middleware=[mcp_cors, mcp_session_middleware])
# We mount at / so that the proxy's /mcp hits the MCP server directly.
# GUI and API routes will take precedence because they were defined first.
web_app.mount("/", mcp_app)

# Ensure MCP lifespan is handled by the parent app
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    async with mcp_app.lifespan(mcp_app):
        await mem.run_consistency_checks()
        await mem.run_diary_consistency_checks()
        await mem.fix_diary_entries()
        await mem.sync_orphans()
        yield

web_app.router.lifespan_context = lifespan


from fastapi.responses import RedirectResponse

if __name__ == "__main__":
    print(f"--- Memory Vault Unified Server starting ---")
    print(f"--- Base URL: {mem.BASE_URL or 'Relative'} ---")
    print(f"--- All services on port 8080: /gui, /api, /mcp ---")

    # Extract nginx proxy prefix from BASE_URL so the SSE endpoint event
    # includes the full path the client needs to POST back through the proxy.
    # e.g. BASE_URL=https://host/mcp → root_path=/mcp → endpoint event=/mcp/messages/
    from urllib.parse import urlparse
    root_path = urlparse(mem.BASE_URL).path.rstrip("/") if mem.BASE_URL else ""

    uvicorn.run(web_app, host="0.0.0.0", port=8080, root_path=root_path)