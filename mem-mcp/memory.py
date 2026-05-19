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

from typing import Any
import asyncio
import os
import uuid
import time
import socket
import secrets
import logging
import base64
import httpx
import numpy as np
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchValue, PointIdsList,
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
# Increase timeout for LLM generation
HTTP_TIMEOUT = float(os.getenv("MEM_HTTP_TIMEOUT", "300.0"))
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


async def get_llm_completion(prompt: str, system: Optional[str] = None) -> str:
    """Run a local LLM completion using Ollama."""
    model = os.getenv("MEM_LLM_MODEL", "llama3")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False
        }
        if system:
            payload["system"] = system

        logger.info(f"[Ollama] GENERATE request to {OLLAMA_URL} with model={model}. Prompt length: {len(prompt)} chars.")

        # Log prompt upfront in case of timeout
        timestamp = datetime.now().isoformat().replace(":", "-")
        log_file = f"logs/ollama_generate_{timestamp}.txt"
        try:
            os.makedirs("logs", exist_ok=True)
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(f"=== SYSTEM ===\n{system or 'None'}\n\n")
                f.write(f"=== PROMPT ===\n{prompt}\n\n")
                f.write(f"=== RESPONSE ===\n[Pending or Timeout]\n")
        except Exception as e:
            logger.error(f"Failed to write initial Ollama log: {e}")

        try:
            resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            response_text = resp.json()["response"]

            # Update log with actual response
            try:
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(f"=== SYSTEM ===\n{system or 'None'}\n\n")
                    f.write(f"=== PROMPT ===\n{prompt}\n\n")
                    f.write(f"=== RESPONSE ===\n{response_text}\n")
            except Exception as e:
                pass

            return response_text
        except Exception as api_err:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[ERROR] {str(api_err)}\n")
            except Exception:
                pass
            raise api_err


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

async def db_add_memory(text: str, category: str, user_id: str, metadata: Optional[dict] = None, name: Optional[str] = None) -> str:
    """Insert a fact into Qdrant (vector) and Neo4j (graph). Returns the new ID."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    doc_id   = str(uuid.uuid4())
    category = category.strip().capitalize()
    meta     = metadata or {}
    embed_text = f"{name}: {text}" if name else text
    vector   = await get_embedding(embed_text)

    # Qdrant
    payload = {"text": text, "name": name, "category": category, "userId": user_id, "metadata": meta}
    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=doc_id,
            vector=vector,
            payload=payload,
        )],
    )

    # Neo4j
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (c:Category {name: $category})
            CREATE (f:Fact {id: $id, text: $text, name: $name, category: $category,
                            timestamp: datetime(), userId: $userId})
            SET f += $metadata
            CREATE (u)-[:KNOWS]->(f)
            CREATE (f)-[:IN_CATEGORY]->(c)
            """,
            userId=user_id, category=category, id=doc_id, text=text, name=name,
            metadata=meta
        )

    await publish_db_event(user_id, "memory_changed", {
        "action": "add",
        "id": doc_id,
        "category": category,
        "name": name
    })
    return doc_id


async def db_update_memory(memory_id: str, name: Optional[str], text: Optional[str], category: Optional[str], user_id: str, metadata: Optional[dict] = None) -> bool:
    """
    Update name, text, category, or metadata of an existing fact.
    Re-embeds if text changes. Returns True if the record was found.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    logger.info(f"[db_update_memory] Updating {memory_id}: name={name}, text={'Yes' if text else 'No'}, category={category}")

    # Get current to see what's changing
    with neo4j_driver.session() as s:
        res = s.run("MATCH (f:Fact {id: $id, userId: $userId}) RETURN f", id=memory_id, userId=user_id)
        existing = res.single()
        if not existing: 
            logger.warning(f"[db_update_memory] Memory {memory_id} not found")
            return False
        old_fact = existing["f"]

    new_text = text if text is not None else old_fact.get("text")
    new_name = name if name is not None else old_fact.get("name")
    new_cat  = category.strip().capitalize() if category else old_fact.get("category")
    new_meta = metadata or {} 
    
    logger.info(f"[db_update_memory] New values: name={new_name}, text_len={len(new_text) if new_text else 0}")
    
    # Qdrant Update
    # Re-embed if text OR name changes
    embed_text = f"{new_name}: {new_text}" if new_name else new_text
    needs_embed = text is not None or name is not None
    
    try:
        vector = await get_embedding(embed_text) if needs_embed else None
        logger.info(f"[db_update_memory] Embedding generated, vector_len={len(vector) if vector else 0}")
    except Exception as e:
        logger.error(f"[db_update_memory] Embedding failed: {e}")
        raise RuntimeError(f"Embedding failed: {e}")
    
    # Prepare payload, converting Neo4j types to JSON-serializable ones
    payload = {}
    for k, v in dict(old_fact).items():
        if hasattr(v, "iso_format"):
            payload[k] = v.iso_format()
        else:
            payload[k] = v

    if text is not None: payload["text"] = new_text
    if name is not None: payload["name"] = new_name
    if category is not None: payload["category"] = new_cat
    if metadata:
        current_meta = payload.get("metadata", {})
        if isinstance(current_meta, str): # Safety check if metadata was stored as string
             import json
             try: current_meta = json.loads(current_meta)
             except: current_meta = {}
        current_meta.update(new_meta)
        payload["metadata"] = current_meta

    logger.info(f"[db_update_memory] Upserting to Qdrant, payload keys: {list(payload.keys())}")
    
    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=memory_id,
            vector=vector or await get_embedding(embed_text),
            payload=payload,
        )],
    )

    # Neo4j Update
    logger.info(f"[db_update_memory] Updating Neo4j")
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (f:Fact {id: $id, userId: $userId})
            SET f.text = $text, f.name = $name, f.category = $category, f.updatedAt = datetime()
            SET f += $metadata
            WITH f
            OPTIONAL MATCH (f)-[r:IN_CATEGORY]->(:Category)
            DELETE r
            WITH f
            MERGE (c:Category {name: $category})
            CREATE (f)-[:IN_CATEGORY]->(c)
            """,
            id=memory_id, userId=user_id, text=new_text, name=new_name, category=new_cat, metadata=new_meta
        )
    logger.info(f"[db_update_memory] Update complete for {memory_id}")
    await publish_db_event(user_id, "memory_changed", {
        "action": "update",
        "id": memory_id,
        "category": new_cat,
        "name": new_name
    })
    return True


