"""
mcp_tools.py – FastMCP tool definitions for the Memory Vault.

All business logic is delegated to memory.py (single source of truth).
User identity is extracted from the incoming HTTP request headers via
fastmcp's get_http_headers() – this preserves Basic-Auth / proxy-header
support from the original implementation.
"""

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from typing import Optional, List

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
# Fact Management Tools (Advanced Schema)
# ---------------------------------------------------------------------------

@mcp.tool(name="create_fact")
async def create_fact(text: str, category: str, metadata: Optional[dict] = None):
    """
    Store a new fact in the knowledge base. 
    Facts are the fundamental unit of knowledge - can represent people, discussions, 
    concepts, principles, or any other knowledge entity.
    """
    try:
        doc_id = await mem.db_add_memory(text, category, _current_user(), metadata)
        return f"Fact created with ID: {doc_id}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="search_facts")
async def search_facts(query: str, category: Optional[str] = None, limit: int = 10):
    """
    Search for facts matching query criteria. 
    Uses semantic similarity for text queries and exact matching for category filters.
    """
    try:
        return await mem.db_search_memories(query, _current_user(), limit, category)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="link_facts")
async def link_facts(sourceFactId: str, targetFactId: str, relationshipType: str, metadata: Optional[dict] = None):
    """
    Create a relationship/association between two facts in the knowledge graph.
    Example relationship types: 'related_to', 'works_on', 'part_of', 'depends_on'.
    """
    try:
        await mem.db_link_facts(sourceFactId, targetFactId, relationshipType, metadata or {}, _current_user())
        return f"Link created: ({sourceFactId}) -[{relationshipType}]-> ({targetFactId})"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="get_fact_neighborhood")
async def get_fact_neighborhood(factId: str, depth: int = 1, relationshipTypes: Optional[List[str]] = None):
    """
    Get all facts related to a specific fact up to N degrees of separation. 
    Useful for exploring context around a specific entity.
    """
    try:
        return mem.db_get_neighborhood(factId, depth, relationshipTypes or [], _current_user())
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="update_fact")
async def update_fact(factId: str, text: Optional[str] = None, category: Optional[str] = None, metadata: Optional[dict] = None):
    """
    Modify an existing fact's text, category, or metadata. Supports partial updates.
    """
    try:
        found = await mem.db_update_memory(factId, text, category, _current_user(), metadata)
        if not found:
            return f"Error: Fact {factId} not found."
        return f"Fact {factId} updated."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="delete_fact")
async def delete_fact(factId: str, hardDelete: bool = False):
    """
    Remove a fact from the knowledge base.
    """
    try:
        # Note: We currently only support hard delete in the backend
        await mem.db_delete_memory(factId, _current_user())
        return f"Fact {factId} deleted."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="find_patterns")
async def find_patterns():
    """
    Discover recurring patterns across facts. 
    Identifies themes or categories that appear together frequently.
    """
    try:
        return mem.db_find_patterns(_current_user())
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="switch_context")
async def switch_context(clientId: str, projectId: Optional[str] = None):
    """
    Change the active client/project context. 
    Note: In this implementation, context is typically derived from proxy headers.
    """
    return f"Context switched to Client: {clientId}, Project: {projectId or 'Default'}"


# ---------------------------------------------------------------------------
# Diary tools (Preserved for GUI consistency)
# ---------------------------------------------------------------------------

@mcp.tool(name="diary_save_entry")
async def save_diary_entry(content: str, date: Optional[str] = None):
    """Create or update a diary entry for a specific date (YYYY-MM-DD)."""
    try:
        entry_date = await mem.db_save_diary(content, _current_user(), date)
        return f"Diary entry saved for {entry_date}."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(name="diary_search_entries")
async def search_diary_entries(query: str, limit: int = 3):
    """Search diary entries based on semantic similarity."""
    try:
        return await mem.db_search_diary(query, _current_user(), limit)
    except Exception as e:
        return f"Error: {e}"
