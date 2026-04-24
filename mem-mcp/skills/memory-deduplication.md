# Skill: Memory Deduplication

This skill provides a systematic workflow for identifying and merging duplicate entries in the Memory Vault. Duplicate entries often occur when information is captured from different sources (e.g., meeting transcriptions, manual notes, diary entries) with slightly different phrasing or levels of detail.

## Objective
Maintain a clean, high-quality knowledge base by merging similar entities into a single, authoritative "Master" record.

## Workflow

### 1. Identify Potential Duplicates
Run the `memory_find_duplicates` tool to scan the memory for clusters of similar items.
- **Category**: Defaults to "People", but can be used for any category.
- **Threshold**: Adjust the similarity threshold (default 0.75). Higher values (0.9+) find near-exact matches; lower values (0.7-0.8) find fuzzy matches.

### 2. Analyze Clusters
For each cluster returned:
- Compare the `text` and `metadata` of all members.
- Look for overlapping fields like `role`, `company`, `email`, or `topics`.
- High `avg_similarity` suggests a strong candidate for merging.

### 3. Select the Master Record
Choose one record to be the "Master". Criteria for selection:
- Most complete information.
- Most recent timestamp.
- Better structured metadata.

### 4. Consolidate Information
Use `update_fact` to move valuable information from the duplicates into the Master record.
- Update the `text` to include missing context.
- Merge `metadata` dictionaries.
- Ensure the `category` is consistent.

### 5. Re-link Relationships
If the duplicate records have relationships (links) to other facts:
- Use `get_fact_neighborhood` on the duplicate IDs to see what they are linked to.
- Use `link_facts` to recreate those relationships pointing to the Master ID.

### 6. Delete Duplicates
Once the Master record is updated and all links are preserved, use `delete_fact` to remove the redundant records.

## Examples of "Duplicate" Patterns
- **People**: "Kate" vs "Katarina" (same role/company).
- **Projects**: "Project Phoenix" vs "Phoenix AGI Research".
- **Concepts**: "Vector DB" vs "Vector Databases".

## Tips
- Always verify high-threshold matches manually; high similarity doesn't always mean identity.
- Use the `recommendation` field in the tool output as a starting guide.
