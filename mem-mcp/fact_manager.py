"""
fact_manager.py – Fact management, search, deduplication, and graph operations.
"""

from typing import List, Optional
import re
import uuid
import numpy as np
import difflib
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue, PointIdsList

from common import (
    get_qdrant, get_neo4j, logger, get_embedding, publish_db_event,
    COLLECTION_NAME
)

# ---------------------------------------------------------------------------
# People metadata extraction
# ---------------------------------------------------------------------------
def extract_people_metadata(name: Optional[str]) -> dict:
    """Extract first_name, last_name, aliases from a People name string."""
    if not name:
        return {}
    n = name.strip()
    if not n:
        return {}
    
    aliases = []
    if '(' in n and ')' in n:
        alias_start = n.index('(')
        alias_end = n.index(')', alias_start)
        alias = n[alias_start+1:alias_end].strip()
        n = (n[:alias_start] + n[alias_end+1:]).strip()
        if alias:
            aliases.append(alias)
    
    n = re.sub(r'\s+', ' ', n)
    parts = n.split()
    
    meta = {}
    if len(parts) >= 2:
        meta["first_name"] = parts[0]
        meta["last_name"] = parts[-1]
    elif len(parts) == 1:
        meta["first_name"] = parts[0]
    
    if aliases:
        meta["aliases"] = aliases
    
    return meta

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
    people_meta = extract_people_metadata(name) if category.lower() == "people" else {}
    meta = {**people_meta, **(metadata or {})}
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
    if new_cat.lower() == "people" and new_name:
        name_meta = extract_people_metadata(new_name)
        new_meta = {**name_meta, **new_meta}
    
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
               OR (size(f.name) > 3 AND toLower($query_str) CONTAINS toLower(f.name))
               OR toLower(f.text) CONTAINS toLower($query_str)
            """
        else:
            cypher = """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.name) CONTAINS toLower($query_str)
            """
        if category:
            cypher += " AND toLower(f.category) = toLower($category)"
        cypher += " RETURN f LIMIT 100"

        params = {"userId": user_id, "query_str": query}
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
            first = (meta.get("first_name") or "").lower()
            last = (meta.get("last_name") or "").lower()
            if first and query_lower == first:
                score = max(score, 2.3)
            elif first and first in query_lower:
                score = max(score, 1.9)
            elif last and query_lower == last:
                score = max(score, 2.3)
            elif last and last in query_lower:
                score = max(score, 1.9)
            # Fuzzy match surname to query surname
            if len(query_words) >= 2:
                query_surname = query_words[-1]
                if last:
                    s = difflib.SequenceMatcher(None, query_surname, last).ratio()
                    if s >= 0.7:
                        score = max(score, 1.4 + s * 0.6)

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

        # Boost score if query matches the name or first_name/last_name metadata
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
            # Also boost if first_name or last_name matches (including fuzzy)
            first = (metadata.get("first_name") or "").lower()
            last = (metadata.get("last_name") or "").lower()
            if first:
                s = difflib.SequenceMatcher(None, query_lower, first).ratio()
                if query_lower == first:
                    score += 0.8
                elif first in query_lower:
                    score += 0.4
                elif s >= 0.7:
                    score += s * 0.6
            if last:
                s = difflib.SequenceMatcher(None, query_lower, last).ratio()
                if query_lower == last:
                    score += 0.8
                elif last in query_lower:
                    score += 0.4
                elif s >= 0.7:
                    score += s * 0.6
            # Fuzzy name match boost
            if name:
                name_norm = name.lower().strip()
                s = difflib.SequenceMatcher(None, query_lower, name_norm).ratio()
                if s >= 0.7:
                    score += s * 0.8
                # Also check fuzzy match against just the surname (last word)
                name_words = name_norm.split()
                if len(name_words) >= 2:
                    surname = name_words[-1]
                    query_words_l = query_lower.split()
                    if len(query_words_l) >= 2:
                        query_surname = query_words_l[-1]
                        s_surname = difflib.SequenceMatcher(None, query_surname, surname).ratio()
                        if s_surname >= 0.7:
                            score += s_surname * 0.8
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


async def db_find_duplicates(user_id: str, category: str = "People", limit: int = 50, threshold: float = 0.75, max_cluster: int = 4):
    """
    Find potential duplicates in a category using multi-signal similarity and clustering.
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

            # Signal 9b: First name match but last names differ → penalize unless description is very similar
            if (is_people and p_i["first_name"] and p_j["first_name"]
                    and p_i["first_name"] == p_j["first_name"]
                    and p_i["last_name"] and p_j["last_name"]
                    and p_i["last_name"] != p_j["last_name"]):
                if vec_sim < 0.88:
                    signals = [s for s in signals if s < vec_sim - 0.1]

            # Signal 10: Full normalized name fuzzy match (only if both have last names)
            if p_i["norm_name"] and p_j["norm_name"] and p_i["last_name"] and p_j["last_name"]:
                full_ratio = difflib.SequenceMatcher(None, p_i["norm_name"], p_j["norm_name"]).ratio()
                if full_ratio >= 0.85:
                    signals.append(min(0.95, full_ratio))

            similarity = max(signals) if signals else vec_sim
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
                "name": item["name"],
                "similarity": round(avg_item_sim, 4),
            }
            members.append(member_info)

        avg_similarity = sum(cluster_scores) / len(cluster_scores) if cluster_scores else 0.0
        recommendation = "MERGE - high overlap" if avg_similarity > 0.9 else "MERGE - verify and combine"

        if avg_similarity < 0.85:
            continue

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
            OPTIONAL MATCH (dup)-[out_r]->(out_t) WHERE (out_t.id IS NULL) OR (out_t.id <> $masterId AND NOT out_t.id IN $duplicateIds)
            OPTIONAL MATCH (in_t)-[in_r]->(dup) WHERE (in_t.id IS NULL) OR (in_t.id <> $masterId AND NOT in_t.id IN $duplicateIds)
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
            if not r['target']:
                continue
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
            if not r['source']:
                continue
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



        # 6. Explicitly delete duplicate nodes as a safety measure to ensure they are removed from Neo4j
        s.run(
            """
            MATCH (dup:Fact) WHERE dup.id IN $duplicateIds AND dup.userId = $userId
            DETACH DELETE dup
            """,
            duplicateIds=duplicate_ids, userId=user_id
        )

    # Delete duplicates from Qdrant
    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=PointIdsList(points=duplicate_ids),
    )



    await publish_db_event(user_id, "memory_changed", {
        "action": "merge",
        "master_id": master_id,
        "duplicate_ids": duplicate_ids
    })


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


