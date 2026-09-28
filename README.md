# Memory MCP — Memory Vault

A self-hosted MCP (Model Context Protocol) server that gives AI agents a **persistent memory layer** using vector embeddings and a Knowledge Graph. Includes an advanced editable web dashboard with insights and relationship tracking.

## Architecture

```
mem-mcp/
├── server.py              # Entry point – unified FastAPI server (Port 8080)
├── memory.py              # Facade: re-exports from common, fact_manager, diary_manager, client_manager
├── common.py              # Config, DB clients (Qdrant, Neo4j, Ollama), helpers
├── fact_manager.py        # Fact CRUD, search, dedup, graph operations
├── diary_manager.py       # Diary CRUD, search, keyword extraction, consistency
├── client_manager.py      # Client/Context nodes, scope inference, status management
├── migrate_client_context.py  # One-time migration: Client/Project facts → Client/Context nodes
├── mcp_tools.py           # FastMCP tool definitions
├── mcp_skills.py          # MCP prompts and resource definitions for skills
├── mcp_logging.py         # MCP tool call logging/monitoring
├── gui.py                 # FastAPI web app: REST API, Landing Page, and Dashboard
├── matching_utils.py      # Dependency-light matching, merge, and LLM candidate helpers
├── test_matching_regressions.py  # Focused regression and fake-boundary tests
├── reindex_diary_keywords.py  # CLI tool to backfill keyword extraction for existing diary entries
├── requirements.txt
├── Dockerfile
└── .dockerignore
```

## Features

- **Semantic Memory** — Store and search facts by vector similarity using Qdrant.
- **Knowledge Graph** — Facts are linked in Neo4j, enabling relationship tracking and graph traversal.
- **Advanced Metadata** — Facts support rich JSON metadata (tags, source, confidence, etc.).
- **Knowledge Patterns** — Automatically identifies recurring themes and associations via graph analysis.
- **Diary** — Narrative entries with Markdown support, LLM-powered keyword extraction, and vector similarity search.
- **Smart Search** — LLM query rewriting (qwen3.5:0.8b) decomposes natural language into keyword phrases; multi-query expansion merges results from multiple vector searches.
- **Memory Deduplication** — Scope-aware multi-signal similarity clustering (vector, name, alias, email) with weighted fuzzy evidence, core-member filtering, and a guided merge workflow.
- **LLM-Assisted Resolution** — LLMs review bounded People and duplicate candidates with diary, scope, metadata, and record context; returned IDs are validated before use.
- **Recoverable Merges** — Neo4j/Qdrant merge cleanup records pending Qdrant deletions and retries them through startup orphan reconciliation when a store is temporarily unavailable.
- **Dashboard Deduplication** — The Deduplicate tab scans scope-compatible clusters, lets you select the records to merge, generates an editable consolidated draft with the LLM, and requires confirmation before merging.
- **Dedicated Merge Model** — Dashboard merge drafts use `MEM_MERGE_MODEL` (default `gemma4:e2b`), while search and classification remain on the lightweight query model.
- **Skills System** — Pluggable skill workflows (e.g., `process-transcription`, `memory-deduplication`) loaded from Markdown files.
- **Unified Web UI** — A modern, proxy-aware dashboard to manage memories, view diary history, and explore insights.
- **Multi-user Isolation** — Secure per-user vaults based on Basic-Auth or proxy headers.
- **Client & Context Scoping** — Facts and diary entries can be scoped to a Client (e.g. "Deutsche Bank") and a Context within it (e.g. "SAP Implementation") via `FOR_CLIENT` / `IN_CONTEXT` graph links. Explicit `client`/`context` parameters hard-filter search; global search still finds everything.
- **Scope Inference** — When no explicit client is passed, search infers the client from the query text (boost-only, never filters).
- **Inactive Client Handling** — Clients untouched for 90 days are auto-deprioritized (−0.15 score, never hidden). Status can be manually pinned via `set_client_status`, `PUT /api/clients/{id}`, or the Setup-tab toggle; pinned clients are never auto-changed.
- **Setup Tab** — Client list with active/inactive toggles, plus MCP connection details.
- **Daily Backups & Restore** — An in-process scheduler writes a vault-wide savepoint (graph + both vector collections) each night, keeps the last `MEM_BACKUP_KEEP`, and Setup → Maintenance → Backup & Restore can take one immediately or restore any of them.
- **Long-Record Chunking** — A fact or diary entry that runs past the embedding budget is indexed as several vectors, so a query about a detail in the middle of a 40k-char transcription can still find the record. Search collapses a chunk family back to one result and returns the full text.
- **Lazy-Loaded Lists** — Memories and diary sidebar render in batches of 50 with infinite scroll; calendar has a 📅 jump-to-today button.

