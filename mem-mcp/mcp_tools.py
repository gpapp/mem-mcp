"""
mcp_tools.py – FastMCP tool definitions for the Memory Vault.
"""
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from typing import Optional, List
import memory as mem

# Using a safe name and explicitly disabling any potential conflicting features
mcp = FastMCP("MemoryVault")

def _current_user() -> str:
    headers = get_http_headers()
    user = mem.extract_user_from_headers(headers)
    return user

@mcp.tool()
async def create_fact(text: str, category: str, metadata: Optional[dict] = None):
    """Store a new fact in the knowledge base."""
    doc_id = await mem.db_add_memory(text, category, _current_user(), metadata)
    return f"Fact created with ID: {doc_id}"

@mcp.tool()
async def search_facts(query: str, category: Optional[str] = None, limit: int = 10):
    """Search for facts matching query criteria."""
    return await mem.db_search_memories(query, _current_user(), limit, category)

@mcp.tool()
async def link_facts(sourceFactId: str, targetFactId: str, relationshipType: str, metadata: Optional[dict] = None):
    """Create a relationship between two facts."""
    await mem.db_link_facts(sourceFactId, targetFactId, relationshipType, metadata or {}, _current_user())
    return f"Link created: {sourceFactId} -> {targetFactId}"

@mcp.tool()
async def get_fact_neighborhood(factId: str, depth: int = 1, relationshipTypes: Optional[List[str]] = None):
    """Explore context around a fact."""
    return mem.db_get_neighborhood(factId, depth, relationshipTypes or [], _current_user())

@mcp.tool()
async def update_fact(factId: str, text: Optional[str] = None, category: Optional[str] = None, metadata: Optional[dict] = None):
    """Update an existing fact."""
    found = await mem.db_update_memory(factId, text, category, _current_user(), metadata)
    return f"Fact {factId} updated" if found else f"Error: {factId} not found"

@mcp.tool()
async def delete_fact(factId: str):
    """Delete a fact."""
    await mem.db_delete_memory(factId, _current_user())
    return f"Fact {factId} deleted"

@mcp.tool()
async def find_patterns():
    """Discover recurring themes."""
    return mem.db_find_patterns(_current_user())

@mcp.tool()
async def diary_save_entry(content: str, date: Optional[str] = None):
    """Save a diary entry."""
    entry_date = await mem.db_save_diary(content, _current_user(), date)
    return f"Saved for {entry_date}"

@mcp.tool()
async def diary_search_entries(query: str, limit: int = 3):
    """Search diary entries."""
    return await mem.db_search_diary(query, _current_user(), limit)

# ---------------------------------------------------------------------------
# Skills & Resources
# ---------------------------------------------------------------------------
from mcp_skills import register_skills
register_skills(mcp)
