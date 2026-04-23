# Memory MCP — Memory Vault

A self-hosted MCP (Model Context Protocol) server that gives AI agents a **persistent memory layer** using vector embeddings and a Knowledge Graph. Includes an advanced editable web dashboard with insights and relationship tracking.

## Architecture

```
mem-mcp/
├── server.py       # Entry point – unified FastAPI server (Port 8080)
├── memory.py       # Shared library: config, DB clients, Graph + Vector logic
├── mcp_tools.py    # FastMCP tool definitions (Advanced Schema)
├── gui.py          # FastAPI web app: REST API, Landing Page, and Dashboard
├── requirements.txt
└── Dockerfile
```

## Features

- **Semantic Memory** — Store and search facts by vector similarity using Qdrant.
- **Knowledge Graph** — Facts are linked in Neo4j, enabling relationship tracking and graph traversal.
- **Advanced Metadata** — Facts support rich JSON metadata (tags, source, confidence, etc.).
- **Knowledge Patterns** — Automatically identifies recurring themes and associations via graph analysis.
- **Diary** — Narrative entries with Markdown support and a sleek master-detail sidebar layout.
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
| `create_fact` | Store a new fact with optional category and rich metadata. |
| `search_facts` | Semantic search for facts with optional category filtering. |
| `link_facts` | Create semantic relationships (e.g., `works_on`) between two facts. |
| `get_fact_neighborhood`| Traverse the knowledge graph around a fact (context exploration). |
| `update_fact` | Partial updates to text, category, or metadata of existing facts. |
| `delete_fact` | Remove a fact from both vector and graph stores. |
| `find_patterns` | Discover recurring themes and category clusters in the graph. |
| `diary_save_entry` | Create/update a narrative diary entry for a specific date. |
| `diary_search_entries` | Semantic search across the personal journal. |

## Quick Start (Docker)

1. **Configure secrets**: `cp .env.example .env` and set `MEM_NEO4J_PASSWORD`.
2. **Launch**: `docker-compose up -d`
3. **Initialize Embedder**: `docker exec ollama ollama pull nomic-embed-text`

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
- **AI/ML**: Ollama (nomic-embed-text)
- **Frontend**: Vanilla JS, Modern CSS3
