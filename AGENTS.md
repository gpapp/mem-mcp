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

The dependency-light regression suite covers matching, scope compatibility, scope-name and `Client:`-header resolution, duplicate scoring and clustering, merge validation, merge callback ordering, People candidate resolution, LLM prompt contracts, and the chunking split. Two more suites need no database either: `test_embedding_reliability.py` lifts the real functions out of `common.py` with `ast.get_source_segment`, and `test_cypher_safety.py` lints the Cypher in every module (see Gotchas for why). `test_backup_compression.py` uses the same `ast` lift for the snapshot compression helpers, since `backup.py` also cannot be imported without `httpx`. `test_people_extraction.py` lifts `people_extract_windows` and the whole of `extract_diary_keywords` out of `diary_manager.py` for the same reason, and *calls* the keyword extractor against a stub LLM rather than only reading its source. It also holds `ReclassifyIsScopeOnlyTests`, which lifts `_classify_and_link_diary` out of `migrate_client_context.py` and runs it with every people extractor bound to a stub that raises, so the test passes only if the function genuinely cannot reach them — a substring guard on that module could not see a twin defined in a sibling, and did not when it mattered. `test_graph_scope.py` lifts the graph scoping and cap policy out of `fact_manager.py` the same way. `test_matching_regressions.py` also holds the `RELEVANT_TO` selection tests — `RelatedClientSelectionTests` **calls** `_related_clients_for` lifted out of `migrate_client_context.py`, and `ScopePromptTests` pins the prompt *sentences* above, because a prompt is text and no other test in the suite notices when a sentence changes. `test_llm_reliability.py` lifts the chat path out of `common.py` the same way and, unlike the others, *raises* from its fake client: the defect it guards was the absence of a log line, which no source assertion can see. `test_mobile_layout.py` reads `templates/dashboard.html` and asserts the properties of the responsive stylesheet — no browser is available here, so it cannot check that the page looks right, only that the things a regression would silently undo are still in place. `test_env_wiring.py` reads `.env.example` and `docker-compose.yml` and asserts every documented variable actually reaches the container (see Gotchas for the incident).

**A test of a helper is not a test of its call site.** `OllamaModelMatchTests` covers `_ollama_model_matches` directly, and re-injecting the old `if model in installed` into `ensure_ollama_models` left all of them green — the download bug lived in the caller. There is now a tenth test that asserts the call site, for the same reason `WriteOrderingGuardTests` exists. Whenever a bug is a wrong call rather than a wrong function, pin the call.

