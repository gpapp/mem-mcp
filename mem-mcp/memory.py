"""
memory.py – Shared library for the Memory Vault.

Provides:
  - Configuration constants
  - DB client references (qdrant, neo4j_driver)
  - Service-readiness helpers
  - Embedding helper
  - User extraction from Basic-Auth / proxy headers (FastMCP context)
  - Low-level CRUD functions that keep Qdrant and Neo4j in sync
"""

import os
import uuid
import time
import socket
import logging
import base64
import httpx
from typing import List, Optional
from datetime import datetime

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchValue,
)
from neo4j import GraphDatabase

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memory-vault")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QDRANT_URL     = os.getenv("MEM_QDRANT_URL",      "http://qdrant:6333")
NEO4J_URL      = os.getenv("MEM_NEO4J_URL",       "bolt://neo4j:7687")
NEO4J_USER     = os.getenv("MEM_NEO4J_USER",      "neo4j")
NEO4J_PASS     = os.getenv("MEM_NEO4J_PASSWORD",  "password")
OLLAMA_URL     = os.getenv("MEM_LLM_URL",         os.getenv("MEM_EMBEDDER_URL", "http://ollama:11434"))
EMBED_MODEL    = os.getenv("MEM_EMBEDDER_MODEL",  "nomic-embed-text")
HTTP_TIMEOUT   = float(os.getenv("HTTPX_TIMEOUT", "600.0"))

COLLECTION_NAME  = "ea_memories"
DIARY_COLLECTION = "ea_diary"

# ---------------------------------------------------------------------------
# Global DB client references (populated by initialize_databases())
# ---------------------------------------------------------------------------
qdrant: Optional[AsyncQdrantClient] = None
neo4j_driver = None

# ---------------------------------------------------------------------------
# Service readiness
# ---------------------------------------------------------------------------
def _parse_url(url: str):
    clean = url.replace("http://", "").replace("bolt://", "").split("/")[0]
    if ":" in clean:
        host, port = clean.split(":", 1)
        return host, int(port)
    return clean, 80


def wait_for_service(url: str, label: str, max_retries: int = 5) -> bool:
    host, port = _parse_url(url)
    for _ in range(max_retries):
        try:
            with socket.create_connection((host, port), timeout=2):
                logger.info(f"{label} is ready at {host}:{port}")
                return True
        except Exception:
            time.sleep(2)
    logger.warning(f"{label} not reachable after {max_retries} retries")
    return False


# ---------------------------------------------------------------------------
# Database initialisation (async – called from server startup)
# ---------------------------------------------------------------------------
async def initialize_databases():
    global qdrant, neo4j_driver

    if wait_for_service(QDRANT_URL, "Qdrant"):
        try:
            qdrant = AsyncQdrantClient(url=QDRANT_URL)
            cols = await qdrant.get_collections()
            existing = [c.name for c in cols.collections]

            if COLLECTION_NAME not in existing:
                await qdrant.create_collection(
                    collection_name=COLLECTION_NAME,
                    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                )
                logger.info(f"Created Qdrant collection: {COLLECTION_NAME}")

            if DIARY_COLLECTION not in existing:
                await qdrant.create_collection(
                    collection_name=DIARY_COLLECTION,
                    vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                )
                logger.info(f"Created Qdrant collection: {DIARY_COLLECTION}")
        except Exception as e:
            logger.error(f"Qdrant init error: {e}")

    if wait_for_service(NEO4J_URL, "Neo4j"):
        try:
            neo4j_driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASS))
            with neo4j_driver.session() as s:
                s.run("CREATE INDEX user_id_index IF NOT EXISTS FOR (m:Memory) ON (m.userId)")
                s.run("CREATE INDEX diary_date_index IF NOT EXISTS FOR (d:DiaryEntry) ON (d.date)")
            logger.info("Neo4j ready")
        except Exception as e:
            logger.error(f"Neo4j init error: {e}")


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
async def get_embedding(text: str) -> List[float]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


# ---------------------------------------------------------------------------
# User extraction
#   Supports:
#     1. Basic-Auth header  (reverse proxy forwards Authorization: Basic …)
#     2. Common proxy headers (Remote-User, X-Remote-User, …)
#   Falls back to "anonymous".
# ---------------------------------------------------------------------------
def extract_user_from_headers(headers: dict) -> str:
    """
    headers: a dict-like with lowercase keys (e.g. from FastAPI Request or
             fastmcp get_http_headers()).
    """
    h = {k.lower(): v for k, v in headers.items()}

    auth = h.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            parts = auth.split()
            if len(parts) == 2:
                decoded = base64.b64decode(parts[1]).decode("utf-8")
                if ":" in decoded:
                    return decoded.split(":", 1)[0]
        except Exception:
            pass

    for name in ("remote-user", "x-remote-user", "x-user", "x-forwarded-user"):
        val = h.get(name)
        if val:
            return val

    return "anonymous"


# ---------------------------------------------------------------------------
# CRUD helpers – single source of truth for Qdrant + Neo4j consistency
# ---------------------------------------------------------------------------

