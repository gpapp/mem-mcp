# AGENTS.md

## Dev Commands

```powershell
# Setup
cp .env.example .env
# Edit .env: set MEM_NEO4J_PASSWORD and NEO_PASS

# Infra (must run first)
docker-compose up -d
docker exec ollama ollama pull nomic-embed-text

# Local dev (infra must be running)
.\.venv\Scripts\Activate.ps1
python mem-mcp\server.py
```

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
- Embedder: pull `nomic-embed-text` into Ollama container (docker-compose uses `nomic-ea` by default)
- User vault resolved from `Authorization: Basic` header or session cookie
- `BASE_URL` must include `/mcp` prefix when behind nginx

## Gotchas

- Qdrant not accessible from host—interact via app only
- Long timeouts (600s) for LLM operations—don't timeout-hunt
- Collection named `ea_memories` (hardcoded in memory.py)