async def db_delete_memory(memory_id: str, user_id: str) -> bool:
    """Delete a fact from both stores. Returns True if found."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
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
        if (rec and rec["n"] > 0):
            await publish_db_event(user_id, "memory_changed", {
                "action": "delete",
                "id": memory_id
            })
            return True
        return True # Qdrant already done


async def db_link_facts(source_id: str, target_id: str, rel_type: str, metadata: dict, user_id: str):
    """Create a bidirectional relationship between two facts in Neo4j.
    Creates forward link (source->target) and reverse link (target->source).
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    rel_type = rel_type.upper().replace(" ", "_")
    reverse_rel = rel_type  # Same verb works for reverse direction (e.g., "CONNECTED_TO" works both ways)
    
    with neo4j_driver.session() as s:
        # Create forward relationship (source -> target)
        s.run(
            f"""
            MATCH (a:Fact {{id: $sid, userId: $userId}})
            MATCH (b:Fact {{id: $tid, userId: $userId}})
            MERGE (a)-[r:{rel_type}]->(b)
            SET r += $metadata
            """,
            sid=source_id, tid=target_id, userId=user_id, metadata=metadata
        )
        # Create reverse relationship (target -> source) for two-way navigation
        s.run(
            f"""
            MATCH (a:Fact {{id: $sid, userId: $userId}})
            MATCH (b:Fact {{id: $tid, userId: $userId}})
            MERGE (b)-[r:{reverse_rel}]->(a)
            SET r += $metadata
            """,
            sid=source_id, tid=target_id, userId=user_id, metadata=metadata
        )

    await publish_db_event(user_id, "graph_changed", {
        "action": "link",
        "source_id": source_id,
        "target_id": target_id,
        "rel_type": rel_type
    })

async def db_unlink_facts(source_id: str, target_id: str, rel_type: str, user_id: str):
    """Remove a bidirectional relationship between two facts in Neo4j."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    rel_type = rel_type.upper().replace(" ", "_") if rel_type else None
    
    with neo4j_driver.session() as s:
        if rel_type:
            s.run(
                f"""
                MATCH (a:Fact {{id: $sid, userId: $userId}})-[r:{rel_type}]->(b:Fact {{id: $tid, userId: $userId}})
                DELETE r
                """,
                sid=source_id, tid=target_id, userId=user_id
            )
            s.run(
                f"""
                MATCH (a:Fact {{id: $sid, userId: $userId}})<-[r:{rel_type}]-(b:Fact {{id: $tid, userId: $userId}})
                DELETE r
                """,
                sid=source_id, tid=target_id, userId=user_id
            )
        else:
            s.run(
                """
                MATCH (a:Fact {id: $sid, userId: $userId})-[r]->(b:Fact {id: $tid, userId: $userId})
                DELETE r
                """,
                sid=source_id, tid=target_id, userId=user_id
            )
            s.run(
                """
                MATCH (a:Fact {id: $sid, userId: $userId})<-[r]-(b:Fact {id: $tid, userId: $userId})
                DELETE r
                """,
                sid=source_id, tid=target_id, userId=user_id
            )

    await publish_db_event(user_id, "graph_changed", {
        "action": "unlink",
        "source_id": source_id,
        "target_id": target_id
    })

def db_get_neighborhood(fact_id: str, depth: int, rel_types: List[str], user_id: str) -> list:
    """Explore context around a fact in the graph."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    # Sanitize rel_types for Cypher
    rel_filter = ""
    if rel_types:
        types = "|:".join([t.upper() for t in rel_types])
        rel_filter = f":{types}"

    with neo4j_driver.session() as s:
        result = s.run(
            f"""
            MATCH (f:Fact {{id: $id, userId: $userId}})
            MATCH path = (f)-[*1..{depth}]-(neighbor:Fact)
            WHERE neighbor.userId = $userId
            RETURN neighbor, labels(neighbor) as labels, relationships(path) as rels
            """,
            id=fact_id, userId=user_id
        )
        nodes = []
        for r in result:
            nodes.append({
                "id": r["neighbor"]["id"],
                "text": r["neighbor"]["text"],
                "category": r["neighbor"]["category"],
                "labels": r["labels"]
            })
        return nodes


def db_get_fact_by_id(fact_id: str, user_id: str) -> Optional[dict]:
    """Get a single fact by its ID."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (f:Fact {id: $id, userId: $userId})
            RETURN f
            """,
            id=fact_id, userId=user_id
        )
        record = result.single()
        if record:
            return dict(record["f"])
        return None


