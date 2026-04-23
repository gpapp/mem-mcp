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
4. Save key facts using 'create_fact' and 'link_facts'.
5. Log the summary in the diary using 'diary_save_entry'.

TRANSCRIPTION CONTENT:
{transcription_text}
"""
