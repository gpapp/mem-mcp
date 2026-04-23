# Memory MCP (Memory Vault)

A Model Context Protocol (MCP) server that provides a persistent memory layer for AI agents using vector embeddings (Qdrant) and a Knowledge Graph (Neo4j).

## Features
- **Semantic Memory**: Store and search facts using vector similarity.
- **Knowledge Graph**: Relationship mapping between users, facts, and categories.
- **Diary System**: Narrative diary entries with semantic search.
- **Web GUI**: A built-in dashboard to visualize your knowledge graph and diary.

## Tech Stack
- **Python 3.11**
- **FastMCP**: For the MCP server implementation.
- **Qdrant**: Vector database for semantic search.
- **Neo4j**: Graph database for structured knowledge.
- **Ollama**: For local embeddings and LLM processing.
- **FastAPI/Uvicorn**: For the Web GUI.

## Setup

### 1. Environment Variables
Create a `.env` file in the root directory (use `.env.example` as a template).

### 2. Running with Docker (Recommended)
```bash
docker-compose up -d
```

### 3. Local Development
1. Install dependencies:
   ```bash
   pip install -r mem-mcp/requirements.txt
   ```
2. Run the server:
   ```bash
   python mem-mcp/server.py
   ```

## MCP Tools
- `mem_add_memory`: Save a fact to both vector and graph databases.
- `mem_search_memories`: Search facts using semantic similarity.
- `diary_save_entry`: Save a diary entry for a specific date.
- `diary_search_entries`: Search diary entries.
- `vault_get_category_summary`: Summarize facts in a specific category.

## License
Apache-2.0