def db_get_connections_by_type(fact_id: str, user_id: str) -> dict:
    """Get all connections for a fact grouped by relationship type."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (f:Fact {id: $id, userId: $userId})
            MATCH (f)-[r]-(neighbor:Fact)
            WHERE neighbor.userId = $userId
            RETURN type(r) as rel_type, collect({id: neighbor.id, name: neighbor.name, text: neighbor.text, category: neighbor.category}) as connections
            """,
            id=fact_id, userId=user_id
        )
        
        connections = {}
        for r in result:
            rel_type = r["rel_type"]
            connections[rel_type] = []
            for conn in r["connections"]:
                connections[rel_type].append({
                    "id": conn["id"],
                    "name": conn.get("name") or conn.get("text", "")[:50],
                    "category": conn.get("category", "General")
                })
        
        return connections


async def db_search_memories(query: str, user_id: str, limit: int = 5, category: Optional[str] = None, top_p: float = 0.4) -> list:
    """Vector-similarity search with optional category filter. Also does a basic substring match on names."""
    import difflib
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Databases not connected.")

    # 1. Neo4j exact/substring match on name or aliases
    # We do a quick lookup for nodes containing the query
    exact_matches = []
    query_lower = query.lower()

    with neo4j_driver.session() as s:
        # Only do broad CONTAINS match on text if the query is reasonably long to avoid massive irrelevant noise
        if len(query) > 3:
            cypher = """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.name) CONTAINS toLower($query_str)
               OR toLower(f.text) CONTAINS toLower($query_str)
            """
        else:
            cypher = """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.name) CONTAINS toLower($query_str)
            """
        if category:
            cypher += " AND toLower(f.category) = toLower($category)"
        cypher += " RETURN f LIMIT $limit"

        params = {"userId": user_id, "query_str": query, "limit": limit}
        if category:
            params["category"] = category.strip()

        neo_result = s.run(cypher, **params)
        for r in neo_result:
            f = r["f"]
            # Construct a result matching Qdrant format
            meta = {k: v for k, v in f.items() if k not in {"id", "text", "name", "category", "timestamp", "userId"}}

            score = 1.0
            name = f.get("name", "")
            if name:
                name_lower = name.lower()
                query_words = query_lower.split()
                if query_lower == name_lower:
                    score = 2.5
                elif name_lower in query_lower:
                    score = 2.0
                elif all(w in name_lower for w in query_words):
                    score = 1.7
                elif query_lower in name_lower:
                    score = 1.5
                name_words = name_lower.split()
                if len(query_words) == 1 and len(name_words) >= 1:
                    for tw in name_words:
                        s = difflib.SequenceMatcher(None, query_lower, tw).ratio()
                        if s >= 0.6:
                            score = max(score, 1.4 * s)
                            break

            exact_matches.append({
                "id": f["id"],
                "text": f["text"],
                "name": f.get("name"),
                "category": f.get("category"),
                "score": score,
                "metadata": meta
            })

    # 2. Qdrant vector search
    vec    = await get_embedding(query)
    conditions = [FieldCondition(key="userId", match=MatchValue(value=user_id))]
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category.strip().capitalize())))

    filt   = Filter(must=conditions)
    # Fetch more results initially so we can re-rank them with our manual boosts
    fetch_limit = max(limit * 5, 50)
    result = await qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=vec,
        query_filter=filt,
        limit=fetch_limit,
        score_threshold=top_p,
    )
    results = []
    query_lower = query.lower()
    for r in result.points:
        score = r.score
        metadata = r.payload.get("metadata", {})
        aliases = metadata.get("aliases", {})
        name = r.payload.get("name")

        # Boost score if query matches the name
        if name:
            name_lower = name.lower()
            query_words = query_lower.split()
            if query_lower == name_lower:
                score += 1.0
            elif name_lower in query_lower:
                score += 0.5
            elif all(w in name_lower for w in query_words):
                score += 0.4
            elif query_lower in name_lower:
                score += 0.2
            name_words = name_lower.split()
            if len(query_words) == 1 and len(name_words) >= 1:
                for tw in name_words:
                    s = difflib.SequenceMatcher(None, query_lower, tw).ratio()
                    if s >= 0.6:
                        score += 0.35 * s
                        break
            if aliases and isinstance(aliases, dict):
                matched_query_words = set()
                best_ratio = 0
                for alias, confidence in aliases.items():
                    alias_words = alias.lower().split()
                    for qw in query_words:
                        if qw in alias.lower():
                            continue
                        for aw in alias_words:
                            ratio = difflib.SequenceMatcher(None, qw, aw).ratio()
                            if ratio >= 0.6 and ratio > best_ratio:
                                best_ratio = ratio
                                matched_query_words.add(qw)
                    if query_lower == alias.lower():
                        try: score += (float(confidence) * 0.2)
                        except: pass
                    elif query_lower in alias.lower() or alias.lower() in query_lower:
                        try: score += (float(confidence) * 0.05)
                        except: pass
                if matched_query_words:
                    score += 0.1 * best_ratio

        results.append({
            "id": r.id,
            "text": r.payload.get("text"),
            "name": r.payload.get("name"),
            "category": r.payload.get("category"),
            "score": score,
            "metadata": metadata
        })

    # Merge exact matches and vector results, deduplicating by ID
    merged_results = {}
    for r in results:
        merged_results[r["id"]] = r

    for r in exact_matches:
        if r["id"] in merged_results:
            merged_results[r["id"]]["score"] = max(merged_results[r["id"]]["score"], r["score"])
        else:
            merged_results[r["id"]] = r

    final_list = list(merged_results.values())
    if category:
        final_list = [r for r in final_list if r.get("category", "").lower() == category.lower()]
    final_list.sort(key=lambda x: x["score"], reverse=True)
    return final_list[:limit]


