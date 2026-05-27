"""
diary_manager.py – Diary entry management, search, and automatic link generation.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

import re
from common import (
    get_qdrant, get_neo4j, logger, get_embedding, publish_db_event,
    DIARY_COLLECTION, QDRANT_URL
)

# ---------------------------------------------------------------------------
# Diary helpers
# ---------------------------------------------------------------------------

def _diary_id(user_id: str, timestamp: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"diary_{user_id}_{timestamp}"))


async def db_save_diary(content: str, user_id: str, timestamp: str, name: str, metadata: Optional[dict] = None, linked_facts: Optional[list] = None) -> str:
    """Upsert a diary entry keyed by user + timestamp. Returns the ISO timestamp string.
    Optional linked_facts is a list of fact IDs to create MENTIONS relationships.
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

    payload = {"content": content, "name": name, "date": entry_date, "timestamp": timestamp, "userId": user_id}
    if metadata:
        payload["metadata"] = metadata

    # Qdrant — upsert by the stable doc_id so re-saving replaces the vector
    await qdrant.upsert(
        collection_name=DIARY_COLLECTION,
        points=[PointStruct(
            id=doc_id,
            vector=vector,
            payload=payload,
        )],
    )

    neo4j_props = "d.date = $date, d.timestamp = $timestamp, d.content = $content, d.name = $name"
    if metadata:
        neo4j_props += ", d.metadata = $metadata"

    with neo4j_driver.session() as s:
        params = dict(userId=user_id, id=doc_id, date=entry_date, timestamp=timestamp, content=content, name=name)
        if metadata:
            params["metadata"] = metadata
        s.run(
            f"""
            MERGE (u:User {{id: $userId}})
            MERGE (d:DiaryEntry {{id: $id, userId: $userId}})
            SET {neo4j_props}
            MERGE (u)-[:WROTE_DIARY]->(d)
            """,
            **params
        )

        # Sync linked facts: remove stale, add new
        if linked_facts is not None:
            # Remove MENTIONS to facts NOT in the incoming list
            s.run(
                """
                MATCH (d:DiaryEntry {id: $id, userId: $userId})-[r:MENTIONS]->(f:Fact)
                WHERE NOT f.id IN $factIds
                DELETE r
                """,
                id=doc_id, userId=user_id, factIds=linked_facts
            )
            # Add MENTIONS for links not yet present
            if linked_facts:
                s.run(
                    """
                    MATCH (d:DiaryEntry {id: $id, userId: $userId})
                    MATCH (f:Fact) WHERE f.id IN $factIds
                    MERGE (d)-[:MENTIONS]->(f)
                    """,
                    id=doc_id, userId=user_id, factIds=linked_facts
                )
        else:
            # Automatic linking — first clear stale mentions, then rebuild from current content
            s.run(
                "MATCH (d:DiaryEntry {id: $id, userId: $userId})-[r:MENTIONS]->() DELETE r",
                id=doc_id, userId=user_id
            )
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
                name_val = r["text"].lower()
                if name_val in content_lower:
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


async def db_update_diary(entry_id: str, user_id: str, content: Optional[str] = None, name: Optional[str] = None, timestamp: Optional[str] = None, metadata: Optional[dict] = None, linked_facts: Optional[list] = None) -> bool:
    """Update a diary entry's content, name, timestamp, metadata, and optionally replace linked facts.
    If linked_facts is provided, existing MENTIONS relationships are cleared and new ones are created.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (d:DiaryEntry {id: $id, userId: $userId}) RETURN d.content as content, d.name as name, d.timestamp as timestamp, d.metadata as metadata
            """,
            id=entry_id, userId=user_id
        )
        existing = res.single()
        if not existing:
            return False

        new_content = content if content is not None else existing["content"]
        new_name = name if name is not None else existing["name"]
        new_ts = timestamp if timestamp is not None else existing["timestamp"]
        new_metadata = metadata if metadata is not None else existing.get("metadata")
        entry_date = new_ts[:10]

        neo4j_props = "d.content = $content, d.name = $name, d.timestamp = $ts, d.date = $date"
        params = dict(id=entry_id, userId=user_id, content=new_content, name=new_name, ts=new_ts, date=entry_date)
        if new_metadata is not None:
            neo4j_props += ", d.metadata = $metadata"
            params["metadata"] = new_metadata

        s.run(
            f"""
            MATCH (d:DiaryEntry {{id: $id, userId: $userId}})
            SET {neo4j_props}
            """,
            **params
        )

        # Sync linked facts: remove stale, add new
        if linked_facts is not None:
            # Remove MENTIONS to facts NOT in the incoming list
            s.run(
                """
                MATCH (d:DiaryEntry {id: $id, userId: $userId})-[r:MENTIONS]->(f:Fact)
                WHERE NOT f.id IN $factIds
                DELETE r
                """,
                id=entry_id, userId=user_id, factIds=linked_facts
            )
            # Add MENTIONS for links not yet present
            if linked_facts:
                s.run(
                    """
                    MATCH (d:DiaryEntry {id: $id, userId: $userId})
                    MATCH (f:Fact) WHERE f.id IN $factIds
                    MERGE (d)-[:MENTIONS]->(f)
                    """,
                    id=entry_id, userId=user_id, factIds=linked_facts
                )

    # Re-embed in Qdrant
    vector = await get_embedding(f"{new_name}: {new_content}" if new_name else new_content)
    payload = {"content": new_content, "name": new_name, "date": entry_date, "timestamp": new_ts, "userId": user_id}
    if new_metadata is not None:
        payload["metadata"] = new_metadata
    await qdrant.upsert(
        collection_name=DIARY_COLLECTION,
        points=[PointStruct(
            id=entry_id,
            vector=vector,
            payload=payload,
        )],
    )

    await publish_db_event(user_id, "diary_changed", {"action": "update", "id": entry_id, "date": entry_date})
    return True


