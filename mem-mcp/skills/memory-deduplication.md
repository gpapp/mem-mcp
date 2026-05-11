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

## Troubleshooting: If No Duplicates are Found

If `find_duplicates` returns no clusters but you suspect there are duplicates:

### 1. Lower the Threshold
Try a lower threshold (e.g., `0.5` or even `0.4`). This increases the sensitivity of the algorithm.
```
find_duplicates(category="People", threshold=0.5)
```

### 2. Perform Manual "Suspect" Searches
If broad detection fails, search for specific names or terms that you suspect are duplicated.
- **Tip**: Do NOT search for the category name (e.g., "people") as a query. Search for specific names or partial strings.
- **Tool**: Use `search_facts(query="Matthias", category="People")`.
- **Action**: If you find two items that look identical, manually create a cluster by taking their IDs and passing them to `suggest_merge(["id1", "id2"])`.

### 3. Check Related Categories
Sometimes facts are miscategorized. If you can't find a person in "People", check "General" or "Client".
```
find_duplicates(category="General", threshold=0.6)
```

## Advanced: Manual Clustering
The `suggest_merge` tool can accept a JSON array of raw IDs. If you manually identify duplicates through search:
1. Collect the IDs of the duplicates.
2. Call `suggest_merge(cluster_json='["id1", "id2", "id3"]')`.
3. Follow the normal merge workflow.

## Verification: Post-Merge Cleanup
1. **Check Relationships**: Use `get_fact_neighborhood` on the Master ID.
2. **Delete Residuals**: If a merge left behind a redundant fact that wasn't part of the cluster, delete it manually.
3. **Update Text**: Use `update_fact` to polish the final consolidated text if the LLM's merge was too verbose.

## Tips for High-Quality Merges
- **Preserve Aliases**: If one record uses a nickname and another a full name, add the nickname to the `aliases` metadata of the Master record.
- **Date Check**: Look at `updatedAt` or `timestamp` to identify which record is the most recent (though "completeness" is usually a better guide for the Master).
- **Confirm with User**: Always show the consolidated text to the user before calling `merge_facts` if there's any ambiguity.