## Ports & Access

The server is **unified** on port **8080** (mapped to **8086** in Docker).

| Component | Path | Description |
*   **Landing Page** | `/` | Onboarding, MCP setup instructions, and auto-detected credentials.
*   **Web Dashboard** | `/gui` | The main interactive dashboard (Memories, Diary, Graph, Setup).
*   **MCP Endpoint** | `/mcp` | The Model Context Protocol entry point for AI clients.
*   **REST API** | `/api/*` | Backend endpoints used by the GUI.

## Authentication

User identity is resolved automatically from:
1. `Authorization: Basic <base64 user:pass>`
2. Proxy headers: `Remote-User`, `X-Remote-User`, `X-User`, `X-Forwarded-User`

## MCP Tools (Advanced Suite)

| Tool | Description |
|---|---|
| `add_fact` | Store a new fact with optional category, rich metadata, and client/context scope (auto-created). |
| `search_facts` | Semantic search for facts with optional category and client/context scoping plus LLM query rewriting. |
| `list_clients` | List all clients with their contexts for the current user. |
| `create_client` | Explicitly create a new client (idempotent). |
| `set_client_status` | Pin a client active/inactive (pinned status is never auto-changed). |
| `link_facts` | Create semantic relationships (e.g., `WORKS_ON`) between two facts or diary entries. |
| `unlink_facts` | Remove a relationship between two facts or diary entries. |
| `get_fact_neighborhood`| Traverse the knowledge graph around a fact (context exploration). |
| `update_fact` | Partial updates to text, category, or metadata of existing facts. |
| `delete_fact` | Remove a fact from both vector and graph stores. |
| `list_categories` | List all distinct categories currently used in the vault. |
| `find_patterns` | Discover recurring themes and category clusters in the graph. |
| `find_duplicates` | Find potential duplicate entries using multi-signal similarity clustering. |
| `suggest_merge` | Analyze a cluster of duplicates and suggest a master record for merging. |
| `merge_facts` | Execute a merge: update master, move relationships, delete duplicates. |
| `diary_save_entry` | Create/update a narrative diary entry with automatic keyword extraction and optional client/context scope. |
| `diary_search_entries` | Semantic search across diary entries with keyword boosting and optional client/context scoping. |
| `list_diary_entries` | List diary entries within an optional time range. |
| `diary_delete_entry` | Delete a diary entry by ID. |
| `find_skills` | Scan the skills directory and list available skill workflows. |
| `get_skill_workflow` | Retrieve the detailed Markdown workflow for a specific skill. |

## Quick Start (Docker)

1. **Configure secrets**: `cp .env.example .env` and set `MEM_NEO4J_PASSWORD`.
2. **Launch (CPU)**: `docker-compose up -d`
3. **Launch (GPU)**: `docker-compose -f docker-compose.yml -f docker-compose.gpu.yml up -d` (requires NVIDIA driver + NVIDIA Container Toolkit)
4. **Initialize Embedder**: `docker exec ollama ollama pull nomic-embed-text`
5. **Initialize Query LLM**: `docker exec ollama ollama pull qwen3.5:0.8b`
6. **Initialize Merge LLM**: `docker exec ollama ollama pull gemma4:e2b`
7. **After code changes**: rebuild the image with `docker-compose up -d --build mem-mcp` (dependency downloads are cached by BuildKit, so this is fast after the first build)

Visit **http://localhost:8086/** for the interactive setup guide.

## Testing

Run the focused dependency-light regression suite from the repository root:

```powershell
Push-Location mem-mcp
C:/tools/miniconda3/python.exe -m unittest -v test_matching_regressions.py test_embedding_reliability.py
Pop-Location
```

The suites cover scope-aware duplicate matching, weighted scoring, bridge-cluster rejection, merge target ownership, merge mutation ordering, People candidate resolution, scope-name and `Client:`-header resolution, LLM prompt contracts, and the embedding retry/fallback behaviour. They do not require the Docker services.

## Backups

The vault writes a **savepoint** every day and keeps the most recent
`MEM_BACKUP_KEEP` of them. Manage them from **Setup → Maintenance → Backup & Restore**.

- A savepoint holds the whole vault: the graph plus both vector collections. It
  lives in `mem-mcp-data/backup/` on the host (bind-mounted to `/app/backup`).
- **Back up now** takes an immediate one instead of waiting for the nightly slot.
- **Restore** replaces all current data with the selected savepoint. Take a fresh
  savepoint first if you have recent work you want to keep.
- Restoring a savepoint that did not finish is not possible — incomplete
  savepoints are listed as *Unusable*.

