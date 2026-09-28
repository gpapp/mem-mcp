"""
fact_manager.py – Fact management, search, deduplication, and graph operations.
"""

from typing import List, Optional
import os
import re
import time
import uuid
import numpy as np
import difflib
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue, PointIdsList

from common import (
    get_qdrant, get_neo4j, logger, get_embedding, get_llm_response, publish_db_event,
    log_search_stats, COLLECTION_NAME, DIARY_COLLECTION
)
from client_manager import (
    db_create_client, db_create_context, db_list_clients,
    db_resolve_client, db_resolve_context,
    link_fact_to_client, link_fact_to_context,
    _resolve_client_by_id, _resolve_context_by_id,
    infer_scope_from_text, db_get_client_status_map,
    INFERRED_SCOPE_BOOST, INACTIVE_PENALTY
)
from matching_utils import (
    MIN_MATCH_CONFIDENCE,
    VECTOR_CEIL,
    VECTOR_FLOOR,
    cluster_has_core,
    combine_duplicate_signals,
    identity_confidence,
    looks_like_person_name,
    people_match_allowed,
    scopes_compatible,
    validate_merge_ids,
)
from chunking import (
    CHUNK_FETCH_MULTIPLIER,
    RECHUNK_CONCURRENCY,
    RECHUNK_ENABLED,
    RECHUNK_LIMIT,
    build_chunk_payloads,
    chunk_text_of,
    parent_of,
    plan_chunks,
    rechunk_candidates,
    strip_chunk_meta,
)

# Max unconfirmed (below top_p) results appended after the confident set.
WEAK_RESULT_LIMIT = 2

# The graph is drawn by vis.js in the browser, so the practical ceiling is
# render time rather than memory. A vault of several thousand records produces
# a hairball that reads as noise and is slow enough to feel broken, which is
# worse than showing the most connected slice and saying it was capped.
GRAPH_MAX_NODES = max(50, int(os.getenv("MEM_GRAPH_MAX_NODES", "600")))


# ---------------------------------------------------------------------------
# Chunked vector writes
#
# A single vector over a 40k-character record is a lossy average — the answer to
# "which meeting did we decide the renewal term in" may live in paragraph 14 and
# be invisible in the mean. A long record therefore gets one point per chunk, so
# a query can match the part that actually contains it.
#
# Two invariants make this safe to adopt incrementally:
#   * chunk 0 keeps ``id == <fact id>``, so every existing id-based path (and
#     every record short enough not to need chunking) is byte-for-byte unchanged.
#   * chunk ids are derived with uuid5 from the parent, so re-writing a record
#     overwrites the same points instead of accumulating orphans.
# ---------------------------------------------------------------------------
async def _upsert_fact_points(qdrant, record_id: str, text: str, base_payload: dict,
                               prefix: Optional[str] = None, replace: bool = False) -> int:
    """Write one point, or N when the text needs chunking. Returns the count.

    ``prefix`` is prepended to every chunk before embedding (the fact name), so
    each chunk carries the same context the single-vector path used to encode
    once.

    ``replace`` drops the record's previous points first. It is required for an
    update: a shorter text produces fewer chunks, and without this the tail
    chunks of the old, longer version survive with stale text and are returned
    by search as though they were current.
    """
    # build_chunk_payloads is the single decision point: it returns one untagged
    # point for a short record and N chunk points for a long one.
    parts = build_chunk_payloads(record_id, text, strip_chunk_meta(base_payload))
    if len(parts) == 1:
        vector = await get_embedding(f"{prefix}: {text}" if prefix else text)
        if replace:
            # A long record edited down to a short one stops being chunked; its
            # old chunks would otherwise outlive the text they describe.
            await _delete_fact_chunks(qdrant, record_id)
        await qdrant.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(id=record_id, vector=vector, payload=parts[0]["payload"])],
        )
        return 1

    points = []
    for part in parts:
        body = chunk_text_of(part["payload"])
        vector = await get_embedding(f"{prefix}: {body}" if prefix else body)
        points.append(PointStruct(id=part["id"], vector=vector, payload=part["payload"]))

    if replace:
        # Every chunk embeds before anything is written, so a rejected embed
        # still leaves the previous version searchable rather than deleting it
        # and failing to replace it.
        await _delete_fact_chunks(qdrant, record_id)

    await qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
    logger.info(f"[chunking] {record_id}: {len(points)} chunk points written")
    return len(points)


async def find_chunk_family(qdrant, record_id: str, collection: str) -> list:
    """Every Qdrant point id belonging to a record, chunked or not.

    Chunk ids are derived, not stored on the record, so a caller that only knows
    ``record_id`` cannot enumerate the family. Rather than guessing a chunk
    count from the text (which is gone by delete time) this filters on the
    ``parentId`` that every chunk carries.

    Returns ``[record_id]`` when the record is not chunked, so a single call
    covers both shapes and the delete path never needs to know which it was.
    """
    ids = [str(record_id)]
    offset = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[FieldCondition(key="parentId", match=MatchValue(value=str(record_id)))]),
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for point in points:
            pid = str(point.id)
            if pid not in ids:
                ids.append(pid)
        if offset is None:
            break
    return ids


async def _delete_fact_chunks(qdrant, record_id: str) -> None:
    """Delete every point belonging to a fact, chunked or not.

    A plain ``delete(record_id)`` would leave chunks 1..N-1 behind as orphans for
    ``sync_orphans()`` to find.
    """
    ids = await find_chunk_family(qdrant, record_id, COLLECTION_NAME)
    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=PointIdsList(points=ids) if len(ids) > 1 else ids,
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
async def db_add_memory(text: str, category: str, user_id: str, metadata: Optional[dict] = None, name: Optional[str] = None, client_id: Optional[str] = None, context_id: Optional[str] = None) -> str:
    """Insert a fact into Qdrant (vector) and Neo4j (graph). Returns the new ID."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    doc_id   = str(uuid.uuid4())
    category = category.strip().capitalize()
    people_meta = extract_people_metadata(name) if category.lower() == "people" else {}
    meta = {**people_meta, **(metadata or {})}

    # Qdrant
    payload = {"text": text, "name": name, "category": category, "userId": user_id, "metadata": meta}

    # Add client/context info to payload if provided
    if client_id:
        client_info = _resolve_client_by_id(client_id, user_id)
        if client_info:
            payload["clientId"] = client_id
            payload["clientName"] = client_info["name"]
    if context_id:
        ctx_info = _resolve_context_by_id(context_id, user_id)
        if ctx_info:
            payload["contextId"] = context_id
            payload["contextName"] = ctx_info["name"]

    # Embeds last, after the payload is final, so a rejected embed leaves no
    # half-written record: a long text is split and each chunk is embedded
    # separately, but the failure still happens before anything is written.
    await _upsert_fact_points(qdrant, doc_id, text, payload, prefix=name)

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

    # Link to Client if provided
    if client_id:
        await link_fact_to_client(doc_id, client_id, user_id)

    # Link to Context if provided
    if context_id:
        await link_fact_to_context(doc_id, context_id, user_id)

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
    needs_embed = text is not None or name is not None

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

    if needs_embed:
        # replace=True: a shortened text produces fewer chunks, and the old
        # tail would otherwise survive and be returned as current text.
        await _upsert_fact_points(qdrant, memory_id, new_text or "", payload,
                                   prefix=new_name, replace=True)
    else:
        # Only category/metadata changed, so the vector still describes the
        # text. It has to reach every chunk: filtering on category is done at
        # query time against the payload, so a chunk left behind would make the
        # record unfindable by its own new category.
        family = await find_chunk_family(qdrant, memory_id, COLLECTION_NAME)
        await qdrant.set_payload(
            collection_name=COLLECTION_NAME,
            payload=strip_chunk_meta(payload),
            points=PointIdsList(points=family) if len(family) > 1 else family,
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

    await _delete_fact_chunks(qdrant, memory_id)
    with neo4j_driver.session() as s:
        # Find category to check for orphans after deletion
        cat_res = s.run(
            "MATCH (f:Fact {id: $id, userId: $userId})-[:IN_CATEGORY]->(c:Category) RETURN c.name as cat",
            id=memory_id, userId=user_id
        ).single()
        category_to_check = cat_res["cat"] if cat_res else None

    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (f:Fact {id: $id, userId: $userId}) DETACH DELETE f RETURN count(f) as n",
            id=memory_id, userId=user_id,
        )
        rec = result.single()
        if (rec and rec["n"] > 0):
            deleted = (rec and rec["n"] > 0)

        if category_to_check:
            # Prune category if it has no more facts
            s.run("MATCH (c:Category {name: $cat}) WHERE NOT (c)<-[:IN_CATEGORY]-() DETACH DELETE c", cat=category_to_check)

        if deleted:
            # Delete from Qdrant only if found in Neo4j
            await _delete_fact_chunks(qdrant, memory_id)

            await publish_db_event(user_id, "memory_changed", {
                "action": "delete",
                "id": memory_id
            })
            return True
        return True # Qdrant already done
        return False


async def db_add_fact_relevant(fact_id: str, client_id: str, user_id: str):
    """Create a RELEVANT_TO relationship from a fact to a client or a project.

    The target is a Client or a Context; both are "related scope" and the UI
    offers whichever the user picked. The relationship type is RELEVANT_TO for
    both, so links that already exist against clients keep working untouched and
    no migration is involved — only the label match in this query changed.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (f:Fact {id: $factId, userId: $userId})
            MATCH (c {id: $clientId, userId: $userId})
            WHERE c:Client OR c:Context
            MERGE (f)-[:RELEVANT_TO]->(c)
            """,
            factId=fact_id, clientId=client_id, userId=user_id,
        )

    await publish_db_event(user_id, "memory_changed", {"action": "relevant_add", "id": fact_id})


async def db_remove_fact_relevant(fact_id: str, client_id: str, user_id: str):
    """Remove a RELEVANT_TO relationship from a fact to a client or project."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (f:Fact {id: $factId, userId: $userId})-[r:RELEVANT_TO]->(c {id: $clientId, userId: $userId})
            WHERE c:Client OR c:Context
            DELETE r
            """,
            factId=fact_id, clientId=client_id, userId=user_id,
        )

    await publish_db_event(user_id, "memory_changed", {"action": "relevant_remove", "id": fact_id})


async def db_link_facts(source_id: str, target_id: str, rel_type: str, metadata: dict, user_id: str):
    """Create a relationship between two nodes in Neo4j.

    Handles:
    - Fact ↔ Fact → bidirectional REL_TYPE (existing behavior)
    - DiaryEntry → Fact → unidirectional MENTIONS
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    rel_type = rel_type.upper().replace(" ", "_")

    with neo4j_driver.session() as s:
        # Determine node labels
        a_label = s.run(
            "MATCH (n {id: $id, userId: $userId}) RETURN head(labels(n)) AS label",
            id=source_id, userId=user_id
        ).single()
        b_label = s.run(
            "MATCH (n {id: $id, userId: $userId}) RETURN head(labels(n)) AS label",
            id=target_id, userId=user_id
        ).single()

        if not a_label or not b_label:
            raise RuntimeError(f"Cannot link: node not found (source={source_id}, target={target_id})")

        a_label = a_label["label"]
        b_label = b_label["label"]

        if a_label == "Fact" and b_label == "Fact":
            # Bidirectional fact-to-fact
            s.run(
                f"""
                MATCH (a:Fact {{id: $sid, userId: $userId}})
                MATCH (b:Fact {{id: $tid, userId: $userId}})
                MERGE (a)-[r:{rel_type}]->(b)
                SET r += $metadata
                """,
                sid=source_id, tid=target_id, userId=user_id, metadata=metadata
            )
            s.run(
                f"""
                MATCH (a:Fact {{id: $sid, userId: $userId}})
                MATCH (b:Fact {{id: $tid, userId: $userId}})
                MERGE (b)-[r:{rel_type}]->(a)
                SET r += $metadata
                """,
                sid=source_id, tid=target_id, userId=user_id, metadata=metadata
            )
            await publish_db_event(user_id, "graph_changed", {
                "action": "link", "source_id": source_id,
                "target_id": target_id, "rel_type": rel_type
            })

        elif a_label == "DiaryEntry" and b_label == "Fact":
            s.run(
                """
                MATCH (d:DiaryEntry {id: $sid, userId: $userId})
                MATCH (f:Fact {id: $tid, userId: $userId})
                MERGE (d)-[:MENTIONS]->(f)
                """,
                sid=source_id, tid=target_id, userId=user_id
            )
            await publish_db_event(user_id, "diary_changed", {"action": "link", "id": source_id})

        elif a_label == "Fact" and b_label == "DiaryEntry":
            # User wanted to link from fact to diary — create MENTIONS from diary to fact
            s.run(
                """
                MATCH (f:Fact {id: $sid, userId: $userId})
                MATCH (d:DiaryEntry {id: $tid, userId: $userId})
                MERGE (d)-[:MENTIONS]->(f)
                """,
                sid=source_id, tid=target_id, userId=user_id
            )
            await publish_db_event(user_id, "diary_changed", {"action": "link", "id": target_id})

        else:
            raise RuntimeError(f"Cannot link {a_label} to {b_label}: only Fact↔Fact and DiaryEntry↔Fact are supported")