# ---------------------------------------------------------------------------
# Startup orphan sync: detect and fix Qdrant ↔ Neo4j inconsistencies
# ---------------------------------------------------------------------------
async def sync_orphans():
    """Detect and fix Qdrant ↔ Neo4j orphans for every user on startup."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        logger.warning("sync_orphans: DB not available, skipping")
        return

    with neo4j_driver.session() as s:
        user_rows = list(s.run("MATCH (f:Fact) RETURN DISTINCT f.userId AS userId"))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        logger.info("sync_orphans: no users found")
        return

    total_deleted = 0
    total_reembedded = 0

    for user_id in user_ids:
        # 1. Get all fact IDs + text from Neo4j
        with neo4j_driver.session() as s:
            facts = list(s.run(
                "MATCH (f:Fact {userId: $userId}) RETURN f.id AS id, f.text AS text, f.name AS name",
                userId=user_id
            ))
        neo4j_ids = {r["id"] for r in facts}
        neo4j_map  = {r["id"]: {"text": r["text"], "name": r.get("name", "")} for r in facts}
        if not neo4j_ids:
            continue

        # 2. Scroll all Qdrant point IDs for this user
        qdrant_ids = set()
        offset = None
        while True:
            result = await qdrant.scroll(
                collection_name=COLLECTION_NAME,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
                scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
            )
            points, next_offset = result
            for p in points:
                qdrant_ids.add(str(p.id))
            if next_offset is None:
                break
            offset = next_offset

        # 3. Qdrant-only → delete
        orphan_qdrant = qdrant_ids - neo4j_ids
        if orphan_qdrant:
            logger.info(f"sync_orphans [{user_id}]: deleting {len(orphan_qdrant)} Qdrant-only orphans")
            await qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=list(orphan_qdrant)),
            )
            total_deleted += len(orphan_qdrant)

        # 4. Neo4j-only → re-embed
        orphan_neo4j = neo4j_ids - qdrant_ids
        if orphan_neo4j:
            logger.info(f"sync_orphans [{user_id}]: re-embedding {len(orphan_neo4j)} Neo4j-only records")
            for oid in orphan_neo4j:
                info = neo4j_map[oid]
                text = info["text"]
                name = info["name"]
                vector = await get_embedding(text)
                await qdrant.upsert(
                    collection_name=COLLECTION_NAME,
                    points=[PointStruct(id=oid, vector=vector, payload={"text": text, "name": name, "userId": user_id})],
                )
            total_reembedded += len(orphan_neo4j)

    if total_deleted or total_reembedded:
        logger.info(f"sync_orphans done: deleted {total_deleted} Qdrant orphans, re-embedded {total_reembedded} Neo4j orphans")
    else:
        logger.info("sync_orphans: nothing to fix")
