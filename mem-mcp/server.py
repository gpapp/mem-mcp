import os
import uuid
import httpx
import time
import socket
import logging
import base64
import threading
import uvicorn
from typing import List, Optional
from datetime import datetime
from fastapi import FastAPI, Request
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from starlette.responses import HTMLResponse

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memory-vault")

# --- Configuration ---
QDRANT_URL = os.getenv("MEM_QDRANT_URL", "http://qdrant:6333")
NEO4J_URL = os.getenv("MEM_NEO4J_URL", "bolt://neo4j:7687")
NEO4J_USER = os.getenv("MEM_NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("MEM_NEO4J_PASSWORD", "password")
OLLAMA_URL = os.getenv("MEM_LLM_URL", os.getenv("MEM_EMBEDDER_URL", "http://ollama:11434"))
EMBED_MODEL = os.getenv("MEM_EMBEDDER_MODEL", "nomic-embed-text")
HTTP_TIMEOUT = float(os.getenv("HTTPX_TIMEOUT", "600.0"))
COLLECTION_NAME = "ea_memories"
DIARY_COLLECTION = "ea_diary"

# --- Initialize MCP ---
mcp = FastMCP("Memory-Vault")
qdrant : AsyncQdrantClient = None
neo4j_driver = None

def parse_url(url):
    clean = url.replace("http://", "").replace("bolt://", "").split("/")[0]
    if ":" in clean:
        host, port = clean.split(":")
        return host, int(port)
    return clean, 80

def wait_for_service(url, label, max_retries=5):
    host, port = parse_url(url)
    for i in range(max_retries):
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except Exception:
            time.sleep(2)
    return False

async def initialize_databases():
    global qdrant, neo4j_driver
    if wait_for_service(QDRANT_URL, "Qdrant"):
        try:
            qdrant = AsyncQdrantClient(url=QDRANT_URL)
            # Initialize Collections
            cols = await qdrant.get_collections()
            existing_cols = [c.name for c in cols.collections]
            
            if COLLECTION_NAME not in existing_cols:
                await qdrant.create_collection(
                    collection_name=COLLECTION_NAME,
                    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                )
            
            if DIARY_COLLECTION not in existing_cols:
                await qdrant.create_collection(
                    collection_name=DIARY_COLLECTION,
                    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                )
        except Exception as e:
            logger.error(f"Qdrant init error: {e}")

    if wait_for_service(NEO4J_URL, "Neo4j"):
        try:
            neo4j_driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASS))
            with neo4j_driver.session() as session:
                session.run("CREATE INDEX user_id_index IF NOT EXISTS FOR (m:Memory) ON (m.userId)")
                session.run("CREATE INDEX diary_date_index IF NOT EXISTS FOR (d:DiaryEntry) ON (d.date)")
        except Exception as e:
            logger.error(f"Neo4j init error: {e}")

# Call initialization in an async manner
import asyncio
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(initialize_databases())
    else:
        loop.run_until_complete(initialize_databases())
except RuntimeError:
    asyncio.run(initialize_databases())

def get_current_user() -> str:
    """Extracts the username from headers or proxy fallbacks."""
    raw_headers = get_http_headers()
    headers = {k.lower(): v for k, v in raw_headers.items()}

    auth_header = headers.get("authorization", "")
    if auth_header and auth_header.lower().startswith("basic "):
        try:
            parts = auth_header.split()
            if len(parts) == 2:
                decoded = base64.b64decode(parts[1]).decode("utf-8")
                if ":" in decoded:
                    return decoded.split(":", 1)[0]
        except Exception: pass

    proxy_user_headers = ["remote-user", "x-remote-user", "x-user", "x-forwarded-user"]
    for hn in proxy_user_headers:
        val = headers.get(hn)
        if val: return val

    return "anonymous"

async def get_embedding(text: str) -> List[float]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/embeddings", json={"model": EMBED_MODEL, "prompt": text})
        resp.raise_for_status()
        return resp.json()["embedding"]

