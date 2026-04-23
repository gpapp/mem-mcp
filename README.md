# Memory MCP — Memory Vault

A self-hosted MCP (Model Context Protocol) server that gives AI agents a **persistent memory layer** using vector embeddings and a Knowledge Graph. Includes an editable web dashboard.

## Architecture

```
mem-mcp/
├── server.py       # Entry point – starts MCP + GUI
├── memory.py       # Shared library: config, DB clients, CRUD helpers
├── mcp_tools.py    # FastMCP tool definitions (MCP server)
├── gui.py          # FastAPI web app: REST API + SPA dashboard
├── requirements.txt
└── Dockerfile
```

| Concern | File |
|---|---|
| Configuration & DB clients | `memory.py` |
| Embedding & CRUD (single source of truth) | `memory.py` |
| MCP tools for AI agents | `mcp_tools.py` |
| REST API for the GUI | `gui.py` |
| Startup / orchestration | `server.py` |

## Features

- **Semantic Memory** — store and search facts by vector similarity (Qdrant)
- **Knowledge Graph** — facts grouped by category in Neo4j
- **Diary** — narrative entries with Markdown and semantic search
- **Editable Dashboard** — create, edit, and delete memories from the browser
- **Multi-user** — per-user isolation via Basic-Auth or proxy headers
- **Consistent Indexing** — every write keeps Qdrant and Neo4j in sync atomically

## Ports

| Service | Port | Path |
|---|---|---|
| Web GUI + REST API | 8080 | `/gui`, `/api/*` |
| MCP server (HTTP) | 8000 | `/mcp` |

## Authentication & User Isolation

User identity is resolved from the incoming request in this priority order:

1. `Authorization: Basic <base64 user:pass>` — username extracted from the credential
2. Proxy headers: `Remote-User`, `X-Remote-User`, `X-User`, `X-Forwarded-User`
3. Falls back to `"anonymous"`

> Both the MCP server and the GUI use the **same extraction logic** (`memory.extract_user_from_headers`).  
> The GUI reads it from FastAPI `Request` headers; the MCP tools use `fastmcp.server.dependencies.get_http_headers()`.

## Tech Stack

| Component | Technology |
|---|---|
| MCP framework | [FastMCP](https://github.com/jlowin/fastmcp) |
| Vector DB | [Qdrant](https://qdrant.tech/) |
| Graph DB | [Neo4j](https://neo4j.com/) |
| Embeddings / LLM | [Ollama](https://ollama.ai/) |
| Web framework | FastAPI + Uvicorn |
| HTTP client | httpx |

## Quick Start (Docker)

### 1. Configure secrets

```bash
cp .env.example .env
# Edit .env and set NEO_PASS to a strong password
```

### 2. Start all services

```bash
docker-compose up -d
```

### 3. Pull an embedding model (first run)

```bash
docker exec ollama ollama pull nomic-embed-text
```

The dashboard is available at **http://localhost:8086/gui**.  
The MCP endpoint is at **http://localhost:8087/mcp** (mapped from port 8000 inside the container).

## Local Development

```bash
cd mem-mcp
pip install -r requirements.txt
# Set environment variables (see .env.example), then:
python server.py
```

## MCP Tools

### Memory tools

| Tool | Description |
|---|---|
| `mem_add_memory` | Save a new fact (text + category) |
| `mem_update_memory` | Update text / category of an existing fact by ID |
| `mem_forget_memory` | Delete a fact from both vector and graph stores |
| `mem_search_memories` | Vector-similarity search across memories |

### Knowledge graph tools

| Tool | Description |
|---|---|
| `vault_save_fact` | Alias for `mem_add_memory` with mandatory category |
| `vault_get_category_summary` | List all facts under a given category |

### Diary tools

| Tool | Description |
|---|---|
| `diary_save_entry` | Create/update a Markdown diary entry for a date |
| `diary_search_entries` | Semantic search across diary entries |

## REST API (GUI backend)

All paths are relative — the GUI uses `./api/*` so it works behind any reverse proxy.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/memories` | List all memories for current user |
| `POST` | `/api/memories` | Create a memory `{text, category}` |
| `PUT` | `/api/memories/{id}` | Update a memory `{text, category}` |
| `DELETE` | `/api/memories/{id}` | Delete a memory |
| `GET` | `/api/categories` | List distinct category names |
| `GET` | `/api/diary` | List all diary entries |
| `POST` | `/api/diary` | Save a diary entry `{content, date?}` |
| `GET` | `/api/whoami` | Returns `{user}` for the current request |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MEM_QDRANT_URL` | `http://qdrant:6333` | Qdrant endpoint |
| `MEM_NEO4J_URL` | `bolt://neo4j:7687` | Neo4j Bolt endpoint |
| `MEM_NEO4J_USER` | `neo4j` | Neo4j username |
| `MEM_NEO4J_PASSWORD` | `password` | Neo4j password |
| `MEM_LLM_URL` | `http://ollama:11434` | Ollama base URL |
| `MEM_EMBEDDER_MODEL` | `nomic-embed-text` | Ollama embedding model name |
| `HTTPX_TIMEOUT` | `600.0` | HTTP timeout in seconds |
