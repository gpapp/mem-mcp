# AGENTS.md

## Dev Commands

```powershell
# Setup
cp .env.example .env
# Edit .env: set MEM_NEO4J_PASSWORD and NEO_PASS

# Infra (must run first)
docker-compose up -d
docker exec ollama ollama pull nomic-embed-text
docker exec ollama ollama pull qwen3.5:0.8b
docker exec ollama ollama pull gemma4:e2b

# Local dev (infra must be running)
.\.venv\Scripts\Activate.ps1
python mem-mcp\server.py
```

## Code Quality

After completing any code changes:
1. Run `python -m py_compile <file.py>` to verify syntax
2. If successful, commit and push to git
3. If running in Docker, rebuild and restart: `docker-compose up -d --build mem-mcp` (or pull + rebuild on the target machine)
4. **Do NOT attempt to access Docker or databases directly** — containers run on a remote machine; only use git to push changes (the ops team deploys)

### Focused Tests

The dependency-light regression suite covers matching, scope compatibility, scope-name and `Client:`-header resolution, duplicate scoring and clustering, merge validation, merge callback ordering, People candidate resolution, and LLM prompt contracts:

```powershell
Push-Location mem-mcp
C:/tools/miniconda3/python.exe -m unittest -v test_matching_regressions.py
Pop-Location
```

Run `git diff --check` after documentation or code edits. The suite uses pure helpers and fake callbacks so it does not require Neo4j, Qdrant, or Ollama.

### Image Build

`mem-mcp/Dockerfile` pins `# syntax=docker/dockerfile:1` and installs Python deps with `RUN --mount=type=cache,target=/root/.cache/pip`. Wheels live in a BuildKit cache that survives rebuilds, so a rebuild after a source-only change no longer re-downloads PyPI. Do not reintroduce `--no-cache-dir` — it disables the mount and is the reason the layer grew on every build. `mem-mcp/.dockerignore` keeps `__pycache__`, `logs/`, and `backup/` out of the build context, since `COPY . .` would otherwise ship them into the image.

## Architecture

| Component | Port | Notes |
|-----------|------|-------|
| App | 8086 (Docker), 8080 (internal) | Unified: GUI `/gui`, MCP `/mcp`, REST `/api/*` |
| Qdrant | 6333 | Internal only, not exposed to host |
| Neo4j | 7687 | Internal only |
| Ollama | 11434 | Internal only |
| nginx | /mem-mcp | Proxy mount point (must include in all URL configs) |

## Nginx Config

**IMPORTANT**: Update `nginx_snippet.conf` before deploying. Two locations:

- `/mem-mcp/mcp/` → MCP (Basic Auth via nginx)
- `/mem-mcp/` → GUI/API (session auth via app, cookie passthrough)

## Critical Config

- `MEM_NEO4J_PASSWORD` (also mapped to `NEO_PASS` in docker-compose)
- Embedder: pull the configured `MEM_EMBEDDER_MODEL` into Ollama (defaults to `nomic-embed-text`)
- Query LLM: pull `qwen3.5:0.8b` into Ollama container (used for search rewriting and diary keyword extraction)
- Merge LLM: `MEM_MERGE_MODEL` defaults to `gemma4:e2b` and is used only for dashboard merge-draft generation; override it if the host GPU cannot run that model
- Scope backfill LLM: `MEM_SCOPE_MODEL` (defaults to the query LLM) classifies unlinked facts/diary entries against existing clients on startup; `MEM_SCOPE_BACKFILL=0` disables it, `MEM_SCOPE_CONCURRENCY` (default 3) caps parallel classifications
- Backups: `MEM_BACKUP_ENABLED` (default 1), `MEM_BACKUP_HOUR` / `MEM_BACKUP_MINUTES` (local server time, default 03:00), `MEM_BACKUP_KEEP` (default 14), `MEM_BACKUP_DIR` (default `<dirname LOG_DIR>/backup`)
- User vault resolved from `Authorization: Basic` header or session cookie
- `BASE_URL` must include `/mcp` prefix when behind nginx

## Diary Consistency & Auto-Fix

On startup, the lifespan runs in this order:

0. `migrate_client_context()` — Client-category facts → Client nodes, `WORKS_FOR` → `FOR_CLIENT`, Project facts → Context nodes (idempotent, skips migrated users)
0b. `llm_backfill_scope()` — Ollama classifies every fact/diary WITHOUT a `FOR_CLIENT` link against existing clients/contexts and links matches (self-limiting; model can only pick existing names-or-null)
1. `run_consistency_checks()` — Neo4j/Qdrant fact count mismatch, dangling MENTIONS, orphan categories, untitled facts (fact_manager.py, read-only)
2. `run_diary_consistency_checks()` — diary entry count mismatch, dangling MENTIONS, untitled/bad-timestamp entries (diary_manager.py, read-only)
3. `fix_diary_entries()` — sets `name = 'Untitled Diary Entry'` and/or `timestamp = now` on null/empty properties (diary_manager.py)
4. `sync_orphans()` — deletes Qdrant-only points, re-embeds Neo4j-only entries, `DETACH DELETE` orphan categories (fact_manager.py)