```powershell
Push-Location mem-mcp
C:/tools/miniconda3/python.exe -m unittest -v test_matching_regressions.py test_embedding_reliability.py test_chunking.py test_cypher_safety.py test_backup_compression.py test_people_extraction.py test_graph_scope.py test_mobile_layout.py test_llm_reliability.py test_env_wiring.py
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
- Query LLM: `LLM_QUERY_MODEL` (used for search rewriting, scope classification and both diary extractors). **Set `MEM_SCOPE_MODEL` as well** — the scope classifier is the slow path, and because `MEM_SCOPE_MODEL` defaults to the query model, setting only one of them leaves the expensive half of the work on a different quant than the one you measured.
- **The GPU reservation is in a SEPARATE file, and dropping it is silent.** `docker-compose.gpu.yml` holds the ollama device reservation (`count: all`). It is *not* in `docker-compose.yml`, and there is no `docker-compose.override.yml` to make it automatic, so the deploy command is `-f docker-compose.yml -f docker-compose.gpu.yml`. Running bare `docker compose up -d ollama` recreates the container with no GPU: it starts healthy, answers every request, and produces entirely plausible latencies — it is just CPU inference. This bit again on 2026-09-29 while reducing `OLLAMA_CONTEXT_LENGTH`, which looked like a clean change. Adding a `deploy:` block to the base file is *not* the fix; it duplicates the overlay and drifts from it. The only reliable check is `docker exec ollama ollama ps`, and `100% CPU` means the overlay was dropped.
- **`OLLAMA_CONTEXT_LENGTH` must be sized against the merge path, not the extractors.** Everything else is windowed — `MEM_PEOPLE_WINDOW`/`MEM_KEYWORD_WINDOW` are *chars*, ~3.6 per token, and a call only ever sees one window, so a 30k-char diary entry costs ~2000 tokens regardless of its length. The merge draft is the one call that is not windowed: it `json.dumps` the full text of every selected record with no cap. Measured against real facts (longest 3055 chars), `max_cluster` 4/8/12/20 → 3783/6393/8801/**13103** prompt tokens. So 16332 covers the full allowed range — barely, 2.3k spare at 20 — and 8192 would have broken at `max_cluster=12`. Cutting it bought 3 points of GPU residency (80%→83%) and cost over half the merge headroom. The merge is the one path where a dropped tail is silent data loss, so it sets the ceiling, not the largest window.
- **A cold model load is 9-20s and is paid on the first call after any idle gap.** `OLLAMA_KEEP_ALIVE=30m` now pins it. Deliberately not `-1`: the card is 4 GB shared with ASR (1.25 GB), and holding a 3.3 GB chat model resident indefinitely starves the transcriber.
- **Verify the model is actually on the GPU before sizing anything against VRAM.** This was measured wrong for days: the Ollama service had no `deploy.resources.reservations.devices` and no `/dev/nvidia*` inside the container, so `ollama ps` reported `100% CPU` and every latency figure was CPU inference on 15 GiB of RAM with 7.1 GiB of swap already used. `docker exec ollama ls /dev/nvidia*` and `docker exec ollama ollama ps` are the two checks; `ollama ps` must say `100% GPU` for a model to be resident. Once the GPU was actually enabled, `qwen3.5:2b-q4_K_M` reported `100% GPU` and `gemma3:4b-it-q4_K_M` reported `49%/51% CPU/GPU` at 3.8 GB — i.e. the 4 GB card cannot hold it, so it pays a load per call. Registry sizes on a 4 GB card shared with ASR (1.25 GB): `qwen3.5:2b` 2.74 GB (Q8_0), `qwen3.5:2b-q4_K_M` 1.95 GB, `gemma3:4b-it-q4_K_M` 3.34 GB, `gemma4:e2b` 7.16 GB.
- **Correctness beats residency, and the deployment disagrees with the default.** `qwen3.5:2b` is the documented default but a 2b model completes fields rather than reasoning about them: on the same scope item, `qwen3.5:2b-q4_K_M` answered `{"client": "EPAM", "context": "PPC"}` in 32.4 s while `gemma3:4b-it-q4_K_M` answered the correct verdict in 5 consistent runs. The current `.env` sets **both** `MEM_SCOPE_MODEL` and `LLM_QUERY_MODEL` to `gemma3:4b-it-q4_K_M`. It also fixed a search-rewrite hallucination (`Thomas Kraikauer` → `["Thomas Kraikauer", "German actor", "film director"]` became `["Thomas Kraikauer", "Deutsche Bank"]`). That gemma3 run is now history — `.env` moved on to `phi4-mini` and then to `nemotron-3-nano:4b`, see above — but the reasoning stands: the model that answers correctly is the one to keep, and the gemma3 measurements are the evidence for why the 2b default was abandoned. Budget for the load cost: gemma3 stays warm, but a cold first call in each window is much slower, and a full reclassify is a multi-minute job under `MEM_LLM_TIMEOUT=300`.
- **The four chat roles are split across two models, and one knob cannot be right for all of them.** `MEM_EXTRACT_MODEL` (`granite3.3:2b`) covers extraction — diary keywords, people names, search rewriting — while `LLM_QUERY_MODEL`, `MEM_SCOPE_MODEL` and `MEM_MERGE_MODEL` stay on `nemotron-3-nano:4b` for judgement. `MEM_EXTRACT_MODEL` defaults to `LLM_QUERY_MODEL`, so an operator who sets nothing changes nothing.

  The split is not a preference; it is the asymmetry between an error you can repeat and one you cannot. Extraction failures are recoverable — re-running the extractor is cheap and overwrites the bad result. **A scope null is not**: it is stamped with `scopeCheckedSig` and never revisited, so it is permanent scope loss, and that is why the null count was the deciding measurement rather than speed or latency. Measured on this host, same prompts, against the real vault:

  | role | granite3.3:2b | nemotron-3-nano:4b |
  |---|---|---|
  | diary keywords | 99.0% precision, **0 invented** | 79.7%, 5 invented, 10 corrupted |
  | people names | F1 **0.938** | F1 0.920 |
  | speed on those two | **2.3-4.2x faster** | baseline |
  | scope nulls (permanent) | 2 | **0** |
  | merge adjudication | `review` on a true duplicate | **`merge`** (correct) |
  | merge draft retention | 90.7/5.1/9.3/14.6 — drops 3 of 4 records | 100/99.1/100/98.8 |

  **Do not collapse the split back to one model.** `ModelRoleRoutingTests` in `test_env_wiring.py` pins the call sites, and two of those tests are behavioural rather than source-shaped: they read the `model=` the lifted function was actually handed. That matters because the rewrite function swallows its own exceptions, so a missing `EXTRACT_MODEL` in the test's namespace degrades it to the heuristic fallback and the cache tests still pass while testing nothing.

  **Two models on one 4 GB card cannot both stay resident** (2.34 + 2.63 GB, plus ASR's 1.25 GB), so a role change evicts the other. That is the intended trade — the cheap high-volume role reloads fast and the expensive one stays warm across a reclassify — but it means a search immediately after a reclassify pays a load. `OLLAMA_KEEP_ALIVE=30m` covers the common case where one role dominates a session.

  **Search rewrite breaches `MEM_SEARCH_LLM_TIMEOUT=45` on a cold load on both models** (granite 65.5 s, nemotron 58.7 s; warm 2.6 s and 6.1 s). This is pre-existing and unchanged by the split, but it is now granite's load rather than nemotron's, and it is the one remaining measurement that argues for a higher `MEM_SEARCH_LLM_TIMEOUT`.

- **The live `.env` set all three chat models to `nemotron-3-nano:4b`** (now four roles across two models, above), and that replaced the `gemma3:4b-it-q4_K_M` recommendation below. It was changed because of a measured defect, not a preference. `phi4-mini` (the interim value, never documented) returned `[]` from the people extractor for *every* window of a 6.3k-char entry that opens with a `## Participants` heading naming six people — the answer was in the first 100 characters. Short inputs worked, so the failure was length-dependent, not a parsing bug and not truncation (`prompt_eval_count=1571` proves the prompt was fully evaluated). On the same entry, same prompt, same request shape, `nemotron-3-nano:4b` returned 20 names including all six participants. Search rewrite (12.5 s warm, inside `MEM_SEARCH_LLM_TIMEOUT=45`) and scope classification (16.2 s, valid `{"client", "context", "related"}`) were both re-verified on nemotron. **The people-extraction prompt needed no change** — the model was the defect. Do not "fix" that prompt.
- **A model swap is not free of re-verification.** The three chat roles have different contracts, and the one that was broken is not the one you would guess. Measure each role against the real input before declaring the swap good.
- **Nemotron echoes the `(none)` context placeholder back as a context value.** This is the third string it (or its predecessor) has lifted out of the client list — see the list at `migrate_client_context.py:689-694`. The guard at `_classify_scope` drops an answered context when the client has no projects, so today this is harmless, but *only because all seven clients currently have zero contexts*. Give one client a project and that branch stops being a no-op.
- **What is not a chat model: `MEM_EMBEDDER_MODEL`.** It must stay an embedding model (`nomic-embed-text`). "Use the new model for everything" is not a safe reading of the chat knobs — pointing the embedder at a chat model silently invalidates every vector already in Qdrant, and nothing errors. Verified in the running container's env, not just in `.env`.
- Merge LLM: `MEM_MERGE_MODEL` defaults to `gemma4:e2b` and is used only for dashboard merge-draft generation; override it if the host GPU cannot run that model

