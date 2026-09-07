"""
client_manager.py – Client and Context management for multi-client memory separation.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from common import get_neo4j, get_qdrant, logger, COLLECTION_NAME, DIARY_COLLECTION

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


async def db_rename_client(client_id: str, name: str, user_id: str) -> bool:
    """Rename a Client node. Returns False if not found; raises ValueError on name collision.

    Qdrant clientName drift self-heals via the boot diff-sync (_backfill_qdrant).
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    name = (name or "").strip()
    if not name:
        raise ValueError("Client name must not be empty.")
    with neo4j_driver.session() as s:
        collision = s.run(
            """
            MATCH (other:Client {userId: $userId})
            WHERE toLower(other.name) = toLower($name) AND other.id <> $clientId
            RETURN count(other) AS n
            """,
            userId=user_id, name=name, clientId=client_id
        ).single()
        if collision and collision["n"] > 0:
            raise ValueError(f"Client name '{name}' already exists.")
        rec = s.run(
            """
            MATCH (c:Client {id: $clientId, userId: $userId})
            SET c.name = $name
            RETURN count(c) AS n
            """,
            clientId=client_id, userId=user_id, name=name
        ).single()
        return bool(rec and rec["n"] > 0)


async def db_rename_context(context_id: str, name: str, user_id: str) -> bool:
    """Rename a Context node. Returns False if not found; raises ValueError on name collision.

    Collision scope is the owning client. The node id stays stable (ids are only
    derived from the name at creation time).
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    name = (name or "").strip()
    if not name:
        raise ValueError("Project name must not be empty.")
    with neo4j_driver.session() as s:
        owner = s.run(
            "MATCH (ctx:Context {id: $contextId, userId: $userId}) RETURN ctx.clientId AS clientId",
            contextId=context_id, userId=user_id
        ).single()
        if not owner:
            return False
        collision = s.run(
            """
            MATCH (other:Context {userId: $userId, clientId: $clientId})
            WHERE toLower(other.name) = toLower($name) AND other.id <> $contextId
            RETURN count(other) AS n
            """,
            userId=user_id, clientId=owner["clientId"], name=name, contextId=context_id
        ).single()
        if collision and collision["n"] > 0:
            raise ValueError(f"Project name '{name}' already exists for this client.")
        rec = s.run(
            """
            MATCH (ctx:Context {id: $contextId, userId: $userId})
            SET ctx.name = $name
            RETURN count(ctx) AS n
            """,
            contextId=context_id, userId=user_id, name=name
        ).single()
        return bool(rec and rec["n"] > 0)


async def db_set_fact_scope(fact_id: str, client_id: Optional[str], context_id: Optional[str],
                            user_id: str) -> Optional[dict]:
    """Replace a Fact's client/project assignment (None clears that side).

    Rewrites the FOR_CLIENT / IN_CONTEXT links and patches the Qdrant payload
    immediately (the boot diff-sync self-heals any drift). Returns the new
    scope dict, or None if the fact was not found. Raises ValueError on
    unknown client/context ids.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    client = _resolve_client_by_id(client_id, user_id) if client_id else None
    if client_id and not client:
        raise ValueError(f"Unknown client '{client_id}'.")
    ctx = _resolve_context_by_id(context_id, user_id) if context_id else None
    if context_id and not ctx:
        raise ValueError(f"Unknown project '{context_id}'.")
    with neo4j_driver.session() as s:
        found = s.run(
            "MATCH (f:Fact {id: $factId, userId: $userId}) RETURN count(f) AS n",
            factId=fact_id, userId=user_id
        ).single()
        if not found or found["n"] == 0:
            return None
        s.run(
            "MATCH (f:Fact {id: $factId, userId: $userId})-[r:FOR_CLIENT]->(:Client) DELETE r",
            factId=fact_id, userId=user_id
        )
        s.run(
            "MATCH (f:Fact {id: $factId, userId: $userId})-[r:IN_CONTEXT]->(:Context) DELETE r",
            factId=fact_id, userId=user_id
        )
    if client:
        await link_fact_to_client(fact_id, client["id"], user_id)
    if ctx:
        await link_fact_to_context(fact_id, ctx["id"], user_id)
    try:
        qdrant = await get_qdrant()
        if qdrant:
            patch = {}
            if client:
                patch["clientId"] = client["id"]
                patch["clientName"] = client["name"]
            if ctx:
                patch["contextId"] = ctx["id"]
                patch["contextName"] = ctx["name"]
            if patch:
                await qdrant.set_payload(collection_name=COLLECTION_NAME, payload=patch, points=[fact_id])
            drop = [k for k, present in (("clientId", client), ("clientName", client),
                                         ("contextId", ctx), ("contextName", ctx)) if not present]
            if drop:
                await qdrant.delete_payload(collection_name=COLLECTION_NAME, keys=drop, points=[fact_id])
    except Exception as e:
        logger.warning(f"db_set_fact_scope: Qdrant payload patch failed for {fact_id}: {e}")
    return {
        "clientId": client["id"] if client else None,
        "clientName": client["name"] if client else None,
        "contextId": ctx["id"] if ctx else None,
        "contextName": ctx["name"] if ctx else None,
    }