def db_find_patterns(user_id: str) -> list:
    """Identify recurring patterns/themes in the graph."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        # Example: Find categories that appear together in paths
        result = s.run(
            """
            MATCH (c1:Category)<-[:IN_CATEGORY]-(f1:Fact)-[]-(f2:Fact)-[:IN_CATEGORY]->(c2:Category)
            WHERE f1.userId = $userId AND f2.userId = $userId AND c1 <> c2
            RETURN c1.name as cat1, c2.name as cat2, count(*) as weight
            ORDER BY weight DESC LIMIT 10
            """,
            userId=user_id
        )
        return [{"pattern": f"{r['cat1']} + {r['cat2']}", "strength": r["weight"]} for r in result]


def db_list_memories(user_id: str) -> list:
    """Return all facts for a user from Neo4j with metadata and links."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (c:Category)<-[:IN_CATEGORY]-(f:Fact {userId: $userId})
            OPTIONAL MATCH (f)-[r]->(target:Fact {userId: $userId})
            WHERE type(r) <> 'IN_CATEGORY' AND type(r) <> 'KNOWS'
            RETURN f, c.name as category, 
                   collect({rel: type(r), target_id: target.id, target_text: target.text, target_name: target.name}) as links
            ORDER BY coalesce(f.name, f.text) ASC
            """,
            userId=user_id,
        )
        memories = []
        for r in result:
            f_node = r["f"]
            # Extract metadata (all properties except core ones)
            core_keys = {"id", "text", "name", "category", "timestamp", "userId"}
            metadata = {}
            for k, v in f_node.items():
                if k not in core_keys:
                    metadata[k] = v.iso_format() if hasattr(v, "iso_format") else v
            
            # Clean up links (remove null targets)
            links = [l for l in r["links"] if l.get("target_id")]

            memories.append({
                "id":        f_node["id"],
                "text":      f_node["text"],
                "name":     f_node.get("name"),
                "category":  r["category"],
                "timestamp": f_node["timestamp"].iso_format() if f_node.get("timestamp") else None,
                "metadata":  metadata,
                "links":     links
            })
        return memories


def db_list_categories(user_id: str) -> list:
    """Return distinct category names for a user."""
    neo4j_driver = get_neo4j()
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


async def db_find_duplicates(user_id: str, category: str = "People", limit: int = 50, threshold: float = 0.6, max_cluster: int = 4):
    """
    Find potential duplicates in a category using multi-signal similarity and clustering.

    Similarity is the MAX of independent signals:
      - Vector cosine similarity (embedding distance)
      - Exact normalized name match → 1.0
      - Email match → 1.0
      - first_name + last_name match → 1.0
      - Alias match → 0.95
      - Fuzzy alias ↔ name match (difflib ≥0.75) → 0.88
      - Title word-overlap boost (additive on vector score)
      - Fuzzy name word match (difflib ≥0.75) → 0.7–0.95

    Clustering:
      1. Form initial clusters at `threshold`.
      2. Any cluster > max_cluster is recursively split by re-clustering
         only its members at a higher internal threshold.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # ── 1. Fetch items from Neo4j ──────────────────────────────────────────
    fetch_limit = max(limit * 10, 1000)
    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.category) = toLower($category)
            RETURN f
            ORDER BY f.timestamp DESC
            LIMIT $limit
            """,
            userId=user_id, category=category.strip(), limit=fetch_limit
        )
        items = []
        for r in result:
            f_node = r["f"]
            core_keys = {"id", "text", "category", "timestamp", "userId"}
            metadata = {}
            for k, v in f_node.items():
                if k not in core_keys:
                    metadata[k] = v.iso_format() if hasattr(v, "iso_format") else v

            items.append({
                "id": f_node["id"],
                "text": f_node["text"],
                "name": f_node.get("name"),
                "category": f_node.get("category"),
                "metadata": metadata
            })

    logger.info(f"[db_find_duplicates] Found {len(items)} items in Neo4j for category '{category}' (user: {user_id})")

    if not items:
        with neo4j_driver.session() as s:
            cats = s.run("MATCH (f:Fact {userId: $userId}) RETURN DISTINCT f.category as cat", userId=user_id)
            available = [str(c["cat"]) for c in cats]
            logger.info(f"[db_find_duplicates] Available categories: {available}")
        return []

    # ── 2. Get vectors from Qdrant ─────────────────────────────────────────
    ids = [item["id"] for item in items]
    points = await qdrant.retrieve(
        collection_name=COLLECTION_NAME,
        ids=ids,
        with_vectors=True
    )

    vectors = {str(p.id): p.vector for p in points if p.vector}
    items_with_vectors = [item for item in items if str(item["id"]) in vectors]
    if not items_with_vectors:
        logger.warning(f"[db_find_duplicates] No vectors found for {len(items)} items in Qdrant. Check sync.")
        return []

    logger.info(f"[db_find_duplicates] {len(items_with_vectors)} items have vectors (of {len(items)} total)")

    # ── 3. Prepare per-item data ───────────────────────────────────────────
    def normalize_name(name):
        """Lower-case, strip, flip 'Last, First' → 'first last'."""
        if not name:
            return ""
        n = name.lower().strip()
        if "," in n:
            parts = [p.strip() for p in n.split(",", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                return f"{parts[1]} {parts[0]}"
        return n

    is_people = category.strip().lower() == "people"
    import difflib

    prepared = []
    for item in items_with_vectors:
        meta = item.get("metadata") or {}

        # Aliases can be list or dict
        aliases_raw = meta.get("aliases") or []
        if isinstance(aliases_raw, dict):
            aliases_list = list(aliases_raw.keys())
        elif isinstance(aliases_raw, list):
            aliases_list = aliases_raw
        else:
            aliases_list = []

        norm = normalize_name(item.get("name"))
        norm_words = set(norm.split()) if norm else set()

        prepared.append({
            "id": item["id"],
            "vec": np.array(vectors[str(item["id"])]),
            "norm_name": norm,
            "norm_words": norm_words,
            "email": str(meta.get("email", "")).lower().strip(),
            "first_name": str(meta.get("first_name", "")).lower().strip(),
            "last_name": str(meta.get("last_name", "")).lower().strip(),
            "aliases": [normalize_name(a) for a in aliases_list],
            "name": item.get("name") or "",
            "metadata": meta,
        })

    # ── 4. Compute pairwise similarity (multi-signal, take MAX) ────────────
    num_items = len(prepared)
    # Store as dict for fast lookup: (i,j) → score
    pair_scores: dict = {}

    for i in range(num_items):
        p_i = prepared[i]
        vec_i = p_i["vec"]
        norm_vec_i = np.linalg.norm(vec_i)
        if norm_vec_i == 0:
            continue

        for j in range(i + 1, num_items):
            p_j = prepared[j]
            vec_j = p_j["vec"]
            norm_vec_j = np.linalg.norm(vec_j)
            if norm_vec_j == 0:
                continue

            signals = []

            # Signal 1: Vector cosine similarity
            vec_sim = float(np.dot(vec_i, vec_j) / (norm_vec_i * norm_vec_j))
            signals.append(vec_sim)

            # Signal 2: Exact normalized-name match
            if p_i["norm_name"] and p_j["norm_name"] and p_i["norm_name"] == p_j["norm_name"]:
                signals.append(1.0)

            # Signal 3: Email match
            if p_i["email"] and p_j["email"] and p_i["email"] == p_j["email"]:
                signals.append(1.0)

            # Signal 4: first_name + last_name match
            if (p_i["first_name"] and p_i["last_name"]
                    and p_j["first_name"] and p_j["last_name"]
                    and p_i["first_name"] == p_j["first_name"]
                    and p_i["last_name"] == p_j["last_name"]):
                signals.append(1.0)

            # Signal 5: Alias ↔ name match
            if p_j["norm_name"] and p_j["norm_name"] in p_i["aliases"]:
                signals.append(0.95)
            if p_i["norm_name"] and p_i["norm_name"] in p_j["aliases"]:
                signals.append(0.95)

            # Signal 5b: Fuzzy alias ↔ name match
            for ali in p_i["aliases"]:
                ali_words = ali.split()
                for tw in p_j["norm_name"].split():
                    if difflib.SequenceMatcher(None, ali, tw).ratio() >= 0.6:
                        signals.append(0.88)
                        break

            # Signal 6: Title word-overlap boost (additive on vec_sim)
            if p_i["norm_words"] and p_j["norm_words"]:
                common = p_i["norm_words"] & p_j["norm_words"]
                if common:
                    valid = [w for w in common if len(w) > 2 or is_people]
                    if valid:
                        min_words = min(len(p_i["norm_words"]), len(p_j["norm_words"]))
                        ratio = len(valid) / min_words if min_words else 0
                        boosted = vec_sim + 0.3 * ratio
                        if ratio >= 1.0:
                            boosted = max(boosted, 0.88)
                        signals.append(min(1.0, boosted))

            # Signal 7: Fuzzy name word match (only if both have last names)
            if p_i["last_name"] and p_j["last_name"]:
                for wi in p_i["norm_words"]:
                    for wj in p_j["norm_words"]:
                        s = difflib.SequenceMatcher(None, wi, wj).ratio()
                        if s >= 0.6:
                            signals.append(min(0.95, 0.7 + 0.25 * s))
                            break

            # Signal 9: First name match (strong signal for people)
            if is_people and p_i["first_name"] and p_j["first_name"]:
                if p_i["first_name"] == p_j["first_name"]:
                    # If at least one record has only first name (no last name), boost strongly
                    if not p_i["last_name"] or not p_j["last_name"]:
                        signals.append(0.95)
                    else:
                        # Both have last names - require surname similarity
                        if p_i["last_name"] and p_j["last_name"]:
                            last_ratio = difflib.SequenceMatcher(None, p_i["last_name"], p_j["last_name"]).ratio()
                            if last_ratio >= 0.7:
                                signals.append(0.93)

            # Signal 10: Full normalized name fuzzy match (only if both have last names)
            if p_i["norm_name"] and p_j["norm_name"] and p_i["last_name"] and p_j["last_name"]:
                full_ratio = difflib.SequenceMatcher(None, p_i["norm_name"], p_j["norm_name"]).ratio()
                if full_ratio >= 0.85:
                    signals.append(min(0.95, full_ratio))

            similarity = max(signals)
            pair_scores[(i, j)] = similarity

    logger.info(f"[db_find_duplicates] Computed {len(pair_scores)} pairwise scores for {num_items} items")

    # Log top matches for debugging
    top_pairs = sorted(pair_scores.items(), key=lambda x: x[1], reverse=True)[:10]
    for (pi, pj), sc in top_pairs:
        logger.info(f"  top pair: '{prepared[pi]['name']}' ↔ '{prepared[pj]['name']}' = {sc:.4f}")

    # ── 5. Clustering with per-cluster splitting ───────────────────────────
    def union_find_cluster(member_indices: list, thresh: float) -> list:
        """Union-Find cluster a subset of items at a given threshold.
        Returns list of clusters (each a list of global indices), size >= 2.
        """
        idx_set = set(member_indices)
        parent = {x: x for x in member_indices}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        for (a, b), score in pair_scores.items():
            if a in idx_set and b in idx_set and score >= thresh:
                union(a, b)

        groups: dict = {}
        for x in member_indices:
            root = find(x)
            groups.setdefault(root, []).append(x)

        return [sorted(g) for g in groups.values() if len(g) >= 2]

    def split_cluster(members: list, thresh: float, step: float = 0.05, ceiling: float = 0.98) -> list:
        """Recursively split an oversized cluster by raising its internal threshold."""
        if len(members) <= max_cluster:
            return [members]

        next_thresh = thresh + step
        if next_thresh > ceiling:
            # Can't split further – accept as-is
            return [members]

        sub_clusters = union_find_cluster(members, next_thresh)
        result = []
        # Singletons (items not in any sub-cluster) are dropped
        for sc in sub_clusters:
            if len(sc) <= max_cluster:
                result.append(sc)
            else:
                result.extend(split_cluster(sc, next_thresh, step, ceiling))
        return result

    # Initial clustering at the user-supplied threshold
    initial_clusters = union_find_cluster(list(range(num_items)), threshold)
    logger.info(f"[db_find_duplicates] Initial clustering at {threshold}: {len(initial_clusters)} clusters")

    # Split oversized clusters
    final_clusters = []
    for cluster in initial_clusters:
        if len(cluster) <= max_cluster:
            final_clusters.append(cluster)
        else:
            final_clusters.extend(split_cluster(cluster, threshold))

    logger.info(f"[db_find_duplicates] After splitting: {len(final_clusters)} clusters")

    # ── 6. Build output ────────────────────────────────────────────────────
    result_clusters = []
    for cluster_indices in final_clusters:
        if len(cluster_indices) < 2:
            continue

        members = []
        cluster_scores = []
        ci_set = set(cluster_indices)

        for idx in cluster_indices:
            item = items_with_vectors[idx]
            item_scores = []
            for (a, b), score in pair_scores.items():
                if (a == idx and b in ci_set) or (b == idx and a in ci_set):
                    item_scores.append(score)

            avg_item_sim = sum(item_scores) / len(item_scores) if item_scores else 1.0
            cluster_scores.extend(item_scores)

            member_info = {
                "id": item["id"],
                "text": item["text"],
                "name": item["name"],
                "similarity": round(avg_item_sim, 4),
            }
            member_info.update(item.get("metadata") or {})
            members.append(member_info)

        avg_similarity = sum(cluster_scores) / len(cluster_scores) if cluster_scores else 0.0
        recommendation = "MERGE - high overlap" if avg_similarity > 0.9 else "MERGE - verify and combine"

        result_clusters.append({
            "cluster_id": len(result_clusters) + 1,
            "members": members,
            "avg_similarity": round(avg_similarity, 4),
            "recommendation": recommendation,
        })

    result_clusters.sort(key=lambda x: x["avg_similarity"], reverse=True)
    return result_clusters


async def db_merge_memories(master_id: str, duplicate_ids: List[str], user_id: str):
    """
    Merge multiple duplicate facts into a single master fact.
    Moves all relationships to the master and deletes duplicates.
    Uses APOC for efficient graph refactoring.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    with neo4j_driver.session() as s:
        # 1. Read relationships into memory from master
        master_rels_res = s.run(
            """
            MATCH (master:Fact {id: $masterId, userId: $userId})
            OPTIONAL MATCH (master)-[out_r]->(out_t)
            OPTIONAL MATCH (in_t)-[in_r]->(master)
            RETURN
                collect(DISTINCT {type: type(out_r), target: out_t.id, props: properties(out_r), dir: 'out'}) as out_rels,
                collect(DISTINCT {type: type(in_r), source: in_t.id, props: properties(in_r), dir: 'in'}) as in_rels
            """,
            masterId=master_id, userId=user_id
        ).single()

        master_out = {(r['type'], r['target']) for r in master_rels_res['out_rels'] if r.get('type')}
        master_in = {(r['type'], r['source']) for r in master_rels_res['in_rels'] if r.get('type')}

        # 2. Read relationships from duplicates
        dup_rels_res = s.run(
            """
            MATCH (dup:Fact) WHERE dup.id IN $duplicateIds AND dup.userId = $userId
            OPTIONAL MATCH (dup)-[out_r]->(out_t) WHERE out_t.id <> $masterId AND NOT out_t.id IN $duplicateIds
            OPTIONAL MATCH (in_t)-[in_r]->(dup) WHERE in_t.id <> $masterId AND NOT in_t.id IN $duplicateIds
            RETURN
                collect(DISTINCT {type: type(out_r), target: out_t.id, props: properties(out_r), dir: 'out'}) as out_rels,
                collect(DISTINCT {type: type(in_r), source: in_t.id, props: properties(in_r), dir: 'in'}) as in_rels
            """,
            masterId=master_id, duplicateIds=duplicate_ids, userId=user_id
        ).single()

        missing_out = []
        for r in dup_rels_res['out_rels']:
            if not r.get('type'): continue
            key = (r['type'], r['target'])
            if key not in master_out:
                missing_out.append(r)
                master_out.add(key)

        missing_in = []
        for r in dup_rels_res['in_rels']:
            if not r.get('type'): continue
            key = (r['type'], r['source'])
            if key not in master_in:
                missing_in.append(r)
                master_in.add(key)

        # 3. Create missing edges on the merge target (master)
        for r in missing_out:
            s.run(
                """
                MATCH (master:Fact {id: $masterId, userId: $userId})
                MATCH (target {id: $targetId})
                CALL apoc.create.relationship(master, $relType, $props, target) YIELD rel
                RETURN rel
                """,
                masterId=master_id, userId=user_id, targetId=r['target'], relType=r['type'], props=r['props']
            )

        for r in missing_in:
            s.run(
                """
                MATCH (master:Fact {id: $masterId, userId: $userId})
                MATCH (source {id: $sourceId})
                CALL apoc.create.relationship(source, $relType, $props, master) YIELD rel
                RETURN rel
                """,
                masterId=master_id, userId=user_id, sourceId=r['source'], relType=r['type'], props=r['props']
            )

        # 4. Delete relationships on duplicates before merging nodes so APOC doesn't duplicate them
        s.run(
            """
            MATCH (dup:Fact) WHERE dup.id IN $duplicateIds AND dup.userId = $userId
            MATCH (dup)-[r]-()
            DELETE r
            """,
            duplicateIds=duplicate_ids, userId=user_id
        )

        # 5. Merge node properties (and delete duplicate nodes)
        s.run(
            """
            MATCH (master:Fact {id: $masterId, userId: $userId})
            MATCH (dup:Fact) WHERE dup.id IN $duplicateIds AND dup.userId = $userId
            WITH master, collect(dup) as dups
            CALL apoc.refactor.mergeNodes([master] + dups, {
                properties: {
                    id: 'discard',
                    text: 'discard',
                    name: 'discard',
                    userId: 'discard',
                    timestamp: 'discard',
                    category: 'discard',
                    `*`: 'combine'
                }
            }) YIELD node
            RETURN count(*)
            """,
            masterId=master_id, duplicateIds=duplicate_ids, userId=user_id
        )

    # Delete duplicates from Qdrant
    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=duplicate_ids,
    )
    await publish_db_event(user_id, "memory_changed", {
        "action": "merge",
        "master_id": master_id,
        "duplicate_ids": duplicate_ids
    })





