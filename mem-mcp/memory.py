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
import logging
import base64
import httpx
import numpy as np
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
# Increase timeout for LLM generation
HTTP_TIMEOUT = float(os.getenv("MEM_HTTP_TIMEOUT", "300.0"))
BASE_URL       = os.getenv("BASE_URL",            "").rstrip("/")

COLLECTION_NAME  = "ea_memories"
DIARY_COLLECTION = "ea_diary"

# ---------------------------------------------------------------------------
# Global DB client references (lazily populated)
# ---------------------------------------------------------------------------
_qdrant: Optional[AsyncQdrantClient] = None
_neo4j_driver = None
_db_initialized = False
_db_lock = asyncio.Lock()

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
            except Exception as e:
                logger.error(f"Neo4j init error: {e}")
    return _neo4j_driver

# Removed top-level initialize_databases call and legacy globals


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
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        response_text = resp.json()["response"]

        # Log prompt and response to file for validation
        try:
            os.makedirs("logs", exist_ok=True)
            timestamp = datetime.now().isoformat().replace(":", "-")
            with open(f"logs/ollama_generate_{timestamp}.txt", "w", encoding="utf-8") as f:
                f.write(f"=== SYSTEM ===\n{system or 'None'}\n\n")
                f.write(f"=== PROMPT ===\n{prompt}\n\n")
                f.write(f"=== RESPONSE ===\n{response_text}\n")
        except Exception as e:
            logger.error(f"Failed to write Ollama log: {e}")

        return response_text


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

async def db_add_memory(text: str, category: str, user_id: str, metadata: Optional[dict] = None, title: Optional[str] = None) -> str:
    """Insert a fact into Qdrant (vector) and Neo4j (graph). Returns the new ID."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    doc_id   = str(uuid.uuid4())
    category = category.strip().capitalize()
    meta     = metadata or {}
    # Use title + text for embedding for better context if title exists
    embed_text = f"{title}: {text}" if title else text
    vector   = await get_embedding(embed_text)

    # Qdrant
    payload = {"text": text, "title": title, "category": category, "userId": user_id, "metadata": meta}
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
            CREATE (f:Fact {id: $id, text: $text, title: $title, category: $category,
                            timestamp: datetime(), userId: $userId})
            SET f += $metadata
            CREATE (u)-[:KNOWS]->(f)
            CREATE (f)-[:IN_CATEGORY]->(c)
            """,
            userId=user_id, category=category, id=doc_id, text=text, title=title,
            metadata=meta
        )

    return doc_id


async def db_update_memory(memory_id: str, title: Optional[str], text: Optional[str], category: Optional[str], user_id: str, metadata: Optional[dict] = None) -> bool:
    """
    Update title, text, category, or metadata of an existing fact.
    Re-embeds if text changes. Returns True if the record was found.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # Get current to see what's changing
    with neo4j_driver.session() as s:
        res = s.run("MATCH (f:Fact {id: $id, userId: $userId}) RETURN f", id=memory_id, userId=user_id)
        existing = res.single()
        if not existing: return False
        old_fact = existing["f"]

    new_text = text if text is not None else old_fact.get("text")
    new_title = title if title is not None else old_fact.get("title")
    new_cat  = category.strip().capitalize() if category else old_fact.get("category")
    new_meta = metadata or {} 
    
    # Qdrant Update
    # Re-embed if text OR title changes
    embed_text = f"{new_title}: {new_text}" if new_title else new_text
    vector = await get_embedding(embed_text) if (text is not None or title is not None) else None
    
    # Prepare payload, converting Neo4j types to JSON-serializable ones
    payload = {}
    for k, v in dict(old_fact).items():
        if hasattr(v, "iso_format"):
            payload[k] = v.iso_format()
        else:
            payload[k] = v

    if text is not None: payload["text"] = new_text
    if title is not None: payload["title"] = new_title
    if category is not None: payload["category"] = new_cat
    if metadata:
        current_meta = payload.get("metadata", {})
        if isinstance(current_meta, str): # Safety check if metadata was stored as string
             import json
             try: current_meta = json.loads(current_meta)
             except: current_meta = {}
        current_meta.update(new_meta)
        payload["metadata"] = current_meta

    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=memory_id,
            vector=vector or await get_embedding(embed_text),
            payload=payload,
        )],
    )

    # Neo4j Update
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (f:Fact {id: $id, userId: $userId})
            SET f.text = $text, f.title = $title, f.category = $category, f.updatedAt = datetime()
            SET f += $metadata
            WITH f
            OPTIONAL MATCH (f)-[r:IN_CATEGORY]->(:Category)
            DELETE r
            WITH f
            MERGE (c:Category {name: $category})
            CREATE (f)-[:IN_CATEGORY]->(c)
            """,
            id=memory_id, userId=user_id, text=new_text, title=new_title, category=new_cat, metadata=new_meta
        )
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
        return (rec and rec["n"] > 0) or True  # qdrant already done