def _ts_str(ts):
    return ts.iso_format() if ts is not None and hasattr(ts, "iso_format") else ts


def _scope_items(user_id: str, match_clause: str, params: dict) -> Optional[dict]:
    """Facts + diary entries linked via a FOR_CLIENT or IN_CONTEXT match.

    match_clause binds (f)-[...]->(scope) and (d)-[...]->(scope); params supply
    the scope identity. Returns None when the scope node does not exist.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        exists = s.run(
            f"MATCH (scope {params['label']} {{id: $scopeId, userId: $userId}}) RETURN count(scope) AS n",
            scopeId=params["scopeId"], userId=user_id
        ).single()
        if not exists or exists["n"] == 0:
            return None
        fact_rows = list(s.run(
            f"""
            MATCH (f:Fact {{userId: $userId}})-[{params['rel']}]->(scope {params['label']} {{id: $scopeId, userId: $userId}})
            OPTIONAL MATCH (f)-[:IN_CATEGORY]->(cat:Category)
            OPTIONAL MATCH (f)-[:FOR_CLIENT]->(cl:Client)
            OPTIONAL MATCH (f)-[:IN_CONTEXT]->(ctx:Context)
            RETURN f.id AS id, f.name AS name, f.text AS text, cat.name AS category,
                   f.timestamp AS timestamp, cl.id AS clientId, cl.name AS clientName,
                   ctx.id AS contextId, ctx.name AS contextName
            ORDER BY coalesce(f.name, f.text) ASC
            """,
            scopeId=params["scopeId"], userId=user_id
        ))
        diary_rows = list(s.run(
            f"""
            MATCH (d:DiaryEntry {{userId: $userId}})-[{params['rel']}]->(scope {params['label']} {{id: $scopeId, userId: $userId}})
            RETURN d.id AS id, d.date AS date, d.name AS name, d.timestamp AS timestamp
            ORDER BY d.date DESC, d.timestamp DESC
            """,
            scopeId=params["scopeId"], userId=user_id
        ))
    return {
        "facts": [
            {"id": r["id"], "name": r["name"], "text": r["text"], "category": r["category"],
             "timestamp": _ts_str(r["timestamp"]), "clientId": r["clientId"],
             "clientName": r["clientName"], "contextId": r["contextId"],
             "contextName": r["contextName"]}
            for r in fact_rows
        ],
        "diary": [
            {"id": r["id"], "date": r["date"], "name": r.get("name") or "Unnamed Entry",
             "timestamp": _ts_str(r["timestamp"])}
            for r in diary_rows
        ],
    }


def db_client_items(client_id: str, user_id: str) -> Optional[dict]:
    """Facts + diary entries linked to a Client (None if unknown)."""
    return _scope_items(user_id, "", {"label": ":Client", "rel": ":FOR_CLIENT", "scopeId": client_id})


def db_context_items(context_id: str, user_id: str) -> Optional[dict]:
    """Facts + diary entries linked to a Context (None if unknown)."""
    return _scope_items(user_id, "", {"label": ":Context", "rel": ":IN_CONTEXT", "scopeId": context_id})


async def _drop_scope_payload(collection: str, point_ids: list, keys: list):
    """Best-effort Qdrant scope-key cleanup (the boot diff-sync self-heals any remainder)."""
    if not point_ids or not keys:
        return
    try:
        qdrant = await get_qdrant()
        if qdrant:
            await qdrant.delete_payload(collection_name=collection, keys=keys, points=point_ids)
    except Exception as e:
        logger.warning(f"scope Qdrant cleanup failed ({collection}): {e}")


async def db_delete_client(client_id: str, user_id: str) -> bool:
    """Delete a Client node with all its Contexts. Facts/diary entries keep
    existing (links removed); their Qdrant scope keys are cleaned up."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        exists = s.run(
            "MATCH (c:Client {id: $clientId, userId: $userId}) RETURN count(c) AS n",
            clientId=client_id, userId=user_id
        ).single()
        if not exists or exists["n"] == 0:
            return False
        fact_ids = [r["id"] for r in s.run(
            """
            MATCH (f:Fact {userId: $userId})-[:FOR_CLIENT]->(c:Client {id: $clientId, userId: $userId})
            RETURN f.id AS id
            """,
            clientId=client_id, userId=user_id
        )]
        diary_ids = [r["id"] for r in s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})-[:FOR_CLIENT]->(c:Client {id: $clientId, userId: $userId})
            RETURN d.id AS id
            """,
            clientId=client_id, userId=user_id
        )]
        ctx_fact_ids = [r["id"] for r in s.run(
            """
            MATCH (f:Fact {userId: $userId})-[:IN_CONTEXT]->(ctx:Context {userId: $userId, clientId: $clientId})
            RETURN f.id AS id
            """,
            clientId=client_id, userId=user_id
        )]
        ctx_diary_ids = [r["id"] for r in s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})-[:IN_CONTEXT]->(ctx:Context {userId: $userId, clientId: $clientId})
            RETURN d.id AS id
            """,
            clientId=client_id, userId=user_id
        )]
        s.run(
            """
            MATCH (c:Client {id: $clientId, userId: $userId})
            OPTIONAL MATCH (c)-[:HAS_CONTEXT]->(ctx:Context)
            DETACH DELETE c, ctx
            """,
            clientId=client_id, userId=user_id
        )
    await _drop_scope_payload(COLLECTION_NAME, fact_ids, ["clientId", "clientName"])
    await _drop_scope_payload(DIARY_COLLECTION, diary_ids, ["clientId", "clientName"])
    await _drop_scope_payload(COLLECTION_NAME, ctx_fact_ids, ["contextId", "contextName"])
    await _drop_scope_payload(DIARY_COLLECTION, ctx_diary_ids, ["contextId", "contextName"])
    return True