async def db_unlink_facts(source_id: str, target_id: str, rel_type: str, user_id: str):
    """Remove a relationship between two nodes in Neo4j.

    Handles Fact↔Fact and DiaryEntry↔Fact (MENTIONS).
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    rel_type = rel_type.upper().replace(" ", "_") if rel_type else None

    with neo4j_driver.session() as s:
        a_label = s.run(
            "MATCH (n {id: $id, userId: $userId}) RETURN head(labels(n)) AS label",
            id=source_id, userId=user_id
        ).single()
        b_label = s.run(
            "MATCH (n {id: $id, userId: $userId}) RETURN head(labels(n)) AS label",
            id=target_id, userId=user_id
        ).single()

        if not a_label or not b_label:
            raise RuntimeError(f"Cannot unlink: node not found (source={source_id}, target={target_id})")

        a_label = a_label["label"]
        b_label = b_label["label"]

        if a_label == "Fact" and b_label == "Fact":
            if rel_type:
                s.run(
                    f"""
                    MATCH (a:Fact {{id: $sid, userId: $userId}})
                    MATCH (b:Fact {{id: $tid, userId: $userId}})
                    OPTIONAL MATCH (a)-[r:{rel_type}]->(b)
                    DELETE r
                    """,
                    sid=source_id, tid=target_id, userId=user_id
                )
                s.run(
                    f"""
                    MATCH (a:Fact {{id: $sid, userId: $userId}})
                    MATCH (b:Fact {{id: $tid, userId: $userId}})
                    OPTIONAL MATCH (b)-[r:{rel_type}]->(a)
                    DELETE r
                    """,
                    sid=source_id, tid=target_id, userId=user_id
                )
            else:
                s.run(
                    """
                    MATCH (a:Fact {id: $sid, userId: $userId})
                    MATCH (b:Fact {id: $tid, userId: $userId})
                    OPTIONAL MATCH (a)-[r]-(b)
                    DELETE r
                    """,
                    sid=source_id, tid=target_id, userId=user_id
                )
            await publish_db_event(user_id, "graph_changed", {
                "action": "unlink", "source_id": source_id,
                "target_id": target_id, "rel_type": rel_type or ""
            })

        elif a_label == "DiaryEntry" and b_label == "Fact":
            s.run(
                """
                MATCH (d:DiaryEntry {id: $sid, userId: $userId})-[r:MENTIONS]->(f:Fact {id: $tid, userId: $userId})
                DELETE r
                """,
                sid=source_id, tid=target_id, userId=user_id
            )
            await publish_db_event(user_id, "diary_changed", {"action": "unlink", "id": source_id})

        elif a_label == "Fact" and b_label == "DiaryEntry":
            s.run(
                """
                MATCH (d:DiaryEntry {id: $tid, userId: $userId})-[r:MENTIONS]->(f:Fact {id: $sid, userId: $userId})
                DELETE r
                """,
                sid=source_id, tid=target_id, userId=user_id
            )
            await publish_db_event(user_id, "diary_changed", {"action": "unlink", "id": target_id})

        else:
            raise RuntimeError(f"Cannot unlink {a_label} to {b_label}: only Fact↔Fact and DiaryEntry↔Fact are supported")


def db_get_neighborhood(fact_id: str, depth: int, rel_types: List[str], user_id: str,
                        client_id: str = "", context_id: str = "") -> list:
    """Everything within ``depth`` hops of a fact, not just other facts.

    The old query ended in ``(neighbor:Fact)``, so "Show all connected" on a
    node could only ever add other facts. The client, project, category and diary
    links attached to a fact are the connections a person actually recognises,
    so the label is not pinned any more.

    Nodes are returned in the same shape as ``db_get_graph`` (``id``, ``label``,
    ``name``, ``group``) so the panel can add them to the build set without a
    second lookup, and each carries the relationship that reached it.
    """
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
            MATCH path = (f)-[{rel_filter}*1..{depth}]-(neighbor)
            WHERE (neighbor:Fact AND neighbor.userId = $userId)
               OR (neighbor:DiaryEntry AND neighbor.userId = $userId)
               OR (neighbor:Client AND neighbor.userId = $userId)
               OR (neighbor:Context AND neighbor.userId = $userId)
            RETURN neighbor, labels(neighbor) as labels,
                   [r IN relationships(path) | type(r)] as rels,
                   length(path) as distance
            """,
            id=fact_id, userId=user_id
        )
        nodes = []
        seen = set()
        for r in result:
            node = r["neighbor"]
            labels = r["labels"] or []
            if "Fact" in labels:
                node_id, label, name = node["id"], "Fact", (node.get("name") or node.get("text") or "")
                group = node.get("category", "General")
            elif "DiaryEntry" in labels:
                node_id, label = node["id"], "DiaryEntry"
                name = node.get("title") or node.get("content", "")
                group = "Diary"
            elif "Client" in labels:
                node_id, label, name, group = node["id"], "Client", node["name"], "Client"
            elif "Context" in labels:
                node_id, label, name, group = node["id"], "Context", node["name"], "Context"
            else:
                continue
            if node_id == fact_id or node_id in seen:
                continue
            seen.add(node_id)
            nodes.append({
                "id": node_id,
                "label": label,
                "name": name,
                "text": node.get("text") or node.get("content") or "",
                "category": node.get("category", "") or group,
                "group": group,
                "labels": labels,
                "rel": (r["rels"] or [None])[-1],
                "distance": r["distance"],
            })
        return _filter_neighborhood_scope(nodes, client_id, context_id)


