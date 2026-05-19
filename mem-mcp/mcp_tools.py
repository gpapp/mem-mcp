import logging
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from typing import Optional, List
import memory as mem

logger = logging.getLogger("memory-vault")

# Initialize FastMCP with the built-in sampling fallback behavior
mcp = FastMCP(
    "MemoryVault",
)

def _current_user() -> str:
    """
    Internal helper to extract the user from the current request headers.
    Used to identify the user vault without exposing user_id to the LLM.
    """
    headers = get_http_headers()
    return mem.extract_user_from_headers(headers)

def _format_fact_md(fact: dict) -> str:
    """
    Render a fact dict as a Markdown block suitable for LLM display.

    Expected keys: id, name, text, category, score (optional), metadata (optional).
    """
    name    = fact.get("name") or ""
    text     = fact.get("text") or ""
    category = fact.get("category") or ""
    fact_id  = fact.get("id") or ""
    score    = fact.get("score")
    metadata = fact.get("metadata") or {}

    lines = []

    # Heading
    if name:
        lines.append(f"### {name}")
    else:
        lines.append(f"### (unnamed)")

    # Body
    if text:
        lines.append("")
        lines.append(text)

    # Meta row
    meta_parts = []
    if category:
        meta_parts.append(f"📂 **{category}**")
    if score is not None:
        meta_parts.append(f"🎯 score: `{score:.3f}`")
    if fact_id:
        meta_parts.append(f"🔑 `{fact_id}`")
    if meta_parts:
        lines.append("")
        lines.append(" · ".join(meta_parts))

    # Optional metadata fields (skip noisy internal keys)
    _skip = {"userId", "updatedAt", "timestamp"}
    extra = {k: v for k, v in metadata.items() if k not in _skip and v not in (None, "", {}, [])}
    if extra:
        lines.append("")
        for k, v in extra.items():
            lines.append(f"- **{k}**: {v}")

    return "\n".join(lines)


def _format_facts_md(facts: list) -> str:
    """Render a list of fact dicts as a combined Markdown document."""
    if not facts:
        return "_No facts found._"
    blocks = [_format_fact_md(f) for f in facts]
    return "\n\n---\n\n".join(blocks)

@mcp.tool()
async def add_fact(name: str, text: str, category: str):
    """
    Save a new fact or memory to the knowledge graph.
    'name' should be a concise header for the fact.
    'text' should be the detailed content of the fact.
    'category' should be one of: People, Technology, Client, Project, Event, Tool.
    """
    memory_id = await mem.db_add_memory(text, category, _current_user(), name=name)
    return f"Successfully added memory with ID: {memory_id}"

@mcp.tool()
async def search_facts(query: str, category: Optional[str] = None, limit: int = 5, top_p: float = 0.7, names_only: bool = False):
    """
    Search for facts matching query criteria.
    Returns results formatted as Markdown for easy reading.
    - query: semantic search string
    - category: optional filter (e.g. 'People', 'Client', 'Preferences') to limit output
    - limit: maximum number of results (default 5)
    - top_p: threshold to filter low-probability results (default 0.75, higher = more selective)
    - names_only: if True, returns only fact names as a newline-separated list
    """
    facts = await mem.db_search_memories(query, _current_user(), limit, category, top_p)
    if names_only:
        return "\n".join(f.get("name", "") or f.get("text", "")[:50] for f in facts if f.get("name"))
    return _format_facts_md(facts)

@mcp.tool()
async def list_categories():
    """List all distinct categories currently used in the memory vault."""
    return mem.db_list_categories(_current_user())

@mcp.tool()
async def link_facts(sourceFactId: str, targetFactId: str, relationshipType: str, metadata: Optional[dict] = None):
    """Create a bidirectional relationship between two facts (two-way link)."""
    await mem.db_link_facts(sourceFactId, targetFactId, relationshipType, metadata or {}, _current_user())
    return f"Linked: {sourceFactId} <-> {targetFactId}"

@mcp.tool()
async def unlink_facts(sourceFactId: str, targetFactId: str, relationshipType: Optional[str] = None):
    """Remove a bidirectional relationship between two facts."""
    await mem.db_unlink_facts(sourceFactId, targetFactId, relationshipType, _current_user())
    return f"Unlinked: {sourceFactId} <-> {targetFactId}"

