"""
client_manager.py – Client and Context management for multi-client memory separation.
"""

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Optional

from common import get_neo4j, get_qdrant, get_llm_response, logger, COLLECTION_NAME, DIARY_COLLECTION
from matching_utils import plan_search_scope
from scoped_ids import client_id_for, context_id_for
import sharing

# ---------------------------------------------------------------------------
# Scope ranking tunables (shared by fact_manager and diary_manager)
# ---------------------------------------------------------------------------
STALE_DAYS = 90            # no mention within this window → client counts as inactive
INACTIVE_PENALTY = 0.15    # score demotion for inactive-scope results in global search
INFERRED_SCOPE_BOOST = 0.2  # boost for query-inferred scope (below explicit +0.3)
SCOPE_LIST_TTL_SECONDS = 60
_SCOPE_LIST_CACHE: dict = {}


async def _scope_targets(qdrant, record_id: str, collection: str) -> list:
    """Every Qdrant point id for a record, so a scope patch reaches all of it.

    A long record is stored as a family of chunk points. Chunk 0 keeps the
    record id, which is why addressing a single point by ``record_id`` looks
    correct until a record is long enough to be chunked — then the other chunks
    keep stale scope keys. The import is function-local because
    ``fact_manager`` and ``client_manager`` are peers; a module-level import
    would couple them for the sake of one helper.
    """
    from fact_manager import find_chunk_family
    try:
        return await find_chunk_family(qdrant, record_id, collection)
    except Exception as exc:  # noqa: BLE001 - a scope patch must not fail the write
        logger.warning(f"could not resolve chunk family for {record_id}: {exc}")
        return [record_id]  # unchunked record: the record id is the point id


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

    client_id = client_id_for(user_id, name)

    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (c:Client {name: $name, userId: $userId})
            SET c.id = $clientId, c.active = true, c.createdAt = coalesce(c.createdAt, datetime()),
                c.updatedAt = datetime()
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

    context_id = context_id_for(user_id, client_id, name)

    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (u:User {id: $userId})
            MERGE (c:Client {id: $clientId, userId: $userId})
            MERGE (ctx:Context {name: $name, userId: $userId, clientId: $clientId})
            SET ctx.id = $contextId, ctx.active = true, ctx.createdAt = coalesce(ctx.createdAt, datetime()),
                ctx.updatedAt = datetime()
            MERGE (u)-[:OWNS_CONTEXT]->(ctx)
            MERGE (c)-[:HAS_CONTEXT]->(ctx)
            """,
            userId=user_id, clientId=client_id, name=name.strip(), contextId=context_id
        )
    return context_id


def db_list_clients(user_id: str) -> list:
    """Return all Client nodes with their Contexts for a user.

    **One row per client.** `HAS_CONTEXT` is many-valued by definition — a
    client has several projects — so collecting the contexts in the `RETURN`
    deduplicated them *within* each row without merging the rows, and a client
    with two projects came back twice with the same complete list attached.
    The client list is pushed one-per into `clientListCache`, so it drew two
    identical cards and the client filter offered the name twice. The collect
    belongs in a `WITH` before the `RETURN`; see
    `test_cypher_safety.OneRowPerRecordTests`.

    **With sharing enabled this list is the owned vault PLUS explicitly granted
    targets.** Shared clients/contexts are merged in (sorted by name) and
    flagged `shared: True`, `owner: <ownerId>`, `permission: <read|write>`;
    own rows carry none of those keys, which keeps every pre-sharing consumer
    byte-compatible. A context grant keeps its parent client visible with
    only the granted context underneath — siblings are never granted along
    with the parent node, and a client grant shares the whole client subtree
    (all of its contexts), which is what sharing a client means.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    scope_map = sharing.accessible_owner_scope(user_id)

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (c:Client {userId: $userId})
            OPTIONAL MATCH (c)-[:HAS_CONTEXT]->(ctx:Context)
            WITH c, collect(ctx) as contexts
            RETURN c, contexts
            ORDER BY c.name ASC
            """,
            userId=user_id,
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
                "crossClient": bool(c_node.get("crossClient", False)),
                "lastMentioned": c_node.get("lastMentioned").iso_format() if c_node.get("lastMentioned") and hasattr(c_node.get("lastMentioned"), "iso_format") else c_node.get("lastMentioned"),
                "effectiveActive": _effective_active(dict(c_node)),
                "contexts": contexts
            })

    if scope_map and scope_map["owners"]:
        with neo4j_driver.session() as s:
            clients.extend(_shared_client_entries(s, scope_map["owners"]))
        clients.sort(key=lambda c: c.get("name") or "")
    return clients


def _shared_client_entries(neo4j_session, owners: dict) -> list:
    """Client entries reachable through grants, one per shared target.

    Three passes per owner, because the shapes differ:

    - **granted clients** — the client plus *all* of its contexts (a client
      grant shares the client's whole subtree);
    - **granted contexts** — the context plus its parent client id;
    - **parent clients** of context grants that are not themselves granted —
      their entry carries only the granted context underneath, so a sibling
      project never rides along.
    """
    entries: dict = {}

    for owner, granted in sorted(owners.items()):
        client_ids = sorted(c for c, _p in granted["clients"].items())
        context_ids = sorted(x for x, _p in granted["contexts"].items())

        # Pass 1: granted clients and every context they own.
        if client_ids:
            rows = neo4j_session.run(
                """
                MATCH (c:Client {userId: $ownerId})
                WHERE c.id IN $clientIds
                OPTIONAL MATCH (c)-[:HAS_CONTEXT]->(ctx:Context)
                WITH c, collect(ctx) as contexts
                RETURN c, contexts
                """,
                ownerId=owner, clientIds=client_ids,
            )
            for r in rows:
                c_node = r["c"]
                cid = c_node["id"]
                contexts = [
                    {"id": ctx["id"], "name": ctx["name"],
                     "active": ctx.get("active", True),
                     **_shared_stamp(owner, granted["clients"][cid])}
                    for ctx in r["contexts"] if ctx.get("id")
                ]
                entry = _shared_client_node(c_node, owner, cid, granted["clients"][cid])
                entry["contexts"] = contexts
                entries[cid] = entry

        # Pass 2: granted contexts and their parent client ids.
        parents: dict = {}
        if context_ids:
            rows = neo4j_session.run(
                """
                MATCH (ctx:Context {userId: $ownerId})
                WHERE ctx.id IN $contextIds
                OPTIONAL MATCH (parent:Client)-[:HAS_CONTEXT]->(ctx)
                RETURN ctx, parent.id AS parentId, parent.name AS parentName
                """,
                ownerId=owner, contextIds=context_ids,
            )
            ctx_rows = []
            for r in rows:
                ctx = r["ctx"]
                if not ctx.get("id"):
                    continue
                perm = granted["contexts"][ctx["id"]]
                ctx_rows.append((ctx, perm))
                if r["parentId"]:
                    parents[ctx["id"]] = (r["parentId"], r["parentName"], perm)

            # Pass 3: parents that need their own entry (navigation only).
            parent_ids = sorted({pid for pid, _n, _p in parents.values()
                                 if pid not in entries})
            if parent_ids:
                prows = neo4j_session.run(
                    "MATCH (c:Client {userId: $ownerId}) WHERE c.id IN $parentIds RETURN c",
                    ownerId=owner, parentIds=parent_ids,
                )
                for pr in prows:
                    pc = pr["c"]
                    entries[pc["id"]] = _shared_client_node(pc, owner, pc["id"], "read")

            for ctx, perm in ctx_rows:
                parent_pid = parents.get(ctx["id"], (None,))[0]
                xentry = {"id": ctx["id"], "name": ctx["name"],
                          "active": ctx.get("active", True), **_shared_stamp(owner, perm)}
                # The parent's permission stays navigation-only ("read")
                # unless a real client grant says otherwise: a context
                # grant covers the context and its records, never the
                # client node — audit.grant_permits refuses exactly that,
                # so the list must not advertise what a check will 404.
                nested = False
                if parent_pid and parent_pid in entries:
                    clist = entries[parent_pid]["contexts"]
                    if not any(x["id"] == ctx["id"] for x in clist):
                        clist.append(xentry)
                    nested = True
                # A context with NO parent client has nowhere to nest, so it
                # gets an entry of its own or it is unreachable. This is the
                # ONLY case that reaches here: testing `ctx["id"] not in
                # entries` instead — which reads like the same thing — is
                # always true, because a Context id is never a key of
                # `entries` unless this line put it there. Every granted
                # context therefore appeared TWICE: nested under its client
                # where it belongs, and again as a top-level row sorted in
                # among the clients.
                if not nested:
                    entries[ctx["id"]] = xentry

    return list(entries.values())


def _shared_stamp(owner: str, perm: str) -> dict:
    """The flags every shared row carries and no own row does."""
    return {"shared": True, "owner": owner, "permission": perm}


def _shared_client_node(node, owner: str, cid: str, perm: str) -> dict:
    return {
        "id": cid,
        "name": node["name"],
        "active": node.get("active", True),
        "statusPinned": node.get("statusPinned", False),
        "crossClient": bool(node.get("crossClient", False)),
        "lastMentioned": node.get("lastMentioned"),
        "effectiveActive": _effective_active(dict(node)),
        "contexts": [],
        **_shared_stamp(owner, perm),
    }


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


def db_owner_of_target(target_id: str) -> Optional[str]:
    """Return the userId that owns a target node, or None if it does not exist.

    Covers Client, Context, Fact and DiaryEntry in one lookup: the id space is
    not partitioned by type in practice (a target id names one node), and the
    gate needs the owner before it knows which store the target lives in. An
    unknown id answers None so the caller can map it to a 404 rather than a 403.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (n) WHERE n.id = $id AND (n:Client OR n:Context OR n:Fact OR n:DiaryEntry) "
            "RETURN n.userId AS userId",
            id=target_id,
        )
        record = result.single()
        return record["userId"] if record else None


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


