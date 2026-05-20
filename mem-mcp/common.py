"""
common.py – Shared configuration, logging, DB clients, and common helpers.
"""

from typing import Any, List, Optional
import os
import re
import uuid
import time
import socket
import secrets
import logging
import base64
import httpx
import numpy as np
import asyncio
from datetime import datetime

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams
)
from neo4j import GraphDatabase

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memory-vault")
logging.getLogger("mcp").setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QDRANT_URL     = os.getenv("MEM_QDRANT_URL",      "http://qdrant:6333")
NEO4J_URL      = os.getenv("MEM_NEO4J_URL",       "bolt://neo4j:7687")
NEO4J_USER     = os.getenv("MEM_NEO4J_USER",      "neo4j")
NEO4J_PASS     = os.getenv("MEM_NEO4J_PASSWORD",  "password")
OLLAMA_URL     = os.getenv("MEM_LLM_URL",         os.getenv("MEM_EMBEDDER_URL", "http://ollama:11434"))
EMBED_MODEL    = os.getenv("MEM_EMBEDDER_MODEL",  "nomic-embed-text")
HTTP_TIMEOUT   = float(os.getenv("MEM_HTTP_TIMEOUT", "300.0"))
BASE_URL       = os.getenv("BASE_URL",            "").rstrip("/")

COLLECTION_NAME  = "ea_memories"
DIARY_COLLECTION = "ea_diary"

SESSION_SECRET = os.getenv("MEM_SESSION_SECRET", secrets.token_hex(32))
SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds

# ---------------------------------------------------------------------------
# Global DB client references (lazily populated)
# ---------------------------------------------------------------------------
_qdrant: Optional[AsyncQdrantClient] = None
_neo4j_driver = None
_db_initialized = False
_db_lock = asyncio.Lock()

# Global event queue for database changes
db_subscribers: List[asyncio.Queue] = []

async def publish_db_event(user_id: str, event_type: str, payload: Optional[dict] = None):
    """Publishes a database change event to the global queue."""
    event = {
        "user_id": user_id,
        "event_type": event_type,
        "payload": payload or {},
        "timestamp": datetime.now().isoformat()
    }
    for queue in db_subscribers:
        await queue.put(event)

async def get_qdrant() -> AsyncQdrantClient:
    global _qdrant, _db_initialized
    async with _db_lock:
        if _qdrant is None:
            _qdrant = AsyncQdrantClient(url=QDRANT_URL)
        
        if not _db_initialized:
            if wait_for_service(QDRANT_URL, "Qdrant"):
                try:
                    cols = await _qdrant.get_collections()
                    existing = [c.name for c in cols.collections]
                    if COLLECTION_NAME not in existing:
                        await _qdrant.create_collection(
                            collection_name=COLLECTION_NAME,
                            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                        )
                    if DIARY_COLLECTION not in existing:
                        await _qdrant.create_collection(
                            collection_name=DIARY_COLLECTION,
                            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
                        )
                    _db_initialized = True
                except Exception as e:
                    logger.error(f"Qdrant init error: {e}")
    return _qdrant

def get_neo4j():
    global _neo4j_driver
    if _neo4j_driver is None:
        if wait_for_service(NEO4J_URL, "Neo4j"):
            try:
                _neo4j_driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASS))
                with _neo4j_driver.session() as s:
                    s.run("CREATE INDEX fact_user_id_index IF NOT EXISTS FOR (f:Fact) ON (f.userId)")
                    s.run("CREATE INDEX fact_category_index IF NOT EXISTS FOR (f:Fact) ON (f.category)")
                    s.run("CREATE INDEX diary_date_index IF NOT EXISTS FOR (d:DiaryEntry) ON (d.date)")
                    s.run("OPTIONAL MATCH (a)-[:MENTIONS]->(b) RETURN 1 LIMIT 0")
            except Exception as e:
                logger.error(f"Neo4j init error: {e}")
    return _neo4j_driver

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
# ---------------------------------------------------------------------------
def extract_user_from_headers(headers: dict) -> str:
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
