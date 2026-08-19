# Memory MCP — Memory Vault

A self-hosted MCP (Model Context Protocol) server that gives AI agents a **persistent memory layer** using vector embeddings and a Knowledge Graph. Includes an advanced editable web dashboard with insights and relationship tracking.

## Architecture

```
mem-mcp/
├── server.py              # Entry point – unified FastAPI server (Port 8080)
├── memory.py              # Facade: re-exports from common, fact_manager, diary_manager
├── common.py              # Config, DB clients (Qdrant, Neo4j, Ollama), helpers
├── fact_manager.py        # Fact CRUD, search, dedup, graph operations
├── diary_manager.py       # Diary CRUD, search, keyword extraction, consistency
├── mcp_tools.py           # FastMCP tool definitions
├── mcp_skills.py          # MCP prompts and resource definitions for skills
├── mcp_logging.py         # MCP tool call logging/monitoring
├── gui.py                 # FastAPI web app: REST API, Landing Page, and Dashboard
├── reindex_diary_keywords.py  # CLI tool to backfill keyword extraction for existing diary entries
├── requirements.txt
└── Dockerfile
```

## Features

- **Semantic Memory** — Store and search facts by vector similarity using Qdrant.
- **Knowledge Graph** — Facts are linked in Neo4j, enabling relationship tracking and graph traversal.
- **Advanced Metadata** — Facts support rich JSON metadata (tags, source, confidence, etc.).
- **Knowledge Patterns** — Automatically identifies recurring themes and associations via graph analysis.
- **Diary** — Narrative entries with Markdown support, LLM-powered keyword extraction, and vector similarity search.
- **Smart Search** — LLM query rewriting (qwen3.5:0.8b) decomposes natural language into keyword phrases; multi-query expansion merges results from multiple vector searches.
- **Memory Deduplication** — Multi-signal similarity clustering (vector, name, alias, email) with guided merge workflow.
- **Skills System** — Pluggable skill workflows (e.g., `process-transcription`, `memory-deduplication`) loaded from Markdown files.
- **Unified Web UI** — A modern, proxy-aware dashboard to manage memories, view diary history, and explore insights.
- **Multi-user Isolation** — Secure per-user vaults based on Basic-Auth or proxy headers.

## Ports & Access

The server is **unified** on port **8080** (mapped to **8086** in Docker).

| Component | Path | Description |
*   **Landing Page** | `/` | Onboarding, MCP setup instructions, and auto-detected credentials.
*   **Web Dashboard** | `/gui` | The main interactive dashboard (Memories, Diary, Insights).
*   **MCP Endpoint** | `/mcp` | The Model Context Protocol entry point for AI clients.
*   **REST API** | `/api/*` | Backend endpoints used by the GUI.

## Authentication

User identity is resolved automatically from:
1. `Authorization: Basic <base64 user:pass>`
2. Proxy headers: `Remote-User`, `X-Remote-User`, `X-User`, `X-Forwarded-User`

## MCP Tools (Advanced Suite)

| Tool | Description |
|---|---|
| `add_fact` | Store a new fact with optional category and rich metadata. |
| `search_facts` | Semantic search for facts with optional category filtering and LLM query rewriting. |
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
| `diary_save_entry` | Create/update a narrative diary entry with automatic keyword extraction. |
| `diary_search_entries` | Semantic search across diary entries with keyword boosting. |
| `list_diary_entries` | List diary entries within an optional time range. |
| `diary_delete_entry` | Delete a diary entry by ID. |
| `find_skills` | Scan the skills directory and list available skill workflows. |
| `get_skill_workflow` | Retrieve the detailed Markdown workflow for a specific skill. |

## Quick Start (Docker)

1. **Configure secrets**: `cp .env.example .env` and set `MEM_NEO4J_PASSWORD`.
2. **Launch**: `docker-compose up -d`
3. **Initialize Embedder**: `docker exec ollama ollama pull nomic-embed-text`
4. **Initialize Query LLM**: `docker exec ollama ollama pull qwen3.5:0.8b`
5. **After code changes**: rebuild the image with `docker-compose up -d --build mem-mcp`

Visit **http://localhost:8086/** for the interactive setup guide.

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