- Scope backfill LLM: `MEM_SCOPE_MODEL` (defaults to the query LLM) classifies unlinked facts/diary entries against existing clients on startup; `MEM_SCOPE_BACKFILL=0` disables it, `MEM_SCOPE_CONCURRENCY` (default 3) caps parallel classifications
- Backups: `MEM_BACKUP_ENABLED` (default 1), `MEM_BACKUP_HOUR` / `MEM_BACKUP_MINUTES` (local server time, default 03:00), `MEM_BACKUP_KEEP` (default 14), `MEM_BACKUP_DIR` (default `<dirname LOG_DIR>/backup`)
- Graph: `MEM_GRAPH_MAX_NODES` (600) caps the records in one graph response, keeping the most connected — see "Build Graph Mode"
- Chunking: `MEM_CHUNK_CHARS` (3000), `MEM_CHUNK_MAX` (16), `MEM_CHUNK_OVERLAP` (200), `MEM_CHUNK_FETCH_MULTIPLIER` (3), and startup re-chunking `MEM_RECHUNK_ENABLED` / `MEM_RECHUNK_LIMIT` (200) / `MEM_RECHUNK_CONCURRENCY` (2) — see "Long Records & Chunking"
- Chat timeouts: `MEM_LLM_TIMEOUT` (default 300, background passes), `MEM_SEARCH_LLM_TIMEOUT` (default 45, search rewrite), `MEM_LLM_CONNECT_TIMEOUT` (default 10) — see "LLM Chat Reliability"
- Chat logging: `MEM_LLM_LOG_CHARS` (default 1000, `0` = sizes only) — how much of each prompt and answer reaches `memory-vault.log`
- Query rewrite cache: `MEM_QUERY_CACHE_TTL` (default 900s) and `MEM_QUERY_CACHE_MAX` (default 256 entries); either `0` disables the cache — see "Query Rewrite Cache"
- Embedding retries: `MEM_EMBED_RETRIES` (default 2) and `MEM_EMBED_RETRY_BACKOFF` (default 1.5, in seconds), `MEM_EMBED_MAX_CHARS` (default 8000) — see "Embedding Reliability"
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
- **Being named on an item is not evidence against being its client.** This is the rule to get right, and the direction was wrong twice. The first version said *"a participant's employer is not the client — decide from what the work is FOR"*, reasoned from a handover meeting about a bank's SAP estate. The user corrected the target answer: that meeting **is** EPAM's own engagement, run by EPAM's own architects, so the correct verdict is `EPAM` with **no** project. The shipped prompt says the opposite of the old rule — an item about an organisation's own people, systems and engagement **has** that organisation as its client, and "being named on an item is not evidence against being its client". Do not reintroduce a rule that treats a participant's employer as a disqualifier.
- **A `[own client: X]` tag is who that person is, and it agrees with the item.** The prompt once called a neighbour's tag "the strongest signal available" and told the model to "strongly prefer that client". It now says a MENTIONS/RELATED tag describes the *person*, and that when the item is about that person's employer, the tag **agrees** with the item rather than competing with it. `_scope_tag()` also relabels a `People` neighbour's tag to `[own client: X]`, because a 2b model reads the token, not the prose around it. `ScopePromptTests` pins the sentences: nothing else in the suite notices when a prompt sentence changes, and `cls.prompt` there is `ast.literal_eval`'d because the assignment is a chain of adjacent string literals.
- **The context list order is not a ranking.** An answer chose `PPC` because it is first in EPAM's list. The prompt says so explicitly, because "do not assign a context just because it is the only one available" was already there and the model filled the field anyway. See the guard below — the prompt alone was never enough.
- **A second subject is a `RELEVANT_TO` edge, not a competing `FOR_CLIENT`.** `_classify_scope` now returns a 4th element, `related` — the clients the item is *also* about — taken from the same LLM call, because a host slow enough to need a 300s budget cannot afford a second pass per window. `classify_scope_full` unions it across windows (the same reasoning as the client union) and strips the winner, since an item linked to its own client twice appears twice in a filtered list. `_related_clients_for()` then unions the model's list with every `client_tags_in_text()` tag, resolves each through `resolve_scope_name`, and drops the primary.
- **That is what makes a wrong primary guess survivable.** A neighbour's scope tag is *wrong* as a reason to file an item under that person's employer and exactly *right* as a reason to also link it there — the item really does discuss them. So the tag stopped being an override and became a cross-reference: even when the primary is wrong, the item is still findable under the attendees' employer.
- **A project the item never names is dropped in code, not by prose.** The classifier is reliable about the **client** and unreliable about the **context**: asked for both it fills both. Measured on the 4160-char handover entry, whose text names no project at all, it answered `PPC` in **six** prompt variants — including one that listed an explicit `(undefined)` option first and told the model to prefer it. The system prompt demonstrably reaches the model (without it, or with an unrelated one, it stops returning JSON entirely), so this is not a comprehension failure: it is a bias to complete the field. `context_named_in_text()` in `matching_utils.py` therefore checks the pairing where it is a fact — a project the item never mentions is not the item's project. `_classify_scope` drops such a context and logs it at INFO. It also drops a context that is not on the chosen client's own list (a cross-client pair, from the model taking a project off another line). Verified end-to-end: 3/3 runs now store `(EPAM, None, [])`. **Six prompt variants could not achieve what one token check did**; do not spend another six attempts on prompt wording for this.
- **`RELEVANT_TO` is written on both node types, and on the null-primary path too.** `_link_diary_relevant_to` takes a `label` parameter (it was hardcoded to `DiaryEntry`, so facts silently lost the feature entirely — the worst kind of partial support, because it looked like it worked). Both `_classify_and_link_fact` and `_classify_and_link_diary` call `_write_related_links` **before** their `return False`, because a null primary is exactly when `RELEVANT_TO` is the only signal left. `_write_related_links` swallows its own failures: a secondary association must not turn a correct primary scope into a failed reclassification, and `MERGE` makes a partial write idempotent.
- **Failure is not the same as "generic".** `_classify_scope()` returns `(client, context, related, ok)`. `ok=False` means the LLM call or JSON parse failed; the item is left unstamped so the next boot retries it. Only `ok=True` writes `scopeCheckedSig`. An Ollama timeout used to be recorded as a genuine "this is generic" verdict, permanently marking the item unclassifiable.
- **A hallucinated name is a real null, not a retry.** If the model names a client that matches nothing, the item is stamped (there is no point retrying the same prompt) but the mismatch is logged at WARNING.
- **Name resolution, not exact compare.** LLM answers vary ("Deutsche Bank" for "Deutsche Bank (DB)"). `resolve_scope_name()` in `matching_utils.py` resolves through an evidence ladder — exact → token subset → high token coverage → containment → fuzzy ratio — and always returns the *stored* spelling. It returns nothing when the top two candidates are within `SCOPE_NAME_AMBIGUITY_MARGIN`: an ambiguous winner is worse than no answer because it writes a confidently wrong link.
- **Explicit `Client:` headers are read, not guessed.** `client_header_value()` in `matching_utils.py` handles bold, bullets, heading markers and `:`/`-`/`–` separators, and cuts trailing prose. Keep it in `matching_utils.py` (not `migrate_client_context.py`) so it stays testable without the DB drivers.
- **Clearing is batched and off the event loop.** `clear_scope_links_batch()` / `clear_scope_links_async()` replace one blocking query per item with chunked round trips via `asyncio.to_thread`. The old per-item sync clear stalled the whole server for the length of a run.
- **A list in a MATCH property map matches nothing, and it fails silently.** `clear_scope_links_batch()` reads `MATCH (n {userId: $userId}) WHERE n.id IN $ids` — the id test must be `IN`. It was `MATCH (n {id: $ids, userId: $userId})`, which reads like "n whose id is in `$ids`" and is not: on the right of a property test a list is compared for **equality**, so it matches a node whose `id` literally *is* that list — no node, ever. The query returned `cleared = 0`, which is indistinguishable from "nothing to clear", and because clearing is how a reclassify *overwrites* the previous verdict, every run only ever **added** links. A real entry accumulated two `FOR_CLIENT` edges and an `IN_CONTEXT` left over from answers the classifier had stopped giving, `REMOVE n.scopeCheckedSig` silently never ran, and re-running the reclassify appeared to do nothing at all. **`test_cypher_safety.ListParameterInPropertyMapTests` lints this across every module** and derives the list-valued kwargs from the AST rather than guessing from a plural parameter name, because `where_ids` is a list and `status` is not. Do not reintroduce a list on the right of a property test; the existing test's `assertIn("$ids", query)` passed on the broken query, which is why a behavioural rule needed a lint.
- **A bad item does not abort the run.** Each item is wrapped; a failure increments the `errors` counter and leaves that item unstamped for retry. Items are drained from a queue by a fixed worker pool, not one `gather` task per row, so live tasks stay bounded by `MEM_SCOPE_CONCURRENCY` instead of the vault size.
- **Single-item reclassify touches one point.** `_backfill_qdrant(only_ids={item_id})` restricts both the Cypher and the Qdrant read to that item. The unbounded pass is O(vault) — it scrolls every point in both collections — which made a one-row UI action cost a full-vault scan.
- **Reclassify is scope-only. It must not extract participants.** `_classify_and_link_diary` used to open by calling a *private* copy of people extraction — `_extract_people_names` / `_link_missing_people`, defined right there in `migrate_client_context.py`, with its own `_PEOPLE_SYSTEM` prompt. So "Reclassify scope" in the UI also rewrote `MENTIONS` edges. The twin resolved to `SCOPE_MODEL` while the real extractor, `diary_manager._auto_link_people`, resolved to `EXTRACT_MODEL` — the same task on two models, and the one a reclassify happened to trigger was the judgement model, so every reclassify paid for participant extraction at window-count price (`MEM_SCOPE_TEXT_WINDOW` per window) on top of the classification it was asked for. All five twin functions are deleted. **Do not reintroduce participant extraction into the reclassify path**; its entry points are the save and update paths and the UI's *Extract participants* action, which is also where the `EXTRACT_MODEL` routing lives. The twin's docstring is the evidence for why it was a duplicate: it says the `content[:2000]` prefix was "still here after the twin in diary_manager was fixed", so the divergence was already known and had been papered over rather than removed.
- **Deleting the write is safe because the reclassify only ever read those edges.** `_fast_diary_scope`'s strongest signal is a unanimous client across the entry's `MENTIONS` neighbours, and `clear_scope_links_batch` deletes only `FOR_CLIENT` and `IN_CONTEXT` — so the evidence survives the clear intact. That is the property to preserve: if the scope clear ever grows a `MENTIONS` clause, the fast path loses its input and a reclassify silently drops to the LLM for every entry.
- **An assertion that counts occurrences is asserting on the dead code too.** `test_the_windowing_helper_is_actually_used` read `assertEqual(source.count("text_windows("), 2)` — two, because the twin called it as well as the classifier. Deleting the twin broke a test that was correct about the classifier. The count was never the property; the classifier's own call site is, and it is now asserted by name. **This is the `assertIn`-over-a-whole-file lesson again:** a guard that pins a number pins every contributor to that number, including the one you just deleted, so it fails on correct code and would have kept passing had the twin stayed while the classifier's call was the thing removed. Both halves were re-injected separately to confirm each bites.

