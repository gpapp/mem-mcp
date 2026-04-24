import os
import logging
from fastmcp import FastMCP, Context
from fastmcp.server.dependencies import get_http_headers
from typing import Optional, List, Any
import memory as mem

logger = logging.getLogger("memory-vault")

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
    
@mcp.tool()
async def memory_find_duplicates(category: str = "People", limit: int = 50, threshold: float = 0.75, group_by: Optional[str] = "first_name"):
    """
    Find potential duplicate entries in memory by comparing embeddings similarity ranking.
    Returns grouped clusters of similar items for manual deduplication.
    """
    return await mem.db_find_duplicates(_current_user(), category, limit, threshold)

@mcp.tool()
async def memory_merge_facts(masterId: str, duplicateIds: List[str], smart: bool = False):
    """
    Merge multiple duplicate facts into a single master fact on the server.
    If 'smart' is True, uses an LLM to consolidate the text descriptions into a cohesive whole.
    This moves all relationships and combines metadata.
    """
    if smart:
        await mem.db_smart_merge_memories(masterId, duplicateIds, _current_user())
    else:
        await mem.db_merge_memories(masterId, duplicateIds, _current_user())
    return f"Successfully merged {len(duplicateIds)} facts into {masterId} (Smart Merge: {smart})"

@mcp.tool()
async def transcription_cleanup(text: str, participants: Optional[List[str]] = None, ctx: Context = None):
    """
    Clean up a raw transcription, identify speakers, and remove filler words on the server.
    Uses local LLM for short contexts and can use sampling for longer ones.
    """
    prompt = f"""
    Please clean up this raw transcription. 
    Participants: {participants if participants else 'Unknown (identify from context)'}
    
    TRANSCRIPTION:
    {text}
    
    OUTPUT FORMAT:
    Return only the cleaned transcript with [Speaker Name]: labels.
    """
    system = "You are a professional transcriptionist. Fix speaker turns, remove filler words (um, uh, like), and correct obvious transcription errors."

    # Use sampling if context is available and text is moderately long (> 1000 chars)
    if ctx and len(text) > 1000:
        try:
            # We sample from the calling LLM for high-quality / long context
            resp = await ctx.create_message(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            )
            if resp and resp.content:
                return resp.content[0].text
        except Exception as e:
            # Log the error (this will show in server logs)
            logger.warning(f"MCP Sampling failed, falling back to local LLM: {e}")

    # Guard against huge texts hitting local Ollama if sampling failed or is missing
    if len(text) > 10000 and not ctx:
         return "Error: Transcription too large (>10k chars) for local processing and MCP sampling is unavailable in this client."

    # Default to local Ollama
    return await mem.get_llm_completion(prompt, system)

@mcp.tool()
async def memory_suggest_merge(cluster_json: str, ctx: Context = None):
    """
    Analyze a cluster of potential duplicates and suggest a Master record and merge strategy.
    Uses LLM reasoning to evaluate which record is most complete.
    """
    prompt = f"""
    Analyze these potential duplicate memories and suggest which one should be the 'Master' record.
    Explain why and what information from other records should be merged into it.
    
    CLUSTER DATA:
    {cluster_json}
    """
    system = "You are a data deduplication expert. Identify the most complete and accurate record in a cluster."

    # Use local Ollama for this evaluation
    return await mem.get_llm_completion(prompt, system)

@mcp.tool()
async def find_skills():
    """
    Scan the skills/ directory and list all available skill workflows.
    Use this to discover new capabilities without a server restart.
    """
    import os
    skills_dir = os.path.join(os.path.dirname(__file__), "skills")
    if not os.path.exists(skills_dir):
        return []
    return [f[:-3] for f in os.listdir(skills_dir) if f.endswith(".md")]

@mcp.tool()
async def get_skill_workflow(skillName: str):
    """
    Retrieve the detailed markdown workflow for a specific skill.
    Allows the LLM to understand and execute complex workflows stored as documentation.
    """
    import os
    path = os.path.join(os.path.dirname(__file__), "skills", f"{skillName}.md")
    if not os.path.exists(path):
        return f"Skill '{skillName}' not found."
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Skills & Resources
# ---------------------------------------------------------------------------
from mcp_skills import register_skills
register_skills(mcp)