# ---------------------------------------------------------------------------
# Diary helpers
# ---------------------------------------------------------------------------

def _diary_id(user_id: str, timestamp: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"diary_{user_id}_{timestamp}"))


async def db_save_diary(content: str, user_id: str, timestamp: str, name: str) -> str:
    """Upsert a diary entry keyed by user + timestamp. Returns the ISO timestamp string.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # Derive a stable ID from user + timestamp so the same timestamp always maps to the same node
    doc_id   = _diary_id(user_id, timestamp)
    # Keep a plain date string for display / grouping purposes
    entry_date = timestamp[:10]
    vector   = await get_embedding(f"{name}: {content}" if name else content)

    # Qdrant — upsert by the stable doc_id so re-saving replaces the vector
    await qdrant.upsert(
        collection_name=DIARY_COLLECTION,
        points=[PointStruct(
            id=doc_id,
            vector=vector,
            payload={"content": content, "name": name, "date": entry_date, "timestamp": timestamp, "userId": user_id},
        )],
    )

    # Neo4j — MERGE on id so re-saving the same timestamp updates content in-place
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (d:DiaryEntry {id: $id, userId: $userId})
            SET d.date = $date, d.timestamp = $timestamp, d.content = $content, d.name = $name
            MERGE (u)-[:WROTE_DIARY]->(d)
            """,
            userId=user_id, id=doc_id, date=entry_date, timestamp=timestamp, content=content, name=name
        )

        # Automatic linking to People and Client facts
        # 1. Fetch relevant facts
        res = s.run(
            """
            MATCH (f:Fact {userId: $userId})
            WHERE f.category IN ['People', 'Client']
            RETURN f.id as id, f.text as text, f.aliases as aliases
            """,
            userId=user_id
        )
        
        content_lower = content.lower()
        mentioned_ids = []
        for r in res:
            name = r["text"].lower()
            # Check for name or aliases
            if name in content_lower:
                mentioned_ids.append(r["id"])
                continue
            
            aliases = r["aliases"]
            if aliases:
                if isinstance(aliases, list):
                    if any(a.lower() in content_lower for a in aliases):
                        mentioned_ids.append(r["id"])
                elif isinstance(aliases, dict):
                    if any(a.lower() in content_lower for a in aliases.keys()):
                        mentioned_ids.append(r["id"])

        # 2. Create MENTIONS links using the unique doc_id
        if mentioned_ids:
            s.run(
                """
                MATCH (d:DiaryEntry {id: $id, userId: $userId})
                MATCH (f:Fact) WHERE f.id IN $factIds
                MERGE (d)-[:MENTIONS]->(f)
                """,
                id=doc_id, userId=user_id, factIds=mentioned_ids
            )

    await publish_db_event(user_id, "diary_changed", {
        "action": "add",
        "id": doc_id,
        "date": entry_date,
        "timestamp": timestamp
    })
    return timestamp