Encapsulation rule: diary persistence and consistency logic lives in `diary_manager.py`, not `fact_manager.py`. The server calls both independently.

## Scope Reclassification Reliability

The full reclassify (`POST /api/maintenance/reclassify`) and the single-item paths rewrite every `FOR_CLIENT` link. The fixes below each address a way that used to silently produce wrong or missing scope.

- **Evidence snapshot before clearing.** The classifier's strongest signal is the `[client: X]` tag on a neighbouring fact, and those tags come from links the run is about to delete. `_capture_scope_snapshot()` reads the current assignments into memory first, so the signal is constant for the whole run instead of decaying as links disappear. Removing this is what made a long run progressively "forget" earlier items' clients.
- **Failure is not the same as "generic".** `_classify_scope()` returns `(client, context, ok)`. `ok=False` means the LLM call or JSON parse failed; the item is left unstamped so the next boot retries it. Only `ok=True` writes `scopeCheckedSig`. An Ollama timeout used to be recorded as a genuine "this is generic" verdict, permanently marking the item unclassifiable.
- **A hallucinated name is a real null, not a retry.** If the model names a client that matches nothing, the item is stamped (there is no point retrying the same prompt) but the mismatch is logged at WARNING.
- **Name resolution, not exact compare.** LLM answers vary ("Deutsche Bank" for "Deutsche Bank (DB)"). `resolve_scope_name()` in `matching_utils.py` resolves through an evidence ladder — exact → token subset → high token coverage → containment → fuzzy ratio — and always returns the *stored* spelling. It returns nothing when the top two candidates are within `SCOPE_NAME_AMBIGUITY_MARGIN`: an ambiguous winner is worse than no answer because it writes a confidently wrong link.
- **Explicit `Client:` headers are read, not guessed.** `client_header_value()` in `matching_utils.py` handles bold, bullets, heading markers and `:`/`-`/`–` separators, and cuts trailing prose. Keep it in `matching_utils.py` (not `migrate_client_context.py`) so it stays testable without the DB drivers.
- **Clearing is batched and off the event loop.** `clear_scope_links_batch()` / `clear_scope_links_async()` replace one blocking query per item with chunked round trips via `asyncio.to_thread`. The old per-item sync clear stalled the whole server for the length of a run.
- **A bad item does not abort the run.** Each item is wrapped; a failure increments the `errors` counter and leaves that item unstamped for retry. Items are drained from a queue by a fixed worker pool, not one `gather` task per row, so live tasks stay bounded by `MEM_SCOPE_CONCURRENCY` instead of the vault size.
- **Single-item reclassify touches one point.** `_backfill_qdrant(only_ids={item_id})` restricts both the Cypher and the Qdrant read to that item. The unbounded pass is O(vault) — it scrolls every point in both collections — which made a one-row UI action cost a full-vault scan.
- **Existing auto-MENTIONS links are preserved.** `_existing_auto_people()` seeds the candidate set before `resolve_people_candidates`, because the stricter `people_match_allowed` gate can otherwise fail to re-find a link that was correct.

Do not reintroduce byte-exact name matching, per-item synchronous clears, or a stamp written on LLM failure.

## Backup & Restore

`mem-mcp/backup.py` maintains **savepoints** under `BACKUP_DIR` (default `<dirname LOG_DIR>/backup`, i.e. `/app/backup` in Docker with `./mem-mcp-data/backup` bind-mounted there).