- **Relabel the evidence instead of arguing with it in prose.** A reclassify run with the rules above still answered `{"client": "EPAM", "context": "PPC"}` for the handover entry. The entry's own text named no client and no project at all; the only client name anywhere in the prompt was `[client: EPAM]` on the two Enterprise Architects who ran it. Telling the model in prose that the tag was "NOT who the item is about" did not help — a 2b model reads the tag, not the instruction. `_scope_tag()` now renders a `People` neighbour as `[own client: X]`, which states the meaning in the token itself, and `client_tags_in_text` reads both forms so the `RELEVANT_TO` union still works. A prose rule only binds behaviour when the evidence it argues with is not competing for the same token.
- **A null has to be offered as a real answer, and a client with no contexts is not a dead end.** The same entry was stamped `context: "PPC"` — EPAM's first context — when it named no project. The prompt now says plainly that a null is "a real, correct, expected answer" and that it must "never fill a field to avoid leaving it empty", because a null is recoverable and a guess is stamped. Separately, `SAP SE` — whose name the text *does* contain — was never considered as the primary while `EPAM` was, because SAP SE has no contexts and EPAM has three; the prompt now says `(no contexts)` is a normal, valid choice and never a reason to prefer another client. A field the model can always complete is a field it will always complete.
- **The classifier reads the whole item, and the read is recorded.** This is the
  one that is a *correctness* bug rather than a precision loss, which is why it
  deserves its own rule. `_classify_scope` used to receive
  `item_text if len(item_text) <= 1500 else item_text[:1500] + "…"`, and the
  module's own `_extract_people_names` sent `content[:2000]` — the same defect
  the diary save path had already been fixed for. A long transcription is not
  a long document for the *classifier*; it is a 40k-char document whose client
  is named on page three. Because the reclassify verdict is stamped
  (`scopeCheckedSig`), a fragment-fed verdict is not merely wrong for one run —
  it is *permanent*, and no later run re-reads the text. `text_windows()` in
  `matching_utils.py` is the single pure implementation both LLM passes here
  use, and `classify_scope_full()` runs it: a client named by **any** window
  counts, disagreement is settled by most-frequent and then by the earliest
  window, and `ok=False` is returned only when no window found anything *and* at
  least one window failed — so a sibling timeout cannot discard a verdict that
  already has evidence behind it.
- **Do not cap the window count.** `SCOPE_TEXT_WARN_WINDOWS` is a warning about
  cost, deliberately not a limit; a cap would reintroduce exactly the silent
  tail-drop this replaces, just at a larger offset. The cost is one LLM call per
  window and it is only paid on items long enough to need it.

Do not reintroduce byte-exact name matching, per-item synchronous clears, a stamp written on LLM failure, or a prefix slice of the item text anywhere in the reclassify path. `test_matching_regressions.py::ScopeClassificationInputTests` walks the AST for a prefix slice applied to a text carrier — see the "gotcha" below for why it is an AST walk and not a substring search.

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
- **`EMBED_MAX_CHARS` is a budget, not the model's actual ceiling.** Ollama enforces a real token limit below it, so a record that passes the budget check can still be rejected as too long. Production log: an 11189-char input was refused and only 6000 was accepted, which puts the real window at ~2048 tokens — about 2000 tokens of English. The default is **8000** for that reason, and the old 12000 was over budget for this model. The shrink ladder absorbed it, so nothing was lost, but every long record paid a rejected request first. The two log lines to compare are `input is N chars, over the 8000 budget — truncating` (budget hit) and `Ollama embed input too long on /api/embeddings (N chars) — retrying with` (the real ceiling is below N). `chunking.EMBED_CEILING` reads the same variable and **must keep the same default** — it sizes chunk parts, so a mismatch would size every chunk for a ceiling the embedder refuses.
- **The cache key stays the original text.** Truncation is a transport detail; keying the cache on the truncated form would let two different long facts that share a prefix collide, and a search query that truncated onto a stored fact's prefix would get that fact's vector back as its own answer.
- **Final failure is a `RuntimeError`** naming the model and quoting the reason plus the `docker exec ollama ollama pull <model>` fix. Nothing upstream catches `httpx.HTTPStatusError`, so the exception type change is safe.
- **A tag is part of the model's name, and `MEM_EMBEDDER_MODEL` usually omits it.** Ollama reports `nomic-embed-text:latest` in `/api/tags`, so `model in installed` never matched and `ensure_ollama_models` re-pulled a 274MB embedder on every boot (23 times in the week it was found). `_ollama_model_matches()` treats a tagless name as satisfied by `:latest` or `:any`, while a name that *does* carry a tag must still match that tag or `:latest` — `qwen3.5:0.8b` must never be satisfied by an installed `qwen3.5:2b`, or the vault is silently embedded with a different model than configured.
- **The log level is a contract, not cosmetics.** Embedding is the high-volume path — one call per query variant per search, plus one per write — and production runs at `LOG_LEVEL=WARNING`. Per-attempt failures and retries are `DEBUG`; **input-too-long is `WARNING`** (rare, and it means the stored vector is lossy); **a failed embed is `ERROR`, logged once**, carrying Ollama's own reason and the `ollama pull` fix. `WARNING` is reserved for chat/LLM traffic. Emitting a `WARNING` per embed call buries the chat traffic that the level exists to surface. `test_embedding_reliability.py` asserts all three of these.
- **A failed embedding never leaves a half-written record.** Every write path (`db_add_memory`, `db_update_memory`, `db_save_diary`, `db_update_diary`) embeds and upserts to Qdrant *before* it writes Neo4j, so a raise means the write never happened — the user gets an error, not a record with a missing vector. It also means the two stores never disagree about the text. `db_update_diary` had the two steps the other way round and left Neo4j holding text the vector store could no longer find; `WriteOrderingGuardTests` now asserts the order against the source, because nothing about that failure is loud. Do not call `get_embedding()` directly on these paths — route it through `_upsert_fact_points`/`_upsert_diary_points` so the guard covers it and the chunk family is handled.

