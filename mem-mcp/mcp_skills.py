"""
mcp_skills.py – MCP Skill and Resource definitions.
"""
from fastmcp import FastMCP
import memory as mem

def register_skills(mcp: FastMCP):
    """Register skills and resources to the given FastMCP instance."""

    @mcp.resource("skill://process-transcription")
    def resource_skill_transcription() -> str:
        """The master workflow for processing meeting transcriptions into the Knowledge Graph."""
        import os
        path = os.path.join(os.path.dirname(__file__), "skills", "process-transcription.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @mcp.prompt("process-transcription")
    def prompt_process_transcription(transcription_text: str) -> str:
        """Instructions for processing a transcription using the dedicated skill workflow."""
        return f"""
Please process the following meeting transcription according to the 'process-transcription' skill guidelines.

CORE TASKS:
1. Extract Metadata (Date, Topic, Context).
2. Identify Participants and their Roles.
3. Extract Entities (People, Projects, Technologies, Decisions, Action Items).
4. Entity Resolution: Search for existing people/projects before creating new facts. Consider possible aliases.
5. Deduplication: Merge new information into existing facts using 'update_fact' instead of creating duplicates.
6. Ambiguity & Aliases: If a name is ambiguous or confidence is low, STOP and ask the user for clarification. Create an 'aliases' dictionary with confidences for mispronunciations or first names, and add it to the metadata and markdown text.
7. Save key facts and links.
8. Log the summary in the diary using 'diary_save_entry'.
9. PERFORMANCE: Batch multiple 'create_fact' and 'link_facts' calls into a single response for maximum efficiency.

TRANSCRIPTION CONTENT:
{transcription_text}
"""

    @mcp.resource("skill://memory-deduplication")
    def resource_skill_deduplication() -> str:
        """The workflow for identifying and merging duplicate entities in memory."""
        import os
        path = os.path.join(os.path.dirname(__file__), "skills", "memory-deduplication.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @mcp.prompt("memory-deduplication")
    def prompt_memory_deduplication(category: str = "People") -> str:
        """Instructions for performing deduplication on a specific category."""
        return f"""
Please help me deduplicate entries in the '{category}' category.

FOLLOW THIS WORKFLOW:
1. Run 'memory_find_duplicates' with category='{category}'.
2. For each cluster found, analyze the members.
3. Propose a 'Master' record and identify what information to merge from others.
4. IMPORTANT: Consolidate all unique data and links to the Master record BEFORE deleting duplicates.
5. PERFORMANCE: Feel free to call multiple tools (update, link, delete) in a single response once the plan is confirmed.
6. Execute the updates and deletions only after I confirm.

Be careful not to lose important context or relationships.
"""
