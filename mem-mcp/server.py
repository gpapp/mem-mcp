"""
server.py  - Entry point for the Memory Vault.

Starts a unified FastAPI server that handles:
  • MCP server  → /mcp
  • Web GUI     → /gui
  • REST API    → /api

All services run on port 8080 by default.
"""

import os
import asyncio
from contextlib import suppress
import uvicorn
import memory as mem
import status_monitor

from mcp_tools import mcp
from gui import web_app, McpAuthGuard
from fastapi.middleware.cors import CORSMiddleware
import sessions as vault_sessions
from memory import SESSION_MAX_AGE
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

async def _session_gc_loop():
    """Drop expired sessions hourly, off the request path.

    A blocking sqlite DELETE inside an event-loop task would stall the server,
    which is the same mistake the reclassify path avoids by draining from a
    worker pool. One row per login makes this trivial work, so it runs in a
    thread rather than growing a pool.
    """
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(vault_sessions.purge_expired_sessions)
        except Exception as exc:
            print(f"Sessions: hourly purge failed: {exc}", flush=True)

# Auth for the MCP app itself.
#
# This used to be a SessionMiddleware and nothing else: nginx authenticated
# /mcp with auth_basic, so the app never checked a credential. Removing
# auth_basic from nginx_snippet.conf is what lets a PSK reach the app, and this
# guard is the app taking that job over. It sits *inside* the parent app's
# VaultSessionMiddleware (which is on web_app), so `scope["session"]` is already
# populated by the time it runs and a dashboard session is one of the three
# credentials it accepts.
mcp_app = mcp.http_app(transport="http", path="/mcp", middleware=[mcp_cors])
web_app.mount("/", McpAuthGuard(mcp_app))

# Ensure MCP lifespan is handled by the parent app
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    async with mcp_app.lifespan(mcp_app):
        from migrate_client_context import migrate_client_context, strip_scope_properties, restore_scope_links, llm_backfill_scope, sync_qdrant_scope
        from backup import BACKUP_ENABLED, scheduled_backup_loop
        # Expired sessions are dropped here and hourly after this. Without it
        # the table only ever grows, and the only other thing that deletes a
        # row is a request that happens to present the dead cookie.
        try:
            removed = vault_sessions.purge_expired_sessions()
            if removed:
                print(f"Sessions: dropped {removed} expired", flush=True)
        except Exception as exc:
            print(f"Sessions: could not purge expired sessions: {exc}", flush=True)
        await mem.ensure_ollama_models()
        await migrate_client_context()
        await sync_qdrant_scope()
        await strip_scope_properties()
        await restore_scope_links()
        await llm_backfill_scope()
        await mem.run_consistency_checks()
        await mem.run_diary_consistency_checks()
        await mem.fix_diary_entries()
        await mem.sync_orphans()
        # Daily savepoints run as their own task so a long export never blocks
        # the app from serving requests; the loop sleeps until the next slot.
        backup_task = None
        if BACKUP_ENABLED:
            backup_task = asyncio.create_task(scheduled_backup_loop())
        # Same reasoning: re-chunking the long records that predate chunking
        # costs one embedding call per chunk. Run after sync_orphans so the two
        # stores are already consistent, and off the lifespan so a large vault
        # does not delay serving. It is idempotent and bounded per boot, so a
        # restart picks up whatever is left.
        rechunk_task = asyncio.create_task(mem.rechunk_unindexed_records())
        # One poller for the whole process, feeding every connected browser via
        # SSE. It is here rather than in the GUI module so that the interval is
        # read from the same config the rest of the lifespan uses, and so the
        # task is cancelled with the others instead of outliving the app.
        status_task = asyncio.create_task(
            status_monitor.broadcast_loop(mem.fetch_ollama_status, mem.STATUS_POLL_SECONDS)
        )
        session_gc_task = asyncio.create_task(_session_gc_loop())
        try:
            yield
        finally:
            for task in (backup_task, rechunk_task, status_task, session_gc_task):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

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