def _filter_neighborhood_scope(nodes, client_id, context_id):
    """Keep only the Client/Context nodes matching the active scope.

    A Client or Context node *is* the scope, so it survives the filter it is not
    the subject of: when the graph is scoped to one client, that client's node
    must still be drawn or the result looks broken.
    """
    if not client_id and not context_id:
        return nodes
    return [
        n for n in nodes
        if (client_id and n["id"] == client_id)
        or (context_id and n["id"] == context_id)
        or n["label"] not in ("Client", "Context")
    ]


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


async def rewrite_search_query(query: str, category: Optional[str] = None,
                               client: Optional[str] = None,
                               context: Optional[str] = None,
                               expand: bool = False) -> list:
    """Use a tiny LLM to rewrite natural language into keyword search phrases.

    Returns a list of (keyword_string, weight) tuples.

    Short queries (<=2 words) are returned as-is unless ``expand`` is set. Callers
    pass ``expand`` when the exact-name lookup already came back empty, so a
    one-word miss ("Radoslav") can still be broadened while a one-word hit
    ("correction") stays a single cheap embedding.
    """
    q = query.strip()
    if len(q.split()) <= 2 and not expand:
        return [(q, 1.0)]

    filters = ", ".join(
        value for value in (
            f"category={category}" if category else "",
            f"client={client}" if client else "",
            f"context={context}" if context else "",
        ) if value
    ) or "none"
    system = (
        "You are a search query rewriter. Given a natural language query, "
        "extract 2-4 short keyword phrases optimised for vector similarity search. "
        "Return ONLY a JSON object with this exact structure: "
        "{\"keywords\": [\"phrase1\", \"phrase2\"]}. "
        "Rules: max 3 words per phrase, use nouns and proper nouns, "
        "omit stop words, no questions, most specific terms first. "
        "Preserve names and terms that may be exact identifiers."
    )
    prompt = f"SEARCH FILTERS: {filters}\nQUERY: {q}"
    try:
        import json as _json
        raw = await get_llm_response(prompt, system=system)
        # Strip optional markdown code fences
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        json_match = re.search(r'\{[^{}]*"keywords"[^{}]*\}', raw, re.DOTALL)
        if json_match:
            data = _json.loads(json_match.group())
            keywords = []
            for keyword in data.get("keywords", []):
                if not isinstance(keyword, str):
                    continue
                cleaned = keyword.strip()
                if cleaned and len(cleaned.split()) <= 3 and cleaned.casefold() not in {
                    q.casefold(), *(item.casefold() for item in keywords)
                }:
                    keywords.append(cleaned)
            if keywords:
                logger.debug(f"[rewrite_search_query] '{q}' → {keywords}")
                return [(q, 1.0)] + [(kw, 0.85) for kw in keywords[:4]]
    except Exception as exc:
        logger.warning(f"[rewrite_search_query] LLM rewrite failed, using heuristic: {type(exc).__name__}: {exc}")

    return _expand_query(q)


def _expand_query(query: str) -> list:
    """Generate multiple query variants for better semantic coverage.

    For complex queries, decomposes into sub-queries and generates
    alternative phrasings. Returns list of (query_string, weight) tuples.
    """
    q = query.strip()
    words = q.split()

    # Simple queries: just return as-is
    if len(words) <= 2:
        return [(q, 1.0)]

    variants = [(q, 1.0)]  # Original query always included

    # Question queries: extract the core concept
    if q.endswith("?"):
        # "who do I work with on AI projects" → "AI projects colleagues"
        # Remove question words and filler
        stop_words = {"who", "do", "i", "we", "did", "does", "what", "where", "when", "why", "how", "is", "are", "was", "were", "the", "a", "an", "my", "your", "our", "about", "with", "for", "from", "to", "on", "in", "at", "of"}
        content_words = [w for w in words if w.lower().rstrip("?") not in stop_words]
        if content_words:
            variants.append((" ".join(content_words), 0.9))

    # Extract noun phrases (consecutive non-stop words)
    stop_words = {"i", "we", "my", "your", "our", "the", "a", "an", "is", "are", "was", "were", "do", "did", "does", "have", "has", "had", "will", "would", "could", "should", "may", "might", "can", "about", "with", "for", "from", "to", "on", "in", "at", "of", "and", "or", "not", "but", "that", "this", "these", "those", "it", "its"}
    content_words = [w for w in words if w.lower() not in stop_words]

    if len(content_words) >= 2:
        # Full content words
        variants.append((" ".join(content_words), 0.85))

        # Word pairs (sliding window)
        if len(content_words) >= 3:
            for i in range(len(content_words) - 1):
                pair = f"{content_words[i]} {content_words[i+1]}"
                variants.append((pair, 0.6))

        # Individual important words (last resort)
        for w in content_words:
            if len(w) > 3:  # Skip very short words
                variants.append((w, 0.4))

    # Deduplicate by query string, keep highest weight
    seen = {}
    for q_str, weight in variants:
        key = q_str.lower().strip()
        if key not in seen or weight > seen[key]:
            seen[key] = weight

    return [(k, v) for k, v in seen.items()]


async def _hydrate_chunk_texts(qdrant, entries: list) -> None:
    """Fill in the full text for results that matched on a non-zero chunk.

    Only chunk 0 carries ``text`` — a partial copy on the others would let a
    search return a fragment as though it were the whole record. So when the
    winning chunk is chunk 7, the record's real text is fetched here.

    Mutates in place and costs one batched ``retrieve`` per search, and only
    when a non-zero chunk actually won.
    """
    missing = [e for e in entries if e.get("text") is None]
    if not missing:
        return
    try:
        found = await qdrant.retrieve(
            collection_name=COLLECTION_NAME,
            ids=[e["id"] for e in missing],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:
        logger.warning(f"chunk hydrate: could not load full text for {len(missing)} result(s): {exc}")
        return
    by_id = {str(p.id): p.payload for p in found}
    for entry in missing:
        payload = by_id.get(str(entry["id"]))
        if not payload:
            continue
        entry["text"] = payload.get("text")
        # The winning chunk is still worth reporting: it is the passage that
        # actually matched, and it is what a user wants to see highlighted.
        entry["matchedChunk"] = payload.get("chunkText")


async def _single_vector_search(qdrant, query: str, user_id: str, category: Optional[str], fetch_limit: int, top_p: float, client: Optional[str] = None, context: Optional[str] = None) -> list:
    """Run a single vector search against Qdrant. Returns raw results before boosting.

    ``top_p`` is a normalized confidence floor, not a raw cosine threshold: the
    embedder's useful band starts well above 0, so filtering here only discards
    results that could not pass the confidence gate after boosting. Passing 0.0
    disables the pre-filter and lets the caller decide.
    """
    vec = await get_embedding(query)
    conditions = [FieldCondition(key="userId", match=MatchValue(value=user_id))]
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category.strip().capitalize())))
    if client:
        conditions.append(FieldCondition(key="clientName", match=MatchValue(value=client.strip())))
    if context:
        conditions.append(FieldCondition(key="contextName", match=MatchValue(value=context.strip())))

    filt = Filter(must=conditions)
    # Translate the confidence floor into the narrowest equivalent raw threshold.
    score_threshold = 0.0
    if top_p > 0:
        score_threshold = max(0.0, VECTOR_FLOOR + top_p * (VECTOR_CEIL - VECTOR_FLOOR))
    result = await qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=vec,
        query_filter=filt,
        limit=fetch_limit,
        score_threshold=score_threshold,
    )
    return result.points


def _boost_result_score(point, query_lower: str) -> float:
    """Apply name/metadata-based score boosting to a Qdrant result."""
    score = point.score
    metadata = point.payload.get("metadata", {})
    name = point.payload.get("name")
    aliases = metadata.get("aliases", {})
    query_words = query_lower.split()

    if name:
        name_lower = name.lower()
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
        if name:
            name_norm = name.lower().strip()
            s = difflib.SequenceMatcher(None, query_lower, name_norm).ratio()
            if s >= 0.7:
                score += s * 0.8
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

    return score


