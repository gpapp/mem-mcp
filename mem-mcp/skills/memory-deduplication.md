# Skill: Memory Deduplication

This skill provides a systematic workflow for identifying and merging duplicate entries in the Memory Vault. Duplicate entries often occur when information is captured from different sources (e.g., meeting transcriptions, manual notes, diary entries) with slightly different phrasing or levels of detail.

## Objective
Maintain a clean, high-quality knowledge base by merging similar entities into a single, authoritative "Master" record.

## Workflow

### 1. Identify Potential Duplicates
Use the `find_duplicates` tool to scan the memory for clusters of similar items.
- **Category**: Defaults to "People", but can be used for any category.
- **Threshold**: Adjust the similarity threshold (default 0.75).
- **Result**: You will receive a list of clusters with an `avg_similarity` score and a basic recommendation.

### 2. Analyze Clusters and Select Master
Use the `suggest_merge` tool to analyze a specific cluster and get a recommendation for the **Master** record.
- Pass the cluster JSON to the tool.
- The tool uses LLM reasoning to determine which record is the most complete and accurate.
- It identifies specific details from other records that should be preserved.

### 3. Perform Smart Merge
Execute the consolidation using the `merge_facts` tool with `smart=True`.
- **masterId**: The ID of the record you want to keep.
- **duplicateIds**: A list of IDs to merge into the master.
- **smart**: Always set to `True` for complex entities like People or Projects.
- **Outcome**: This tool automatically:
    1. Consolidates all text descriptions into a single cohesive Markdown summary.
    2. Safely migrates all missing graph relationships from duplicates to the Master (preventing duplicate edges natively). Do NOT manually link them afterwards.
    3. Merges node properties (tags, aliases, etc.).
    4. Deletes the duplicate records.

### 4. Verification
After a merge, verify the results:
- Use `get_fact_neighborhood` on the Master ID to see the new consolidated graph.
- If the text needs further refinement, use `update_fact`.

## Efficiency: Multi-Tool Execution
You are encouraged to call multiple tools in a single response. For example, you can call `find_duplicates` and then process multiple clusters with `suggest_merge` and `merge_facts` in subsequent turns.

## Examples of "Duplicate" Patterns
- **People**: "Kate" vs "Katarina" (same role/company).
- **Projects**: "Project Phoenix" vs "Phoenix AGI Research".
- **Concepts**: "Vector DB" vs "Vector Databases".

## Tips
- **Human-in-the-Loop**: If a merge seems risky or data might be lost, STOP and ask the user for confirmation.
- **Manual Merge**: For simple facts where you don't need LLM consolidation, you can use `merge_facts` with `smart=False`.