async def db_delete_context(context_id: str, user_id: str) -> bool:
    """Delete a Context node. Linked facts/diary entries keep existing (unlinked)."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        exists = s.run(
            "MATCH (ctx:Context {id: $contextId, userId: $userId}) RETURN count(ctx) AS n",
            contextId=context_id, userId=user_id
        ).single()
        if not exists or exists["n"] == 0:
            return False
        fact_ids = [r["id"] for r in s.run(
            """
            MATCH (f:Fact {userId: $userId})-[:IN_CONTEXT]->(ctx:Context {id: $contextId, userId: $userId})
            RETURN f.id AS id
            """,
            contextId=context_id, userId=user_id
        )]
        diary_ids = [r["id"] for r in s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})-[:IN_CONTEXT]->(ctx:Context {id: $contextId, userId: $userId})
            RETURN d.id AS id
            """,
            contextId=context_id, userId=user_id
        )]
        s.run(
            "MATCH (ctx:Context {id: $contextId, userId: $userId}) DETACH DELETE ctx",
            contextId=context_id, userId=user_id
        )
    await _drop_scope_payload(COLLECTION_NAME, fact_ids, ["contextId", "contextName"])
    await _drop_scope_payload(DIARY_COLLECTION, diary_ids, ["contextId", "contextName"])
    return True


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


async def db_set_diary_scope(entry_id: str, client_id: Optional[str], context_id: Optional[str],
                              user_id: str) -> Optional[dict]:
    """Replace a DiaryEntry's client/project assignment (None clears that side).

    Mirrors db_set_fact_scope but targets DiaryEntry nodes and DIARY_COLLECTION.
    Returns the new scope dict, or None if the entry was not found.
    Raises ValueError on unknown client/context ids.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    client = _resolve_client_by_id(client_id, user_id) if client_id else None
    if client_id and not client:
        raise ValueError(f"Unknown client '{client_id}'.")
    ctx = _resolve_context_by_id(context_id, user_id) if context_id else None
    if context_id and not ctx:
        raise ValueError(f"Unknown project '{context_id}'.")
    with neo4j_driver.session() as s:
        found = s.run(
            "MATCH (d:DiaryEntry {id: $entryId, userId: $userId}) RETURN count(d) AS n",
            entryId=entry_id, userId=user_id
        ).single()
        if not found or found["n"] == 0:
            return None
        s.run(
            "MATCH (d:DiaryEntry {id: $entryId, userId: $userId})-[r:FOR_CLIENT]->(:Client) DELETE r",
            entryId=entry_id, userId=user_id
        )
        s.run(
            "MATCH (d:DiaryEntry {id: $entryId, userId: $userId})-[r:IN_CONTEXT]->(:Context) DELETE r",
            entryId=entry_id, userId=user_id
        )
    if client:
        await link_diary_to_client(entry_id, client["id"], user_id)
    if ctx:
        await link_diary_to_context(entry_id, ctx["id"], user_id)
    try:
        qdrant = await get_qdrant()
        if qdrant:
            patch = {}
            if client:
                patch["clientId"] = client["id"]
                patch["clientName"] = client["name"]
            if ctx:
                patch["contextId"] = ctx["id"]
                patch["contextName"] = ctx["name"]
            if patch:
                await qdrant.set_payload(collection_name=DIARY_COLLECTION, payload=patch, points=[entry_id])
            drop = [k for k, present in (("clientId", client), ("clientName", client),
                                         ("contextId", ctx), ("contextName", ctx)) if not present]
            if drop:
                await qdrant.delete_payload(collection_name=DIARY_COLLECTION, keys=drop, points=[entry_id])
    except Exception as e:
        logger.warning(f"db_set_diary_scope: Qdrant payload patch failed for {entry_id}: {e}")
    return {
        "clientId": client["id"] if client else None,
        "clientName": client["name"] if client else None,
        "contextId": ctx["id"] if ctx else None,
        "contextName": ctx["name"] if ctx else None,
    }


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