async def db_search_memories(query: str, user_id: str, limit: int = 5, category: Optional[str] = None, top_p: float = 0.4, client: Optional[str] = None, context: Optional[str] = None) -> list:
    """Search facts, ranking by score and filtering by normalized confidence.

    Two independent rankings are combined. ``score`` is the legacy blended value
    (vector similarity plus additive name heuristics) and is kept unchanged for
    ranking and back-compat. ``confidence`` is a normalized 0-1 value derived by
    ``identity_confidence`` and is what ``top_p`` filters on, because the blended
    score is not a similarity and cannot be thresholded meaningfully.

    Results at or above ``top_p`` come first. Results below it are returned after
    the confident set, flagged ``weak: True`` and capped, so recall is preserved
    without letting an unconfirmed match look like a match.
    """
    started = time.perf_counter()
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Databases not connected.")

    name_like = (category or "").strip().lower() == "people" and looks_like_person_name(query)

    # 1. Neo4j exact/substring match on name or aliases
    # We do a quick lookup for nodes containing the query
    exact_matches = []
    query_lower = query.lower()

    with neo4j_driver.session() as s:
        # Only do broad CONTAINS match on text if the query is reasonably long to avoid massive irrelevant noise
        if len(query) > 3 and not name_like:
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
        if client:
            cypher += " AND EXISTS((f)-[:FOR_CLIENT]->(:Client {name: $clientName, userId: $userId}))"
        if context:
            cypher += " AND EXISTS((f)-[:IN_CONTEXT]->(:Context {name: $contextName, userId: $userId}))"
        cypher += " OPTIONAL MATCH (f)-[:FOR_CLIENT]->(fc:Client)"
        cypher += " OPTIONAL MATCH (f)-[:IN_CONTEXT]->(fx:Context)"
        cypher += " RETURN f, fc.name AS clientName, fx.name AS contextName ORDER BY f.name LIMIT 25"

        params = {"userId": user_id, "query_str": query}
        if category:
            params["category"] = category.strip()
        if client:
            params["clientName"] = client.strip()
        if context:
            params["contextName"] = context.strip()

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
                # A literal Neo4j substring hit is stronger evidence than any
                # embedding, so feed it in as a saturated vector component.
                "raw_score": 1.0,
                "clientName": r["clientName"],
                "contextName": r["contextName"],
                "metadata": meta
            })

    # 2. Multi-query vector search
    # Long queries go through the LLM rewriter. Short queries only get expanded
    # when the exact-name lookup found nothing, so a name-shaped miss is retried
    # while an ordinary hit stays a single embedding.
    query_variants = await rewrite_search_query(
        query,
        category=category,
        client=client,
        context=context,
        expand=not exact_matches,
    )
    # A chunked record occupies one fetch slot per chunk, so the raw limit has to
    # leave room for the family collapsing back down to `limit` records.
    fetch_limit = max(limit * 5 * CHUNK_FETCH_MULTIPLIER, 50)

    # Collect all results from all query variants
    all_vector_results = {}  # record id -> best result across all variants

    for variant_query, weight in query_variants:
        raw_points = await _single_vector_search(qdrant, variant_query, user_id, category, fetch_limit, top_p, client, context)

        for r in raw_points:
            boosted_score = _boost_result_score(r, variant_query.lower())
            # Apply query variant weight
            weighted_score = boosted_score * weight

            # A chunked record contributes one point per chunk, all matching the
            # same filters. Key on the parent so the record appears once, with
            # the score of its best-matching chunk — otherwise a long record
            # fills the result list with N copies of itself.
            record_id = parent_of(r.id, r.payload)

            result_entry = {
                "id": record_id,
                "text": r.payload.get("text"),
                "name": r.payload.get("name"),
                "category": r.payload.get("category"),
                "score": weighted_score,
                "raw_score": r.score,
                "clientName": r.payload.get("clientName"),
                "contextName": r.payload.get("contextName"),
                "metadata": r.payload.get("metadata", {})
            }

            # Keep the best version of each result
            if record_id not in all_vector_results or weighted_score > all_vector_results[record_id]["score"]:
                all_vector_results[record_id] = result_entry

    results = list(all_vector_results.values())
    await _hydrate_chunk_texts(qdrant, results)

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

    # Apply client/context scoring (names live top-level; fall back to metadata).
    # Explicit scope boosts; inferred scope is a weaker boost-only fallback;
    # inactive clients are penalized in unscoped (global) search only.
    if client:
        for r in final_list:
            cname = r.get("clientName") or r.get("metadata", {}).get("clientName", "")
            if (cname or "").lower() == client.lower():
                r["score"] += 0.3
    else:
        # Fetch status map first so inactive clients skip the inferred boost too.
        status_map = db_get_client_status_map(user_id)
        # A name-shaped query cannot name a client, so skip the inference round
        # trip entirely on the highest-volume search shape.
        inferred_client = None
        if not name_like:
            inferred_client, _ = infer_scope_from_text(query, user_id)
        if inferred_client:
            for r in final_list:
                cname = r.get("clientName") or r.get("metadata", {}).get("clientName", "")
                cname_lower = (cname or "").lower()
                # Don't boost inactive clients even if the query text mentions them.
                if cname_lower == inferred_client.lower() and status_map.get(cname_lower) is not False:
                    r["score"] += INFERRED_SCOPE_BOOST
        if status_map:
            for r in final_list:
                cname = (r.get("clientName") or r.get("metadata", {}).get("clientName", "") or "").lower()
                if cname and status_map.get(cname) is False:
                    r["score"] -= INACTIVE_PENALTY
    if context:
        for r in final_list:
            xname = r.get("contextName") or r.get("metadata", {}).get("contextName", "")
            if (xname or "").lower() == context.lower():
                r["score"] += 0.3

    # Normalized identity confidence drives thresholding; score drives ranking.
    for r in final_list:
        meta = r.get("metadata") or {}
        confidence, evidence = identity_confidence(
            query,
            name=r.get("name"),
            first_name=meta.get("first_name"),
            last_name=meta.get("last_name"),
            aliases=meta.get("aliases"),
            raw_vector=r.get("raw_score"),
            name_like=name_like,
        )
        r["confidence"] = round(confidence, 3)
        r["evidence"] = evidence

    final_list.sort(key=lambda x: (x["score"], x["confidence"]), reverse=True)
    confident = [r for r in final_list if r["confidence"] >= top_p]
    weak = [r for r in final_list if r["confidence"] < top_p]

    results = confident[:limit]
    for r in weak[:WEAK_RESULT_LIMIT]:
        r["weak"] = True
        results.append(r)

    log_search_stats(
        query=query,
        category=category,
        top_p=top_p,
        name_like=name_like,
        exact_hits=len(exact_matches),
        candidates=len(final_list),
        confident=len(confident),
        weak=len(weak),
        returned=len(results),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        top=[
            {
                "name": r.get("name"),
                "score": r["score"],
                "confidence": r["confidence"],
                "evidence": r["evidence"],
                "weak": bool(r.get("weak")),
            }
            for r in results[:5]
        ],
    )
    return results