def db_plan_search_scope(client: Optional[str], context: Optional[str], user_id: str) -> dict:
    """Resolve a search's requested client/context to canonical stored names.

    Returns ``{"client", "context", "clientEvidence", "contextEvidence",
    "unresolvedClient", "unresolvedContext", "knownClients"}``.

    A name that cannot be resolved is reported, not raised: it means the search
    gets no scope signal, not that the search returns nothing. Known spellings
    come back in the result so the tool can tell the caller what it did have.
    """
    clients = db_list_clients(user_id)
    resolved_client, resolved_context, client_evidence, context_evidence = \
        plan_search_scope(client, context, clients)
    return {
        "client": resolved_client,
        "context": resolved_context,
        "clientEvidence": client_evidence,
        "contextEvidence": context_evidence,
        "unresolvedClient": client if client and not resolved_client else None,
        "unresolvedContext": context if context and not resolved_context else None,
        "knownClients": [c["name"] for c in clients if c.get("name")],
    }


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
            SET c.active = $active, c.statusPinned = true, c.updatedAt = datetime()
            RETURN count(c) AS n
            """,
            clientId=client_id, userId=user_id, active=bool(active)
        )
        rec = result.single()
        return bool(rec and rec["n"] > 0)


async def db_set_client_cross(client_id: str, cross: bool, user_id: str) -> bool:
    """Set the crossClient flag on a Client node.

    A cross-client is a pseudo-client (e.g. 'Colleagues') whose facts span
    multiple real clients. Its facts are excluded from the unanimous-MENTIONS
    fast-path and from step-4 migration so they never drive diary scoping.

    Returns True if the client was found.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (c:Client {id: $clientId, userId: $userId})
            SET c.crossClient = $cross, c.updatedAt = datetime()
            RETURN count(c) AS n
            """,
            clientId=client_id, userId=user_id, cross=bool(cross)
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
            SET c.name = $name, c.updatedAt = datetime()
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
            SET ctx.name = $name, ctx.updatedAt = datetime()
            RETURN count(ctx) AS n
            """,
            contextId=context_id, userId=user_id, name=name
        ).single()
        return bool(rec and rec["n"] > 0)