async def db_search_diary(query: str, user_id: str, limit: int = 3, top_p: float = 0.4) -> list:
    """Vector-similarity search across the diary collection with mention enrichment."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    vec    = await get_embedding(query)
    filt   = Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))])
    result = await qdrant.query_points(
        collection_name=DIARY_COLLECTION,
        query=vec,
        query_filter=filt,
        limit=limit,
        with_payload=True,
        score_threshold=top_p,
    )
    
    entries = []
    query_lower = query.lower()
    for r in result.points:
        date = r.payload.get("date")
        entry_ts = r.payload.get("timestamp", date)
        content = r.payload.get("content")
        name = r.payload.get("name")
        score = r.score

        # Boost score if query matches the name
        if name:
            if query_lower == name.lower():
                score += 0.5
            elif query_lower in name.lower() or name.lower() in query_lower:
                score += 0.2
        
        # Enrich with mentions from Neo4j — match by stable entry id
        mentions = []
        with neo4j_driver.session() as s:
            m_res = s.run(
                "MATCH (d:DiaryEntry {id: $id, userId: $userId})-[:MENTIONS]->(f:Fact) RETURN f.id as id, f.text as text",
                id=str(r.id), userId=user_id
            )
            mentions = [{"id": mr["id"], "text": mr["text"]} for mr in m_res]
            
        entries.append({
            "id": r.id,
            "date": date,
            "timestamp": entry_ts,
            "content": content,
            "name": name,
            "score": score,
            "mentions": mentions
        })

    # Re-sort based on boosted scores
    entries.sort(key=lambda x: x["score"], reverse=True)
    return entries


async def db_delete_diary(entry_id: str, user_id: str) -> bool:
    """Delete a single diary entry by id. Returns True if the entry existed and was deleted."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # Verify ownership before deleting
    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (d:DiaryEntry {id: $id, userId: $userId}) RETURN d.id as id",
            id=entry_id, userId=user_id
        )
        if not result.single():
            return False

        # Delete from Neo4j (detach removes all relationships)
        s.run(
            "MATCH (d:DiaryEntry {id: $id, userId: $userId}) DETACH DELETE d",
            id=entry_id, userId=user_id
        )

    # Delete from Qdrant
    await qdrant.delete(
        collection_name=DIARY_COLLECTION,
        points_selector=PointIdsList(points=[entry_id]),
    )

    await publish_db_event(user_id, "diary_changed", {"action": "delete", "id": entry_id})
    return True


