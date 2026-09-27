"""
mcp_skills.py – MCP Skill and Resource definitions.
"""
from fastmcp import FastMCP
from typing import Optional
import memory as mem

def register_skills(mcp: FastMCP):
    """Register skills and resources to the given FastMCP instance."""

    @mcp.prompt("find-skills")
    def prompt_find_skills() -> str:
        """Instructions for discovering and using available skill workflows."""
        return """
You are a multi-skilled assistant. To handle complex tasks, you should:
1. Use 'find_skills' to see the list of available specialized workflows.
2. Use 'get_skill_workflow' with the name of a skill to read its full documentation.
3. Follow the instructions in the skill documentation to complete the user's request.
"""

    @mcp.resource("skill://process-transcription")
    def resource_skill_transcription() -> str:
        """The master workflow for processing meeting transcriptions into the Knowledge Graph."""
        import os
        path = os.path.join(os.path.dirname(__file__), "skills", "process-transcription.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @mcp.prompt("process-transcription")
    def prompt_process_transcription(transcription_text: str = "") -> str:
        """Instructions for processing a transcription using the dedicated skill workflow.

        Defers to skills/process-transcription.md — the skill file is the single
        source of truth for the workflow (timestamp ladder, reprocessing, format,
        delegation rules). Passing a whole transcript as a prompt argument is
        token-expensive; prefer reading the file locally and delegating extraction
        to a subagent per the skill's contract.
        """
        body = transcription_text.strip()
        return """
Please process the meeting transcription according to the 'process-transcription' skill.

1. Load the skill first: get_skill_workflow("process-transcription") (or resource skill://process-transcription) and follow it exactly — it is the single source of truth for timestamp resolution, transcript formats, smalltalk suppression, reprocessing rules, the diary format (## Participants, ## Context, ## Description, ## Decisions, ## Actions, ## Open Questions, ## Notes, ## Keywords), and the pre/post-save checklists.
2. For a directory of transcripts, load skill "process-directory" instead and follow it.
3. Run extraction in a subagent that returns only the digest — never bring the raw transcript into the main context (prefer reading the file over passing content here).
4. Wait for the human gate (one consolidated question call) before any writes.

TRANSCRIPTION CONTENT (prefer reading the file over passing content here):
""" + (body if body else "(none passed — read the transcript file locally)")

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
1. Run 'find_duplicates' with category='{category}'.
2. For each cluster found, use 'suggest_merge' to analyze the members and identify the 'Master' record.
3. Review the suggestion and use the 'merge_facts' tool to perform the consolidation on the server.
4. PERFORMANCE: Using these specialized tools is much more efficient than manual logic.
5. Execute the merge only after I confirm.

Be careful not to lose important context or relationships.
"""
