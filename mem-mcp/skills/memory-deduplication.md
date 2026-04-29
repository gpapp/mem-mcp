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
Use the `suggest_merge` tool for each cluster to get a structured comparison.
- Pass the cluster JSON to the tool.
- The tool returns all records sorted by completeness (field count + text length) with a `suggested_master_id`.
- **Your job**: Review the records, confirm or override the suggested master, and identify which details from the other records must be preserved in the consolidated text.

### 3. Smart Merge (client-side consolidation)
Execute the consolidation in three steps:

**Step A** — Fetch texts for consolidation:
Call `merge_facts(masterId=<master_id>, duplicateIds=[...], smart=True)`.
- The tool returns the full text and relationships of every record in the cluster.
- Do NOT skip this — you need the graph relationship data.

**Step B** — Write the consolidated text:
Using the returned records, write a single comprehensive Markdown text that:
1. Preserves EVERY unique fact, name, date, decision, role, and technical detail from ALL records.
2. Does NOT generalize or drop granular specifics.
3. Uses sections/bullets if the entity has multiple distinct topics.
4. Incorporates any listed graph relationships into the narrative.

**Step C** — Apply and complete:
1. `update_fact(memoryId=<master_id>, text=<consolidated_text>)` — update the master with your consolidated text.
2. `merge_facts(masterId=<master_id>, duplicateIds=[...], smart=False)` — move all graph relationships from duplicates to master and delete the duplicate nodes.

### 4. Simple Merge (no text consolidation needed)
For simple facts where the records are nearly identical and no LLM consolidation is needed:
- Call `merge_facts(masterId=<master_id>, duplicateIds=[...], smart=False)` directly.

### 5. Verification
After a merge, verify the results:
- Use `get_fact_neighborhood` on the Master ID to see the consolidated graph.
- If the text needs further refinement, use `update_fact`.

## Efficiency: Multi-Tool Execution
You are encouraged to call multiple tools in a single response. For example, you can call `find_duplicates` and then process multiple clusters with `suggest_merge` in the same turn, then handle the merges cluster by cluster.

## Examples of "Duplicate" Patterns
- **People**: "Kate" vs "Katarina" (same role/company).
- **Projects**: "Project Phoenix" vs "Phoenix AGI Research".
- **Concepts**: "Vector DB" vs "Vector Databases".

## Tips
- **Human-in-the-Loop**: If a merge seems risky or data might be lost, STOP and ask the user for confirmation.
- **Relationship Safety**: The graph merge (`smart=False`) automatically prevents duplicate edges. Do NOT manually re-link after a merge.
