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

TIP: Use 'find_skills' to discover other related workflows like 'cleanup-transcription'.
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
2. For each cluster found, use 'memory_suggest_merge' to analyze the members and identify the 'Master' record.
3. Review the suggestion and use the 'memory_merge_facts' tool to perform the consolidation on the server.
4. PERFORMANCE: Using these specialized tools is much more efficient than manual logic.
5. Execute the merge only after I confirm.

Be careful not to lose important context or relationships.

TIP: Use 'find_skills' to see other data management workflows.
"""
    @mcp.resource("skill://cleanup-transcription")
    def resource_skill_cleanup() -> str:
        """The workflow for cleaning up transcriptions, identifying speakers, and summarizing."""
        import os
        path = os.path.join(os.path.dirname(__file__), "skills", "cleanup-transcription.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @mcp.prompt("cleanup-transcription")
    def prompt_cleanup_transcription(transcription_text: str, participants: Optional[str] = None) -> str:
        """Instructions for cleaning up a transcription and identifying speakers."""
        participant_info = f"PARTICIPANTS LIST: {participants}" if participants else "PARTICIPANTS LIST: Not provided. Please ask the user if you cannot identify them from memory."
        return f"""
Please clean up the following meeting transcription according to the 'cleanup-transcription' skill guidelines.

CORE TASKS:
1. Search Memory: Use 'search_facts' to find relevant projects, participants, and corrections.
2. Participant Verification: {participant_info}
3. Speaker Identification: Map generic speaker labels to actual names. Use direct addressing or deductive guessing.
4. Transcription Cleanup: Remove filler words and fix errors using stored corrections.
5. Produce Output: Provide a Cleaned Transcript followed by a Summary of Main Points.
TRANSCRIPTION CONTENT:
{transcription_text}

TIP: Use 'get_skill_workflow' to read the full 'cleanup-transcription' guidelines if needed.
"""