async def db_find_people_matches(names: list[str], user_id: str, min_score: float = MIN_MATCH_CONFIDENCE) -> list:
    """Find high-confidence existing People facts for extracted names.

    Uses the same scoring as search_facts, but rejects weak and conflicting
    results before they can create MENTIONS links. ``min_score`` is a normalized
    confidence, so it is matched to the confidence floor passed to the search.
    """
    matches = []
    seen_ids = set()
    for name in names:
        results = await db_search_memories(
            name,
            user_id,
            limit=3,
            category="People",
            top_p=MIN_MATCH_CONFIDENCE,
        )
        for result in results:
            if result.get("id") in seen_ids or not people_match_allowed(name, result, min_score):
                continue
            seen_ids.add(result["id"])
            matches.append(result)
    return matches


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
            OPTIONAL MATCH (f)-[:FOR_CLIENT]->(cl:Client)
            OPTIONAL MATCH (f)-[:IN_CONTEXT]->(ctx:Context)
            OPTIONAL MATCH (f)-[:RELEVANT_TO]->(rc)
            RETURN f, c.name as category, cl.name as clientName, cl.id as clientId,
                   ctx.name as contextName, ctx.id as contextId,
                   collect(DISTINCT {id: rc.id, name: rc.name,
                                     kind: CASE WHEN rc:Context THEN 'context' ELSE 'client' END}) as relevantClients,
                   [(f)-[r]-(other {userId: $userId})
                    WHERE (other:Fact OR other:DiaryEntry)
                      AND type(r) <> 'IN_CATEGORY' AND type(r) <> 'KNOWS'
                    | {
                      rel: type(r),
                      target_id: other.id,
                      target_text: coalesce(other.text, other.content),
                      target_name: other.name,
                      target_label: head(labels(other)),
                      direction: CASE WHEN startNode(r) = f THEN 'out' ELSE 'in' END
                    }] as links
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

            # Scope lives top-level on the response (clientName/clientId/...);
            # it is intentionally NOT duplicated into metadata anymore.

            # Clean up links (remove null entries from collect)
            links = [l for l in r["links"] if l and l.get("target_id")]

            memories.append({
                "id":        f_node["id"],
                "text":      f_node["text"],
                "name":     f_node.get("name"),
                "category":  r["category"],
                "timestamp": f_node["timestamp"].iso_format() if f_node.get("timestamp") else None,
                "clientName":  r["clientName"],
                "clientId":    r["clientId"],
                "contextName": r["contextName"],
                "contextId":   r["contextId"],
                "relevantClients": [rc for rc in r["relevantClients"] if rc.get("id")],
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
            OPTIONAL MATCH (f)-[:FOR_CLIENT]->(cl:Client {userId: $userId})
            OPTIONAL MATCH (f)-[:IN_CONTEXT]->(ctx:Context {userId: $userId})
            RETURN f, cl.name AS clientName, ctx.name AS contextName
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
                "clientName": r["clientName"],
                "contextName": r["contextName"],
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

        # Prefer explicit metadata fields; fall back to parsing norm_name.
        # Strip suffixes like "— Forme", "(former)", "[archived]" before parsing.
        meta_first = str(meta.get("first_name", "")).lower().strip()
        meta_last  = str(meta.get("last_name",  "")).lower().strip()
        if not meta_first or not meta_last:
            # Strip anything after "—", "(", "[" before splitting
            clean_norm = re.split(r"[—–()\[\]]", norm)[0].strip()
            parts = clean_norm.split()
            if len(parts) >= 2 and not meta_first:
                meta_first = parts[0]
            if len(parts) >= 2 and not meta_last:
                meta_last = parts[-1]

        prepared.append({
            "id": item["id"],
            "vec": np.array(vectors[str(item["id"])]),
            "norm_name": norm,
            "norm_words": norm_words,
            "email": str(meta.get("email", "")).lower().strip(),
            "first_name": meta_first,
            "last_name": meta_last,
            "aliases": [normalize_name(a) for a in aliases_list],
            "name": item.get("name") or "",
            "client_name": (item.get("clientName") or "").casefold(),
            "context_name": (item.get("contextName") or "").casefold(),
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

            if not scopes_compatible(
                {"clientName": p_i["client_name"], "contextName": p_i["context_name"]},
                {"clientName": p_j["client_name"], "contextName": p_j["context_name"]},
            ):
                continue

            signals = []
            strong_identity = False

            # Signal 1: Vector cosine similarity
            vec_sim = float(np.dot(vec_i, vec_j) / (norm_vec_i * norm_vec_j))
            signals.append(vec_sim)

            # Signal 2: Exact normalized-name match
            if p_i["norm_name"] and p_j["norm_name"] and p_i["norm_name"] == p_j["norm_name"]:
                signals.append(1.0)
                strong_identity = True

            # Signal 3: Email match
            if p_i["email"] and p_j["email"] and p_i["email"] == p_j["email"]:
                signals.append(1.0)
                strong_identity = True

            # Signal 4: first_name + last_name match
            if (p_i["first_name"] and p_i["last_name"]
                    and p_j["first_name"] and p_j["last_name"]
                    and p_i["first_name"] == p_j["first_name"]
                    and p_i["last_name"] == p_j["last_name"]):
                signals.append(1.0)
                strong_identity = True

            # Signal 5: Alias ↔ name match
            if p_j["norm_name"] and p_j["norm_name"] in p_i["aliases"]:
                signals.append(0.95)
                strong_identity = True
            if p_i["norm_name"] and p_i["norm_name"] in p_j["aliases"]:
                signals.append(0.95)
                strong_identity = True

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

            # Signal 10: Full normalized name fuzzy match
            if p_i["norm_name"] and p_j["norm_name"]:
                full_ratio = difflib.SequenceMatcher(None, p_i["norm_name"], p_j["norm_name"]).ratio()
                if full_ratio >= 0.8:
                    signals.append(min(0.95, full_ratio))

            evidence_similarity = max(signals[1:]) if len(signals) > 1 else vec_sim
            similarity = combine_duplicate_signals(
                vec_sim, evidence_similarity, strong_identity
            )
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

    final_clusters = [
        cluster for cluster in final_clusters
        if cluster_has_core(cluster, pair_scores, threshold)
    ]

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
                "text": item["text"],
                "category": item.get("category"),
                "clientName": item.get("clientName"),
                "contextName": item.get("contextName"),
                "metadata": item.get("metadata") or {},
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

    duplicate_ids = validate_merge_ids(master_id, duplicate_ids)

    with neo4j_driver.session() as s:
        records = list(s.run(
            """
            MATCH (f:Fact {userId: $userId})
            WHERE f.id = $masterId OR f.id IN $duplicateIds
            RETURN f.id AS id
            """,
            userId=user_id, masterId=master_id, duplicateIds=duplicate_ids,
        ))
    found_ids = {record["id"] for record in records}
    expected_ids = {master_id, *duplicate_ids}
    if found_ids != expected_ids:
        raise ValueError("master and duplicate IDs must all belong to the current user")

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
                MATCH (target {id: $targetId, userId: $userId})
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
                MATCH (source {id: $sourceId, userId: $userId})
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

        # Prune orphan categories that might have been left behind by merged duplicates
        s.run(
            "MATCH (c:Category) WHERE NOT (c)<-[:IN_CATEGORY]-() DETACH DELETE c"
        )



        # 6. Explicitly delete duplicate nodes as a safety measure to ensure they are removed from Neo4j
        s.run(
            """
            MATCH (dup:Fact) WHERE dup.id IN $duplicateIds AND dup.userId = $userId
            DETACH DELETE dup
            """,
            duplicateIds=duplicate_ids, userId=user_id
        )
        s.run(
            """
            MATCH (master:Fact {id: $masterId, userId: $userId})
            SET master.pendingQdrantDeletes = $duplicateIds
            """,
            masterId=master_id, userId=user_id, duplicateIds=duplicate_ids
        )

    # Delete duplicates from Qdrant
    try:
        # The family, not just the record id: a merged-away chunked fact still
        # has chunks 1..N-1 in the collection, and `pendingQdrantDeletes` is
        # replayed by sync_orphans using the same expansion.
        for dup_id in duplicate_ids:
            await _delete_fact_chunks(qdrant, dup_id)
    except Exception:
        logger.exception(
            "Merge committed to Neo4j but Qdrant cleanup failed; "
            "pendingQdrantDeletes will be retried by sync_orphans"
        )
        raise

    with neo4j_driver.session() as s:
        s.run(
            "MATCH (master:Fact {id: $masterId, userId: $userId}) "
            "REMOVE master.pendingQdrantDeletes",
            masterId=master_id, userId=user_id
        )



    await publish_db_event(user_id, "memory_changed", {
        "action": "merge",
        "master_id": master_id,
        "duplicate_ids": duplicate_ids
    })


def db_get_graph(user_id: str, client_id: str = "", context_id: str = "", limit: int = 0) -> dict:
    """Return the knowledge graph for a user (nodes and edges), optionally scoped.

    ``client_id`` / ``context_id`` restrict *records* — Facts and Diary entries —
    to those linked to that client or project. Scope membership is read from the
    FOR_CLIENT / IN_CONTEXT edges the passes below already walk, not from the
    denormalised ``clientId`` property, because that property is written by the
    classifier and can lag the relationship. Category, Client and Context nodes
    are always kept so a filtered graph stays connected.

    ``limit`` caps how many records are returned, largest-degree first, so a big
    vault cannot hand vis.js more than it can draw. The response carries
    ``truncated`` and ``total`` so the UI can say so rather than silently
    showing a partial graph.
    """
    if limit <= 0:
        limit = GRAPH_MAX_NODES
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
        # record_id -> set(scope node ids), filled by the Client/Context passes
        # so the filter below never has to trust a denormalised property.
        fact_clients = {}
        fact_contexts = {}
        diary_scope = {}  # diary id -> [client_id, context_id]

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

        # Add DiaryEntry nodes and MENTIONS edges
        diag_res = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            OPTIONAL MATCH (d)-[r:MENTIONS]->(f:Fact)
            WHERE f.userId = $userId
            RETURN d, type(r) as rel_type, f
            """,
            userId=user_id
        )
        for dr in diag_res:
            d_node = dr["d"]
            d_id = d_node["id"]
            if d_id not in node_map:
                node_map[d_id] = {
                    "id": d_id,
                    "label": "DiaryEntry",
                    "name": d_node.get("title") or d_node.get("content", ""),
                    "group": "Diary"
                }
            # Seeded empty and filled by the FOR_CLIENT / IN_CONTEXT passes
            # below. There is deliberately no read of a clientId property here:
            # a Fact or DiaryEntry never has one in Neo4j. clientId is a Qdrant
            # payload key, and the only thing that writes scope in the graph is
            # the edge. Reading the property returned None for every entry, so
            # every diary entry was unscoped and a client filter dropped the lot.
            diary_scope.setdefault(d_id, ["", ""])
            f_node = dr["f"]
            rel = dr["rel_type"]
            if f_node and rel:
                f_id = f_node["id"]
                edge_sig = (d_id, f_id, rel)
                reverse_sig = (f_id, d_id, rel)
                if reverse_sig in edge_lookup:
                    edge_lookup[reverse_sig]["arrows"] = "to,from"
                elif edge_sig not in edge_lookup:
                    edge_lookup[edge_sig] = {
                        "id": f"{d_id}_{f_id}_{rel}",
                        "from": d_id,
                        "to": f_id,
                        "label": rel,
                        "arrows": "to"
                    }
                    edges.append(edge_lookup[edge_sig])

        # Add Client nodes and FOR_CLIENT edges
        client_res = s.run(
            """
            MATCH (c:Client {userId: $userId})
            OPTIONAL MATCH (n)-[:FOR_CLIENT]->(c)
            WHERE (n:Fact OR n:DiaryEntry) AND n.userId = $userId
            RETURN c, n
            """,
            userId=user_id
        )
        for cr in client_res:
            c_node = cr["c"]
            c_id = c_node["id"]
            if c_id not in node_map:
                node_map[c_id] = {
                    "id": c_id,
                    "label": "Client",
                    "name": c_node["name"],
                    "group": "Client"
                }
            f_node = cr["f"]
            if f_node:
                # Diary entries carry FOR_CLIENT too -- link_diary_to_client
                # writes it -- and scoping them from the denormalised property
                # instead of the edge is what put them in the wrong client.
                scope_of = fact_clients if f_node.get("id") in node_map and \
                    node_map[f_node["id"]]["label"] == "Fact" else None
                if scope_of is not None:
                    scope_of.setdefault(f_node["id"], set()).add(c_id)
                else:
                    diary_scope.setdefault(f_node["id"], ["", ""])[0] = c_id
                edge_sig = (f_node["id"], c_id, "FOR_CLIENT")
                reverse_sig = (c_id, f_node["id"], "FOR_CLIENT")
                if reverse_sig not in edge_lookup and edge_sig not in edge_lookup:
                    new_edge = {
                        "id": f"{f_node['id']}_{c_id}_FOR_CLIENT",
                        "from": f_node["id"],
                        "to": c_id,
                        "label": "FOR_CLIENT",
                        "arrows": "to"
                    }
                    edge_lookup[edge_sig] = new_edge
                    edges.append(new_edge)

        # Add Context nodes and IN_CONTEXT/HAS_CONTEXT edges
        ctx_res = s.run(
            """
            MATCH (ctx:Context {userId: $userId})
            OPTIONAL MATCH (n)-[:IN_CONTEXT]->(ctx)
            WHERE (n:Fact OR n:DiaryEntry) AND n.userId = $userId
            OPTIONAL MATCH (c:Client)-[:HAS_CONTEXT]->(ctx)
            RETURN ctx, n, c
            """,
            userId=user_id
        )
        for xrr in ctx_res:
            ctx_node = xrr["ctx"]
            ctx_id = ctx_node["id"]
            if ctx_id not in node_map:
                node_map[ctx_id] = {
                    "id": ctx_id,
                    "label": "Context",
                    "name": ctx_node["name"],
                    "group": "Context"
                }
            f_node = xrr["f"]
            if f_node:
                if f_node.get("id") in node_map and node_map[f_node["id"]]["label"] == "Fact":
                    fact_contexts.setdefault(f_node["id"], set()).add(ctx_id)
                else:
                    diary_scope.setdefault(f_node["id"], ["", ""])[1] = ctx_id
                edge_sig = (f_node["id"], ctx_id, "IN_CONTEXT")
                reverse_sig = (ctx_id, f_node["id"], "IN_CONTEXT")
                if reverse_sig not in edge_lookup and edge_sig not in edge_lookup:
                    new_edge = {
                        "id": f"{f_node['id']}_{ctx_id}_IN_CONTEXT",
                        "from": f_node["id"],
                        "to": ctx_id,
                        "label": "IN_CONTEXT",
                        "arrows": "to"
                    }
                    edge_lookup[edge_sig] = new_edge
                    edges.append(new_edge)
            c_node = xrr["c"]
            if c_node:
                edge_sig = (c_node["id"], ctx_id, "HAS_CONTEXT")
                reverse_sig = (ctx_id, c_node["id"], "HAS_CONTEXT")
                if reverse_sig not in edge_lookup and edge_sig not in edge_lookup:
                    new_edge = {
                        "id": f"{c_node['id']}_{ctx_id}_HAS_CONTEXT",
                        "from": c_node["id"],
                        "to": ctx_id,
                        "label": "HAS_CONTEXT",
                        "arrows": "to"
                    }
                    edge_lookup[edge_sig] = new_edge
                    edges.append(new_edge)

        return _scope_and_cap_graph(
            node_map, edges, fact_clients, fact_contexts, diary_scope,
            client_id, context_id, limit,
        )


def _scope_and_cap_graph(node_map, edges, fact_clients, fact_contexts, diary_scope,
                         client_id, context_id, limit):
    """Drop out-of-scope records, cap the size, and drop the edges left dangling.

    Split out of ``db_get_graph`` so the policy is readable on its own. Every
    record scopes through its ``FOR_CLIENT`` / ``IN_CONTEXT`` edge and nothing
    else: a Fact or DiaryEntry carries no ``clientId`` property in Neo4j, so
    reading one yields None for every record. Membership by edge is also what
    makes a just-unlinked fact leave the view immediately rather than on the
    next reclassify.
    """
    def in_scope(node):
        label = node.get("label")
        if label == "Fact":
            if client_id and client_id not in fact_clients.get(node["id"], ()):
                return False
            if context_id and context_id not in fact_contexts.get(node["id"], ()):
                return False
            return True
        if label == "DiaryEntry":
            d_client, d_context = diary_scope.get(node["id"], ("", ""))
            if client_id and client_id != d_client:
                return False
            if context_id and context_id != d_context:
                return False
            return True
        # Category / Client / Context nodes: keep them, a filtered graph with no
        # client node in it reads as "this client has no facts".
        return True

    scoped = [n for n in node_map.values() if in_scope(n)]
    total = len(scoped)
    truncated = False

    records = [n for n in scoped if n.get("label") in ("Fact", "DiaryEntry")]
    if limit and len(records) > limit:
        # Keep the most connected records: a graph of leaves explains nothing and
        # is the case that makes the full graph feel useless.
        degree = {}
        for e in edges:
            degree[e["from"]] = degree.get(e["from"], 0) + 1
            degree[e["to"]] = degree.get(e["to"], 0) + 1
        records.sort(key=lambda n: (-degree.get(n["id"], 0), n["id"]))
        keep = {n["id"] for n in records[:limit]}
        dropped = {n["id"] for n in records[limit:]}
        truncated = True
        keep |= {n["id"] for n in scoped if n.get("label") not in ("Fact", "DiaryEntry")}
        scoped = [n for n in scoped if n["id"] in keep]
        edges = [e for e in edges if e["from"] not in dropped and e["to"] not in dropped]
    else:
        kept = {n["id"] for n in scoped}
        edges = [e for e in edges if e["from"] in kept and e["to"] in kept]

    return {"nodes": scoped, "edges": edges, "truncated": truncated, "total": total}


# ---------------------------------------------------------------------------
# Startup consistency checks: read-only validation across all stores
# ---------------------------------------------------------------------------
async def run_consistency_checks():
    """Read-only consistency checks across Neo4j and Qdrant. Logs all discrepancies."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        logger.warning("consistency: DB not available, skipping checks")
        return

    # Collect all user IDs from both Fact and DiaryEntry nodes
    with neo4j_driver.session() as s:
        user_rows = list(s.run(
            "MATCH (f:Fact) RETURN DISTINCT f.userId AS userId "
            "UNION "
            "MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS userId"
        ))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        logger.info("consistency: no users found in graph")
        return

    issues_found = False

    for user_id in sorted(user_ids):
        # --- Fact counts: Neo4j vs Qdrant ---
        with neo4j_driver.session() as s:
            fact_rows = list(s.run(
                "MATCH (f:Fact {userId: $userId}) RETURN f.id AS id",
                userId=user_id
            ))
        neo4j_fact_ids = {r["id"] for r in fact_rows}
        neo4j_fact_count = len(neo4j_fact_ids)

        qdrant_fact_ids = await _scroll_qdrant_ids(qdrant, COLLECTION_NAME, user_id)
        qdrant_fact_count = len(qdrant_fact_ids)

        if neo4j_fact_count != qdrant_fact_count:
            issues_found = True
            logger.warning(
                f"consistency [{user_id}]: Fact count mismatch — "
                f"Neo4j: {neo4j_fact_count}, Qdrant: {qdrant_fact_count}"
            )
            _log_diff(neo4j_fact_ids, qdrant_fact_ids, user_id, "fact", "Neo4j", "Qdrant")
        else:
            logger.info(f"consistency [{user_id}]: Facts OK ({neo4j_fact_count})")

    # --- Data quality (cross-user) ---
    with neo4j_driver.session() as s:
        no_user = list(s.run("MATCH (f:Fact) WHERE f.userId IS NULL RETURN count(*) AS count"))
    no_user_count = no_user[0]["count"] if no_user else 0
    if no_user_count:
        issues_found = True
        logger.warning(f"consistency: {no_user_count} facts have no userId")

    with neo4j_driver.session() as s:
        orphan_cats = list(s.run("""
            MATCH (c:Category)
            WHERE NOT (c)<-[:IN_CATEGORY]-(:Fact)
            RETURN c.name AS name
        """))
    for r in orphan_cats:
        issues_found = True
        logger.warning(f"consistency: Orphan category '{r['name']}' has no facts linked")

    # --- Facts without a title ---
    with neo4j_driver.session() as s:
        untitled_facts = list(s.run(
            "MATCH (f:Fact) WHERE f.name IS NULL OR f.name = '' RETURN f.id AS id, f.text AS text LIMIT 20"
        ))
    if untitled_facts:
        issues_found = True
        logger.warning(
            f"consistency: {len(untitled_facts)}+ facts without a title "
            f"(showing first IDs: {', '.join(r['id'] for r in untitled_facts[:20])})"
        )

    # --- Dangling MENTIONS from diary entries to non-Fact nodes ---
    with neo4j_driver.session() as s:
        dangling_mentions = list(s.run(
            "MATCH (d:DiaryEntry)-[r:MENTIONS]->(t) WHERE NOT t:Fact "
            "RETURN d.userId AS userId, count(*) AS count"
        ))
    for row in dangling_mentions:
        issues_found = True
        logger.warning(
            f"consistency [{row['userId']}]: {row['count']} MENTIONS link(s) "
            f"from diary entry to non-Fact node"
        )

    # --- Cross-user MENTIONS ---
    with neo4j_driver.session() as s:
        cross_user = list(s.run(
            "MATCH (d:DiaryEntry)-[r:MENTIONS]->(f:Fact) WHERE d.userId <> f.userId "
            "RETURN d.userId AS diaryUser, f.userId AS factUser, count(*) AS count"
        ))
    for row in cross_user:
        issues_found = True
        logger.warning(
            f"consistency [{row['diaryUser']}]: {row['count']} MENTIONS link(s) "
            f"crossing user boundary (fact belongs to '{row['factUser']}')"
        )

    if not issues_found:
        logger.info("consistency: All checks passed — no discrepancies found")
    else:
        logger.info(f"consistency: Summary — {len(user_ids)} users checked")


def _normalize_point_id(raw_id: str):
    """Ensure ID is valid for Qdrant (unsigned integer or UUID).
    Returns (normalized_id, needs_update) — needs_update is True when Neo4j
    must be updated to match the new ID.
    """
    raw_id = raw_id.strip()
    # All digits → unsigned integer (valid Qdrant point ID)
    if raw_id.isdigit():
        return raw_id, False
    # Already valid UUID: 32 hex chars (no dashes) or 8-4-4-4-12 with dashes
    if re.match(r'^[0-9a-f]{32}$', raw_id, re.I):
        return raw_id, False
    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', raw_id, re.I):
        return raw_id, False
    # Not a valid Qdrant ID — generate a deterministic UUID5 from it
    new_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, raw_id))
    return new_id, True


async def _scroll_qdrant_ids(qdrant, collection: str, user_id: str) -> set:
    """Scroll all Qdrant record IDs for a user in a collection.

    Counts records, not points: a chunked record is one Fact/DiaryEntry in
    Neo4j and N points here, so returning point ids would report every long
    record as a Qdrant-only orphan and make the count check fire constantly.
    """
    ids = set()
    try:
        offset = None
        while True:
            result = await qdrant.scroll(
                collection_name=collection,
                limit=1000,
                offset=offset,
                with_payload=["parentId"],
                with_vectors=False,
                scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
            )
            points, next_offset = result
            for p in points:
                ids.add(parent_of(p.id, p.payload))
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.error(f"consistency: Qdrant scroll failed for {collection}/{user_id}: {e}")
    return ids


def _log_diff(set_a: set, set_b: set, user_id: str, label: str, label_a: str, label_b: str):
    """Log IDs that are in one set but not the other."""
    only_a = set_a - set_b
    only_b = set_b - set_a
    if only_a:
        logger.warning(
            f"consistency [{user_id}]: {len(only_a)} {label}(s) in {label_a} only: "
            f"{','.join(sorted(only_a)[:20])}"
        )
    if only_b:
        logger.warning(
            f"consistency [{user_id}]: {len(only_b)} {label}(s) in {label_b} only: "
            f"{','.join(sorted(only_b)[:20])}"
        )


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
        user_rows = list(s.run(
            "MATCH (f:Fact) RETURN DISTINCT f.userId AS userId "
            "UNION "
            "MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS userId"
        ))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        logger.info("sync_orphans: no users found")
        return

    total_deleted = 0
    total_reembedded = 0
    total_pruned_links = 0

    for user_id in user_ids:
        # -----------------------------------------------------------------------
        # Fact orphans
        # -----------------------------------------------------------------------
        with neo4j_driver.session() as s:
            facts = list(s.run(
                "MATCH (f:Fact {userId: $userId}) "
                "RETURN f.id AS id, f.text AS text, f.name AS name, "
                "f.pendingQdrantDeletes AS pendingQdrantDeletes",
                userId=user_id
            ))
        neo4j_fact_ids = {r["id"] for r in facts}
        neo4j_fact_map = {
            r["id"]: {"text": r["text"], "name": r.get("name", "")}
            for r in facts
        }

        for fact in facts:
            pending_ids = fact.get("pendingQdrantDeletes") or []
            if not pending_ids:
                continue
            try:
                # pendingQdrantDeletes holds record ids. Replaying it verbatim
                # would clear only chunk 0 of a merged-away chunked fact and
                # leave the rest to be treated as orphans on the next boot.
                for pending_id in pending_ids:
                    await _delete_fact_chunks(qdrant, pending_id)
            except Exception:
                logger.exception(
                    f"sync_orphans [{user_id}]: pending merge cleanup failed "
                    f"for master {fact['id']}"
                )
                continue
            with neo4j_driver.session() as s:
                s.run(
                    "MATCH (master:Fact {id: $masterId, userId: $userId}) "
                    "REMOVE master.pendingQdrantDeletes",
                    masterId=fact["id"], userId=user_id
                )
            total_deleted += len(pending_ids)

        # Scroll Qdrant fact collection
        qdrant_fact_ids = set()
        offset = None
        while True:
            result = await qdrant.scroll(
                collection_name=COLLECTION_NAME,
                limit=1000,
                offset=offset,
                # parentId is needed to collapse a chunk family onto its record.
                # Without it every chunk 1..N-1 looks like a Qdrant-only point
                # and sync_orphans deletes the middle of every long record.
                with_payload=["parentId"],
                with_vectors=False,
                scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
            )
            points, next_offset = result
            for p in points:
                qdrant_fact_ids.add(parent_of(p.id, p.payload))
            if next_offset is None:
                break
            offset = next_offset

        # Qdrant-only fact records → delete
        orphan_fact_qdrant = qdrant_fact_ids - neo4j_fact_ids
        if orphan_fact_qdrant:
            logger.info(f"sync_orphans [{user_id}]: deleting {len(orphan_fact_qdrant)} Qdrant-only fact orphans")
            # Per record, not per id: the set holds record ids (chunks were
            # collapsed by parent_of), so a bulk delete by those ids would
            # remove chunk 0 and strand chunks 1..N-1 forever.
            for orphan_id in orphan_fact_qdrant:
                await _delete_fact_chunks(qdrant, orphan_id)
            total_deleted += len(orphan_fact_qdrant)

        # Neo4j-only facts → re-embed
        orphan_fact_neo4j = neo4j_fact_ids - qdrant_fact_ids
        if orphan_fact_neo4j:
            logger.info(f"sync_orphans [{user_id}]: re-embedding {len(orphan_fact_neo4j)} Neo4j-only facts")
            for oid in orphan_fact_neo4j:
                info = neo4j_fact_map[oid]
                # Through the chunking helper so a re-embedded long record is
                # restored as the same multi-point family it was written as,
                # not collapsed back into one lossy vector.
                await _upsert_fact_points(qdrant, oid, info["text"], {
                    "text": info["text"], "name": info["name"], "userId": user_id,
                })
            total_reembedded += len(orphan_fact_neo4j)

        # -----------------------------------------------------------------------
        # Diary entry orphans
        # -----------------------------------------------------------------------
        with neo4j_driver.session() as s:
            diaries = list(s.run(
                "MATCH (d:DiaryEntry {userId: $userId}) "
                "RETURN d.id AS id, d.content AS content, d.name AS name, "
                "       d.date AS date, d.timestamp AS timestamp",
                userId=user_id
            ))
        neo4j_diary_ids = {r["id"] for r in diaries}
        neo4j_diary_map = {
            r["id"]: {
                "content": r["content"], "name": r.get("name", ""),
                "date": r.get("date", ""), "timestamp": r.get("timestamp", ""),
            }
            for r in diaries
        }

        # Scroll Qdrant diary collection
        qdrant_diary_ids = set()
        offset = None
        while True:
            result = await qdrant.scroll(
                collection_name=DIARY_COLLECTION,
                limit=1000,
                offset=offset,
                # parentId collapses a chunk family onto its entry, as above.
                with_payload=["parentId"],
                with_vectors=False,
                scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
            )
            points, next_offset = result
            for p in points:
                qdrant_diary_ids.add(parent_of(p.id, p.payload))
            if next_offset is None:
                break
            offset = next_offset

        # Qdrant-only diary records → delete
        orphan_diary_qdrant = qdrant_diary_ids - neo4j_diary_ids
        if orphan_diary_qdrant:
            logger.info(
                f"sync_orphans [{user_id}]: deleting {len(orphan_diary_qdrant)} "
                f"Qdrant-only diary orphans"
            )
            # Per record, so a chunked orphan loses all of its points.
            # Local import: diary_manager is a peer module, not a dependency of
            # this one, and pulling it in at module scope would tie them together.
            from diary_manager import _delete_diary_chunks
            for orphan_id in orphan_diary_qdrant:
                await _delete_diary_chunks(orphan_id)
            total_deleted += len(orphan_diary_qdrant)

        # Neo4j-only diary entries → re-embed
        orphan_diary_neo4j = neo4j_diary_ids - qdrant_diary_ids
        if orphan_diary_neo4j:
            logger.info(
                f"sync_orphans [{user_id}]: re-embedding {len(orphan_diary_neo4j)} "
                f"Neo4j-only diary entries"
            )
            for oid in orphan_diary_neo4j:
                info = neo4j_diary_map[oid]
                qdrant_id, needs_update = _normalize_point_id(oid)
                if needs_update:
                    # Update Neo4j to use the canonical UUID as its ID
                    logger.info(f"sync_orphans: migrating diary ID {oid} → {qdrant_id}")
                    with neo4j_driver.session() as s:
                        s.run(
                            "MATCH (d:DiaryEntry {id: $oldId, userId: $userId}) "
                            "SET d.id = $newId",
                            oldId=oid, userId=user_id, newId=qdrant_id
                        )
                embed_text = f"{info['name']}: {info['content']}" if info["name"] else info["content"]
                payload = {
                    "content": info["content"],
                    "name": info["name"],
                    "date": info["date"],
                    "timestamp": info["timestamp"],
                    "userId": user_id,
                }
                # Chunked for the same reason as facts: a long diary entry is
                # not one searchable vector.
                points = build_chunk_payloads(qdrant_id, info["content"], payload)
                structs = []
                for part in points:
                    body = chunk_text_of(part["payload"])
                    vectors = await get_embedding(f"{info['name']}: {body}" if info["name"] else body)
                    structs.append(PointStruct(id=part["id"], vector=vectors, payload=part["payload"]))
                await qdrant.upsert(collection_name=DIARY_COLLECTION, points=structs)
            total_reembedded += len(orphan_diary_neo4j)

        # -----------------------------------------------------------------------
        # Dangling links: relationships to nodes with wrong labels
        # -----------------------------------------------------------------------
        with neo4j_driver.session() as s:
            # Mentions from diary entries must point to Facts
            res = s.run(
                "MATCH (d:DiaryEntry {userId: $userId})-[r:MENTIONS]->(t) WHERE NOT t:Fact DELETE r RETURN count(*) as n",
                userId=user_id
            ).single()
            if res and res["n"] > 0:
                logger.info(f"sync_orphans [{user_id}]: deleted {res['n']} dangling MENTIONS")
                total_pruned_links += res["n"]

            # Cross-user MENTIONS: diary entry owner must match fact owner
            res = s.run(
                "MATCH (d:DiaryEntry {userId: $userId})-[r:MENTIONS]->(f:Fact) WHERE f.userId <> $userId DELETE r RETURN count(*) as n",
                userId=user_id
            ).single()
            if res and res["n"] > 0:
                logger.info(f"sync_orphans [{user_id}]: deleted {res['n']} cross-user MENTIONS")
                total_pruned_links += res["n"]

            # Knowledge graph links from Facts must point to other Facts, DiaryEntries,
            # or scope nodes (Client/Context via FOR_CLIENT/IN_CONTEXT)
            res = s.run(
                """
                MATCH (f:Fact {userId: $userId})-[r]->(t)
                WHERE NOT type(r) IN ['IN_CATEGORY', 'KNOWS', 'FOR_CLIENT', 'IN_CONTEXT']
                  AND NOT t:Fact AND NOT t:DiaryEntry AND NOT t:Client AND NOT t:Context
                DELETE r
                RETURN count(*) as n
                """,
                userId=user_id
            ).single()
            if res and res["n"] > 0:
                logger.info(f"sync_orphans [{user_id}]: deleted {res['n']} dangling Fact links")
                total_pruned_links += res["n"]

    # -----------------------------------------------------------------------
    # Orphan categories: Category nodes with no facts → delete
    # -----------------------------------------------------------------------
    with neo4j_driver.session() as s:
        orphan_cats = list(s.run(
            "MATCH (c:Category) WHERE NOT (c)<-[:IN_CATEGORY]-(:Fact) "
            "RETURN c.name AS name"
        ))
    if orphan_cats:
        names = [r["name"] for r in orphan_cats]
        logger.info(f"sync_orphans: deleting {len(orphan_cats)} orphan categories: {names}")
        with neo4j_driver.session() as s:
            s.run(
                "MATCH (c:Category) WHERE NOT (c)<-[:IN_CATEGORY]-(:Fact) "
                "DETACH DELETE c"
            )

    if total_deleted or total_reembedded or orphan_cats or total_pruned_links:
        logger.info(
            f"sync_orphans done: deleted {total_deleted} Qdrant orphans, "
            f"re-embedded {total_reembedded} Neo4j orphans, "
            f"pruned {len(orphan_cats)} orphan categories, "
            f"removed {total_pruned_links} dangling links"
        )
    else:
        logger.info("sync_orphans: nothing to fix")


# ---------------------------------------------------------------------------
# Startup re-chunking
# ---------------------------------------------------------------------------
# New and edited long records are chunked on write. This exists only for records
# that predate chunking and are still stored as a single point: they are
# searchable, but their vector averages the whole document, so a query about a
# detail in the middle scores poorly against everything else.
async def _chunked_record_ids(qdrant, collection: str) -> set:
    """Every record id in a collection that already has more than one point.

    One scroll for the whole collection rather than a lookup per candidate: the
    lookup is cheap but the count is not, and on a large vault this is the
    difference between one pass and hundreds of round trips.
    """
    chunked = set()
    try:
        offset = None
        while True:
            result = await qdrant.scroll(
                collection_name=collection,
                limit=1000,
                offset=offset,
                with_payload=["parentId"],
                with_vectors=False,
            )
            points, next_offset = result
            for p in points:
                if p.payload and p.payload.get("parentId"):
                    chunked.add(str(p.payload["parentId"]))
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        # Returning empty here would make every large record look un-chunked and
        # the run would rewrite all of them, so this is not a soft failure.
        logger.error(f"rechunk: could not read chunk state from {collection}: {e}")
        return None
    return chunked


async def _rechunk_collection(qdrant, neo4j_driver, collection: str, label: str,
                              text_prop: str, upsert, limit: int, sem) -> dict:
    chunked_ids = await _chunked_record_ids(qdrant, collection)
    if chunked_ids is None:
        return {"chunked": 0, "failed": 0, "remaining": 0}

    with neo4j_driver.session() as s:
        res = s.run(
            f"""
            MATCH (n:{label})
            WHERE n.{text_prop} IS NOT NULL AND n.{text_prop} <> ''
            RETURN n.id AS id, n.{text_prop} AS text, n.name AS name
            """,
        )
        records = [dict(r) for r in res]

    wanted = rechunk_candidates(records, chunked_ids)[:limit]
    if not wanted:
        return {"chunked": 0, "failed": 0, "remaining": 0}

    totals = {"chunked": 0, "failed": 0, "remaining": 0}

    async def one(record):
        # replace=True: the single point being replaced is deleted as part of
        # the write, and it happens after every chunk is embedded, so a failed
        # embed leaves the old vector searchable rather than the record gone.
        async with sem:
            await upsert(
                qdrant, record["id"], record["text"], {},
                prefix=record.get("name"), replace=True,
            )

    # A bad record must not abort the run: it is counted and the rest continue.
    results = await asyncio.gather(
        *(one(r) for r in wanted), return_exceptions=True
    )
    for record, res in zip(wanted, results):
        if isinstance(res, Exception):
            totals["failed"] += 1
            logger.warning(
                f"rechunk: failed to chunk {label} {(record.get('name') or record['id'])[:60]!r} "
                f"— {type(res).__name__}: {res}"
            )
        else:
            totals["chunked"] += 1
            logger.info(
                f"rechunk: {label} {(record.get('name') or record['id'])[:60]!r} split into "
                f"{len(plan_chunks(record['text'])['chunks'])} chunks"
            )

    # Anything past the limit is left for the next boot rather than started and
    # abandoned, which would leave records half-written.
    eligible = rechunk_candidates(records, chunked_ids)
    totals["remaining"] = max(0, len(eligible) - len(wanted))
    return totals


async def rechunk_unindexed_records() -> dict:
    """Chunk the long records that are still stored as a single point.

    Runs as a background task at startup, not as a blocking lifespan step: it
    costs one embedding call per chunk and the app should not wait on that. The
    work is idempotent and bounded, so a restart finishes what is left.
    """
    if not RECHUNK_ENABLED or RECHUNK_LIMIT <= 0:
        logger.info("rechunk: disabled (MEM_RECHUNK_ENABLED=0)")
        return {}

    from diary_manager import _upsert_diary_points  # local: avoids an import cycle

    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        logger.warning("rechunk: skipped — Qdrant or Neo4j unavailable")
        return {}

    sem = asyncio.Semaphore(RECHUNK_CONCURRENCY)
    totals = {"chunked": 0, "failed": 0, "remaining": 0}
    for collection, label, prop, upsert in (
        (COLLECTION_NAME, "Fact", "text", _upsert_fact_points),
        (DIARY_COLLECTION, "DiaryEntry", "content", _upsert_diary_points),
    ):
        try:
            part = await _rechunk_collection(
                qdrant, neo4j_driver, collection, label, prop, upsert,
                RECHUNK_LIMIT, sem,
            )
        except Exception as e:
            logger.exception(f"rechunk: {label} pass failed: {e}")
            continue
        for key in totals:
            totals[key] += part.get(key, 0)

    if totals["chunked"] or totals["failed"]:
        logger.warning(
            f"rechunk: split {totals['chunked']} long record(s) into multiple vectors, "
            f"{totals['failed']} failed"
            + (f", {totals['remaining']} still pending for the next restart"
               if totals["remaining"] else "")
        )
    else:
        logger.info("rechunk: no long records waiting to be chunked")
    return totals