async def db_link_facts(source_id: str, target_id: str, rel_type: str, metadata: dict, user_id: str):
    """Create a relationship between two facts in Neo4j."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    rel_type = rel_type.upper().replace(" ", "_")
    with neo4j_driver.session() as s:
        s.run(
            f"""
            MATCH (a:Fact {{id: $sid, userId: $userId}})
            MATCH (b:Fact {{id: $tid, userId: $userId}})
            MERGE (a)-[r:{rel_type}]->(b)
            SET r += $metadata
            """,
            sid=source_id, tid=target_id, userId=user_id, metadata=metadata
        )


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


async def db_search_memories(query: str, user_id: str, limit: int = 5, category: Optional[str] = None, top_p: float = 0.4) -> list:
    """Vector-similarity search with optional category filter. Also does a basic substring match on titles."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Databases not connected.")

    # 1. Neo4j exact/substring match on title or aliases
    # We do a quick lookup for nodes containing the query
    exact_matches = []
    query_lower = query.lower()

    with neo4j_driver.session() as s:
        # Only do broad CONTAINS match on text if the query is reasonably long to avoid massive irrelevant noise
        if len(query) > 3:
            cypher = """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.title) CONTAINS toLower($query_str)
               OR toLower(f.text) CONTAINS toLower($query_str)
            """
        else:
            cypher = """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.title) CONTAINS toLower($query_str)
            """
        if category:
            cypher += " AND f.category = $category"
        cypher += " RETURN f LIMIT $limit"

        params = {"userId": user_id, "query_str": query, "limit": limit}
        if category:
            params["category"] = category.strip().capitalize()

        neo_result = s.run(cypher, **params)
        for r in neo_result:
            f = r["f"]
            # Construct a result matching Qdrant format
            meta = {k: v for k, v in f.items() if k not in {"id", "text", "title", "category", "timestamp", "userId"}}

            score = 1.0
            title = f.get("title", "")
            if title and query_lower == title.lower():
                score = 2.0
            elif title and query_lower in title.lower():
                score = 1.5

            exact_matches.append({
                "id": f["id"],
                "text": f["text"],
                "title": f.get("title"),
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
        title = r.payload.get("title")

        # Boost score if query matches the title
        if title:
            if query_lower == title.lower():
                score += 0.5
            elif query_lower in title.lower() or title.lower() in query_lower:
                score += 0.2

        # Boost score if query matches an alias
        if aliases and isinstance(aliases, dict):
            for alias, confidence in aliases.items():
                if query_lower == alias.lower():
                    # Exact alias match. Boost based on confidence.
                    try: score += (float(confidence) * 0.2)
                    except: pass
                elif query_lower in alias.lower() or alias.lower() in query_lower:
                    # Partial match
                    try: score += (float(confidence) * 0.05)
                    except: pass
        
        results.append({
            "id": r.id,
            "text": r.payload.get("text"),
            "title": r.payload.get("title"),
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
                   collect({rel: type(r), target_id: target.id, target_text: target.text, target_title: target.title}) as links
            ORDER BY c.name ASC, f.timestamp DESC
            """,
            userId=user_id,
        )
        memories = []
        for r in result:
            f_node = r["f"]
            # Extract metadata (all properties except core ones)
            core_keys = {"id", "text", "title", "category", "timestamp", "userId"}
            metadata = {}
            for k, v in f_node.items():
                if k not in core_keys:
                    metadata[k] = v.iso_format() if hasattr(v, "iso_format") else v
            
            # Clean up links (remove null targets)
            links = [l for l in r["links"] if l.get("target_id")]

            memories.append({
                "id":        f_node["id"],
                "text":      f_node["text"],
                "title":     f_node.get("title"),
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


async def db_find_duplicates(user_id: str, category: str = "People", limit: int = 50, threshold: float = 0.85):
    """
    Find potential duplicates in a category using embedding similarity and clustering.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # 1. Fetch items from Neo4j (fetch a larger pool to ensure we don't miss duplicates)
    fetch_limit = max(limit * 5, 500)
    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (f:Fact {userId: $userId, category: $category})
            RETURN f
            ORDER BY f.timestamp DESC
            LIMIT $limit
            """,
            userId=user_id, category=category.strip().capitalize(), limit=fetch_limit
        )
        items = []
        for r in result:
            f_node = r["f"]
            # Extract metadata (all properties except core ones)
            core_keys = {"id", "text", "category", "timestamp", "userId"}
            metadata = {}
            for k, v in f_node.items():
                if k not in core_keys:
                    metadata[k] = v.iso_format() if hasattr(v, "iso_format") else v
            
            items.append({
                "id": f_node["id"],
                "text": f_node["text"],
                "title": f_node.get("title"),
                "category": f_node.get("category"),
                "metadata": metadata
            })

    if not items:
        return []

    # 2. Get vectors from Qdrant
    ids = [item["id"] for item in items]
    points = await qdrant.retrieve(
        collection_name=COLLECTION_NAME,
        ids=ids,
        with_vectors=True
    )
    
    # Map id to vector
    vectors = {p.id: p.vector for p in points if p.vector}
    
    # Filter items that have vectors
    items_with_vectors = [item for item in items if item["id"] in vectors]
    if not items_with_vectors:
        return []

    # 3. Compute similarity and find pairs
    candidate_pairs = []
    num_items = len(items_with_vectors)
    for i in range(num_items):
        for j in range(i + 1, num_items):
            id_i = items_with_vectors[i]["id"]
            id_j = items_with_vectors[j]["id"]
            vec_i = np.array(vectors[id_i])
            vec_j = np.array(vectors[id_j])
            
            # Cosine similarity
            norm_i = np.linalg.norm(vec_i)
            norm_j = np.linalg.norm(vec_j)
            if norm_i == 0 or norm_j == 0:
                continue

            similarity = np.dot(vec_i, vec_j) / (norm_i * norm_j)

            title_i = items_with_vectors[i].get("title")
            title_j = items_with_vectors[j].get("title")

            # Boost similarity based on title matches
            if title_i and title_j:
                ti, tj = title_i.strip().lower(), title_j.strip().lower()
                if ti == tj:
                    similarity = max(similarity, 1.0)
                else:
                    # Tokenize to check for matching words (e.g. matching first names or last names)
                    ti_words = set(ti.split())
                    tj_words = set(tj.split())
                    common_words = ti_words.intersection(tj_words)

                    if common_words:
                        # Filter out very short common words to avoid matching on 'the', 'a', etc. (unless it's a name)
                        valid_common = [w for w in common_words if len(w) > 2 or category.strip().capitalize() == "People"]
                        if valid_common:
                            # Boost based on ratio of matched words to the shorter title
                            min_words = min(len(ti_words), len(tj_words))
                            match_ratio = len(valid_common) / min_words if min_words > 0 else 0

                            boost = 0.25 * match_ratio
                            similarity = max(similarity + boost, 0.86 if match_ratio >= 1.0 else similarity + boost)

            if similarity >= threshold:
                candidate_pairs.append((i, j, float(similarity)))

    # 4. Cluster using Union-Find
    parent = list(range(num_items))
    def find(i):
        if parent[i] == i:
            return i
        parent[i] = find(parent[i])
        return parent[i]

    def union(i, j):
        root_i = find(i)
        root_j = find(j)
        if root_i != root_j:
            parent[root_i] = root_j

    for i, j, score in candidate_pairs:
        union(i, j)

    # Group into clusters
    clusters_map = {}
    for i in range(num_items):
        root = find(i)
        if root not in clusters_map:
            clusters_map[root] = []
        clusters_map[root].append(i)

    # Format results
    final_clusters = []
    cluster_id_counter = 1
    for root, member_indices in clusters_map.items():
        if len(member_indices) < 2:
            continue
        
        members = []
        cluster_scores = []
        for idx in member_indices:
            item = items_with_vectors[idx]
            # Find similarity with other members in the cluster
            # We use the average similarity of this item to other members
            item_scores = []
            for i, j, score in candidate_pairs:
                if (i == idx and j in member_indices) or (j == idx and i in member_indices):
                    item_scores.append(score)
            
            avg_item_sim = sum(item_scores) / len(item_scores) if item_scores else 1.0
            cluster_scores.extend(item_scores)
            
            # Merge metadata into top level for easier reading, like the user example
            member_info = {
                "id": item["id"],
                "text": item["text"],
                "title": item["title"],
                "similarity": round(avg_item_sim, 4)
            }
            member_info.update(item["metadata"])
            members.append(member_info)
        
        avg_similarity = sum(cluster_scores) / len(cluster_scores) if cluster_scores else 0.0
        
        recommendation = "MERGE - high overlap" if avg_similarity > 0.9 else "MERGE - verify and combine"
        
        final_clusters.append({
            "cluster_id": cluster_id_counter,
            "members": members,
            "avg_similarity": round(avg_similarity, 4),
            "recommendation": recommendation
        })
        cluster_id_counter += 1

    # Sort clusters by avg_similarity DESC
    final_clusters.sort(key=lambda x: x["avg_similarity"], reverse=True)
    
    return final_clusters


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
                    title: 'discard',
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


async def db_smart_merge_memories(master_id: str, duplicate_ids: List[str], user_id: str, ctx: Any = None):
    """
    Advanced merge that uses an LLM to consolidate multiple descriptions into one.
    Uses MCP sampling if context is available and prompt is long.
    """
    neo4j_driver = get_neo4j()
    
    # 1. Fetch current texts and relationships
    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (f:Fact) WHERE f.id IN ([ $masterId ] + $duplicateIds) AND f.userId = $userId
            OPTIONAL MATCH (f)-[r]->(target:Fact) WHERE type(r) <> 'IN_CATEGORY' AND type(r) <> 'KNOWS' AND target.userId = $userId
            RETURN f.text as text, f.title as title, collect({rel: type(r), target_title: target.title, target_text: target.text}) as links
            """,
            masterId=master_id, duplicateIds=duplicate_ids, userId=user_id
        )
        texts = []
        for r in res:
            entry = f"**{r['title'] or 'Untitled'}**\n{r['text']}"
            links = [l for l in r["links"] if l.get("rel")]
            if links:
                entry += "\n\n**Known Graph Relationships:**\n"
                for l in links:
                    target_desc = l.get('target_title') or (l.get('target_text', '')[:50] + "...")
                    entry += f"- {l['rel']} -> {target_desc}\n"
            texts.append(entry)
        
    if not texts:
        return

    # 2. Use LLM to consolidate
    system = (
        "You are a meticulous knowledge graph curator. Your job is to consolidate multiple overlapping "
        "descriptions into one comprehensive master record. You MUST preserve ALL specific details: "
        "names, dates, technical specifics, numbers, roles, affiliations, and action items. "
        "Do NOT generalize or summarize away granular facts."
    )

    prompt = (
        "Combine the following separate memories about the same entity into one single, cohesive, "
        "and detailed markdown-formatted description.\n\n"
        "CRITICAL INSTRUCTIONS:\n"
        "1. Extract and include EVERY unique fact, name, date, decision, role, and technical detail from ALL provided memories.\n"
        "2. Do NOT summarize or generalize. Instead, synthesize all granular information into a readable, comprehensive format.\n"
        "3. Use markdown bullet points or sections if there are multiple distinct topics (e.g., 'Background', 'Interactions', 'Technical Details').\n"
        "4. Preserve ANY markdown links (e.g. [text](url) or [[text]]) that exist in the original texts.\n"
        "5. Incorporate any 'Known Graph Relationships' explicitly mentioned in the inputs into the text narrative.\n"
        "6. Avoid repeating the same exact information, but do not lose nuance when resolving overlapping facts.\n\n"
        "MEMORIES TO CONSOLIDATE:\n"
        "-----------------------\n"
    ) + "\n\n-----------------------\n\n".join(texts)
    
    # Use MCP sampling if context is available and prompt is moderately long (> 1000 chars)
    if ctx and hasattr(ctx, "sample") and len(prompt) > 1000:
        try:
            from mcp.types import SamplingMessage, TextContent
            result = await ctx.sample(
                messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
                system_prompt=system
            )
            if result and result.text:
                new_text = result.text
            else:
                new_text = await get_llm_completion(prompt, system)
        except Exception as e:
            logger.warning(f"MCP Sampling failed during smart merge, falling back to local LLM: {e}")
            new_text = await get_llm_completion(prompt, system)
    else:
        new_text = await get_llm_completion(prompt, system)
    
    # 3. Update master with consolidated text (this also updates vector)
    await db_update_memory(master_id, new_text, None, user_id)
    
    # 4. Perform the graph-level merge (relationships and other metadata)
    await db_merge_memories(master_id, duplicate_ids, user_id)


# ---------------------------------------------------------------------------
# Diary helpers
# ---------------------------------------------------------------------------

def _diary_id(user_id: str, entry_date: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"diary_{user_id}_{entry_date}"))


async def db_save_diary(content: str, user_id: str, date: Optional[str] = None) -> str:
    """Save a diary entry (allows multiple entries per day). Returns the entry date string."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    entry_date = date if date else datetime.now().strftime("%Y-%m-%d")
    doc_id     = str(uuid.uuid4())
    vector     = await get_embedding(content)

    # Qdrant
    await qdrant.upsert(
        collection_name=DIARY_COLLECTION,
        points=[PointStruct(
            id=doc_id,
            vector=vector,
            payload={"content": content, "date": entry_date, "userId": user_id},
        )],
    )

    # Neo4j
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            CREATE (d:DiaryEntry {id: $id, date: $date, content: $content, 
                                 userId: $userId, timestamp: datetime()})
            MERGE (u)-[:WROTE_DIARY]->(d)
            """,
            userId=user_id, date=entry_date, content=content, id=doc_id
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

    return entry_date


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
    for r in result.points:
        date = r.payload.get("date")
        content = r.payload.get("content")
        
        # Enrich with mentions from Neo4j
        mentions = []
        with neo4j_driver.session() as s:
            m_res = s.run(
                "MATCH (d:DiaryEntry {date: $date, userId: $userId})-[:MENTIONS]->(f:Fact) RETURN f.id as id, f.text as text",
                date=date, userId=user_id
            )
            mentions = [{"id": mr["id"], "text": mr["text"]} for mr in m_res]
            
        entries.append({
            "id": r.id,
            "date": date,
            "content": content,
            "score": r.score,
            "timestamp": None, # Qdrant payload doesn't have it yet, we could fetch from Neo4j if needed
            "mentions": mentions
        })
    return entries


def db_list_diary(user_id: str) -> list:
    """Return all diary entries for a user from Neo4j with mention links, grouped by date."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            OPTIONAL MATCH (d)-[:MENTIONS]->(f:Fact)
            RETURN d.id as id, d.date as date, d.content as content, d.timestamp as timestamp,
                   collect({id: f.id, text: f.text, title: f.title}) as mentions
            ORDER BY d.date DESC, d.timestamp DESC
            """,
            userId=user_id,
        )
        return [
            {
                "id": r["id"],
                "date": r["date"], 
                "content": r["content"], 
                "timestamp": r["timestamp"].iso_format() if r.get("timestamp") and hasattr(r["timestamp"], "iso_format") else None,
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
        seen_edges = set()
        
        for r in result:
            f = r["f"]
            if f["id"] not in node_map:
                node_map[f["id"]] = {
                    "id": f["id"],
                    "label": "Fact",
                    "title": f.get("title") or f["text"],
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
                            "title": m["name"],
                            "group": "CategoryNode"
                        }
                    else:
                        node_map[m_id] = {
                            "id": m["id"],
                            "label": "Fact",
                            "title": m["text"],
                            "group": m.get("category", "General")
                        }
                
                edge_sig = (f["id"], m_id, rel)
                if edge_sig not in seen_edges:
                    seen_edges.add(edge_sig)
                    edges.append({
                        "from": f["id"],
                        "to": m_id,
                        "label": rel
                    })
                
        return {
            "nodes": list(node_map.values()),
            "edges": edges
        }