@mcp.tool()
async def get_fact_neighborhood(factId: str, depth: int = 1, relationshipTypes: Optional[List[str]] = None):
    """
    Explore context around a fact.
    Returns neighboring facts formatted as Markdown.
    """
    neighbors = mem.db_get_neighborhood(factId, depth, relationshipTypes or [], _current_user())
    if not neighbors:
        return "_No connected facts found._"
    return _format_facts_md(neighbors)

@mcp.tool()
async def update_fact(memoryId: str, name: Optional[str] = None, text: Optional[str] = None, category: Optional[str] = None):
    """
    Update an existing memory by ID. Provide only the fields that need updating.
    """
    success = await mem.db_update_memory(memoryId, name, text, category, _current_user())
    if success:
        return f"Successfully updated memory {memoryId}"
    return f"Error: Memory {memoryId} not found or unauthorized."

@mcp.tool()
async def delete_fact(factId: str):
    """Delete a fact."""
    await mem.db_delete_memory(factId, _current_user())
    return f"Fact {factId} deleted"

@mcp.tool()
async def find_patterns():
    """
    Discover recurring themes across the knowledge graph.
    Returns a Markdown-formatted list of patterns.
    """
    patterns = mem.db_find_patterns(_current_user())
    if not patterns:
        return "_No patterns found yet._"
    lines = ["## Recurring Themes\n"]
    for p in patterns:
        lines.append(f"- **{p['pattern']}** — strength: `{p['strength']}`")
    return "\n".join(lines)

@mcp.tool()
async def diary_save_entry(content: str, name: str, timestamp: str, entryId: Optional[str] = None):
    """Save or update a diary entry.

    - name: Concise name for the entry.
    - timestamp: ISO-8601 datetime string including time (e.g. '2026-05-15T14:30:00').
      Passing the same timestamp a second time **replaces** the existing entry.
    - entryId: Optional ID of an existing entry. Use this if you are changing the 
      timestamp of an existing entry to ensure the old one is moved/deleted.
    - Returns a dict with 'id' (use this to update or delete the entry later) and 
      'timestamp' (the ISO string that keys the entry).
    """
    user = _current_user()
    if entryId:
        new_id = mem._diary_id(user, timestamp)
        if entryId != new_id:
            await mem.db_delete_diary(entryId, user)
    entry_ts = await mem.db_save_diary(content, user, timestamp, name)
    entry_id = mem._diary_id(user, entry_ts)
    return {"id": entry_id, "timestamp": entry_ts}

@mcp.tool()
async def diary_search_entries(query: str, limit: int = 3, top_p: float = 0.4):
    """Search diary entries using vector similarity or date/time filters.

    Query supports:
    - Natural language: "May 2026", "May 15 2026", "15 May 2026"
    - ISO formats: "2026-05", "2026-05-15", "2026-05-15 14:30"
    - Month names: "May", "May 2026", "May 15"
    - Year only: "2026"

    Returns a list of matching entries. Each entry contains:
    - id: use this with diary_delete_entry or diary_save_entry (as timestamp key) to update/delete
    - timestamp: ISO-8601 datetime string (e.g. '2026-05-15T14:30:00') — the time the entry was saved
    - date: YYYY-MM-DD portion of the timestamp
    - content: the full Markdown text of the entry
    - score: similarity score (higher = more relevant)
    - mentions: list of facts linked to this entry
    """
    return await mem.db_search_diary(query, _current_user(), limit, top_p)

@mcp.tool()
async def list_diary_entries(fromTs: Optional[str] = None, toTs: Optional[str] = None):
    """
    List diary entry names with timestamps within a time range.
    - fromTs: Start timestamp (ISO-8601). Defaults to 30 days ago.
    - toTs: End timestamp (ISO-8601). Defaults to now.
    Returns a list of (id, timestamp, name) tuples.
    """
    return mem.db_list_diary_entries(_current_user(), fromTs, toTs)