# --- MEMORY TOOLS ---
@mcp.tool(name="mem_add_memory")
async def add_memory(text: str, category: str = "General"):
    """
    Primary tool to save facts.
    It now saves to BOTH the vector database and the Knowledge Graph.
    """
    # Simply call your smart fact logic here
    return await save_smart_fact(text, category)

@mcp.tool(name="mem_update_memory")
async def update_memory(memory_id: str, text: str):
    """Update content and vector for a specific memory ID."""
    if not qdrant or not neo4j_driver: return "Error: Database connections not established."
    user_id = get_current_user()
    vector = await get_embedding(text)

    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(id=memory_id, vector=vector, payload={"text": text, "userId": user_id})]
    )

    with neo4j_driver.session() as session:
        result = session.run("""
            MATCH (m:Memory {id: $id, userId: $userId})
            SET m.text = $text, m.updatedAt = datetime()
            RETURN m
            """, id=memory_id, text=text, userId=user_id)
        if not result.peek():
            return f"Error: Memory {memory_id} not found or access denied."
    return f"Memory {memory_id} updated."

@mcp.tool(name="mem_forget_memory")
async def forget_memory(memory_id: str):
    """Permanently delete a memory from the vault."""
    if not qdrant or not neo4j_driver: return "Error: Database connections not established."
    user_id = get_current_user()
    await qdrant.delete(collection_name=COLLECTION_NAME, points_selector=[memory_id])
    with neo4j_driver.session() as session:
        session.run("MATCH (m:Memory {id: $id, userId: $userId}) DETACH DELETE m", id=memory_id, userId=user_id)
    return f"Memory {memory_id} forgotten."

@mcp.tool(name="mem_search_memories")
async def search_memories(query: str):
    """Search for relevant facts using vector similarity."""
    if not qdrant: return "Error: Qdrant not connected."
    user_id = get_current_user()
    vec = await get_embedding(query)
    user_filter = Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))])

    results = await qdrant.query_points(
        collection_name=COLLECTION_NAME, 
        query=vec, 
        query_filter=user_filter, 
        limit=5
    )
    return [{"id": r.id, "text": r.payload.get("text", ""), "score": r.score} for r in results.points]

@mcp.tool(name="vault_save_fact")
async def save_smart_fact(fact: str, category: str):
    """
    Saves a structured fact to the knowledge graph.
    Categories should be broad (e.g., 'Health', 'Career', 'Preferences', 'Projects').
    """
    if not qdrant or not neo4j_driver:
        return "Error: Database connections not established."

    user_id = get_current_user()
    doc_id = str(uuid.uuid4())
    category = category.strip().capitalize() # Normalize: 'health' -> 'Health'

    # 1. Generate Vector for Semantic Search (Qdrant)
    vector = await get_embedding(fact)
    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=doc_id,
            vector=vector,
            payload={"text": fact, "category": category, "userId": user_id}
        )]
    )

    # 2. Graph Insertion (Neo4j)
    # We use MERGE for Category so it creates a 'Hub' if it doesn't exist
    with neo4j_driver.session() as session:
        session.run("""
            MERGE (u:User {id: $userId})
            MERGE (c:Category {name: $category})
            CREATE (f:Fact {
                id: $id,
                text: $text,
                timestamp: datetime(),
                userId: $userId
            })
            CREATE (u)-[:KNOWS]->(f)
            CREATE (f)-[:IN_CATEGORY]->(c)
            """,
            userId=user_id, category=category, id=doc_id, text=fact
        )

    return f"Fact archived under [{category}]: {fact}"