Tests for all of the above live in `mem-mcp/test_embedding_reliability.py`. `common.py` cannot be imported without the DB drivers, so that file lifts the real functions out of the source with `ast.get_source_segment` and execs them against stubs — it tests the shipping code, not a copy of it.

## Query Rewrite Cache

`rewrite_search_query` is on the critical path of every search and costs one LLM call — measured at 12.5 s warm on `nemotron-3-nano:4b`, tens of seconds on a slower quant. It is the one expensive call in the request path whose answer does not depend on the contents of the vault: its inputs are `(query, category, client, context, expand)`. Repeating a search therefore re-asked a settled question and got a slightly different answer each time, which is a worse property than being slow.

- **Cache the rewrite, not the results.** Qdrant answers change as the vault changes; a cached result list would be a correctness bug. The rewrite is pure over its inputs.
- **Only a real answer is cached.** The `_expand_query` heuristic fallback and the exception path are deliberately not stored. A timeout is transient, and pinning the degraded result for the TTL would turn one slow call into a permanently worse search. `test_matching_regressions.RewriteSearchQueryCallSiteTests` pins this by counting LLM calls, not by reading the source.
- **Both paths out of the cache return a copy.** `list(cached)` on the read path, and `list(variants)` on the write path — the list handed to `put()` *is* the cached object, so returning it directly let the first caller's in-place append rewrite what every later search saw. That was a real bug, caught by the call-site test, and the two copies are the fix.
- **`TTLCache` uses an injectable clock.** Expiry is testable without sleeping, so the TTL boundary is pinned exactly instead of raced. It expires on *read*, not only on write: a cache that evicts only on write still holds the memory of every query ever asked, which is the thing the bound exists to prevent. Eviction is oldest-first for free, because `put` re-inserts at the end of the dict.
- **The key normalizes `None` and unhashable parts.** Filters are optional strings the caller does not control; a stray list raising `TypeError` on the lookup path is a 500 the caller can do nothing about.
- `TTLCache(ttl<=0)` or `max_entries<=0` is inert rather than unbounded — a cache that cannot expire must not retain entries.
- `TTLCache` is not thread-safe by design. It holds values derived from a model call, never authoritative state, so the worst a lost update costs is one extra LLM call.

## LLM Chat Reliability

`get_llm_response()` in `common.py` is the single choke point for every chat call. It is the *other* Ollama path and it failed differently from the embedding one: not a bad status code, but a call that simply never came back.

A reclassify returned `503 Service Unavailable` and was read as "out of memory loading the model". It was neither. The log held the request line, then a **60.07s gap**, then the next request — the signature of `httpx.AsyncClient(timeout=60.0)` firing. The reclassify had *not* failed: `classify_scope_full`'s per-window `except` caught the failed window, set `ok=False`, and the entry was left unstamped for retry, then the next window succeeded. The 503 the user actually saw came from their **second click** hitting the maintenance lock held by the still-running first. Nothing about that was visible.

- **The read budget is its own knob, and it is not 60s.** `LLM_TIMEOUT` (`MEM_LLM_TIMEOUT`, default **300**) for background passes; `SEARCH_LLM_TIMEOUT` (`MEM_SEARCH_LLM_TIMEOUT`, default 45) for the search-query rewrite, which sits between the user and their results and must not inherit the background budget; a per-call `timeout=` argument for anything else. The measured reason: a 4420-char prompt took **46s** on `qwen3.5:2b` on the production host. A 60s cap is not a safety margin on a machine doing CPU inference, it is a coin flip.
- **Connect stays short and independent.** `LLM_CONNECT_TIMEOUT` (10s) so a genuinely dead Ollama fails fast instead of burning the full read budget first. It is a separate argument to `httpx.Timeout`, not the same value.
- **A chat timeout used to write nothing at all.** The request line was logged at WARNING *before* the call and the response line only *after* it returned, with no `except` in between — so a timeout was indistinguishable from a request that never happened, which is why this took a log dive to explain. Every failure mode now logs at ERROR with the model, the prompt size, the elapsed time and the knob to turn, and raises `RuntimeError` naming the remedy. Nothing upstream caught `httpx.ReadTimeout`, so the type change is safe.
- **An empty answer is logged, not raised.** A model that loads and then produces nothing is the memory-pressure shape the user suspected — and it used to return `""` in total silence while callers fell back to the raw query or to no keywords, so the vault quietly got worse. Raising here would break those deliberate fallbacks and turn a degraded search into a failed request, so the fix is the ERROR line, not an exception. Keep it that way: *log* the empty answer, never throw on it.
- **The log carries the content, not just the size.** `prompt_chars=3572` tells you a call was big; it does not tell you what the model was shown or what it said, so a mis-scoped or hallucinating verdict could not be checked without reproducing the call. `_llm_excerpt()` adds `system=`, `prompt=` and `content=` (and the raw `body=`) capped at `LLM_LOG_CHARS` (`MEM_LLM_LOG_CHARS`, default 1000), with `…+N more chars` so the total is still known. It **escapes newlines, tabs and backslashes** — prompts are multi-line by nature, and a raw newline in a `RotatingFileHandler` record makes one event span several lines, at which point `grep` reports a fragment as if it were the whole message. `MEM_LLM_LOG_CHARS=0` restores sizes-only, and that is a privacy switch, not an "unlimited" one: this writes entry and meeting text into the log file. The 1K cap is per field, not per line, so a 40k-char entry cannot put 40k chars on a reclassify line.
- **A 503 must say why.** All 44 `except RuntimeError` handlers in `gui.py` route through `_service_unavailable(e)`, which logs the reason before building the response. The detail used to be formatted into an `HTTPException` and discarded: the access log recorded the status and nothing recorded the cause. "Another maintenance operation is running" and "Ollama timed out" are completely different problems, and the most informative message in the codebase was the one that never got logged. Do not reintroduce a bare `raise HTTPException(status_code=503, detail=str(e))` — `ServiceUnavailableLoggingTests` checks for it.
- **A 503 from a single-item reclassify is usually a lock collision, not a failure.** `reclassify_single_*` holds the per-user maintenance lock for the whole run and releases it in a `finally`. With the windowing above, a reclassify is several LLM calls at tens of seconds each, so a second click is very likely to arrive while the first is still running and get an instant 503 with `"Another maintenance operation (backup/restore/reclassify) is running"`. Read the lock message as "the first one is still going", and check whether it completed before assuming the reclassify failed.

