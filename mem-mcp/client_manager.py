"""
client_manager.py – Client and Context management for multi-client memory separation.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from common import get_neo4j, logger

# ---------------------------------------------------------------------------
# Scope ranking tunables (shared by fact_manager and diary_manager)
# ---------------------------------------------------------------------------
STALE_DAYS = 90            # no mention within this window → client counts as inactive
INACTIVE_PENALTY = 0.15    # score demotion for inactive-scope results in global search
INFERRED_SCOPE_BOOST = 0.2  # boost for query-inferred scope (below explicit +0.3)


def _resolve_client_by_id(client_id: str, user_id: str) -> Optional[dict]:
    """Look up a Client by ID. Returns dict with id/name or None."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return None
    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (c:Client {id: $id, userId: $userId}) RETURN c",
            id=client_id, userId=user_id
        )
        record = result.single()
        if record:
            c = record["c"]
            return {"id": c["id"], "name": c["name"]}
    return None


def _resolve_context_by_id(context_id: str, user_id: str) -> Optional[dict]:
    """Look up a Context by ID. Returns dict with id/name or None."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return None
    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (ctx:Context {id: $id, userId: $userId}) RETURN ctx",
            id=context_id, userId=user_id
        )
        record = result.single()
        if record:
            ctx = record["ctx"]
            return {"id": ctx["id"], "name": ctx["name"]}
    return None


async def db_create_client(name: str, user_id: str) -> str:
    """Create or retrieve a Client node. Returns the client ID."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    client_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"client_{user_id}_{name.strip().lower()}"))

    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (c:Client {name: $name, userId: $userId})
            SET c.id = $clientId, c.active = true, c.createdAt = coalesce(c.createdAt, datetime())
            MERGE (u)-[:OWNS_CLIENT]->(c)
            """,
            userId=user_id, name=name.strip(), clientId=client_id
        )
    return client_id


async def db_create_context(name: str, client_id: str, user_id: str) -> str:
    """Create or retrieve a Context node under a Client. Returns the context ID."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    context_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"context_{user_id}_{client_id}_{name.strip().lower()}"))

    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (c:Client {id: $clientId, userId: $userId})
            MERGE (ctx:Context {name: $name, userId: $userId, clientId: $clientId})
            SET ctx.id = $contextId, ctx.active = true, ctx.createdAt = coalesce(ctx.createdAt, datetime())
            MERGE (u)-[:OWNS_CONTEXT]->(ctx)
            MERGE (c)-[:HAS_CONTEXT]->(ctx)
            """,
            userId=user_id, clientId=client_id, name=name.strip(), contextId=context_id
        )
    return context_id


def db_list_clients(user_id: str) -> list:
    """Return all Client nodes with their Contexts for a user."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (c:Client {userId: $userId})
            OPTIONAL MATCH (c)-[:HAS_CONTEXT]->(ctx:Context)
            RETURN c, collect(ctx) as contexts
            ORDER BY c.name ASC
            """,
            userId=user_id
        )
        clients = []
        for r in result:
            c_node = r["c"]
            contexts = [
                {"id": ctx["id"], "name": ctx["name"], "active": ctx.get("active", True)}
                for ctx in r["contexts"] if ctx.get("id")
            ]
            clients.append({
                "id": c_node["id"],
                "name": c_node["name"],
                "active": c_node.get("active", True),
                "statusPinned": c_node.get("statusPinned", False),
                "lastMentioned": c_node.get("lastMentioned").iso_format() if c_node.get("lastMentioned") and hasattr(c_node.get("lastMentioned"), "iso_format") else c_node.get("lastMentioned"),
                "effectiveActive": _effective_active(dict(c_node)),
                "contexts": contexts
            })
        return clients


def _effective_active(props: dict) -> bool:
    """Compute effective active status for a Client/Context node.

    Manually pinned status always wins. Otherwise a node counts as active
    when its stored flag is true AND it was mentioned within STALE_DAYS
    (falling back to createdAt, then benefit-of-the-doubt active).
    """
    if props.get("statusPinned"):
        return bool(props.get("active", True))
    if not props.get("active", True):
        return False
    ref = props.get("lastMentioned") or props.get("createdAt")
    if ref is None:
        return True
    try:
        ts = ref.iso_format() if hasattr(ref, "iso_format") else str(ref)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return age_days <= STALE_DAYS
    except Exception:
        return True


def db_get_client_status_map(user_id: str) -> dict:
    """Return {lower_client_name: effectiveActive} for all clients of a user.

    Single query used by search functions to demote inactive-scope results.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return {}
    status = {}
    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (c:Client {userId: $userId}) RETURN c",
            userId=user_id
        )
        for r in result:
            c = r["c"]
            name = (c.get("name") or "")
            if name:
                status[name.lower()] = _effective_active(dict(c))
    return status


