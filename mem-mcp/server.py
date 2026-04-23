"""
server.py – Entry point for the Memory Vault.

Starts two servers:
  • MCP server  → port 8000  (FastMCP over HTTP, transport="http")
  • Web GUI     → port 8080  (FastAPI / Uvicorn with REST API + dashboard)

Both share the same database clients initialised once on startup via the
memory module's async initialize_databases().
"""

import asyncio
import threading
import uvicorn

import memory as mem
from mcp_tools import mcp
from gui import web_app


# ---------------------------------------------------------------------------
# MCP server runner (blocking – runs in its own thread)
# ---------------------------------------------------------------------------
def run_mcp():
    mcp.run(transport="http", host="0.0.0.0", port=8000)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Initialise DB connections once (synchronous bootstrap before the event
    # loop is handed to uvicorn).
    asyncio.run(mem.initialize_databases())

    # Start MCP in a background daemon thread
    mcp_thread = threading.Thread(target=run_mcp, daemon=True)
    mcp_thread.start()

    print("--- MCP server starting on http://0.0.0.0:8000 ---")
    print("--- Web GUI starting on http://0.0.0.0:8080/gui ---")

    uvicorn.run(web_app, host="0.0.0.0", port=8080)