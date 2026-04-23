"""
mcp_tools.py – FastMCP tool definitions for the Memory Vault.

All business logic is delegated to memory.py (single source of truth).
User identity is extracted from the incoming HTTP request headers via
fastmcp's get_http_headers() – this preserves Basic-Auth / proxy-header
support from the original implementation.
"""

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from typing import Optional

import memory as mem

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP("Memory-Vault")


# ---------------------------------------------------------------------------
# Helper: resolve current user inside an MCP tool call
# ---------------------------------------------------------------------------
def _current_user() -> str:
    headers = get_http_headers()
    user = mem.extract_user_from_headers(headers)
    print(f"[MCP] Tool/Request for user: {user}")
    return user


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

@mcp.tool(name="mem_add_memory")
async def add_memory(text: str, category: str = "General"):
    """
    Save a new fact to the Memory Vault.
    Stores in both the vector database (Qdrant) and the knowledge graph (Neo4j).
    """
    try:
        doc_id = await mem.db_add_memory(text, category, _current_user())
        return f"Memory saved (id={doc_id}) under [{category.strip().capitalize()}]: {text}"
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(name="mem_update_memory")
async def update_memory(memory_id: str, text: str, category: str = "General"):
    """
    Update the text (and optionally category) of an existing memory.
    Re-embeds the new text so the vector index stays consistent.
    """
    try:
        found = await mem.db_update_memory(memory_id, text, category, _current_user())
        if not found:
            return f"Error: Memory {memory_id} not found or access denied."
        return f"Memory {memory_id} updated."
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(name="mem_forget_memory")
async def forget_memory(memory_id: str):
    """Permanently delete a memory from the vault (both vector and graph)."""
    try:
        await mem.db_delete_memory(memory_id, _current_user())
        return f"Memory {memory_id} forgotten."
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(name="mem_search_memories")
async def search_memories(query: str, limit: int = 5):
    """Search for relevant facts using vector similarity."""
    try:
        return await mem.db_search_memories(query, _current_user(), limit)
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(name="vault_save_fact")
async def save_smart_fact(fact: str, category: str):
    """
    Save a structured fact to the knowledge graph.
    Categories should be broad (e.g. 'Health', 'Career', 'Preferences', 'Projects').
    """
    try:
        category = category.strip().capitalize()
        doc_id = await mem.db_add_memory(fact, category, _current_user())
        return f"Fact archived (id={doc_id}) under [{category}]: {fact}"
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(name="vault_get_category_summary")
async def get_category_summary(category: str):
    """Retrieves all facts associated with a specific category hub."""
    try:
        user_id  = _current_user()
        category = category.strip().capitalize()
        all_mems = mem.db_list_memories(user_id)
        facts    = [
            f"({m['timestamp'][:10] if m['timestamp'] else '?'}) {m['text']}"
            for m in all_mems if m["category"] == category
        ]
        if not facts:
            return f"No facts found in the '{category}' category."
        return f"### {category} Knowledge\n" + "\n".join(facts)
    except RuntimeError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Diary tools
# ---------------------------------------------------------------------------

@mcp.tool(name="diary_save_entry")
async def save_diary_entry(content: str, date: Optional[str] = None):
    """
    Create or update a diary entry for a specific date (format: YYYY-MM-DD).
    Content should be in Markdown format. If date is omitted, today's date is used.
    """
    try:
        entry_date = await mem.db_save_diary(content, _current_user(), date)
        return f"Diary entry saved for {entry_date}."
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool(name="diary_search_entries")
async def search_diary_entries(query: str, limit: int = 3):
    """Search diary entries based on semantic similarity."""
    try:
        return await mem.db_search_diary(query, _current_user(), limit)
    except RuntimeError as e:
        return f"Error: {e}"