- A savepoint is a directory: `manifest.json`, `neo4j.jsonl.gz`, and one `qdrant-<collection>.snapshot` per collection.
- **Ordering is load-bearing.** Qdrant is snapshotted *first*, then Neo4j is exported. A fact written in between then exists in Neo4j but not in the vector snapshot, and `sync_orphans()` re-embeds it. The reverse order strands a fact with no vector and nothing to rebuild it from.
- **Qdrant uses its own snapshot API**; the server-side copy is deleted after download so repeated runs cannot fill the volume. Restore is `DELETE` the collection then `PUT .../snapshots/upload`.
- **Neo4j Community has no online backup** (`neo4j-admin database backup` is Enterprise; `dump`/`load` need the DB stopped), so the graph is exported over Bolt and replayed. Relationships resolve endpoints by **business key, never `elementId`** — elementIds are reassigned by any dump/load, so keying on them produces edges that point at nothing after a restore.
- **Temporal properties are type-tagged.** `_encode_value()` writes `{"__t": "datetime", "v": "<iso>"}`; the replay re-applies them with the matching Cypher constructor. Without this, a `ZonedDateTime` returns as a plain string and the corruption does not surface until something queries a date.
- Dynamic relationship types are rebuilt with `apoc.create.relationship()` (APOC is already required by compose).
- After a restore, `_reconcile_after_restore()` re-runs `sync_qdrant_scope()`, `restore_scope_links()` and `sync_orphans()` so the two stores agree again.
- **Savepoints are vault-wide, not per-user** — the Qdrant snapshot API is per-collection and the graph export is a full dump. The GUI session still gates who may trigger one.
- **The scheduler is an in-process task** started in the lifespan (`scheduled_backup_loop()`); the container has no Docker socket, no `neo4j-admin` and no APScheduler. A restart after the scheduled hour waits for the next day.
- **Savepoints are never watched on a timer.** The panel fetches once on initial load (deduped via `savepointsLoaded`/`savepointsInFlight`), again on manual refresh, and otherwise only while a job is actually in flight. `startBackupPolling()` arms a 2.5s interval; the first terminal state read calls `stopBackupPolling()`, so an idle vault makes no requests.
- **`renderBackupStatus()` must stay a pure renderer.** Completion side effects live in `applyBackupCompletion()`, reached only from `backupTick()` and guarded by `backupSettled`. A `render*` function that re-fetches its own input is a loop: `renderBackupStatus('done')` → `loadSavepoints()` → `renderBackupStatus('done')` … was measured issuing 500+ requests for a single finished backup.
- `prune_savepoints()` keeps the newest `MEM_BACKUP_KEEP` **complete** savepoints only, so a failed attempt is never promoted into the retention window.

### Maintenance lock

`claim_maintenance()` / `release_maintenance()` in `common.py` are a per-user in-process mutex. Reclassification and backup/restore both rewrite large parts of the graph; running them together interleaves the writes. Every maintenance job takes the lock when it starts and releases it in a `finally` block, and the API answers `409` with a human-readable reason when it cannot.

The scheduled backup is the one exception: it is vault-wide, so there is no single user whose lock it could take. `scheduled_backup_loop()` calls `active_maintenance()` first and defers by `_RETRY_SECONDS` if any user holds the lock. Keep it that way — calling `run_backup()` directly from the scheduler would snapshot a graph mid-reclassify.

## Gotchas

- Qdrant not accessible from host—interact via app only
- Long timeouts (600s) for LLM operations—don't timeout-hunt
- Collection named `ea_memories` (hardcoded in memory.py)

## Matching, Deduplication & Merge Safety

- `matching_utils.py` is the dependency-light home for shared matching, scope, merge-validation, candidate-resolution, and duplicate-scoring helpers.
- Duplicate candidates are filtered by compatible client/context scope. Exact identity evidence can be decisive; fuzzy evidence is blended with vector similarity and cannot override a weak vector by itself.
- Clusters must contain a threshold-qualified core member, which prevents weak bridge-shaped transitive clusters from being presented as duplicates.
- LLMs adjudicate bounded candidate sets and return validated IDs. They do not directly select arbitrary graph nodes or mutate storage.
- `merge_facts` resolves every target for the current user before calling update or delete operations. The merge master records `pendingQdrantDeletes` after Neo4j deletion; `sync_orphans()` retries those deletions and clears the marker only after Qdrant accepts the cleanup.
- Merge work remains cross-store rather than globally transactional. Keep the recovery marker and startup reconciliation path intact when changing merge behavior.

## Search Scoring & Confidence

`db_search_memories` returns two independent numbers per result. Do not confuse them:

- `score` — legacy blended value (vector similarity + additive name heuristics, range ~0.3–3.2). Used for **ranking only**. It is not a similarity and must never be thresholded.
- `confidence` — normalized 0–1 from `identity_confidence()` in `matching_utils.py`. `top_p` filters on **this**.
- `evidence` — the identity label behind the confidence: `exact` / `alias` / `first+last` / `first_name` (same record) vs `surname_strong` / `fuzzy_name` / `partial_token` / `name_conflict` / `none` (near miss).

Rules that matter when changing this code:

- `top_p` is a real confidence gate. Results at or above it are returned first; results below are appended after, flagged `weak: True` and capped at `WEAK_RESULT_LIMIT`. The escape hatch preserves recall, so a tighter floor alone does not cause silent "no results".
- A name-shaped query with weak identity evidence has its **vector** component damped (`VECTOR_DAMP_MISS` / `VECTOR_DAMP_CONFLICT`). Without this, a misspelling of someone absent returns a confident stranger: `'Radoslav'` scored a success against `'Oleg Tolstashov'`.
- A clearly conflicting first name is a veto (`EVIDENCE_CONFLICT`), not a deduction. A shared surname cannot rescue it — `'Ben Deutsche'` vs `'Lukas Deutsch'` is two different people.
- `people_match_allowed` is the trust boundary for auto-created MENTIONS links (`diary_manager.py`, `migrate_client_context.py`). It gates on confidence and rejects anything `weak`. It previously gated on the blended `score` with 1.2/1.6 thresholds, which passed a wrong person at 1.627.
- `db_search_diary` applies `top_p` to its own blended `score`. This is inconsistent with fact search by design of that older path — do not assume the two are interchangeable.
- Per-query ranking data is written to `logs/search_stats.log` via `log_search_stats()` in `common.py`. That logger is pinned to INFO and independent of `LOG_LEVEL` (production runs WARNING), so search data is never silenced by the app log level.

## Features

### Build Graph Mode
Build your own focused subgraph starting from any memory.

1. Go to **Graph** tab → click **Build Graph** mode toggle
2. In **Memories** tab, click **📍 Show on map** on any memory
3. Node appears centered with its connections
4. **Right-click** any node for context menu:
   - **Go to fact** → navigate to memory details
   - **Show all connected** → add all direct neighbors
   - **Show connection → [verb]** → add nodes by specific relationship type
5. Click **🗑️ Clear** to reset the build graph

### Link Management
Modify or delete links between memories directly from the UI.

- Each link badge shows ✏️ (edit) and 🗑️ (delete) buttons
- Click ✏️ to change the relationship type inline
- Click 🗑️ to delete the link (with confirmation)

### Diary Search
Search diary entries from the sidebar.

- Type in the **Search entries…** box at the top of the diary sidebar
- Instant client-side substring filter runs as you type
- After 400 ms a server-side **vector similarity search** (`GET /api/diary/search?q=`) fires and updates results
- Clicking a result navigates to that date's entries
- Clearing the input restores the full date history list
- API endpoint: `GET /api/diary/search?q=<text>&limit=10&top_p=0.4`

### Dashboard Deduplication
The dashboard's **Deduplicate** tab provides a review-first merge workflow.

- Scan a category with a configurable similarity threshold and maximum cluster size.
- Select the records to merge, choose the master, and use the LLM to generate an editable merged title and text draft from only those records.
- Merges require explicit confirmation and use the same ownership validation and recovery marker as the MCP `merge_facts` tool.
- GUI endpoints: `GET /api/duplicates`, `POST /api/duplicates/draft`, and `POST /api/duplicates/merge`.

### Backup & Restore
Setup → Maintenance → **Backup & Restore** manages the savepoints.

- **Back up now** takes an immediate savepoint; one is also written automatically at `MEM_BACKUP_HOUR:MEM_BACKUP_MINUTES` server time, keeping the newest `MEM_BACKUP_KEEP`.
- Each savepoint lists its time, whether it was automatic, and the node/link counts and size.
- **Restore** replaces the entire vault with that savepoint, after an explicit warning. It restores both vector collections and the graph, then reconciles the two.
- A savepoint that did not finish is shown as **Unusable** and cannot be restored.
- The list loads once when the page opens and refreshes on **↻ Refresh**. It does not poll; the status line updates only while a backup or restore you started is still running, then stops.
- API endpoints: `GET /api/backup/savepoints`, `GET /api/backup/status`, `POST /api/backup/run`, `POST /api/backup/restore/{id}`.
- Restore is blocked with `409` while a reclassification is running, and reclassification is blocked while a backup or restore is running.

### Skills System
Pluggable skill workflows loaded from Markdown files in `mem-mcp/skills/`.

- `find_skills` — lists all available skill `.md` files
- `get_skill_workflow(name)` — returns the full Markdown content of a skill
- MCP prompts (`process-transcription`, `memory-deduplication`) inject skill instructions into the conversation
- Add new skills by creating a `.md` file in `mem-mcp/skills/` — no server restart required

### Diary Keyword Extraction
Every diary save/update triggers automatic keyword extraction via the query LLM (`qwen3.5:0.8b`).

- Up to 10 keywords extracted per entry, stored in both Qdrant payload and Neo4j node
- Keywords boost vector search relevance in `diary_search_entries`
- Backfill existing entries: `python mem-mcp/reindex_diary_keywords.py -u <user_id>`
- CLI options: `-f/--force` (re-extract even if keywords exist), `-d/--dry-run`, `-c/--concurrency` (default 3)