@mcp.tool()
async def diary_delete_entry(entryId: str):
    """Delete a diary entry by its ID.

    - entryId: the 'id' field returned by diary_save_entry or diary_search_entries.
    - Returns a confirmation message, or an error if the entry was not found.
    """
    deleted = await mem.db_delete_diary(entryId, _current_user())
    if not deleted:
        return f"Entry '{entryId}' not found or does not belong to the current user."
    return f"Deleted diary entry {entryId}"
    
@mcp.tool()
async def find_duplicates(category: str = "People", limit: int = 50, threshold: float = 0.6, max_cluster: int = 4, group_by: Optional[str] = "first_name"):
    """
    Find potential duplicate entries in memory by comparing embeddings similarity ranking.
    Returns grouped clusters of similar items for manual deduplication.
    - category: category to search (default 'People')
    - limit: max items to fetch (default 50)
    - threshold: starting similarity threshold (default 0.6). If clusters exceed max_cluster, threshold is increased iteratively.
    - max_cluster: max items per cluster (default 4). Large clusters are split by increasing threshold.
    """
    try:
        return await mem.db_find_duplicates(_current_user(), category, limit, threshold, max_cluster)
    except Exception as e:
        logger.exception(f"Error in find_duplicates: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def merge_facts(masterId: str, duplicateIds: List[str], mergedName: str, mergedText: str):
    """
    Merge duplicate facts into a single master record.

    The caller is responsible for consolidating content before calling this tool:
    use suggest_merge to retrieve all records, write a comprehensive mergedName
    and mergedText that preserves every detail from every record, then call this.

    This tool:
    1. Updates the master record with the provided mergedName and mergedText.
    2. Moves all graph relationships from duplicates to the master.
    3. Deletes the duplicate nodes.
    """
    user = _current_user()
    await mem.db_update_memory(masterId, mergedName, mergedText, None, user)
    await mem.db_merge_memories(masterId, duplicateIds, user)
    return f"Successfully merged {len(duplicateIds)} facts into {masterId}"

@mcp.tool()
async def suggest_merge(cluster_json: str):
    """
    Analyze a cluster of potential duplicates and return a structured comparison
    for the client to evaluate. The client decides which record is the master
    and what to merge, then calls merge_facts to execute.

    cluster_json may be:
      - A JSON array of full record dicts (from find_duplicates output), OR
      - A JSON array of string IDs — records will be fetched automatically.
    """
    import json

    try:
        records = json.loads(cluster_json)
    except Exception as e:
        return f"Error parsing cluster JSON: {e}"

    if not isinstance(records, list) or not records:
        return "Empty or invalid cluster data."

    # If items are plain strings, treat them as IDs and fetch full records.
    if records and isinstance(records[0], str):
        user = _current_user()
        fetched = []
        for rid in records:
            result = mem.db_get_fact_by_id(rid, user)
            if result:
                fetched.append(result)
            else:
                # Fallback to search if not found by direct ID (might be in Qdrant but not Neo4j)
                search_res = await mem.db_search_memories(rid, user, limit=1, top_p=0.0)
                if search_res and isinstance(search_res, list):
                    fetched.append(search_res[0])
        records = fetched

    analyzed = []
    for r in records:
        if not isinstance(r, dict):
            logger.warning(f"suggest_merge: skipping non-dict record: {r!r}")
            continue
        text = r.get("text") or ""
        non_empty = sum(1 for v in r.values() if v not in (None, "", []))
        analyzed.append({
            "id": r.get("id"),
            "name": r.get("name", ""),
            "text": text,
            "date": r.get("date") or r.get("updatedAt") or "",
            "extra_fields": {k: v for k, v in r.items() if k not in ("id", "name", "text", "date", "updatedAt", "similarity")},
            "_completeness": {"text_length": len(text), "non_empty_fields": non_empty},
        })

    analyzed.sort(key=lambda x: (x["_completeness"]["non_empty_fields"], x["_completeness"]["text_length"]), reverse=True)
    top = analyzed[0]

    return {
        "record_count": len(analyzed),
        "records_by_completeness": analyzed,
        "suggested_master_id": top["id"],
        "suggested_master_name": top["name"],
        "note": (
            "Records are sorted by completeness (field count, then text length). "
            "Review all records, confirm or override the suggested master, then call merge_facts."
        ),
    }

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