Tests live in `mem-mcp/test_llm_reliability.py`, using the same `ast.get_source_segment` lift as the embedding suite. They are behavioural rather than source-shape checks **on purpose**: the defect was the *absence* of a log line, and only a fake client that actually raises can assert that. Reinjecting the old body makes 17 of the 23 fail and drops the recorded ERROR lines from 1 to 0.

## Gotchas

- Qdrant not accessible from host—interact via app only
- Long timeouts (600s) for LLM operations—don't timeout-hunt
- Collection named `ea_memories` (hardcoded in memory.py)
- **A knob in `.env.example` that is not in the compose `environment:` block does not exist.** Docker Compose passes only the variables listed on a service, so a documented variable that is missing from that block is unreachable: it sits at its code default forever and editing `.env` does nothing. **24 documented variables were in exactly that state**, including `MEM_LLM_TIMEOUT` — shipped in `514e184` as the fix for the hardcoded 60s chat timeout, documented in `.env.example` and Critical Config, and genuinely untunable. Nothing errored: the app started normally, the default applied, and the only symptom was "I changed the env and nothing happened", which is indistinguishable from a caching problem. `test_env_wiring.py` derives the variable set from `.env.example` rather than hardcoding it, so a newly documented knob is covered the moment it is documented. Add a new knob in **both** files, with an inline `${VAR:-default}` so the effective value is visible in one place.
  - **Two `os.getenv` forms, only one of them dangerous.** `os.getenv(NAME, "fallback")` returns the empty string when the variable is present-but-empty, so a bare `${NAME}` in compose defeats the code's own default; `os.getenv(NAME) or "fallback"` treats empty as absent and is safe. The first version of that test asserted every bare interpolation was a crash, which was **false** — `LLM_QUERY_MODEL`, `MEM_SCOPE_MODEL` and `BASE_URL` are all the safe `or` form, and `BASE_URL`'s fallback is literally `""`. The assertion had to be narrowed to the two-argument form with a non-empty fallback before it said anything true.
  - **The two files also fail in opposite directions when a test is copied out of tree.** `test_env_wiring.py` and `test_mobile_layout.py` both resolve their inputs relative to their own location and walk up a directory, so a scratch copy needs the real layout (`mem-mcp/test_*.py` plus `.env.example` one level up). Running the copy in a flat directory produced 6 `FileNotFoundError`s that read exactly like a suite failing on the defect.