async def db_set_client_active(client_id: str, active: bool, user_id: str) -> bool:
    """Manually pin a client's active status. Pinned status wins over staleness.

    Returns True if the client was found.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (c:Client {id: $clientId, userId: $userId})
            SET c.active = $active, c.statusPinned = true
            RETURN count(c) AS n
            """,
            clientId=client_id, userId=user_id, active=bool(active)
        )
        rec = result.single()
        return bool(rec and rec["n"] > 0)


def infer_scope_from_text(text: str, user_id: str) -> tuple:
    """Infer (client_name, context_name) from free text. Returns (None, None) on no match.

    Pure name matching, no LLM: exact full-name substring wins, otherwise the
    client with the best word-overlap score (minimum one shared word of len > 2).
    Context is only returned when it belongs to the matched client.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver or not text:
        return (None, None)
    try:
        clients = db_list_clients(user_id)
    except Exception as e:
        logger.warning(f"[infer_scope] list_clients failed: {e}")
        return (None, None)

    t = text.lower()
    t_words = {w for w in t.split() if len(w) > 2}

    best_client = None
    best_score = 0.0
    for c in clients:
        cname = (c.get("name") or "")
        if not cname:
            continue
        cl = cname.lower()
        if cl and cl in t:
            # Exact full-name hit beats everything
            best_client = c
            best_score = 2.0
            break
        c_words = {w for w in cl.split() if len(w) > 2}
        overlap = t_words & c_words
        if overlap:
            score = len(overlap) / max(len(c_words), 1)
            if score > best_score:
                best_score = score
                best_client = c

    if not best_client:
        return (None, None)

    best_context = None
    for cx in best_client.get("contexts") or []:
        xname = (cx.get("name") or "")
        if not xname:
            continue
        xl = xname.lower()
        if xl and xl in t:
            best_context = xname
            break
    return (best_client.get("name"), best_context)


def db_resolve_client(name: str, user_id: str) -> Optional[dict]:
    """Look up a Client by name (case-insensitive). Returns dict with id/name or None."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return None

    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (c:Client {userId: $userId}) WHERE toLower(c.name) = toLower($name) RETURN c",
            userId=user_id, name=name.strip()
        )
        record = result.single()
        if record:
            c = record["c"]
            return {"id": c["id"], "name": c["name"]}
    return None


def db_resolve_context(name: str, client_id: str, user_id: str) -> Optional[dict]:
    """Look up a Context by name under a specific Client. Returns dict with id/name or None."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return None

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (ctx:Context {userId: $userId, clientId: $clientId})
            WHERE toLower(ctx.name) = toLower($name)
            RETURN ctx
            """,
            userId=user_id, clientId=client_id, name=name.strip()
        )
        record = result.single()
        if record:
            ctx = record["ctx"]
            return {"id": ctx["id"], "name": ctx["name"]}
    return None


async def link_fact_to_client(fact_id: str, client_id: str, user_id: str):
    """Link a Fact to a Client via FOR_CLIENT relationship."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (c:Client {id: $clientId, userId: $userId})
            WITH c
            MATCH (f:Fact {id: $factId, userId: $userId})
            MERGE (f)-[:FOR_CLIENT]->(c)
            SET c.lastMentioned = datetime(), c.active = CASE WHEN coalesce(c.statusPinned, false) THEN c.active ELSE true END
            """,
            clientId=client_id, userId=user_id, factId=fact_id
        )


async def link_fact_to_context(fact_id: str, context_id: str, user_id: str):
    """Link a Fact to a Context via IN_CONTEXT relationship."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (ctx:Context {id: $contextId, userId: $userId})
            WITH ctx
            MATCH (f:Fact {id: $factId, userId: $userId})
            MERGE (f)-[:IN_CONTEXT]->(ctx)
            SET ctx.lastMentioned = datetime(), ctx.active = CASE WHEN coalesce(ctx.statusPinned, false) THEN ctx.active ELSE true END
            """,
            contextId=context_id, userId=user_id, factId=fact_id
        )


async def link_diary_to_client(entry_id: str, client_id: str, user_id: str):
    """Link a DiaryEntry to a Client via FOR_CLIENT relationship."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (c:Client {id: $clientId, userId: $userId})
            WITH c
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})
            MERGE (d)-[:FOR_CLIENT]->(c)
            SET c.lastMentioned = datetime(), c.active = CASE WHEN coalesce(c.statusPinned, false) THEN c.active ELSE true END
            """,
            clientId=client_id, userId=user_id, entryId=entry_id
        )


async def link_diary_to_context(entry_id: str, context_id: str, user_id: str):
    """Link a DiaryEntry to a Context via IN_CONTEXT relationship."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (ctx:Context {id: $contextId, userId: $userId})
            WITH ctx
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})
            MERGE (d)-[:IN_CONTEXT]->(ctx)
            SET ctx.lastMentioned = datetime(), ctx.active = CASE WHEN coalesce(ctx.statusPinned, false) THEN ctx.active ELSE true END
            """,
            contextId=context_id, userId=user_id, entryId=entry_id
        )