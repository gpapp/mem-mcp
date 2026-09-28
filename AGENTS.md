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

The dependency-light regression suite covers matching, scope compatibility, scope-name and `Client:`-header resolution, duplicate scoring and clustering, merge validation, merge callback ordering, People candidate resolution, LLM prompt contracts, and the chunking split. Two more suites need no database either: `test_embedding_reliability.py` lifts the real functions out of `common.py` with `ast.get_source_segment`, and `test_cypher_safety.py` lints the Cypher in every module (see Gotchas for why). `test_backup_compression.py` uses the same `ast` lift for the snapshot compression helpers, since `backup.py` also cannot be imported without `httpx`. `test_people_extraction.py` lifts `people_extract_windows` out of `diary_manager.py` for the same reason. `test_graph_scope.py` lifts the graph scoping and cap policy out of `fact_manager.py` the same way. `test_mobile_layout.py` reads `templates/dashboard.html` and asserts the properties of the responsive stylesheet — no browser is available here, so it cannot check that the page looks right, only that the things a regression would silently undo are still in place.

**A test of a helper is not a test of its call site.** `OllamaModelMatchTests` covers `_ollama_model_matches` directly, and re-injecting the old `if model in installed` into `ensure_ollama_models` left all of them green — the download bug lived in the caller. There is now a tenth test that asserts the call site, for the same reason `WriteOrderingGuardTests` exists. Whenever a bug is a wrong call rather than a wrong function, pin the call.

```powershell
Push-Location mem-mcp
C:/tools/miniconda3/python.exe -m unittest -v test_matching_regressions.py test_embedding_reliability.py test_chunking.py test_cypher_safety.py test_backup_compression.py test_people_extraction.py test_graph_scope.py test_mobile_layout.py
Pop-Location
```

Run `git -c core.whitespace=cr-at-eol diff --check` after documentation or code edits (the `cr-at-eol` avoids false positives on the CRLF files). The suites use pure helpers and fake callbacks so they do not require Neo4j, Qdrant, or Ollama.

### Image Build

All three backing services are pinned to an exact tag — `qdrant/qdrant:v1.19.1`, `neo4j:5.26.0`, `ollama/ollama:0.34.4`. They were all `:latest` at one point, which meant `docker-compose up -d --build` silently upgraded them. Two concrete consequences, not hypothetical ones: Ollama moved its embed route across versions (hence the dual `/api/embeddings` + `/api/embed` client), and `backup.py` depends on Qdrant's snapshot create/download/upload API, so an unpinned major bump there would make a savepoint unrestorable rather than fail visibly. When bumping, change the tag deliberately and expect to re-run the backup round trip.

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
- Graph: `MEM_GRAPH_MAX_NODES` (600) caps the records in one graph response, keeping the most connected — see "Build Graph Mode"
- Chunking: `MEM_CHUNK_CHARS` (3000), `MEM_CHUNK_MAX` (16), `MEM_CHUNK_OVERLAP` (200), `MEM_CHUNK_FETCH_MULTIPLIER` (3), and startup re-chunking `MEM_RECHUNK_ENABLED` / `MEM_RECHUNK_LIMIT` (200) / `MEM_RECHUNK_CONCURRENCY` (2) — see "Long Records & Chunking"
- Embedding retries: `MEM_EMBED_RETRIES` (default 2) and `MEM_EMBED_RETRY_BACKOFF` (default 1.5, in seconds), `MEM_EMBED_MAX_CHARS` (default 12000) — see "Embedding Reliability"
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

**A count mismatch logged at steps 1–2 is not data loss.** The checks run *before*
`sync_orphans()`, so by construction they report the work step 4 is about to do.
Production log on a normal boot: `Diary count mismatch — Neo4j: 447, Qdrant: 371`
followed by 76 `in Neo4j only` entries, which are then re-embedded. Read the
mismatch as "step 4 has that many to repair", and the number of `Ollama embed
input too long` lines immediately after it as the ones that cost a retry. It
only becomes a real problem if the same mismatch appears on *every* boot with
the same count, which means step 4 is failing — check for a `sync_orphans` error
below it.

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

