"""
mcp_skills.py – MCP Skill and Resource definitions.
"""
from fastmcp import FastMCP
from typing import Optional
import memory as mem
import skill_catalogue


def _skill_reader(path: str, name: str, description: str):
    """Build the zero-argument reader one `skill://<name>` resource serves.

    A factory rather than a closure written inside the registration loop: a
    loop body closes over the variable, so every URI would end up serving the
    last skill's text. The default-argument trick would work too, but it makes
    the trap invisible to the next reader, and this one names it.
    """
    def read() -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    read.__name__ = "resource_skill_" + name.replace("-", "_")
    read.__doc__ = description
    return read


def register_skills(mcp: FastMCP):
    """Register skills and resources to the given FastMCP instance."""

    @mcp.prompt("find-skills")
    def prompt_find_skills() -> str:
        """Instructions for discovering and using available skill workflows."""
        return """
You are a multi-skilled assistant. To handle complex tasks, you should:
1. Use 'find_skills' to see the list of available specialized workflows, each with a description of when it applies.
2. Use 'get_skill_workflow' with the name of a skill to read its full documentation.
3. Follow the instructions in the skill documentation to complete the user's request.
"""

    # Every skill becomes a resource, derived from the directory rather than
    # hand-registered. `process-directory` had no URI at all while the
    # process-transcription prompt told the reader to load it, so the one path
    # a client was pointed at was the one path it could not address. Adding a
    # `.md` file is now all a new skill needs, which is what the tool
    # documentation has claimed all along.
    for skill in skill_catalogue.list_skills():
        mcp.resource(f"skill://{skill['name']}")(
            _skill_reader(skill["path"], skill["name"], skill["description"])
        )

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

    @mcp.prompt("process-directory")
    def prompt_process_directory(directory_path: str = "") -> str:
        """Instructions for processing a directory of transcripts in one pass."""
        target = directory_path.strip() or "(none passed — ask for the path)"
        return f"""
Please process the transcripts in this directory according to the 'process-directory' skill.

DIRECTORY: {target}

1. Load the skill first: get_skill_workflow("process-directory") or resource
   skill://process-directory. It is the single source of truth for the batch
   workflow: the dry-run inventory, the slot-based ledger that makes a re-run
   idempotent, per-file error isolation, the one consolidated question round,
   anchor-first writes, and the closing status report.
2. Do NOT process files one prompt at a time. The skill drives one batch and
   reports per-file outcomes at the end.
3. Per file, follow 'process-transcription' — that skill remains the authority
   on timestamp resolution, transcript formats, smalltalk suppression and the
   diary shape. The directory skill only sequences it.
4. Ask the one consolidated question round before any write, and never let a
   single failing file abort the batch: record it and continue.
"""