- **A substring guard on source is not a guard, and it will pass on the bug.** `ScopeClassificationInputTests` forbids a text prefix slice like `item_text[:1500]`. Written as `assertNotIn("item_text[:", source)` it matched the *docstring I had just written*, which quotes the very slice it forbids — so the guard reported green on the file that contains the bug. It also failed to bite when the bug was genuinely re-injected, because I had checked `slice.lower` when `text[:N]` slices the **upper** bound. A blanket "no numeric prefix slice" rule then failed on the legitimate `kws[:10]`, a deliberate cap on a keyword list. What actually works is an `ast` walk for a `Slice` with `lower is None` and a numeric `upper`, applied only to names in a declared set of text carriers — docstrings are `ast.Constant` and cannot trip it. Related: `assertNotIn` over a whole 2500-line file makes unittest echo the entire file into the failure output; use `assertFalse(needle in src, msg)`. The same applies to `assertIn` over a single *function* — `ResolverInputTests` dumps kilobytes of source unless it uses `assertTrue(needle in segment, msg)`.
- **`assertIn(needle, query)` asserts the needle is somewhere, not that it is where it must be.** This is the same lesson as the substring guard above, in a different place, and it hid a production bug for days: `ClearScopeQueryTests` asserted the batch clear contains `"$ids"`, which the broken query `MATCH (n {id: $ids, userId: $userId})` satisfies exactly. The parameter was present; it was in the wrong clause, on the wrong side of a property test, where a list is compared for equality and therefore matches nothing. Any guard that checks a token appears is checking the token, not the property. Derive the property: the fix was a lint (`ListParameterInPropertyMapTests`) that resolves each `s.run(...)` to the names bound to lists in its enclosing function and fails if one is used as a property-map value.
- **A "guard verified to bite" claim is only true when the failure is the assertion.** My first reinjection of the keyword slice was written at 4-space indent into an 8-space block, so the file raised `IndentationError` and all 38 tests errored — which looks identical to "the guard caught it" in a summary, and proved nothing. Always `ast.parse` the injected file before running the suite, and check the failure names the assertion.
- **These files are not all one line-ending, and a round-trip rewrite will churn them.** `test_matching_regressions.py` is CRLF except for 26 bare-LF lines left by an earlier scripted splice, so a decode → edit → re-encode pass cannot be used on it. The safe forms are the `edit` tool, or a **byte-level insertion** at the tail. Detect with `raw.count(b'\r\n') == raw.count(b'\n')`; if that is false, insert, do not rewrite. And `open(p, 'wb')` truncates *before* the write — one failed write wiped a whole test suite and it came back with `git checkout`.
- **Validate `templates/dashboard.html` JS with `node --check` after any template edit.** `py_compile` and the Python suites structurally cannot see a JS syntax error, and one stray `await` in a non-async function took down the entire panel — `<body onload="init()">` reported `init is not defined` only as a downstream symptom of the script block failing to parse.
- **Cypher cannot be parsed locally** — there is no Neo4j and no driver in this environment, so a syntax error ships to production and surfaces as `neo4j.exceptions.CypherSyntaxError` on first execution. `test_cypher_safety.py` exists because of this: it extracts every Cypher string constant and f-string fragment and lints `FOREACH (v IN <list> | ...)` for a variable referenced inside its own list. A `FOREACH (x IN ... ELSE [x] END | DELETE x)` is a parse error, not a runtime one, and it was the reason every full reclassify aborted on its first call. The suite also pins the scope-clear query's shape. Add to it when you add a query.
  - **Every desktop scroll pane here is a flex item with a zero flex basis**, and the mobile block has to release *all* of them, not the one you happen to be looking at. `flex: 1` (and `flex: 1 1 0` with an explicit `min-height: 0` on `#diary-dates-list`) is basis 0. Stacked, the parent column is `height: auto`, a scroll container is sized from that basis, the parent resolves against zero, and the content renders into a box with no height — no error, nothing to scroll, the tab just looks empty. The fix needs **both** halves, `flex: 0 0 auto` *and* `overflow-y: visible`; either alone reproduces it. This bit twice, one level apart: the memories pane was released and `.diary-main` was not, then `.diary-main` was released and `#diary-dates-list` — the date/search-results list, not a detail pane — was not, so searching returned nothing visible while the entries pane looked fine. The sidebars then become the bounded scroll region (`max-height` + `overflow-y: auto`), giving one scroll area per sidebar rather than a nested one. `test_mobile_layout.py::StackedPaneVisibilityTests` derives the whole class from the stylesheet — any full-width selector that declares vertical scrolling *and* a zero flex basis — so a new tab cannot silently repeat this. The same test class is the worked example of **assert presence before asserting a negative**: an earlier version only checked that `overflow-y: auto` was *absent*, and `_decls` returns `""` for a missing selector, so deleting the rule outright made it pass.
  - **CSS is not validated by anything here, and an inline style beats a media query.** The three page layouts are fixed-width columns — memories `180px + 280px + flex`, diary `220px + flex`, graph `200px + flex` — and each pane sits inside `height: calc(100vh - Npx)` with its own `overflow-y: auto`, so on touch the *page* cannot scroll: you drag inside a pane that may be one line tall. `test_mobile_layout.py` pins the `@media (max-width: 900px)` / `640px` rules that fix both. Four things it is protecting, each of which fails silently:
  - **An inline `style` on `.graph-layout` disables the entire mobile block.** Inline styles win at every specificity, so one re-added attribute reverts the graph to two 200px columns with no error anywhere. The `display: flex` lives in the stylesheet for exactly this reason.
  - **`input, textarea, select { font-size: 16px }` is a functional fix, not a style preference.** Below 16px iOS zooms the viewport on focus and the zoom cannot be undone without a reload, so the form never recovers its layout.
  - **`overflow-x: clip` on `body`, never `hidden`.** `hidden` makes `body` a scroll container, which unsticks the `position: sticky` nav.
  - **`network.on("hold")` is the only way to reach the graph context menu on touch.** `oncontext` is a right-click; vis fires `hold` on long-press. Without the handler, Build Graph mode is unusable on a phone — the menu is the only way to add a neighbour. The `ResizeObserver` alongside it matters for the same reason: vis sizes its canvas once at construction, so stacking the layout leaves the canvas at the old width and clipped, and it must be disconnected in `rebuildNetwork()` before `network.destroy()`.
  - **A control that lives inside a rail that becomes a scroller is not a styling problem.** The client filter is the last flex child of `.tabs`, and the mobile block made `.tabs` a horizontal scroller — so the filter sat after all seven tab buttons, off the right edge, with the scrollbar hidden. It was present in the DOM and fully functional; nothing about restyling the control would have found it. The fix is structural: `.tabs` wraps instead of scrolling, and `.cf-bar` (`order: 99; width: 100%`) takes a row of its own. The second half is the one that is easy to miss — `.cf-panel` is `position: absolute`, so in a scroller it is **clipped to the rail** and opens as a sliver; it needs `position: static` once the rail stops scrolling. `test_mobile_layout.py::ClientFilterOnMobileTests` pins all of it, including a test that the filter is still a child of `.tabs` — if it is ever moved out, the wrapping rules go inert silently.
  - **The graph's own boxes carried inline styles, so the mobile rules for them were inert.** `.graph-sidebar` had `width: 200px` inline (making the block's `width: auto` a no-op) and `#graph-container` had `height: 70vh; min-height: 500px` inline (making its `height: 55vh` rule a no-op) — the *same* trap as `.graph-layout`, which was fixed two years of commits earlier while these two were left behind. vis.js sizes its canvas once from that box, so the rule that never applied was the rule deciding how much room the user has to pan. The mobile fix is `flex: 0 0 auto` plus an explicit `62vh`/`420px`: `flex: 1` is a **zero** flex basis, and in the stacked auto-height column the mobile block creates, that resolves against no available space — the same zero-basis trap as the scroll panes. `LayoutInlineStyleTests` now lints all five layout containers rather than three, and `GraphSizeOnMobileTests` pins the basis and the size.

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

#### A Cypher `RETURN` alias and the key Python reads back are a contract

`db_get_graph` raised `KeyError: 'f'` on **every** call in production, so the
whole graph tab was dead. The two scope passes had been changed to match
`(n:Fact OR n:DiaryEntry)` and return `c, n` / `ctx, n, c`, but the Python kept
reading `cr["f"]` and `xrr["f"]`. The variable had been renamed and the read had
not.

`DiaryScopeSourceTests` passed the whole time, because every fact it asserted
was individually true on the broken file: the query mentions `DiaryEntry`,
nothing reads a `clientId` property, and the edge passes do write into
`diary_scope`. None of that says whether the key the loop reads is a key the
query returns. A source test can check the query and the writes and still miss
the seam between them.

`ReturnAliasTests` closes it: it walks the AST, pairs each `for <var> in <res>`
loop with the `s.run(...)` immediately above it, and asserts that every
`var["key"]` in the loop body appears in that query's `RETURN` clause
(`type(r) as rel_type` -> `rel_type`). When you rename a Cypher variable,
rename the read — and prefer a name that is true of *both* node types: a
variable that can be a Fact **or** a DiaryEntry should not be called `f`.

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
Setup → **🧹 Deduplicate memories** provides a review-first merge workflow. It is a section of the
**Settings** page, not a top-level tab: the tab rail held eight tabs and this one is a maintenance
task, not a daily view. The six controls (`#dedup-category`, `#dedup-threshold`,
`#dedup-max-cluster`, `#dedup-scan-btn`, `#dedup-status`, `#dedup-clusters`) live inside
`#page-setup`'s single `.card`, between the Maintenance and Backup sections.

- Scan a category with a configurable similarity threshold and maximum cluster size.
- Select the records to merge, choose the master, and use the LLM to generate an editable merged title and text draft from only those records.
- Merges require explicit confirmation and use the same ownership validation and recovery marker as the MCP `merge_facts` tool.
- GUI endpoints: `GET /api/duplicates`, `POST /api/duplicates/draft`, and `POST /api/duplicates/merge`.