- A savepoint is a directory: `manifest.json`, `neo4j.jsonl.gz`, and one `qdrant-<collection>.snapshot.gz` per collection.
- **Ordering is load-bearing.** Qdrant is snapshotted *first*, then Neo4j is exported. A fact written in between then exists in Neo4j but not in the vector snapshot, and `sync_orphans()` re-embeds it. The reverse order strands a fact with no vector and nothing to rebuild it from.
- **Qdrant uses its own snapshot API**; the server-side copy is deleted after download so repeated runs cannot fill the volume. Restore is `DELETE` the collection then `PUT .../snapshots/upload`.
- **Qdrant snapshots are gzipped, and the restore path has two traps.** A chunked long record repeats its whole payload across every chunk, so the raw snapshot is mostly near-duplicate JSON — measured 3% of original on a repeating chunk family. Download streams through `gzip.GzipFile(fileobj=..., mtime=0)` (handed a file object, not a path, so the target's basename is not recorded in the header, and `mtime=0` so two exports of identical data are byte-identical and a diff is meaningful). On restore, `_open_snapshot()` decides by **filename suffix, not manifest version** — a version check would strand every savepoint taken before compression, and the suffix is the only thing on disk that cannot lie. `_upload_name()` **rebuilds** the multipart filename as `<collection>.snapshot` because Qdrant validates the extension and rejects `*.snapshot.gz` outright, after the collection has already been dropped.
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

## Long Records & Chunking

One embedding vector over a long fact or diary entry is a lossy average of the whole document: a query about a detail in the middle scores poorly against everything else. `mem-mcp/chunking.py` splits a long record into several vector points instead. It is dependency-light (no DB drivers) so it is unit-testable on its own, like `matching_utils.py`.

- **Chunk 0 keeps the record id.** Every existing id-based path keeps working for chunk 0, and a record short enough not to chunk is written byte-identically to before, so existing data needs no migration. Chunks 1..N-1 get deterministic ids from `chunk_point_id()` (`uuid5` under a pinned `CHUNK_NS`), which makes a rewrite idempotent instead of accumulating orphan points.
- **Every chunk carries the full record payload**, plus `parentId`, `chunkIndex`, `chunkCount`. The scope/category/keyword filters are evaluated against the payload at query time, so a chunk with a partial payload would be invisible to those filters.
- **Only chunk 0 carries `text`/`content`.** Chunks 1..N-1 carry `chunkText` and omit the full text entirely, so a search that forgets to hydrate cannot return a fragment as if it were the whole record. Hydration is a single batched `retrieve` per search.
- **`parent_of(point_id, payload)` is the only correct way to get a record id from a Qdrant point.** Every reconciliation pass must use it. Raw `str(point.id)` is wrong in both directions: chunk ids would look like orphans, and a delete would leave the rest of the family searchable.
- **Search collapses a family to its best chunk.** Results are keyed by parent and a record appears once. Because a chunked record contributes N points to every vector query, `fetch_limit` is multiplied by `CHUNK_FETCH_MULTIPLIER` (default 3) — without it a single long entry can occupy the entire top-N window and no other record is ever returned.
- **An update deletes the old family before upserting.** Shortening a text produces fewer chunks, and the leftover high-index points would keep answering for text that no longer exists. `replace=True` does this, and it happens *after* all chunks are embedded so a rejected embed leaves the previous version searchable.
- **Scope is a property of the record, not the point.** `_backfill_qdrant` diffs per record and writes to every point of the family; `restore_scope_links` links once per record. Keying either by point id makes every chunk mismatch and silently strips the scope keys, which then surfaces as searches returning facts the user filtered out.
- **The same rule applies to a manual scope change, and to deleting a client or context.** `db_set_fact_scope` / `db_set_diary_scope` in `client_manager.py` patch and drop `clientId`/`contextId` in Qdrant, so they resolve the family through `_scope_targets()` first. Addressing one point by the record id is chunk 0 only, which is why it looks right until a record is long enough to chunk. `_drop_scope_payload` is the sharper version of the same trap: `db_delete_client` / `db_delete_context` pass it **record** ids read out of Neo4j, so passing them straight through would leave chunks 1..N-1 holding a `clientId` for a node that was just `DETACH DELETE`d — the record keeps answering a client-filtered search for a client that no longer exists. It expands them through `_scope_targets()` too.
- **Chunk ids are derived, never stored as an index.** The count of points for a record is discovered by scrolling on `parentId` (`find_chunk_family`). Deriving it from `chunkCount` would need the payload, which is gone by the time a delete runs, and a stale `chunkCount` would orphan points.
- `MEM_CHUNK_CHARS` (3000), `MEM_CHUNK_MAX` (16), `MEM_CHUNK_OVERLAP` (200) and `MEM_CHUNK_FETCH_MULTIPLIER` (3) tune it. `CHUNK_MAX` is a *call budget*, not a coverage limit: when honouring it would drop the tail, `chunk_limit()` raises it to whatever keeps every part inside the embed ceiling. Silently abandoning the end of a document is the failure mode chunking exists to prevent.

Existing long records are converted with `python mem-mcp/reindex_chunks.py --dry-run` first, then without. It is idempotent and skips anything already chunked.

**Startup re-chunking does the same work automatically.** `rechunk_unindexed_records()` in `fact_manager.py` runs as a background task in the lifespan, *after* `sync_orphans()` so the two stores are already consistent. Two filters keep it cheap, and both matter: a record is only a candidate if it is **large enough to split** and **not already chunked**. Re-chunking an already-chunked record costs one embedding call per chunk to rebuild identical vectors, so `_chunked_record_ids()` reads the whole collection's chunk state in a single scroll rather than one lookup per candidate, and it returns `None` on failure — an empty set there would make every large record look un-chunked and rewrite all of them on every boot. Candidates are sorted longest-first so a boot that hits `RECHUNK_LIMIT` spends it where recall is worst, and the remainder is left for the next restart rather than started and abandoned. A failed record is counted and the run continues. Tunables: `MEM_RECHUNK_ENABLED`, `MEM_RECHUNK_LIMIT` (200), `MEM_RECHUNK_CONCURRENCY` (2).

## Embedding Reliability

`get_embedding()` in `common.py` is the single choke point for every vector write, and Ollama fails it more often than the rest of the stack: a model still loading, a GPU under memory pressure, or a busy container behind a rolling restart. A bare `raise_for_status()` turns any of those into a `httpx.HTTPStatusError` whose only text is `500 Internal Server Error` — the operator learns nothing about whether the model is missing, the host is OOM, or the route moved.

- **Two routes, legacy first.** `("/api/embeddings", {"model","prompt"})` then `("/api/embed", {"model","input"})`. Both response shapes are accepted (`{"embedding": [...]}` and `{"embeddings": [[...]]}`). `ollama/ollama:latest` is unpinned in compose, so the surface can genuinely shift between deploys — a 404 on the legacy route drops straight through to the modern one instead of burning retries on a 404 that will never recover.
- **Retry only what can recover.** `_EMBED_RETRY_STATUS` is `408/409/429/500/502/503/504`; backoff is `EMBED_RETRY_BACKOFF * attempt`. A `400`/`404` (missing model, bad request) is not retried on that route — it moves to the other route and then fails.
- **Log the reason, not the status.** `_ollama_detail(resp)` prefers Ollama's own `{"error": ...}` body and falls back to the raw text. Without this, the traceback said `500` and nothing else. The reason is carried in `last_detail` and surfaced once in the terminal error, not per attempt — see the logging policy below.
- **An over-long input is a length error wearing a 500.** Ollama answers `the input length exceeds the context length` with HTTP 500, so it looks retryable and the loop re-sent the same oversized text five times before failing with a message that never mentioned length. `_is_input_too_long()` classifies it as deterministic: the remedy is a *smaller input*, not another attempt, so the text is halved and re-sent with no backoff. Do not reclassify these hints as transient — that is what made the second production incident take five requests and still fail.
- **Truncation keeps head and tail.** `_truncate_for_embed()` cuts to `EMBED_MAX_CHARS` and keeps 75% from the front and the rest from the end, with the `\n...\n` marker counted *inside* the budget. A transcription puts the subject at the top and the conclusions at the bottom, and a search query is far more likely to match the tail. The marker being inside the budget is not cosmetic — measuring it showed a 1000-char budget producing 1005 chars, which defeats the point of a ceiling.
- **`EMBED_MAX_CHARS` is a budget, not the model's actual ceiling.** Ollama enforces a real token limit below it, so a record that passes the budget check can still be rejected as too long. Production log: an 11189-char input was refused under the default 12000 budget and only succeeded at 5594. That is the designed path, not a fault — the halving costs one wasted round trip and then succeeds. But if the log is full of these, lower `MEM_EMBED_MAX_CHARS` to match the embedder rather than leaving every long record to pay a rejected request first. The two log lines to compare are `input is N chars, over the 12000 budget — truncating` (budget hit) and `Ollama embed input too long on /api/embeddings (N chars) — retrying with` (the real ceiling is below N).
- **The cache key stays the original text.** Truncation is a transport detail; keying the cache on the truncated form would let two different long facts that share a prefix collide, and a search query that truncated onto a stored fact's prefix would get that fact's vector back as its own answer.
- **Final failure is a `RuntimeError`** naming the model and quoting the reason plus the `docker exec ollama ollama pull <model>` fix. Nothing upstream catches `httpx.HTTPStatusError`, so the exception type change is safe.
- **A tag is part of the model's name, and `MEM_EMBEDDER_MODEL` usually omits it.** Ollama reports `nomic-embed-text:latest` in `/api/tags`, so `model in installed` never matched and `ensure_ollama_models` re-pulled a 274MB embedder on every boot (23 times in the week it was found). `_ollama_model_matches()` treats a tagless name as satisfied by `:latest` or `:any`, while a name that *does* carry a tag must still match that tag or `:latest` — `qwen3.5:0.8b` must never be satisfied by an installed `qwen3.5:2b`, or the vault is silently embedded with a different model than configured.
- **The log level is a contract, not cosmetics.** Embedding is the high-volume path — one call per query variant per search, plus one per write — and production runs at `LOG_LEVEL=WARNING`. Per-attempt failures and retries are `DEBUG`; **input-too-long is `WARNING`** (rare, and it means the stored vector is lossy); **a failed embed is `ERROR`, logged once**, carrying Ollama's own reason and the `ollama pull` fix. `WARNING` is reserved for chat/LLM traffic. Emitting a `WARNING` per embed call buries the chat traffic that the level exists to surface. `test_embedding_reliability.py` asserts all three of these.
- **A failed embedding never leaves a half-written record.** Every write path (`db_add_memory`, `db_update_memory`, `db_save_diary`, `db_update_diary`) embeds and upserts to Qdrant *before* it writes Neo4j, so a raise means the write never happened — the user gets an error, not a record with a missing vector. It also means the two stores never disagree about the text. `db_update_diary` had the two steps the other way round and left Neo4j holding text the vector store could no longer find; `WriteOrderingGuardTests` now asserts the order against the source, because nothing about that failure is loud. Do not call `get_embedding()` directly on these paths — route it through `_upsert_fact_points`/`_upsert_diary_points` so the guard covers it and the chunk family is handled.

Tests for all of the above live in `mem-mcp/test_embedding_reliability.py`. `common.py` cannot be imported without the DB drivers, so that file lifts the real functions out of the source with `ast.get_source_segment` and execs them against stubs — it tests the shipping code, not a copy of it.

## Gotchas

- Qdrant not accessible from host—interact via app only
- Long timeouts (600s) for LLM operations—don't timeout-hunt
- Collection named `ea_memories` (hardcoded in memory.py)
- **Validate `templates/dashboard.html` JS with `node --check` after any template edit.** `py_compile` and the Python suites structurally cannot see a JS syntax error, and one stray `await` in a non-async function took down the entire panel — `<body onload="init()">` reported `init is not defined` only as a downstream symptom of the script block failing to parse.
- **Cypher cannot be parsed locally** — there is no Neo4j and no driver in this environment, so a syntax error ships to production and surfaces as `neo4j.exceptions.CypherSyntaxError` on first execution. `test_cypher_safety.py` exists because of this: it extracts every Cypher string constant and f-string fragment and lints `FOREACH (v IN <list> | ...)` for a variable referenced inside its own list. A `FOREACH (x IN ... ELSE [x] END | DELETE x)` is a parse error, not a runtime one, and it was the reason every full reclassify aborted on its first call. The suite also pins the scope-clear query's shape. Add to it when you add a query.
- **CSS is not validated by anything here, and an inline style beats a media query.** The three page layouts are fixed-width columns — memories `180px + 280px + flex`, diary `220px + flex`, graph `200px + flex` — and each pane sits inside `height: calc(100vh - Npx)` with its own `overflow-y: auto`, so on touch the *page* cannot scroll: you drag inside a pane that may be one line tall. `test_mobile_layout.py` pins the `@media (max-width: 900px)` / `640px` rules that fix both. Four things it is protecting, each of which fails silently:
  - **An inline `style` on `.graph-layout` disables the entire mobile block.** Inline styles win at every specificity, so one re-added attribute reverts the graph to two 200px columns with no error anywhere. The `display: flex` lives in the stylesheet for exactly this reason.
  - **`input, textarea, select { font-size: 16px }` is a functional fix, not a style preference.** Below 16px iOS zooms the viewport on focus and the zoom cannot be undone without a reload, so the form never recovers its layout.
  - **`overflow-x: clip` on `body`, never `hidden`.** `hidden` makes `body` a scroll container, which unsticks the `position: sticky` nav.
  - **`network.on("hold")` is the only way to reach the graph context menu on touch.** `oncontext` is a right-click; vis fires `hold` on long-press. Without the handler, Build Graph mode is unusable on a phone — the menu is the only way to add a neighbour. The `ResizeObserver` alongside it matters for the same reason: vis sizes its canvas once at construction, so stacking the layout leaves the canvas at the old width and clipped, and it must be disconnected in `rebuildNetwork()` before `network.destroy()`.

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
2. In **Memories** or **Diary** tab, open a record's menu and choose **🕸️ Show in Graph**
   (the `📍 Show on map` button on a memory card does the same thing)
3. Node appears centered with its connections
4. **Right-click** any node for context menu (long-press on touch):
   - **Go to fact** → navigate to memory details
   - **Show on map** → add this node and its connections
   - **Show all connected** → add every direct neighbour
   - **Remove from map** → drop the node again
5. Click **🗑️ Clear** to reset the build graph

There is no "add by relationship type" item. `showConnectionType()` and the
`.submenu` stylesheet block are dead code — the function has no callers and no
markup references it. Do not document it as a feature without also wiring it
up.

The graph obeys the **same client / project selection as the memory list**. Two
filters exist and they are independent:

- **Scope** — the client (and optionally the project) picker. Handed to the API
  as `clientId` / `contextId` and resolved in `db_get_graph`, so the server does
  the filtering rather than the browser.
- **Categories** — the chips in the graph sidebar. Client-side, applied to every
  path that adds a node, including "Show all connected".

#### Scope is resolved server-side, from the edges

`db_get_graph(user_id, client_id, context_id, limit)` filters through
`_scope_and_cap_graph`, which decides membership from the `FOR_CLIENT` /
`IN_CONTEXT` **relationships**, not the denormalised `clientId` property. The
property is written by the scope classifier and can lag the edge it mirrors, and
a fact whose link was just deleted has to leave the filtered view immediately
rather than on the next reclassify. `gui.py` forwards `clientId` / `contextId`
on `/api/graph`, `/api/graph/neighbors/{id}` and `/api/graph/focus/{id}`.

**Diary entries are scoped from their edges too, and only from their edges.**
`db_get_graph` used to read a diary entry's client from a `clientId` property.
**No Fact or DiaryEntry node has that property in Neo4j** — scope lives on the
`FOR_CLIENT` / `IN_CONTEXT` edges, and `clientId` is only ever a *Qdrant payload*
key. So that read returned `None` for every entry, every diary entry came out
unscoped, and a client or project filter dropped all of them. The two scope
passes also matched `:Fact` only, so a diary entry's scope edge produced no
graph edge at all. Both now match `Fact` or `DiaryEntry` and the property read
is gone. The Fact/DiaryEntry test is `node_map[id]["label"] == "Fact"` rather
than a label in Cypher, because both passes run after every node is in
`node_map`. `diary_scope` holds a **list**, not a tuple, because the passes
assign into it by index and a tuple raises `TypeError` on the first edge. The
`r["clientId"]` reads in `fact_manager` / `diary_manager` are fine — those are
Cypher `AS clientId` aliases off the edges, not properties. Guarded by
`DiaryScopeSourceTests` and `DiaryScopeTests` in `test_graph_scope.py`.

Client, Context and Category nodes are **always** kept under a scope filter. A
graph that has been filtered to one client and then omits that client's node
reads as "this client has no facts", which is worse than a slightly noisy graph.

`clientFilter` is `'all' | 'active' | <clientId>`. Only the third is a node id;
`graphScopeParams()` deliberately drops the first two rather than sending a
literal `'all'` as an id that matches nothing. `'active'` is expanded client-side
to the active ids in `clientListCache` when filtering node lists.

#### The cap

`MEM_GRAPH_MAX_NODES` (default 600) caps the records in one response. Over the
cap the **most connected** records are kept — a graph of leaves explains nothing,
and that is the case that makes "full graph" feel useless. The response carries
`truncated` and `total`; the UI shows a banner rather than silently rendering a
partial graph. Edges touching a dropped record are dropped with it.

#### Neighbours are not just facts

`db_get_neighborhood` used to end its Cypher in `(neighbor:Fact)`, so "Show all
connected" could only ever add other facts. It now returns Fact, DiaryEntry,
Client and Context nodes — the connections a person actually recognises. Category
is still excluded: it is shared across the whole vault and only adds clutter.
`_filter_neighborhood_scope` keeps a Client/Context node that **is** the active
scope, for the reason given above.

**Every node-adding path filters through `nodeInCategory()`**, and reports what
it skipped in the toast. A path that ignores the category chips puts nodes on the
map that the sidebar says are hidden, which is indistinguishable from the filter
being broken.

#### "Show in Graph" has to say when it cannot help

`openInGraph(id)` checks the id is in the current DataSet before focusing it.
`network.focus()` on an absent id is a silent no-op: the tab switches, the canvas
looks identical, and the user concludes the button is broken. It now toasts
"not in the current graph — outside the selected client / project / categories".

Diary entries have a **Show in Graph** menu item (`_diaryMenuGraph`). They did
not, which meant a diary entry could not reach the graph by any route at all.

#### Related entries

`RELEVANT_TO` targets a Client **or** a Context — the relationship type is the
same for both, and the target was only pinned to `:Client` by the Cypher label,
so widening it needs no migration and no backfill. The read side emits
`kind: 'client' | 'context'`, which the chips use for their `📁` prefix and the
"add" picker, which enumerates `client.contexts` alongside the clients.

Do not reintroduce a `:Client`-only match here. A project stored as relevant
would be unreachable, and the existing `:Client` links are indistinguishable
from the new ones except by label.

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

### Diary People Extraction

Diary people extraction runs the **whole entry**, in overlapping windows, not a prefix. `_extract_people_names()` in `diary_manager.py` iterates `people_extract_windows(content)` and unions the results.

The prefix form is the one to avoid. It sent `content[:2000]`, so on a 40k-char transcription every person named after character 2000 was silently missed — no error, just a missing `MENTIONS` edge nobody was looking for. Keyword extraction (above) already ran on the full content, so the two paths disagreed on what "the entry" means.

- `people_extract_windows(content, window=0, overlap=0)` is pure and unit-tested (`test_people_extraction.py`): blank → `[]`, a doc that fits the window → one element (short entries cost what they always did), otherwise `body[i:i+size] for i in range(0, len(body), size - overlap)`.
- The overlap exists because a name straddling a boundary is cut in half, and both halves then look like garbage to the extractor. `step` is clamped to `size - 1` so a degenerate overlap cannot make the loop non-terminating.
- `MEM_PEOPLE_WINDOW` (6000) and `MEM_PEOPLE_OVERLAP` (600) tune it.
- **Do not cap the window count.** Capping would reintroduce exactly the silent tail-drop the windowing fixed. Above `PEOPLE_EXTRACT_WARN_WINDOWS` (6) the entry is expensive (one LLM call per window) and logs a WARNING naming the knob instead. This is chat traffic, so WARNING is the right level per the logging policy in "Embedding Reliability".
- Each window is wrapped in its own `try/except` inside the loop, so one bad window cannot discard the names the other windows found. `clean_extracted_people_names(found)` dedupes the union, since a name spanning the overlap is found twice.