@mcp.tool(name="vault_get_category_summary")
async def get_category_summary(category: str):
    """Retrieves all facts associated with a specific category hub."""
    user_id = get_current_user()
    category = category.strip().capitalize()

    if not neo4j_driver: return "Neo4j not connected."

    with neo4j_driver.session() as session:
        result = session.run("""
            MATCH (c:Category {name: $category})<-[:IN_CATEGORY]-(f:Fact {userId: $userId})
            RETURN f.text as text, f.timestamp as time
            ORDER BY f.timestamp DESC
            """, category=category, userId=user_id)

        facts = [f"({r['time'].strftime('%Y-%m-%d')}) {r['text']}" for r in result]

    if not facts:
        return f"No facts found in the '{category}' category."

    return f"### {category} Knowledge\n" + "\n".join(facts)

# --- DIARY TOOLS ---

@mcp.tool(name="diary_save_entry")
async def save_diary_entry(content: str, date: Optional[str] = None):
    """
    Create or update a diary entry for a specific date (format: YYYY-MM-DD).
    Content should be in Markdown format. If date is omitted, today's date is used.
    """
    if not qdrant or not neo4j_driver: return "Error: Database connections not established."
    user_id = get_current_user()
    entry_date = date if date else datetime.now().strftime("%Y-%m-%d")
    entry_id = f"diary_{user_id}_{entry_date}"

    vector = await get_embedding(content)

    # Qdrant Upsert (Handle search)
    await qdrant.upsert(
        collection_name=DIARY_COLLECTION,
        points=[PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, entry_id)), # Deterministic ID based on date/user
            vector=vector,
            payload={"content": content, "date": entry_date, "userId": user_id}
        )]
    )

    # Neo4j Upsert (Handle structure/retrieval)
    with neo4j_driver.session() as session:
        session.run("""
            MERGE (u:User {id: $userId})
            MERGE (d:DiaryEntry {date: $date, userId: $userId})
            SET d.content = $content, d.updatedAt = datetime()
            MERGE (u)-[:WROTE_DIARY]->(d)
            """, userId=user_id, date=entry_date, content=content)

    return f"Diary entry saved for {entry_date}."

@mcp.tool(name="diary_search_entries")
async def search_diary_entries(query: str, limit: int = 3):
    """Search diary entries based on keywords or semantic similarity."""
    if not qdrant:
        return "Error: Qdrant not connected."

    user_id = get_current_user()
    vec = await get_embedding(query)
    user_filter = Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))])

    try:
        # Use query_points instead of search
        response = await qdrant.query_points(
            collection_name=DIARY_COLLECTION,
            query=vec,
            query_filter=user_filter,
            limit=limit,
            with_payload=True
        )

        return [
            {
                "date": r.payload.get("date"),
                "content": r.payload.get("content"),
                "score": r.score
            } for r in response.points
        ]
    except AttributeError:
        # Fallback for older versions if query_points isn't found
        results = await qdrant.search(
            collection_name=DIARY_COLLECTION,
            query_vector=vec,
            query_filter=user_filter,
            limit=limit
        )
        return [{"date": r.payload.get("date"), "content": r.payload.get("content"), "score": r.score} for r in results]

# 1. Define the Web App for the GUI
web_app = FastAPI()