def _stamp_manual_scope(node_id: str, label: str, user_id: str) -> None:
    """Stamp scopeCheckedSig so the LLM backfill won't overwrite a manual assignment.

    Computes the current client/context signature for this user and writes it onto
    the node.  Called by db_set_fact_scope and db_set_diary_scope.

    The signature alone is not enough to mean "a human decided this". It is
    written by the classifier too, byte for byte the same value, so a reclassify
    could not tell a chosen scope from a guessed one. ``scopeManual`` is that
    distinction: the boot backfill already skips a linked item, but the full
    reclassify selects *every* item and rewrites it, so a hand-set scope was
    silently replaced by a guess. Once this is set, the scope is trusted.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return
    try:
        clients = db_list_clients(user_id)
        parts = []
        for c in sorted(clients, key=lambda x: x.get("name", "")):
            ctxs = sorted(x.get("name", "") for x in c.get("contexts", []))
            parts.append(c.get("name", "") + "|" + ",".join(ctxs))
        sig = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        with neo4j_driver.session() as s:
            s.run(
                f"MATCH (n:{label} {{id: $id, userId: $userId}}) "
                "SET n.scopeCheckedSig = $sig, n.scopeManual = true, n.updatedAt = datetime()",
                id=node_id, userId=user_id, sig=sig,
            )
    except Exception as exc:
        logger.debug(f"_stamp_manual_scope: failed for {node_id}: {exc}")


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

    # Phase 4: write gate on the fact's current scope.
    actor = user_id
    owner = db_owner_of_target(fact_id)
    if owner is None:
        return None
    if owner != user_id:
        with neo4j_driver.session() as s:
            res = s.run(
                "MATCH (f:Fact {id: $id})-[:FOR_CLIENT]->(c:Client) RETURN c.id as cid",
                id=fact_id)
            fc = [r["cid"] for r in res if r.get("cid")]
            res = s.run(
                "MATCH (f:Fact {id: $id})-[:IN_CONTEXT]->(cx:Context) RETURN cx.id as xid",
                id=fact_id)
            fx = [r["xid"] for r in res if r.get("xid")]
            owners = (sharing.accessible_owner_scope(user_id) or {}).get("owners")
            if not (any(sharing.has_access(user_id, owner, "client", cid,
                                          level="write") for cid in fc)
                    or any(sharing.has_access(user_id, owner, "context", xid,
                                             level="write") for xid in fx)):
                raise PermissionError("write access required")
        user_id = owner

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
            "MATCH (f:Fact {id: $factId, userId: $userId}) SET f.updatedAt = datetime() "
            "WITH f MATCH (f)-[r:FOR_CLIENT]->(:Client) DELETE r",
            factId=fact_id, userId=user_id
        )
        s.run(
            "MATCH (f:Fact {id: $factId, userId: $userId}) SET f.updatedAt = datetime() "
            "WITH f MATCH (f)-[r:IN_CONTEXT]->(:Context) DELETE r",
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
            # Scope belongs to the record, not to a point. A long fact is several
            # points, so patching only the one whose id equals the record id
            # leaves every other chunk with stale scope keys — a client-filtered
            # search would then match only the first passage, and unlinking a
            # client would not remove the record from that client's results.
            family = await _scope_targets(qdrant, fact_id, COLLECTION_NAME)
            if patch:
                await qdrant.set_payload(collection_name=COLLECTION_NAME, payload=patch, points=family)
            drop = [k for k, present in (("clientId", client), ("clientName", client),
                                         ("contextId", ctx), ("contextName", ctx)) if not present]
            if drop:
                await qdrant.delete_payload(collection_name=COLLECTION_NAME, keys=drop, points=family)
    except Exception as e:
        logger.warning(f"db_set_fact_scope: Qdrant payload patch failed for {fact_id}: {e}")
    _stamp_manual_scope(fact_id, "Fact", user_id)
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

    **A shared scope node belongs to its owner, not to the caller.** The
    single-owner ``{userId: $userId}`` test therefore answered 404 for every
    project somebody had shared: the grantee saw the row in the client list,
    clicked it, and got "could not load items" — the panel looked broken and
    the grant looked honoured. The owner is resolved first, over an id set the
    caller already holds a grant from, so a scope node belonging to anybody
    else is never read at all. When that owner is somebody else the rows are
    then filtered through ``_shared_records_visible`` — the same rule the
    search and list paths use — so a grant never widens to the owner's
    unrelated records.

    The filter import is function-local on purpose: ``fact_manager`` imports
    this module at module level, so a module-level import here is a cycle.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    scope_map = sharing.accessible_owner_scope(user_id)
    owners = (scope_map or {}).get("owners") or {}
    owner_ids = [user_id] + sorted(owners)
    target_key = "clients" if params["label"] == ":Client" else "contexts"

    with neo4j_driver.session() as s:
        exists = s.run(
            f"MATCH (scope {params['label']} {{id: $scopeId, userId: $userId}}) RETURN count(scope) AS n",
            scopeId=params["scopeId"], userId=user_id
        ).single()
        if exists and exists["n"]:
            owner_id, shared = user_id, False
        else:
            # Not the caller's own node. It may still be one shared with them;
            # the query only ever reads owners the caller already holds a grant
            # from, and the exact target must be the one granted — a context
            # grant does not carry its parent client, which is exactly what
            # audit.grant_permits refuses, so the panel must not offer it.
            # No grants at all (sharing off, or a caller holding none) short-
            # circuits: the widened lookup would read the caller's own id back
            # and find nothing, which is one wasted query per panel open on a
            # path that is supposed to be byte-identical to its pre-sharing
            # shape.
            if not owners:
                return None
            hit = s.run(
                f"MATCH (scope {params['label']} {{id: $scopeId}}) "
                "WHERE scope.userId IN $ownerIds RETURN scope.userId AS ownerId",
                scopeId=params["scopeId"], ownerIds=owner_ids,
            ).single()
            if not hit:
                return None
            owner_id = hit["ownerId"]
            if params["scopeId"] not in (owners.get(owner_id) or {}).get(target_key, {}):
                return None
            shared = True

        fact_rows = list(s.run(
            f"""
            MATCH (f:Fact {{userId: $ownerId}})-[{params['rel']}]->(scope {params['label']} {{id: $scopeId, userId: $ownerId}})
            OPTIONAL MATCH (f)-[:IN_CATEGORY]->(cat:Category)
            OPTIONAL MATCH (f)-[:FOR_CLIENT]->(cl:Client)
            OPTIONAL MATCH (f)-[:IN_CONTEXT]->(ctx:Context)
            RETURN f.id AS id, f.name AS name, f.text AS text, cat.name AS category,
                   f.timestamp AS timestamp, cl.id AS clientId, cl.name AS clientName,
                   ctx.id AS contextId, ctx.name AS contextName
            ORDER BY coalesce(f.name, f.text) ASC
            """,
            scopeId=params["scopeId"], ownerId=owner_id
        ))
        diary_rows = list(s.run(
            f"""
            MATCH (d:DiaryEntry {{userId: $ownerId}})-[{params['rel']}]->(scope {params['label']} {{id: $scopeId, userId: $ownerId}})
            RETURN d.id AS id, d.date AS date, d.name AS name, d.timestamp AS timestamp
            ORDER BY d.date DESC, d.timestamp DESC
            """,
            scopeId=params["scopeId"], ownerId=owner_id
        ))
        # A record that only *mentions* this project — RELEVANT_TO rather than
        # IN_CONTEXT — is linked to it too, and the match above never returned
        # it. That is the record a grant is most often worth having: the scope
        # classifier writes RELEVANT_TO before it gives up on a primary client,
        # so a null-primary record's only scope evidence is this edge. A second
        # match rather than `-[:{rel}|RELEVANT_TO]->` in the first, because a
        # record that is both comes back twice and de-duplicating that in Cypher
        # means an aggregating WITH per OPTIONAL MATCH in a query that already
        # has three.
        rel_fact_rows = list(s.run(
            f"""
            MATCH (f:Fact {{userId: $ownerId}})-[:RELEVANT_TO]->(scope {params['label']} {{id: $scopeId, userId: $ownerId}})
            OPTIONAL MATCH (f)-[:IN_CATEGORY]->(cat:Category)
            OPTIONAL MATCH (f)-[:FOR_CLIENT]->(cl:Client)
            OPTIONAL MATCH (f)-[:IN_CONTEXT]->(ctx:Context)
            RETURN f.id AS id, f.name AS name, f.text AS text, cat.name AS category,
                   f.timestamp AS timestamp, cl.id AS clientId, cl.name AS clientName,
                   ctx.id AS contextId, ctx.name AS contextName
            ORDER BY coalesce(f.name, f.text) ASC
            """,
            scopeId=params["scopeId"], ownerId=owner_id
        ))
        rel_diary_rows = list(s.run(
            f"""
            MATCH (d:DiaryEntry {{userId: $ownerId}})-[:RELEVANT_TO]->(scope {params['label']} {{id: $scopeId, userId: $ownerId}})
            RETURN d.id AS id, d.date AS date, d.name AS name, d.timestamp AS timestamp
            ORDER BY d.date DESC, d.timestamp DESC
            """,
            scopeId=params["scopeId"], ownerId=owner_id
        ))
        # The primary match wins: its rows already carry the client and project
        # names, and a related-only row would render the same record twice with
        # two different scope captions.
        seen = {r["id"] for r in fact_rows}
        fact_rows += [r for r in rel_fact_rows if r["id"] not in seen]
        seen = {r["id"] for r in diary_rows}
        diary_rows += [r for r in rel_diary_rows if r["id"] not in seen]
        if shared:
            from fact_manager import _shared_records_visible  # local: import cycle
            visible = _shared_records_visible(
                s, [(r["id"], owner_id) for r in fact_rows + diary_rows], owners)
            fact_rows = [r for r in fact_rows if r["id"] in visible]
            diary_rows = [r for r in diary_rows if r["id"] in visible]
    # `readOnly` is present only on a shared panel's rows, so an own panel's
    # payload stays byte-identical to its pre-sharing shape. It is what lets
    # the UI withhold the inline ✏️ on a record the caller may read but not
    # write; the server enforces the same rule, this is the affordance half.
    shared_flag = {"readOnly": True} if shared else {}
    return {
        "facts": [
            {"id": r["id"], "name": r["name"], "text": r["text"], "category": r["category"],
             "timestamp": _ts_str(r["timestamp"]), "clientId": r["clientId"],
             "clientName": r["clientName"], "contextId": r["contextId"],
             "contextName": r["contextName"], **shared_flag}
            for r in fact_rows
        ],
        "diary": [
            {"id": r["id"], "date": r["date"], "name": r.get("name") or "Unnamed Entry",
             "timestamp": _ts_str(r["timestamp"]), **shared_flag}
            for r in diary_rows
        ],
    }


def db_client_items(client_id: str, user_id: str) -> Optional[dict]:
    """Facts + diary entries linked to a Client (None if unknown)."""
    return _scope_items(user_id, "", {"label": ":Client", "rel": ":FOR_CLIENT", "scopeId": client_id})


def db_context_items(context_id: str, user_id: str) -> Optional[dict]:
    """Facts + diary entries linked to a Context (None if unknown)."""
    return _scope_items(user_id, "", {"label": ":Context", "rel": ":IN_CONTEXT", "scopeId": context_id})


async def _drop_scope_payload(collection: str, record_ids: list, keys: list):
    """Drop scope keys from every Qdrant point belonging to these records.

    The ids arrive from Neo4j, so they are *record* ids. Passing them straight
    through as point ids would clear chunk 0 only, leaving chunks 1..N-1 holding
    a clientId/contextId whose node has just been deleted — the record would
    then keep appearing under a client that no longer exists. The boot
    diff-sync would eventually repair it, but only after a restart, so the stale
    filter is live in the meantime. Resolve the family first.
    """
    if not record_ids or not keys:
        return
    try:
        qdrant = await get_qdrant()
        if not qdrant:
            return
        point_ids = []
        for record_id in record_ids:
            point_ids.extend(await _scope_targets(qdrant, record_id, collection))
        if point_ids:
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

    The client list is cached briefly because this runs inside unscoped search,
    where it would otherwise add a Neo4j round trip to every query.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver or not text:
        return (None, None)

    now = time.time()
    cached = _SCOPE_LIST_CACHE.get(user_id)
    if cached and now - cached[0] < SCOPE_LIST_TTL_SECONDS:
        clients = cached[1]
    else:
        try:
            clients = db_list_clients(user_id)
        except Exception as e:
            logger.warning(f"[infer_scope] list_clients failed: {e}")
            return (None, None)
        _SCOPE_LIST_CACHE[user_id] = (now, clients)

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


_SCOPE_MATCH_SYSTEM = (
    "You map a caller's client and project onto names that already exist in "
    "their vault. Answer with a single JSON object.\n"
    "- Pick a client ONLY from the given list, spelled exactly as stored.\n"
    "- Pick a project ONLY from that client's own project list, spelled "
    "exactly as stored. A project listed under another client is not this "
    "client's project, so never return it.\n"
    "- Use null when nothing in the list is what the caller meant. A null is a "
    "real, correct answer; a wrong guess is not recoverable, so never fill the "
    "field to avoid leaving it empty.\n"
    "Reply with only the JSON object, no commentary."
)


def _scope_match_payload(requested: str, candidates: list) -> str:
    return json.dumps({
        "requested": requested,
        "candidates": candidates,
    })


async def _llm_pick_scope_name(requested: str, candidates: list, label: str) -> Optional[str]:
    """Ask the LLM which existing name `requested` means, or nothing.

    The model may only echo a name from `candidates`. An answer outside that
    set is discarded rather than written, because the whole point of routing
    through the model here is to pick among names that already exist — a name it
    invents is a new reference, which is what this path exists to avoid.
    """
    if not requested or not candidates:
        return None
    lowered = {name.lower(): name for name in candidates if name}
    if requested.strip().lower() in lowered:
        return lowered[requested.strip().lower()]
    try:
        raw = await get_llm_response(
            _scope_match_payload(requested, candidates),
            system=_SCOPE_MATCH_SYSTEM,
        )
    except Exception as exc:  # noqa: BLE001 - a failed guess must not become a new node
        logger.warning(f"scope match for {label} {requested!r} failed: {exc}")
        return None
    if not raw:
        logger.warning(f"scope match for {label} {requested!r} returned nothing")
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        logger.warning(f"scope match for {label} {requested!r} was not JSON: {raw[:200]!r}")
        return None
    try:
        answer = json.loads(match.group(0)).get("match")
    except ValueError:
        logger.warning(f"scope match for {label} {requested!r} was unparseable JSON")
        return None
    if not answer:
        return None
    picked = lowered.get(str(answer).strip().lower())
    if not picked:
        logger.warning(
            f"scope match for {label} {requested!r} returned {answer!r}, which "
            f"is not an existing name — treating it as no match"
        )
    return picked


async def resolve_write_scope(client: Optional[str], context: Optional[str],
                              user_id: str) -> dict:
    """Resolve the client/project a write was asked for onto existing nodes.

    Returns ``{"clientId", "clientName", "contextId", "contextName"}`` — the
    stored spellings and ids, with ``None`` for anything not asked for. Raises
    ``ValueError`` when a name was asked for and cannot be matched to something
    that already exists.

    The ladder is ``plan_search_scope`` (declared abbreviation first, then
    case-insensitive exact, then the token/containment/fuzzy evidence ladder),
    then one LLM approximation.
    **Nothing here creates a node.** A caller that handed over a near-miss name
    used to get a second ``Client`` spelled slightly differently, which is worse
    than no scope at all: both nodes then match a client filter, the counts
    disagree, and neither is the node the other queries resolve to. A duplicate
    is also unrecoverable by the reclassify — the classifier only ever picks
    from existing names, so the near-miss copy is never the one it links to and
    never gets a second look.

    The context is resolved **only against the resolved client's own projects**,
    and the LLM is given only that list. A project belonging to a different
    client is not this client's project, and pairing the two is the cross-client
    guess the classifier is already documented as making.

    A Context that matches nothing is an **error, not a new project**, which is
    the deliberate half of the rule: the vault's clients and projects are curated
    (they are created from the UI), so an unmatched name means the caller
    mistyped or named something that is not in this vault, and inventing a node
    for it would put the entry somewhere no later query will ever look.
    """
    client = (client or "").strip() or None
    context = (context or "").strip() or None
    resolved = {"clientId": None, "clientName": None,
                "contextId": None, "contextName": None}
    if not client and not context:
        return resolved

    clients = await asyncio.to_thread(db_list_clients, user_id)
    client_name, context_name, _c_ev, _x_ev = plan_search_scope(client, context, clients)

    if client and not client_name:
        client_name = await _llm_pick_scope_name(
            client, [c["name"] for c in clients if c.get("name")], "client")
    if context and not context_name and client_name:
        own = next((c for c in clients if c.get("name") == client_name), None)
        context_name = await _llm_pick_scope_name(
            context, [x["name"] for x in (own or {}).get("contexts", []) if x.get("name")],
            "context")

    if client and not client_name:
        known = ", ".join(sorted(c["name"] for c in clients if c.get("name"))) or "none"
        raise ValueError(
            f"client {client!r} does not match any existing client ({known}). "
            f"Clients are not created by this tool — create the client first, "
            f"then file the entry under it."
        )
    if context and not context_name:
        if not client_name:
            raise ValueError(
                f"context {context!r} was given with no client to match it "
                f"against. Pass client as well, so the project can be resolved."
            )
        known = ", ".join(sorted(
            x["name"] for c in clients if c.get("name") == client_name
            for x in (c.get("contexts") or []) if x.get("name"))) or "none"
        raise ValueError(
            f"context {context!r} is not a project of client {client_name!r} "
            f"({known}). Projects are not created by this tool — use an "
            f"existing project, or file the entry without a project."
        )

    if client_name:
        row = next(c for c in clients if c.get("name") == client_name)
        resolved["clientId"], resolved["clientName"] = row.get("id"), client_name
    if context_name:
        # The owning client is found by the project's presence, not by
        # `client_name`: a context given on its own resolves against *every*
        # project's list, so there may be no client to name. Knowing which
        # client owns it is what lets the entry be found by a client filter.
        owner = next(
            (c for c in clients
             if any(x.get("name") == context_name for x in (c.get("contexts") or []))),
            None)
        row = next(x for x in (owner.get("contexts") or []) if x.get("name") == context_name)
        resolved["contextId"], resolved["contextName"] = row.get("id"), context_name
        if not resolved["clientId"]:
            resolved["clientId"], resolved["clientName"] = owner.get("id"), owner.get("name")
    return resolved


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
            SET c.lastMentioned = datetime(), c.active = CASE WHEN coalesce(c.statusPinned, false) THEN c.active ELSE true END,
                f.updatedAt = datetime(), c.updatedAt = datetime()
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
            SET ctx.lastMentioned = datetime(), ctx.active = CASE WHEN coalesce(ctx.statusPinned, false) THEN ctx.active ELSE true END,
                f.updatedAt = datetime(), ctx.updatedAt = datetime()
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

    # Phase 4: write gate on the entry's current scope.
    actor = user_id
    owner = db_owner_of_target(entry_id)
    if owner is None:
        return None
    if owner != user_id:
        with neo4j_driver.session() as s:
            res = s.run(
                "MATCH (d:DiaryEntry {id: $id})-[:FOR_CLIENT]->(c:Client) RETURN c.id as cid",
                id=entry_id)
            fc = [r["cid"] for r in res if r.get("cid")]
            res = s.run(
                "MATCH (d:DiaryEntry {id: $id})-[:IN_CONTEXT]->(cx:Context) RETURN cx.id as xid",
                id=entry_id)
            fx = [r["xid"] for r in res if r.get("xid")]
            owners = (sharing.accessible_owner_scope(user_id) or {}).get("owners")
            if not (any(sharing.has_access(user_id, owner, "client", cid,
                                          level="write") for cid in fc)
                    or any(sharing.has_access(user_id, owner, "context", xid,
                                             level="write") for xid in fx)):
                raise PermissionError("write access required")
        user_id = owner

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
            "MATCH (d:DiaryEntry {id: $entryId, userId: $userId}) SET d.updatedAt = datetime() "
            "WITH d MATCH (d)-[r:FOR_CLIENT]->(:Client) DELETE r",
            entryId=entry_id, userId=user_id
        )
        s.run(
            "MATCH (d:DiaryEntry {id: $entryId, userId: $userId}) SET d.updatedAt = datetime() "
            "WITH d MATCH (d)-[r:IN_CONTEXT]->(:Context) DELETE r",
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
            # Same reasoning as db_set_fact_scope: a long entry is several points
            # and they must all carry the same scope, or the link survives on the
            # chunks that were never patched.
            family = await _scope_targets(qdrant, entry_id, DIARY_COLLECTION)
            if patch:
                await qdrant.set_payload(collection_name=DIARY_COLLECTION, payload=patch, points=family)
            drop = [k for k, present in (("clientId", client), ("clientName", client),
                                         ("contextId", ctx), ("contextName", ctx)) if not present]
            if drop:
                await qdrant.delete_payload(collection_name=DIARY_COLLECTION, keys=drop, points=family)
    except Exception as e:
        logger.warning(f"db_set_diary_scope: Qdrant payload patch failed for {entry_id}: {e}")
    _stamp_manual_scope(entry_id, "DiaryEntry", user_id)
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
            SET c.lastMentioned = datetime(), c.active = CASE WHEN coalesce(c.statusPinned, false) THEN c.active ELSE true END,
                d.updatedAt = datetime(), c.updatedAt = datetime()
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
            SET ctx.lastMentioned = datetime(), ctx.active = CASE WHEN coalesce(ctx.statusPinned, false) THEN ctx.active ELSE true END,
                d.updatedAt = datetime(), ctx.updatedAt = datetime()
            """,
            contextId=context_id, userId=user_id, entryId=entry_id
        )