**Moving a page out of the tab rail has a persistence hazard.** `activeTab` is saved to
`localStorage` under `mem_vault_session_state`, so anyone who left the Deduplicate tab open has
`'deduplicate'` in their saved state forever. A `switchTab` that does
`document.getElementById('page-' + tab).classList.add('active')` then dereferences `null` and takes
the whole page down on load. `switchTab` now resolves the element first and falls back to
`memories` with a `console.warn`. Do not reintroduce the direct dereference, and keep
`renderDuplicateEmptyState()` in the `if (tab === 'setup')` branch — without it the panel is a
blank div until Scan is pressed, which reads as a broken button.

**The merge path is budgeted in two directions, and both bounds were wrong.** `num_predict=900` sat
below what a merge actually spends: nemotron-3-nano:4b used **2,776** tokens for four records, so
the closing brace was never emitted, `re.search(r"\{.*\}")` found nothing, and the only symptom was
a 502. It is now `MERGE_DRAFT_NUM_PREDICT` (4000). In the other direction, `max_cluster` was
validated 2–20 while a 20-cluster of long facts is a **13,123**-token prompt against a 16,332
context — unservable at *any* output budget, so the advertised range was a lie. It is now
`MERGE_MAX_CLUSTER` (12, ~3.5k headroom) and `api_generate_duplicate_draft` refuses an
over-budget selection with a 400 naming the record count and the estimate, instead of letting
Ollama truncate.

- The char/token ratio **drifts**: 3.21 at 6.7k chars, 3.31 at 43.5k. A ratio that is safe on a
  small prompt is not safe on a large one, so `MERGE_PROMPT_CHARS_PER_TOKEN` is 3.0 — rounded
  down, because the guard must over-estimate tokens, not under-estimate them.
- `MERGE_CONTEXT_TOKENS` must track `OLLAMA_CONTEXT_LENGTH` in `docker-compose.yml`. They are
  separate knobs because one is read by Ollama and the other by the app, and a silent divergence
  means the guard protects a context window that is not the real one.
- `MERGE_MAX_CLUSTER` reaches the template via `ctx["MERGE_MAX_CLUSTER"]` in `get_gui`, so the
  input's `max` attribute, the client-side check and the server's 400 cannot drift apart.
- `MergeDraftBudgetTests` and `DedupUnderSetupTests` in `test_cypher_safety.py` pin all of this
  from the AST, including that the budget comparison has no `ast.Constant` on either side (an
  `if estimated > 0` reads like a guard and never fires) and that the guard's line number precedes
  the `get_llm_response` call.

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

- Keywords are extracted **per window over the whole entry** and the union is capped at `KEYWORD_LIMIT` (20, `MEM_KEYWORD_LIMIT`), stored in both Qdrant payload and Neo4j node
- Keywords boost vector search relevance in `diary_search_entries`
- Backfill existing entries: `python mem-mcp/reindex_diary_keywords.py -u <user_id>`
- CLI options: `-f/--force` (re-extract even if keywords exist), `-d/--dry-run`, `-c/--concurrency` (default 3)

**The union is capped; the window count is not.** `KEYWORD_LIMIT` bounds what a
long entry can add, and the truncation keeps the *front* of the list because each
window's prompt asks for the most important keywords first. `KEYWORD_EXTRACT_WARN_WINDOWS`
(6) only warns about LLM cost — capping the window count would reintroduce the
silent tail-drop the windowing exists to prevent.

**The 10-keyword cap was per window, not per entry.** Do not reintroduce a slice
of the *entry* to keep the prompt fast: the old `text[:1500]` meant a 40k
transcription got keywords describing its opening, so a query about anything in
the last thirty pages scored as though the entry had never mentioned it. That is
a silent loss — the boost is simply absent, with no error to explain it. The
entry name is prepended to **every** window, not just the first: a window from the
middle of a long entry has no other way to know which entry it belongs to.
`KeywordWindowTests` asserts on the prompt that reached the model, because a
call-count assertion cannot tell the two implementations apart — both make calls.

### People Candidate Resolution

`resolve_people_candidates()` in `matching_utils.py` binds extracted names to
People records, and it now runs **once per window** of the entry too, unioning
the accepted bindings. It used to send `(content or '')[:2500]`, and this is the
sharpest form of the truncation defect class: the prompt's job is to
disambiguate, so a person discussed on page three had no supporting context and
the model either declined to bind the mention or — worse — bound it to a
different, similarly-named candidate who *was* in the opening. That writes a
**wrong `MENTIONS` edge**, which is a false statement, not a recall loss.

Two properties of this function are load-bearing:

- **The merge is a plain union, and deliberately so.** The candidate set is
  already filtered by `people_match_allowed` and does not vary by window, and
  the original code discarded the LLM's confidence value and kept only accepted
  ids. So there is no cross-window confidence strategy to get right. If a
  confidence value is ever *kept* for ranking, the merge has to grow one.
- **The 0.8 gate applies in every window.** A thin window is not a weaker gate.
  Note `bool` is an `int` subclass, so `confidence >= 0.8` accepts `True` unless
  it is excluded explicitly — the code does.

`matching_utils.py` keeps **zero app imports** (`os`, `re`, `json`, `difflib`
only) so it can be imported without the DB drivers and tested directly. Do not
add a `logger` from `common` for the per-window failure paths: a failed window
is skipped silently, as the whole call used to be, and the union means the
surviving windows still apply. `ResolverInputTests` pins the import set.

Because the resolver is pure and importable, its tests **call** it rather than
reading its source. That is the reason it is worth testing behaviourally: a
truncation is invisible to a call-count assertion, so the tests have to inspect
what the prompt actually contained and which bindings survived it.


### Diary People Extraction

Diary people extraction runs the **whole entry**, in overlapping windows, not a prefix. `_extract_people_names()` in `diary_manager.py` iterates `people_extract_windows(content)` and unions the results.

The prefix form is the one to avoid. It sent `content[:2000]`, so on a 40k-char transcription every person named after character 2000 was silently missed — no error, just a missing `MENTIONS` edge nobody was looking for. Keyword extraction (above) now windows the same way, so the two paths agree on what "the entry" means.

- `people_extract_windows(content, window=0, overlap=0)` is pure and unit-tested (`test_people_extraction.py`): blank → `[]`, a doc that fits the window → one element (short entries cost what they always did), otherwise `body[i:i+size] for i in range(0, len(body), size - overlap)`.
- The overlap exists because a name straddling a boundary is cut in half, and both halves then look like garbage to the extractor. `step` is clamped to `size - 1` so a degenerate overlap cannot make the loop non-terminating.
- `MEM_PEOPLE_WINDOW` (6000) and `MEM_PEOPLE_OVERLAP` (600) tune it.
- **Do not cap the window count.** Capping would reintroduce exactly the silent tail-drop the windowing fixed. Above `PEOPLE_EXTRACT_WARN_WINDOWS` (6) the entry is expensive (one LLM call per window) and logs a WARNING naming the knob instead. This is chat traffic, so WARNING is the right level per the logging policy in "Embedding Reliability".
- Each window is wrapped in its own `try/except` inside the loop, so one bad window cannot discard the names the other windows found. `clean_extracted_people_names(found)` dedupes the union, since a name spanning the overlap is found twice.