def db_list_diary_entries(user_id: str, from_ts: Optional[str] = None, to_ts: Optional[str] = None) -> list:
    """Return diary entries as (id, timestamp, name) tuples within optional time range.
    Defaults to last month if timestamps not provided."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    now = datetime.now(timezone.utc)
    default_from = (now - timedelta(days=30)).isoformat()
    default_to = now.isoformat()
    from_clause = from_ts or default_from
    to_clause = to_ts or default_to

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            WHERE d.timestamp >= $fromTs AND d.timestamp <= $toTs
            RETURN d.id as id, d.timestamp as timestamp, d.name as name
            ORDER BY d.timestamp DESC
            """,
            userId=user_id,
            fromTs=from_clause,
            toTs=to_clause,
        )
        return [(r["id"], str(r["timestamp"]) if r["timestamp"] else None, r.get("name") or "Unnamed")
                for r in result]


def db_list_diary(user_id: str) -> list:
    """Return all diary entries for a user from Neo4j with mention links, grouped by date."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    def format_ts_for_picker(ts):
        """Format timestamp to YYYY-MM-DDTHH:MM for browser date picker compatibility."""
        if not ts: return None
        # Handle Neo4j datetime or strings
        s_ts = ts.iso_format() if hasattr(ts, "iso_format") else str(ts)
        # Return YYYY-MM-DDTHH:MM (first 16 characters)
        return s_ts[:16]

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            OPTIONAL MATCH (d)-[:MENTIONS]->(f:Fact)
            RETURN d.id as id, d.date as date, d.content as content, d.timestamp as timestamp, d.name as name,
                   collect({id: f.id, text: f.text, name: f.name}) as mentions
            ORDER BY d.date DESC, d.timestamp DESC
            """,
            userId=user_id,
        )
        return [
            {
                "id": r["id"],
                "date": r["date"], 
                "content": r["content"], 
                "name": r.get("name") or "Unnamed Entry",
                "timestamp": format_ts_for_picker(r.get("timestamp")),
                "mentions": [m for m in r["mentions"] if m.get("id")]
            } for r in result
        ]