async def db_add_memory(text: str, category: str, user_id: str) -> str:
    """Insert a fact into Qdrant (vector) and Neo4j (graph). Returns the new ID."""
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    doc_id   = str(uuid.uuid4())
    category = category.strip().capitalize()
    vector   = await get_embedding(text)

    # Qdrant
    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=doc_id,
            vector=vector,
            payload={"text": text, "category": category, "userId": user_id},
        )],
    )

    # Neo4j
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (c:Category {name: $category})
            CREATE (f:Fact {id: $id, text: $text, category: $category,
                            timestamp: datetime(), userId: $userId})
            CREATE (u)-[:KNOWS]->(f)
            CREATE (f)-[:IN_CATEGORY]->(c)
            """,
            userId=user_id, category=category, id=doc_id, text=text,
        )

    return doc_id


async def db_update_memory(memory_id: str, text: str, category: str, user_id: str) -> bool:
    """
    Update text (and optionally category) of an existing fact.
    Re-embeds and updates both stores. Returns True if the record was found.
    """
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    category = category.strip().capitalize()
    vector   = await get_embedding(text)

    # Qdrant – upsert preserves the id
    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=memory_id,
            vector=vector,
            payload={"text": text, "category": category, "userId": user_id},
        )],
    )

    # Neo4j
    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (f:Fact {id: $id, userId: $userId})
            SET f.text = $text, f.category = $category, f.updatedAt = datetime()
            WITH f
            OPTIONAL MATCH (f)-[r:IN_CATEGORY]->(:Category)
            DELETE r
            WITH f
            MERGE (c:Category {name: $category})
            CREATE (f)-[:IN_CATEGORY]->(c)
            RETURN f
            """,
            id=memory_id, userId=user_id, text=text, category=category,
        )
        return result.peek() is not None


async def db_delete_memory(memory_id: str, user_id: str) -> bool:
    """Delete a fact from both stores. Returns True if found."""
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=[memory_id],
    )

    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (f:Fact {id: $id, userId: $userId}) DETACH DELETE f RETURN count(f) as n",
            id=memory_id, userId=user_id,
        )
        rec = result.single()
        return (rec and rec["n"] > 0) or True  # qdrant already done


async def db_search_memories(query: str, user_id: str, limit: int = 5) -> list:
    """Vector-similarity search across the memories collection."""
    if not qdrant:
        raise RuntimeError("Qdrant not connected.")

    vec    = await get_embedding(query)
    filt   = Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))])
    result = await qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=vec,
        query_filter=filt,
        limit=limit,
    )
    return [
        {
            "id":       r.id,
            "text":     r.payload.get("text", ""),
            "category": r.payload.get("category", ""),
            "score":    r.score,
        }
        for r in result.points
    ]


def db_list_memories(user_id: str) -> list:
    """Return all facts for a user from Neo4j (no vector needed)."""
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (c:Category)<-[:IN_CATEGORY]-(f:Fact {userId: $userId})
            RETURN f.id as id, f.text as text, c.name as category,
                   f.timestamp as timestamp
            ORDER BY c.name ASC, f.timestamp DESC
            """,
            userId=user_id,
        )
        return [
            {
                "id":        r["id"],
                "text":      r["text"],
                "category":  r["category"],
                "timestamp": r["timestamp"].iso_format() if r["timestamp"] else None,
            }
            for r in result
        ]


def db_list_categories(user_id: str) -> list:
    """Return distinct category names for a user."""
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (c:Category)<-[:IN_CATEGORY]-(f:Fact {userId: $userId})
            RETURN DISTINCT c.name as category
            ORDER BY c.name ASC
            """,
            userId=user_id,
        )
        return [r["category"] for r in result]


# ---------------------------------------------------------------------------
# Diary helpers
# ---------------------------------------------------------------------------

def _diary_id(user_id: str, entry_date: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"diary_{user_id}_{entry_date}"))


async def db_save_diary(content: str, user_id: str, date: Optional[str] = None) -> str:
    """Upsert a diary entry. Returns the entry date string."""
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    entry_date = date if date else datetime.now().strftime("%Y-%m-%d")
    vector     = await get_embedding(content)

    # Qdrant (deterministic id keeps one entry per user/date)
    await qdrant.upsert(
        collection_name=DIARY_COLLECTION,
        points=[PointStruct(
            id=_diary_id(user_id, entry_date),
            vector=vector,
            payload={"content": content, "date": entry_date, "userId": user_id},
        )],
    )

    # Neo4j
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (d:DiaryEntry {date: $date, userId: $userId})
            SET d.content = $content, d.updatedAt = datetime()
            MERGE (u)-[:WROTE_DIARY]->(d)
            """,
            userId=user_id, date=entry_date, content=content,
        )

    return entry_date


async def db_search_diary(query: str, user_id: str, limit: int = 3) -> list:
    """Vector-similarity search across the diary collection."""
    if not qdrant:
        raise RuntimeError("Qdrant not connected.")

    vec    = await get_embedding(query)
    filt   = Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))])
    result = await qdrant.query_points(
        collection_name=DIARY_COLLECTION,
        query=vec,
        query_filter=filt,
        limit=limit,
        with_payload=True,
    )
    return [
        {
            "date":    r.payload.get("date"),
            "content": r.payload.get("content"),
            "score":   r.score,
        }
        for r in result.points
    ]


def db_list_diary(user_id: str) -> list:
    """Return all diary entries for a user from Neo4j, newest first."""
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            RETURN d.date as date, d.content as content
            ORDER BY d.date DESC
            """,
            userId=user_id,
        )
        return [{"date": r["date"], "content": r["content"]} for r in result]
