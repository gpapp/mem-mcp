"""
server.py – Entry point for the Memory Vault.

Starts a unified FastAPI server that handles:
  • MCP server  → /mcp
  • Web GUI     → /gui
  • REST API    → /api

All services run on port 8080 by default.
"""

import uvicorn
import memory as mem
from mcp_tools import mcp
from gui import web_app

# ---------------------------------------------------------------------------
# Merge MCP into the Web GUI app
# ---------------------------------------------------------------------------
# This mounts the MCP HTTP/SSE server at the /mcp prefix of our main app.
# Port 8080 will now handle EVERYTHING (/gui, /api, and /mcp).
web_app.mount("/mcp", mcp.http_app)


if __name__ == "__main__":
    print(f"--- Memory Vault Unified Server starting ---")
    print(f"--- Base URL: {mem.BASE_URL or 'Relative'} ---")
    print(f"--- All services on port 8080: /gui, /api, /mcp ---")

    uvicorn.run(web_app, host="0.0.0.0", port=8080)