def db_get_graph(user_id: str) -> dict:
    """Return the entire knowledge graph for a user (nodes and edges)."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (f:Fact {userId: $userId})
            OPTIONAL MATCH (f)-[r]->(m)
            WHERE (m:Fact AND m.userId = $userId) OR (m:Category)
            RETURN f, type(r) as rel_type, m
            """,
            userId=user_id
        )
        
        node_map = {}
        edges = []
        edge_lookup = {}  # sig -> edge_dict for collapsing bidirectional links
        
        for r in result:
            f = r["f"]
            if f["id"] not in node_map:
                node_map[f["id"]] = {
                    "id": f["id"],
                    "label": "Fact",
                    "name": f.get("name") or f["text"],
                    "group": f.get("category", "General")
                }
            
            m = r["m"]
            rel = r["rel_type"]
            if m and rel:
                # Category nodes have 'name', Facts have 'id'
                m_label = "Category" if "name" in m and "id" not in m else "Fact"
                m_id = m.get("name") if m_label == "Category" else m.get("id")
                
                if m_id not in node_map:
                    if m_label == "Category":
                        node_map[m_id] = {
                            "id": m_id,
                            "label": "Category",
                            "name": m["name"],
                            "group": "CategoryNode"
                        }
                    else:
                        node_map[m_id] = {
                            "id": m["id"],
                            "label": "Fact",
                            "name": m.get("name") or m["text"],
                            "group": m.get("category", "General")
                        }
                
                edge_sig = (f["id"], m_id, rel)
                reverse_sig = (m_id, f["id"], rel)

                if reverse_sig in edge_lookup:
                    # Collapse bidirectional arrows with same label
                    edge_lookup[reverse_sig]["arrows"] = "to,from"
                elif edge_sig not in edge_lookup:
                    new_edge = {
                        "id": f"{f['id']}_{m_id}_{rel}",
                        "from": f["id"],
                        "to": m_id,
                        "label": rel,
                        "arrows": "to"
                    }
                    edge_lookup[edge_sig] = new_edge
                    edges.append(new_edge)
                
        return {
            "nodes": list(node_map.values()),
            "edges": edges
        }