async def db_link_diary_mention(entry_id: str, fact_id: str, user_id: str):
    """Create a MENTIONS relationship from a diary entry to a fact."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})
            MATCH (f:Fact {id: $factId, userId: $userId})
            MERGE (d)-[:MENTIONS]->(f)
            """,
            entryId=entry_id, factId=fact_id, userId=user_id
        )
    await publish_db_event(user_id, "diary_changed", {"action": "link", "id": entry_id})


async def db_unlink_diary_mention(entry_id: str, fact_id: str, user_id: str):
    """Remove a MENTIONS relationship from a diary entry to a fact."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})-[r:MENTIONS]->(f:Fact {id: $factId, userId: $userId})
            DELETE r
            """,
            entryId=entry_id, factId=fact_id, userId=user_id
        )
    await publish_db_event(user_id, "diary_changed", {"action": "unlink", "id": entry_id})


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

    # Delete from Qdrant (direct HTTP call to avoid serializing shard_key: null)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{QDRANT_URL}/collections/{DIARY_COLLECTION}/points/delete",
            json={"points": [entry_id]},
            params={"wait": "true"},
        )
        r.raise_for_status()

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


async def _scroll_diary_ids(qdrant, user_id: str) -> set:
    """Scroll all Qdrant point IDs for a user in the diary collection."""
    ids = set()
    try:
        offset = None
        while True:
            result = await qdrant.scroll(
                collection_name=DIARY_COLLECTION,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
                scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
            )
            points, next_offset = result
            for p in points:
                ids.add(str(p.id))
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.error(f"consistency: Qdrant diary scroll failed for {user_id}: {e}")
    return ids


async def run_diary_consistency_checks():
    """Read-only diary-specific consistency checks across Neo4j and Qdrant. Logs all discrepancies."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        logger.warning("consistency: DB not available, skipping diary checks")
        return

    with neo4j_driver.session() as s:
        user_rows = list(s.run("MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS userId"))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        logger.info("consistency: no diary users found")
        return

    issues_found = False
    dangling_links = 0

    for user_id in sorted(user_ids):
        # --- Diary counts: Neo4j vs Qdrant ---
        with neo4j_driver.session() as s:
            diary_rows = list(s.run(
                "MATCH (d:DiaryEntry {userId: $userId}) RETURN d.id AS id",
                userId=user_id
            ))
        neo4j_ids = {r["id"] for r in diary_rows}
        neo4j_count = len(neo4j_ids)

        qdrant_ids = await _scroll_diary_ids(qdrant, user_id)
        qdrant_count = len(qdrant_ids)

        if neo4j_count != qdrant_count:
            issues_found = True
            logger.warning(
                f"consistency [{user_id}]: Diary count mismatch — "
                f"Neo4j: {neo4j_count}, Qdrant: {qdrant_count}"
            )
            only_neo4j = neo4j_ids - qdrant_ids
            only_qdrant = qdrant_ids - neo4j_ids
            if only_neo4j:
                logger.warning(
                    f"consistency [{user_id}]: {len(only_neo4j)} diary(s) in Neo4j only: "
                    f"{','.join(sorted(only_neo4j)[:20])}"
                )
            if only_qdrant:
                logger.warning(
                    f"consistency [{user_id}]: {len(only_qdrant)} diary(s) in Qdrant only: "
                    f"{','.join(sorted(only_qdrant)[:20])}"
                )
        else:
            logger.info(f"consistency [{user_id}]: Diary OK ({neo4j_count})")

        # --- Dangling MENTIONS ---
        with neo4j_driver.session() as s:
            bad = list(s.run(
                "MATCH (d:DiaryEntry {userId: $userId})-[r:MENTIONS]->(target) "
                "WHERE NOT target:Fact RETURN count(*) AS count",
                userId=user_id
            ))
        count = bad[0]["count"] if bad else 0
        if count:
            issues_found = True
            dangling_links += count
            logger.warning(f"consistency [{user_id}]: {count} MENTIONS link(s) to non-Fact nodes")

    # --- Cross-user diary checks ---
    with neo4j_driver.session() as s:
        no_user = list(s.run("MATCH (d:DiaryEntry) WHERE d.userId IS NULL RETURN count(*) AS count"))
    count = no_user[0]["count"] if no_user else 0
    if count:
        issues_found = True
        logger.warning(f"consistency: {count} diary entries have no userId")

    with neo4j_driver.session() as s:
        untitled = list(s.run(
            "MATCH (d:DiaryEntry) WHERE d.name IS NULL OR d.name = '' "
            "RETURN d.id AS id LIMIT 20"
        ))
    if untitled:
        issues_found = True
        logger.warning(
            f"consistency: {len(untitled)}+ diary entries without a title "
            f"(IDs: {', '.join(r['id'] for r in untitled)})"
        )

    with neo4j_driver.session() as s:
        bad_ts = list(s.run(
            "MATCH (d:DiaryEntry) WHERE d.timestamp IS NULL OR d.timestamp = '' "
            "RETURN d.id AS id LIMIT 20"
        ))
    if bad_ts:
        issues_found = True
        logger.warning(
            f"consistency: {len(bad_ts)}+ diary entries without a valid timestamp "
            f"(IDs: {', '.join(r['id'] for r in bad_ts)})"
        )

    if not issues_found:
        logger.info("consistency diary: All checks passed")
    else:
        logger.info(f"consistency diary: Summary — {len(user_ids)} users checked, {dangling_links} dangling links")
