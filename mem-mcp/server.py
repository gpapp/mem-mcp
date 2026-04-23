"""
server.py – Entry point for the Memory Vault.

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

# ---------------------------------------------------------------------------
# Merge MCP into the Web GUI app
# ---------------------------------------------------------------------------
# We call .http_app() to get the actual ASGI application.
# We also integrate the MCP lifespan with our main app.
mcp_app = mcp.http_app()
web_app.mount("/mcp", mcp_app)

# Ensure MCP lifespan is handled by the parent app
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    async with mcp_app.lifespan(mcp_app):
        yield

web_app.router.lifespan_context = lifespan


if __name__ == "__main__":
    print(f"--- Memory Vault Unified Server starting ---")
    print(f"--- Base URL: {mem.BASE_URL or 'Relative'} ---")
    print(f"--- All services on port 8080: /gui, /api, /mcp ---")

    uvicorn.run(web_app, host="0.0.0.0", port=8080)