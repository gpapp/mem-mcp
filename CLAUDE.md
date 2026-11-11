# CLAUDE.md

This file is a short orientation for AI assistants. **`AGENTS.md` is the source
of truth** — it is long, it is specific, and it documents the reason behind each
rule. Read it before changing behaviour in any area it covers.

## What this is

A self-hosted MCP server that gives agents and people a persistent memory layer
over vector embeddings (Qdrant) and a knowledge graph (Neo4j), with an editable
web dashboard on the same process.

| Component | Port | Notes |
|---|---|---|
| App | 8086 (Docker), 8080 (internal) | GUI `/gui`, MCP `/mcp`, REST `/api/*` |
| Qdrant | 6333 | Internal only |
| Neo4j | 7687 | Internal only |
| Ollama | 11434 | Internal only |
| nginx | `/mem-mcp` | Proxy mount point — include in every URL |

## Architecture

```
mem-mcp/
├── server.py             # Lifespan, middleware order, uvicorn entry
├── memory.py             # The fan-out module: re-exports every manager
├── common.py             # Config, Ollama chat/embed, Neo4j driver, graph lock
├── fact_manager.py       # :Fact CRUD, search, merge, consistency, chunks
├── diary_manager.py      # :DiaryEntry CRUD, keyword + people extraction
├── client_manager.py     # :Client / :Context, scope, resolution
├── action_manager.py     # The :ActionItem register
├── decision_manager.py   # The :Decision register and its supersession links
├── deliverable_manager.py # The :Deliverable register: what was shipped, in what state
├── timeline_manager.py   # One engagement's story: meetings, decisions, closed items, facts
├── roster_manager.py     # Stakeholder roster (People facts, mentions, last-seen)
├── brief_manager.py      # Assembles a brief's material, then has the model polish it
├── matching_utils.py     # Pure matching, merge, scope, date/action parsing
├── roster.py             # Stdlib-only: role parsing, staleness, roster Markdown
├── timeline.py           # Stdlib-only: the merge, month grouping and ordering rules
├── deliverables.py       # Stdlib-only: kind inference, status markers, ordering
├── briefing.py           # Stdlib-only: the meeting-prep brief's layout rules
├── skill_catalogue.py    # Stdlib-only: reads skills/, front matter, descriptions
├── gui.py                # Every HTTP route, the live-notes WS proxy
├── mcp_tools.py          # The MCP tools
├── mcp_skills.py         # MCP skills, resources and prompts
├── mcp_logging.py        # MCP tool call logging/monitoring
├── sessions.py           # Sessions, credentials, access keys (stdlib-only)
├── backup.py             # Savepoints, restore, vault fingerprint
├── status_monitor.py     # Ollama status poller + SSE fan-out (stdlib-only)
├── live_notes.py         # Live-notes prompts, gate, budgets (stdlib-only)
├── live_manager.py       # Transcript/segment CRUD, consumption markers, the note job runner
├── sharing.py, audit.py  # Project/context sharing and its audit trail
├── chunking.py, scoped_ids.py, google_auth.py
├── model_bench.py        # CLI: every chat model against every role's real prompt (stdlib-only)
├── search_diagnostics.py, search_batch_test.py, search_report.py
                        # CLI: trace one query, run a fixed query suite, render the log
├── migrate_client_context.py  # One-time migration: Client/Project facts to nodes
├── migrate_vault_user.py      # CLI: hand one user's whole vault to another
├── reindex_chunks.py, reindex_diary_keywords.py  # CLI: backfill chunking / keywords
├── skills/               # Markdown workflows, discovered from the directory
├── templates/            # dashboard.html and the live-notes worklet
└── test_*.py             # 28 dependency-light regression suites
```

Only the stdlib-only modules are importable on a machine with no fastapi, no
httpx and no database driver. That is why the pure rules live in them.

## Authentication — two gates, different credentials

| gate | accepts | covers |
|---|---|---|
| `/mcp` | **only** `Authorization: Bearer mvk_…` | every route fastmcp registers |
| `/gui`, `/api/*` | session cookie → verified `Basic` → `Bearer mvk_…` | ~60 handlers |
| `/api/auth/*` | nothing, by design | login, logout, signup, OAuth |

**Do not reintroduce header-based identity.** `_user` reads `session["user"]`
and `request.state.user` only. Proxy headers (`X-Remote-User`) and a decoded
`Basic` username authenticate nothing — that model was removed on purpose, and
`AGENTS.md` → Authentication explains each hole it left.

## Development

```bash
cp .env.example .env          # set MEM_NEO4J_PASSWORD
docker compose up -d
docker exec ollama ollama pull nomic-embed-text
docker compose up -d --build mem-mcp
```

Run the suites (no database needed, from `mem-mcp/`):

```bash
python3 -m unittest test_matching_regressions.py test_embedding_reliability.py \
  test_chunking.py test_cypher_safety.py test_backup_compression.py \
  test_people_extraction.py test_graph_scope.py test_mobile_layout.py \
  test_llm_reliability.py test_env_wiring.py test_status_monitor.py \
  test_sessions.py test_auth_guard.py test_google_auth.py test_registration.py \
  test_vault_migration.py test_sharing.py test_share_endpoints.py \
  test_sharing_readpaths.py test_live_notes.py test_skill_catalogue.py \
  test_action_items.py test_briefing.py test_roster.py test_model_bench.py \
  test_decision_log.py test_timeline.py test_deliverables.py
```

Before committing: `python3 -m py_compile <file>.py` for Python, `node --check`
on the extracted dashboard `<script>` for JavaScript, and
`git -c core.whitespace=cr-at-eol diff --check`.

The container is Python **3.11**; the editing host may be newer. Anything legal
only on 3.12+ (PEP 695 `type X = …`, `itertools.batched`) imports fine here and
breaks the container.

## Task tracking

- `tasks.md` — **open work only**, with acceptance criteria.
- `task_history.md` — completed work, newest first, naming the commit.

When a task finishes, move it: write the entry into `task_history.md`, delete
the section from `tasks.md`.
