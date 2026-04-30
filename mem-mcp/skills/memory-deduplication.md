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

### 2. Analyze Each Cluster
Use the `suggest_merge` tool for each cluster.
- The tool returns all records sorted by completeness with a `suggested_master_id`.
- **Your job**: Read every record's `text` and `extra_fields`. Decide which ID should be the master (confirm or override the suggestion). Then write a single consolidated title and text that preserves **every** unique fact, name, date, decision, and technical detail from all records.

**Consolidation rules:**
1. Preserve every unique fact — do NOT generalize or drop granular specifics.
2. Use sections/bullets if the entity has multiple distinct topics.
3. Incorporate any listed graph relationships into the narrative.
4. Resolve overlapping facts without losing nuance.

### 3. Execute the Merge
Call `merge_facts` with the master ID, duplicate IDs, and your consolidated content:

```
merge_facts(
    masterId = <chosen master ID>,
    duplicateIds = [<all other IDs in the cluster>],
    mergedTitle = <consolidated title>,
    mergedText = <consolidated text>
)
```

This tool:
1. Updates the master record with your merged title and text (and re-indexes the vector).
2. Moves all graph relationships from the duplicate nodes to the master.
3. Deletes the duplicate nodes.

### 4. Verification
After a merge, verify the results:
- Use `get_fact_neighborhood` on the Master ID to confirm the consolidated graph.
- If the text needs further refinement, use `update_fact`.

## Efficiency: Multi-Tool Execution
Call `find_duplicates` first, then process all clusters: call `suggest_merge` for each cluster in the same response, read all results, write consolidated content for each, then execute all `merge_facts` calls together.

## Examples of "Duplicate" Patterns
- **People**: "Kate" vs "Katarina" (same role/company).
- **Projects**: "Project Phoenix" vs "Phoenix AGI Research".
- **Concepts**: "Vector DB" vs "Vector Databases".

## Tips
- **Human-in-the-Loop**: If a merge seems risky or data might be lost, STOP and ask the user for confirmation.
- **Relationship Safety**: `merge_facts` automatically prevents duplicate edges. Do NOT manually re-link after a merge.