@web_app.get("/gui", response_class=HTMLResponse)
async def get_combined_gui(request: Request):
    """Combined GUI: Displays categorized facts and full Markdown diary entries."""
    user_id = "anonymous"
    # Extract user from proxy headers
    for hn in ["remote-user", "x-remote-user", "x-user", "x-forwarded-user"]:
        val = request.headers.get(hn)
        if val:
            user_id = val
            break

    memories_html = ""
    diary_html = ""

    if neo4j_driver:
        with neo4j_driver.session() as session:
            # 1. Fetch Categorized Facts
            # We use a JOIN-like match to get category and fact together
            mem_res = session.run("""
                MATCH (c:Category)<-[:IN_CATEGORY]-(f:Fact {userId: $u})
                RETURN f.text as text, c.name as cat
                ORDER BY c.name ASC, f.timestamp DESC
                """, u=user_id)

            current_cat = ""
            for m in mem_res:
                if m['cat'] != current_cat:
                    current_cat = m['cat']
                    memories_html += f'<h3 style="margin: 25px 0 10px 0; color: #4f46e5; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #e5e7eb;">{current_cat}</h3>'

                memories_html += f"""
                <div style="background: white; padding: 12px; border-radius: 6px; margin-bottom: 8px; border-left: 3px solid #6366f1; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">
                    <p style="margin: 0; font-size: 0.9rem; color: #374151;">{m['text']}</p>
                </div>
                """

            # 2. Fetch Full Diary Entries (Markdown)
            diary_res = session.run("""
                MATCH (d:DiaryEntry {userId: $u})
                RETURN d.date as date, d.content as content
                ORDER BY d.date DESC
                """, u=user_id)

            for d in diary_res:
                diary_html += f"""
                <div style="background: white; padding: 25px; border-radius: 12px; margin-bottom: 25px; border: 1px solid #e5e7eb; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
                    <div style="margin-bottom: 15px; padding-bottom: 10px; border-bottom: 1px solid #f3f4f6;">
                        <span style="background: #eef2ff; color: #4f46e5; padding: 4px 12px; border-radius: 20px; font-weight: bold; font-size: 0.9rem;">
                            📅 {d['date']}
                        </span>
                    </div>
                    <div class="markdown-body" style="color: #1f2937;">{d['content']}</div>
                </div>
                """

    # Fallbacks
    if not memories_html: memories_html = "<p style='color: #9ca3af;'>No categorized facts found.</p>"
    if not diary_html: diary_html = "<p style='color: #9ca3af;'>No diary entries recorded yet.</p>"

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Memory Vault Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css">
        <style>
            :root {{ --primary: #4f46e5; --bg: #f8fafc; }}
            body {{ background-color: var(--bg); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }}
            .nav-bar {{ background: var(--primary); color: white; padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .layout {{ max-width: 1200px; margin: 2rem auto; display: grid; grid-template-columns: 320px 1fr; gap: 2rem; padding: 0 1rem; }}
            .sidebar {{ position: sticky; top: 5rem; height: calc(100vh - 7rem); overflow-y: auto; padding-right: 10px; }}
            .content-area {{ min-width: 0; }}
            h2 {{ font-size: 1.25rem; color: #111827; margin-top: 0; }}
            .markdown-body {{ background: transparent !important; }}
            @media (max-width: 850px) {{ .layout {{ grid-template-columns: 1fr; }} .sidebar {{ position: static; height: auto; }} }}
        </style>
    </head>
    <body>
        <div class="nav-bar">
            <h1 style="margin: 0; font-size: 1.25rem;">🧠 Vault Dashboard</h1>
            <div style="font-size: 0.85rem; opacity: 0.9;">User: <strong>{user_id}</strong></div>
        </div>

        <div class="layout">
            <aside class="sidebar">
                <h2>📝 Knowledge Graph</h2>
                <p style="font-size: 0.8rem; color: #64748b; margin-bottom: 20px;">Facts grouped by category hub.</p>
                {memories_html}
            </aside>

            <main class="content-area">
                <h2>📖 Narrative Diary</h2>
                {diary_html}
            </main>
        </div>

        <script>
            // Render Markdown
            document.querySelectorAll('.markdown-body').forEach(el => {{
                // Parse markdown and sanitize/set content
                const raw = el.innerHTML;
                el.innerHTML = marked.parse(raw);
            }});
        </script>
    </body>
    </html>
    """
# 2. Function to run MCP
def run_mcp():
    # This runs the MCP server on default port (usually 8000)
    mcp.run(transport="http", host="0.0.0.0", port=8000)

if __name__ == "__main__":
    # Start MCP in a separate thread
    mcp_thread = threading.Thread(target=run_mcp, daemon=True)
    mcp_thread.start()

    # Start the Web GUI in the main thread
    print("--- Starting Web GUI on http://0.0.0.0:8080/gui ---")
    uvicorn.run(web_app, host="0.0.0.0", port=8080)