Schedule and retention are controlled in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `MEM_BACKUP_ENABLED` | `1` | `0` disables the nightly run; manual backups still work |
| `MEM_BACKUP_HOUR` / `MEM_BACKUP_MINUTES` | `3` / `0` | Local server time of the nightly run |
| `MEM_BACKUP_KEEP` | `14` | How many completed savepoints to keep |
| `MEM_BACKUP_DIR` | `<dirname LOG_DIR>/backup` | Where savepoints are written |

Savepoints are copies, not a live mirror — if the host disk is lost, they are
lost with it. Copy `mem-mcp-data/backup/` off the machine if you need a
disaster-recovery copy.

## Embedding Reliability

Every fact and diary entry is embedded through a single choke point. Ollama is
flakier than the rest of the stack — a model still loading, a GPU under memory
pressure, a container mid-restart — so the client retries transient failures and
tries both the legacy `/api/embeddings` and the modern `/api/embed` route.

If it still cannot embed, the save is rejected **before** anything is written, so
a failed embedding never leaves a record without a vector. The error names the
model and the reason Ollama gave, instead of a bare `500`.

| Variable | Default | Meaning |
|---|---|---|
| `MEM_EMBED_RETRIES` | `2` | Retries per route for a transient failure |
| `MEM_EMBED_RETRY_BACKOFF` | `1.5` | Seconds multiplied by the attempt number |
| `MEM_EMBED_MAX_CHARS` | `12000` | Character budget per embedding |

A missing model is not retried — the error tells you to run
`docker exec ollama ollama pull <model>`.

Ollama serves embedding models with a smaller context window than the model
advertises, so a long fact or diary entry can exceed it — and Ollama reports
that as a `500`, not a `413`. Text over the budget is truncated before sending
(the beginning and the end are kept, since that is where the subject and the
conclusions live), and if Ollama still rejects the length the input is halved
and retried. That case is never retried with backoff, because re-sending the
same oversized text is what made the first two production failures take five
requests each and still fail.

### Log levels

Embedding is the high-volume path, so it does not log at `WARNING` per call.
At `LOG_LEVEL=WARNING` (the production setting) you get:

- `WARNING` — the input was over budget and got truncated, which means the
  stored vector is a lossy summary of a long record. Rare, and worth knowing.
- `ERROR` — the embed failed. Logged once, with Ollama's own reason and the
  `ollama pull` command to fix it. Nothing is written in this case.
- `DEBUG` — every individual attempt and retry, if you turn the level down.

`WARNING` otherwise belongs to the chat/LLM traffic, which is what you are
usually looking for.

## Long Records & Chunking

One vector for a 40k-char transcription is a lossy average of the whole thing: a
query about a decision in the middle scores poorly against it and against
everything else. Truncating to fit the embedding window bounds the damage but
discards the middle entirely. So a record that runs past the budget is indexed
as **several vectors**, one per chunk.

- A short record is stored exactly as before — one point, and nothing to
  migrate. Only records that exceed the budget get chunked.
- A long record becomes N points sharing the record's category, client, context
  and keywords, so filtering behaves identically whichever chunk matched.
- Search collapses a family to its best chunk, so a record still appears once,
  and the full text is returned regardless of which chunk matched.
- Updating a record replaces the whole family. Shortening the text removes the
  now-stale trailing chunks.

| Variable | Default | Meaning |
|---|---|---|
| `MEM_CHUNK_CHARS` | `3000` | Target characters per chunk |
| `MEM_CHUNK_MAX` | `16` | Embedding calls per save (a cost budget, not a coverage cap — the chunk size grows to keep every part covered) |
| `MEM_CHUNK_OVERLAP` | `200` | Characters repeated between neighbouring chunks so a sentence on a boundary is not lost |
| `MEM_CHUNK_FETCH_MULTIPLIER` | `3` | Widens the vector result window, since one record can now occupy several slots |

Existing long records are converted with:

```bash
python mem-mcp/reindex_chunks.py --dry-run   # report only, writes nothing
python mem-mcp/reindex_chunks.py             # rewrite the long ones
```

The script is idempotent — it skips anything already chunked — and each record
costs one embedding call per chunk, so start with the dry run.

## Claude Desktop Setup

Run this command to add the vault to your Claude configuration:
```bash
claude mcp add --transport http memory-vault http://<your-host>:8086/mcp --header "Authorization: Basic <base64-creds>"
```
*(Copy your pre-filled command directly from the landing page!)*

## Tech Stack

- **Frameworks**: FastAPI, FastMCP
- **Databases**: Qdrant (Vector), Neo4j (Graph)
- **AI/ML**: Ollama (nomic-embed-text for embeddings, qwen3.5:0.8b for query rewriting and keyword extraction)
- **Frontend**: Vanilla JS, Modern CSS3
