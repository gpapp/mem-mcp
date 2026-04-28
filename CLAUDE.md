# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## High-Level Architecture & Structure

This repository is `mem-mcp`, a self-hosted Model Context Protocol (MCP) server that provides a persistent semantic memory layer for AI agents using vector embeddings (Qdrant) and a Knowledge Graph (Neo4j). 

- **Unified Server**: The application is a unified FastAPI server exposing a web GUI, REST API, and an MCP endpoint, all running on the same port (8080 by default).
- **Core Components** (`mem-mcp/` directory):
  - `server.py`: The entry point that mounts the web app, MCP server, and handles routing and CORS middleware.
  - `memory.py`: The core shared library containing database clients (Qdrant, Neo4j, Ollama), configuration loading from environment variables, and the logic for graph traversal and vector searches.
  - `mcp_tools.py`: Contains the tool definitions utilizing `FastMCP` (e.g., `create_fact`, `search_facts`, `link_facts`).
  - `gui.py`: The FastAPI web app handling the Landing Page (`/`), Web Dashboard (`/gui`), and REST API endpoints (`/api/*`).
- **Multi-user Isolation**: Operations automatically resolve the user vault dynamically from headers (`Authorization: Basic` or proxy headers like `X-Remote-User`). 
- **Infrastructure**: Dependencies (Neo4j, Qdrant, Ollama) are orchestrated via Docker Compose (`docker-compose.yml`).

## Common Development Commands

### Environment Setup
We use `uv` and Python virtual environments as specified in global instructions.

```powershell
cd mem-mcp
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
```

### Running Infrastructure (Databases & Embedder)
Before starting the application, the local infrastructure must be running:

```powershell
# Copy environment template if you haven't
Copy-Item .env.example .env

# Start Qdrant, Neo4j, Ollama
docker-compose up -d

# Initialize the embedder in Ollama
docker exec ollama ollama pull nomic-embed-text
```

### Starting the Server for Development
To run the server locally outside of Docker for easier debugging (requires infrastructure from `docker-compose` to be running):

```powershell
cd mem-mcp
.\.venv\Scripts\Activate.ps1
python server.py
```
*(Ensure `.env` values like `MEM_NEO4J_PASSWORD` are loaded into your environment, or rely on `python-dotenv` if added.)*

Alternatively, build and run everything in Docker:
```powershell
docker-compose up --build -d
```

### Viewing Logs
```powershell
docker-compose logs -f mem-mcp
```