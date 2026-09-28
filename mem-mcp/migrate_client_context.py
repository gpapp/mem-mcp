"""
migrate_client_context.py – One-time migration: Client-category facts → Client nodes,
Project-category facts → Context nodes. Idempotent (MERGE throughout).
"""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

from common import (
    get_neo4j, get_qdrant, get_llm_response, logger, publish_db_event,
    COLLECTION_NAME, DIARY_COLLECTION,
    SCOPE_MODEL, SCOPE_BACKFILL_ENABLED, SCOPE_BACKFILL_CONCURRENCY,
    clean_extracted_people_names,
    claim_maintenance, release_maintenance,
)
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
from client_manager import (
    db_create_client, db_create_context, db_list_clients,
    db_resolve_client, db_resolve_context,
    _resolve_client_by_id, _resolve_context_by_id,
    link_fact_to_client, link_fact_to_context,
    link_diary_to_client, link_diary_to_context,
)
from matching_utils import (
    SCOPE_EVIDENCE_EXACT,
    client_header_value,
    resolve_people_candidates,
    resolve_scope_name,
)


async def migrate_client_context():
    """Create Client/Context nodes from existing Client/Project facts and link related facts."""
    neo4j_driver = get_neo4j()
    qdrant = await get_qdrant()
    if not neo4j_driver or not qdrant:
        logger.warning("migrate_client_context: DB not available, skipping")
        return

    with neo4j_driver.session() as s:
        user_rows = list(s.run(
            "MATCH (f:Fact) RETURN DISTINCT f.userId AS userId "
            "UNION "
            "MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS userId"
        ))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        logger.info("migrate_client_context: no users found")
        return

    for user_id in user_ids:
        await _migrate_user(user_id, neo4j_driver, qdrant)


async def _migrate_user(user_id: str, neo4j_driver, qdrant):
    with neo4j_driver.session() as s:
        # Skip if already migrated (Client nodes exist for this user)
        existing = s.run(
            "MATCH (c:Client {userId: $userId}) RETURN count(c) AS n",
            userId=user_id
        ).single()
        if existing and existing["n"] > 0:
            logger.info(f"migrate_client_context [{user_id}]: already migrated, skipping")
            return

        # 1. Client-category facts → Client nodes
        client_facts = list(s.run(
            """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.category) = 'client'
            RETURN f.id AS id, f.name AS name
            """,
            userId=user_id
        ))

    client_map = {}  # fact_id -> client_id
    for cf in client_facts:
        name = cf["name"]
        if not name:
            continue
        client_id = await db_create_client(name, user_id)
        client_map[cf["id"]] = (client_id, name)
        logger.info(f"migrate_client_context [{user_id}]: Client '{name}' → {client_id}")

    # 2. WORKS_FOR → FOR_CLIENT (facts linked to a Client-category fact get FOR_CLIENT to the new Client node)
    with neo4j_driver.session() as s:
        for fact_id, (client_id, cname) in client_map.items():
            s.run(
                """
                MATCH (src:Fact {userId: $userId})-[r:WORKS_FOR]->(cf:Fact {id: $factId, userId: $userId})
                MERGE (c:Client {id: $clientId, userId: $userId})
                MERGE (src)-[:FOR_CLIENT]->(c)
                SET c.lastMentioned = datetime(), c.active = true
                """,
                userId=user_id, factId=fact_id, clientId=client_id
            )
            # Link the Client-category fact itself to its own Client node
            s.run(
                """
                MATCH (cf:Fact {id: $factId, userId: $userId})
                MERGE (c:Client {id: $clientId, userId: $userId})
                MERGE (cf)-[:FOR_CLIENT]->(c)
                """,
                factId=fact_id, clientId=client_id, userId=user_id
            )

    # 3. Project-category facts → Context nodes (linked to associated Client when detectable)
    with neo4j_driver.session() as s:
        project_facts = list(s.run(
            """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.category) IN ['project', 'projects']
            OPTIONAL MATCH (f)-[:WORKS_FOR]->(cf:Fact)
            WHERE toLower(cf.category) = 'client'
            RETURN f.id AS id, f.name AS name, cf.id AS clientFactId
            """,
            userId=user_id
        ))

    for pf in project_facts:
        pname = pf["name"]
        if not pname:
            continue
        client_fact_id = pf["clientFactId"]
        if client_fact_id and client_fact_id in client_map:
            client_id = client_map[client_fact_id][0]
        else:
            # No associated client — skip context creation (contexts must belong to a client)
            logger.info(f"migrate_client_context [{user_id}]: Project '{pname}' has no client, skipping")
            continue
        context_id = await db_create_context(pname, client_id, user_id)
        with neo4j_driver.session() as s:
            s.run(
                """
                MATCH (pf:Fact {id: $factId, userId: $userId})
                MERGE (ctx:Context {id: $contextId, userId: $userId})
                MERGE (pf)-[:IN_CONTEXT]->(ctx)
                SET ctx.lastMentioned = datetime(), ctx.active = true
                """,
                factId=pf["id"], contextId=context_id, userId=user_id
            )
        logger.info(f"migrate_client_context [{user_id}]: Context '{pname}' → {context_id}")

    # 4. Diary MENTIONS to Client-category facts → FOR_CLIENT on the diary entry
    #    Only for entries that have no FOR_CLIENT link yet — existing manual/LLM
    #    assignments must not be overwritten.
    #    crossClient nodes (e.g. "Colleagues") are excluded: they span multiple real
    #    clients and should never drive primary diary scoping.
    with neo4j_driver.session() as s:
        for fact_id, (client_id, cname) in client_map.items():
            s.run(
                """
                MATCH (d:DiaryEntry {userId: $userId})-[:MENTIONS]->(cf:Fact {id: $factId, userId: $userId})
                MATCH (c:Client {id: $clientId, userId: $userId})
                WHERE NOT (d)-[:FOR_CLIENT]->(:Client)
                  AND NOT coalesce(c.crossClient, false) = true
                MERGE (d)-[:FOR_CLIENT]->(c)
                SET c.lastMentioned = datetime()
                """,
                userId=user_id, factId=fact_id, clientId=client_id
            )

    logger.info(f"migrate_client_context [{user_id}]: done ({len(client_map)} clients)")


async def _backfill_qdrant(user_id: str, neo4j_driver, qdrant):
    """Sync FOR_CLIENT / IN_CONTEXT links into Qdrant payloads for fast filtering.

    Diff-based: scrolls current payloads (no vectors) and upserts only points
    whose scope keys actually differ — including stripping stale keys off
    unlinked items. Steady-state boots therefore issue zero write requests,
    while any drift source (UI reassignment, re-saves, restores) self-heals.
    Vectors are preserved (no re-embed): retrieved only for changed points.
    """
    with neo4j_driver.session() as s:
        fact_rows = list(s.run(
            """
            MATCH (f:Fact {userId: $userId})
            OPTIONAL MATCH (f)-[:FOR_CLIENT]->(c:Client)
            OPTIONAL MATCH (f)-[:IN_CONTEXT]->(ctx:Context)
            RETURN f.id AS id, c.id AS clientId, c.name AS clientName,
                   ctx.id AS contextId, ctx.name AS contextName
            """,
            userId=user_id
        ))
    with neo4j_driver.session() as s:
        diary_rows = list(s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            OPTIONAL MATCH (d)-[:FOR_CLIENT]->(c:Client)
            OPTIONAL MATCH (d)-[:IN_CONTEXT]->(ctx:Context)
            RETURN d.id AS id, c.id AS clientId, c.name AS clientName,
                   ctx.id AS contextId, ctx.name AS contextName
            """,
            userId=user_id
        ))

    def _desired(rows) -> dict:
        out = {}
        for r in rows:
            scope = {}
            if r["clientId"]:
                scope["clientId"] = r["clientId"]
                scope["clientName"] = r["clientName"]
            if r["contextId"]:
                scope["contextId"] = r["contextId"]
                scope["contextName"] = r["contextName"]
            out[r["id"]] = scope
        return out

    synced = 0
    for collection, desired in ((COLLECTION_NAME, _desired(fact_rows)),
                                (DIARY_COLLECTION, _desired(diary_rows))):
        current: dict = {}
        offset = None
        while True:
            points, next_offset = await qdrant.scroll(
                collection_name=collection,
                limit=1000,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
            )
            for p in points:
                current[str(p.id)] = dict(p.payload or {})
            if next_offset is None:
                break
            offset = next_offset
        changed = [
            pid for pid, payload in current.items()
            if {k: payload.get(k) for k in _SCOPE_PAYLOAD_KEYS if payload.get(k) is not None}
            != desired.get(pid, {})
        ]
        for pid in changed:
            try:
                # Preserve existing vector; skip points with none (sync_orphans re-embeds those).
                with_vec = await qdrant.retrieve(collection_name=collection, ids=[pid], with_vectors=True)
                vec = with_vec[0].vector if with_vec else None
                if vec is None:
                    continue
                merged = dict(current[pid])
                for k in _SCOPE_PAYLOAD_KEYS:
                    merged.pop(k, None)
                merged.update(desired.get(pid, {}))
                await qdrant.upsert(
                    collection_name=collection,
                    points=[PointStruct(id=pid, vector=vec, payload=merged)],
                )
                synced += 1
            except Exception as e:
                logger.warning(f"migrate_client_context [{user_id}]: Qdrant scope sync failed for {pid}: {e}")
    if synced:
        logger.info(f"scope_qdrant_sync [{user_id}]: updated {synced} payloads")


async def sync_qdrant_scope():
    """Push Neo4j FOR_CLIENT/IN_CONTEXT links into Qdrant payloads for all users.

    Called every boot (after migrate_client_context) so that manual scope
    assignments made via the UI are reliably reflected in Qdrant before
    restore_scope_links reads it back.  Diff-based — steady-state is a no-op.
    """
    neo4j_driver = get_neo4j()
    qdrant = await get_qdrant()
    if not neo4j_driver or not qdrant:
        logger.warning("sync_qdrant_scope: DB not available, skipping")
        return

    with neo4j_driver.session() as s:
        user_rows = list(s.run(
            "MATCH (c:Client) RETURN DISTINCT c.userId AS userId"
        ))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    for user_id in user_ids:
        await _backfill_qdrant(user_id, neo4j_driver, qdrant)


# ---------------------------------------------------------------------------
# LLM scope backfill: classify every unlinked fact / diary entry against the
# user's existing Client/Context list via Ollama and link the matches.
# Only items WITHOUT a FOR_CLIENT link are processed, so each boot only
# handles newly added (or never classified) items — subsequent boots are cheap.
# The model may ONLY pick from existing clients/contexts (or null); it can
# never invent new ones.
# ---------------------------------------------------------------------------
_SCOPE_SYSTEM = (
    "You are a scope classifier. Given a memory item and a list of known clients "
    "(each with its contexts), decide which single client the item belongs to, "
    "and optionally which context within that client. "
    "Return ONLY a JSON object: {\"client\": \"<exact client name or null>\", "
    "\"context\": \"<exact context name or null>\"}. "
    "Rules: use exact names from the list, never invent names; "
    "context must belong to the chosen client; "
    "return nulls when the item is generic/shared knowledge or matches no client. "
    "IMPORTANT: if the MENTIONS section lists facts that are already scoped to a specific client, "
    "strongly prefer that client — it is the strongest signal available. "
    "IMPORTANT: if the RELATED section lists facts or people already scoped to a specific client "
    "(shown as [client: X] tags), strongly prefer that client — it is a strong signal. "
    "IMPORTANT: if the item content contains an explicit '**Client:**' or 'Client:' header, "
    "that declaration is authoritative — use it and do not override it with content keywords. "
    "IMPORTANT: context (project) selection must be conservative — only assign a context when "
    "the item content explicitly and directly relates to that specific project. "
    "Do NOT assign a context simply because it is the only one available for the chosen client; "
    "if the item is about the client in general or could belong to any of their projects, "
    "return null for context."
)


# ---------------------------------------------------------------------------
# Fast (no-LLM) scope resolution for diary entries.
# Returns (client_name, context_name) or (None, None) if no hard signal found.
# ---------------------------------------------------------------------------
# Tolerates list bullets, blockquote/heading markers and bold markers before the
# label, and stops the captured value at a trailing separator so a line such as
# "- **Client:** Acme - Q3 review" yields "Acme" rather than the whole sentence.
def _link_diary_relevant_to(diary_id: str, client_ids: list, user_id: str, neo4j_driver) -> None:
    """Create RELEVANT_TO edges from a DiaryEntry to each Client in client_ids.

    These are secondary associations: the diary entry is not *scoped to* these
    clients (no FOR_CLIENT link) but mentions their facts/people prominently.
    """
    if not client_ids or neo4j_driver is None:
        return
    with neo4j_driver.session() as s:
        for cid in client_ids:
            s.run(
                """
                MATCH (d:DiaryEntry {id: $did, userId: $userId})
                MATCH (c:Client {id: $clientId, userId: $userId})
                MERGE (d)-[:RELEVANT_TO]->(c)
                """,
                did=diary_id, userId=user_id, clientId=cid
            )


def _fast_diary_scope(item: dict, clients: list, neo4j_driver, user_id: str) -> tuple:
    """Return (client_name, None) when a hard deterministic signal is found.

    Three signals checked in priority order:
    1. All MENTIONS-linked facts that have a non-crossClient client agree on ONE client.
       If they disagree (multiple distinct clients) → entry stays unscoped but RELEVANT_TO
       edges are written for each mentioned client so diary filtering can still surface it.
    2. The diary content contains an explicit '**Client:** <name>' header line.

    Both signals resolve through ``resolve_scope_name`` rather than an exact
    string compare, so a header written as "Client: Deutsche Bank" still binds to
    the stored "Deutsche Bank (DB)".

    Returns (None, None) when no hard signal is present — caller falls back to LLM.
    """
    # Signal 1: unanimous MENTIONS client (excluding crossClient facts)
    if neo4j_driver is not None:
        try:
            with neo4j_driver.session() as s:
                rows = list(s.run(
                    """
                    MATCH (d:DiaryEntry {userId: $userId, id: $did})-[:MENTIONS]->(f:Fact)
                    MATCH (f)-[:FOR_CLIENT]->(cl:Client)
                    WHERE NOT coalesce(cl.crossClient, false) = true
                    RETURN DISTINCT cl.id AS clientId, cl.name AS clientName
                    """,
                    userId=user_id, did=item["id"]
                ))
            scoped = [(r["clientId"], r["clientName"]) for r in rows if r["clientName"]]
            unique_ids = {cid for cid, _ in scoped}
            if len(unique_ids) == 1:
                cid, cname = scoped[0]
                matched, _ = resolve_scope_name(cname, [c["name"] for c in clients])
                if matched:
                    logger.debug(f"[scope_fast] diary {item.get('id')}: unanimous MENTIONS → {matched}")
                    return matched, None
            elif len(unique_ids) > 1:
                # Multi-client: leave unscoped but write RELEVANT_TO for each client.
                # The query already returned the ids, so match on those rather than
                # re-deriving them from the name.
                matched_ids = list(dict.fromkeys(cid for cid, _ in scoped))
                _link_diary_relevant_to(item["id"], matched_ids, user_id, neo4j_driver)
                logger.debug(
                    f"[scope_fast] diary {item.get('id')}: multi-client MENTIONS "
                    f"({len(unique_ids)} clients) → unscoped, RELEVANT_TO written"
                )
                return None, None
        except Exception as exc:
            logger.debug(f"[scope_fast] MENTIONS lookup failed for {item.get('id')}: {exc}")

    # Signal 2: explicit **Client:** header in content
    raw_header = client_header_value(item.get("content", "") or "")
    if raw_header:
        matched, _ = resolve_scope_name(raw_header, [c["name"] for c in clients])
        if matched:
            logger.debug(f"[scope_fast] diary {item.get('id')}: content header {raw_header!r} → {matched}")
            return matched, None
        logger.debug(
            f"[scope_fast] diary {item.get('id')}: header {raw_header!r} matched no known client"
        )

    return None, None


async def _classify_scope(item_text: str, clients: list) -> tuple:
    """Ask Ollama which existing client/context an item belongs to.

    Returns ``(client_name|None, context_name|None, ok)``. ``ok`` is False only
    when the LLM call or the JSON parse failed, which is deliberately distinct
    from a successful "this item is generic" answer. Callers use that to decide
    whether the item may be stamped as checked: a transient Ollama timeout must
    not permanently mark an item as unclassifiable, because the stamp is what
    makes the boot-time backfill skip it forever.

    Names are resolved through ``resolve_scope_name`` against the known list, so
    an answer that differs from the stored spelling only by abbreviation or a
    dropped "(DB)" qualifier still binds.
    """
    scope_lines = []
    for c in clients:
        ctxs = ", ".join(x["name"] for x in c.get("contexts", [])) or "(no contexts)"
        scope_lines.append(f"- {c['name']} [contexts: {ctxs}]")
    scope_block = "\n".join(scope_lines)

    text = item_text if len(item_text) <= 1500 else item_text[:1500] + "…"
    prompt = f"KNOWN CLIENTS:\n{scope_block}\n\nITEM:\n{text}"

    try:
        raw = await get_llm_response(prompt, system=_SCOPE_SYSTEM, model=SCOPE_MODEL, num_predict=80)
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if not m:
            logger.warning("[scope_backfill] classifier returned no JSON object")
            return None, None, False
        data = json.loads(m.group())
        client_name = (data.get("client") or "").strip() or None
        context_name = (data.get("context") or "").strip() or None

        if not client_name:
            return None, None, True

        # Resolve against known names; reject inventions.
        matched_client, client_evidence = resolve_scope_name(
            client_name, [c["name"] for c in clients]
        )
        if not matched_client:
            logger.warning(
                f"[scope_backfill] classifier answered {client_name!r}, which matches no known "
                f"client — treating the item as generic"
            )
            return None, None, True
        if client_evidence != SCOPE_EVIDENCE_EXACT:
            logger.info(
                f"[scope_backfill] resolved {client_name!r} to {matched_client!r} via {client_evidence}"
            )

        resolved_context = None
        if context_name:
            # Look the resolved name back up on the client record: the resolver
            # returns a name, not the node, and the context list hangs off it.
            client = next((c for c in clients if c["name"] == matched_client), None)
            if client:
                resolved_context, _ = resolve_scope_name(
                    context_name, [x["name"] for x in client.get("contexts", [])]
                )
        return matched_client, resolved_context, True
    except Exception as exc:
        logger.warning(f"[scope_backfill] classification failed: {type(exc).__name__}: {exc}")
        return None, None, False


# ---------------------------------------------------------------------------
# Enriched classification input: the classifier sees the item's own text plus
# its graph neighborhood — category + linked facts/diary snippets for facts,
# keywords + MENTIONS-linked fact names for diary entries. Neighbor fetch
# failures degrade gracefully to the plain item text.
# ---------------------------------------------------------------------------
_NEIGHBOR_LIMIT = 6
_SNIPPET_CHARS = 200


def _snippet(text: str, limit: int = _SNIPPET_CHARS) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _capture_scope_snapshot(user_id: str, neo4j_driver) -> dict:
    """Read every current FOR_CLIENT assignment for a user into memory.

    Full reclassification clears all scope links before classifying. Without a
    snapshot the classifier's RELATED / MENTIONS sections progressively lose
    their ``[client: X]`` tags as the job works through the list, so items
    classified late in the run are materially worse than items classified first
    — the symptom that reads as "reclassification does not match". Capturing the
    tags up front and feeding them to the enrichers keeps the evidence constant
    for the whole run.
    """
    snapshot = {}
    if neo4j_driver is None:
        return snapshot
    try:
        with neo4j_driver.session() as s:
            for row in s.run(
                """
                MATCH (n) WHERE n.userId = $userId AND (n:Fact OR n:DiaryEntry)
                MATCH (n)-[:FOR_CLIENT]->(cl:Client)
                WHERE NOT coalesce(cl.crossClient, false) = true
                RETURN n.id AS id, cl.name AS clientName
                """,
                userId=user_id,
            ):
                if row["id"] and row["clientName"]:
                    snapshot[row["id"]] = row["clientName"]
    except Exception as exc:
        logger.debug(f"[scope_backfill] scope snapshot failed for {user_id}: {exc}")
    return snapshot


def _enriched_fact_text(item: dict, neo4j_driver, user_id: str, scope_snapshot=None) -> str:
    parts = []
    if item.get("name"):
        parts.append(item["name"])
    parts.append(item.get("text", ""))
    if item.get("category"):
        parts.append(f"[category: {item['category']}]")
    if neo4j_driver is not None:
        try:
            with neo4j_driver.session() as s:
                rows = list(s.run(
                    """
                    MATCH (f:Fact {userId: $userId, id: $fid})-[r]-(n)
                    WHERE (n:Fact OR n:People) AND n.userId = $userId
                    OPTIONAL MATCH (n)-[:FOR_CLIENT]->(cl:Client {userId: $userId})
                    RETURN DISTINCT type(r) AS rel, n.id AS nid, n.name AS name,
                           coalesce(n.text, n.content, '') AS body,
                           cl.name AS clientName, coalesce(cl.crossClient, false) AS crossClient
                    LIMIT 6
                    """,
                    userId=user_id, fid=item["id"]
                ))
            if rows:
                rel_lines = []
                for r in rows:
                    line = f"- [{r['rel']}] {r['name'] or 'Unnamed'}: {_snippet(r['body'])}"
                    # Prefer the pre-cleared snapshot so reclassification always
                    # sees the scope the graph had before this run started.
                    client_name = (scope_snapshot or {}).get(r["nid"]) or r["clientName"]
                    if client_name and not r["crossClient"]:
                        line += f" [client: {client_name}]"
                    rel_lines.append(line)
                parts.append("RELATED:\n" + "\n".join(rel_lines))
        except Exception as exc:
            logger.debug(f"[scope_backfill] neighbor fetch failed for {item.get('id')}: {exc}")
    return "\n".join(p for p in parts if p)


def _enriched_diary_text(item: dict, neo4j_driver, user_id: str, scope_snapshot=None) -> str:
    parts = []
    if item.get("name"):
        parts.append(item["name"])
    parts.append(item.get("content", ""))
    kws = item.get("keywords") or []
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",") if k.strip()]
    if kws:
        parts.append(f"[keywords: {', '.join(kws[:10])}]")
    if neo4j_driver is not None:
        try:
            with neo4j_driver.session() as s:
                rows = list(s.run(
                    """
                    MATCH (d:DiaryEntry {userId: $userId, id: $did})-[:MENTIONS]->(f:Fact)
                    WHERE f.userId = $userId
                    OPTIONAL MATCH (f)-[:FOR_CLIENT]->(cl:Client {userId: $userId})
                    RETURN DISTINCT f.id AS fid, f.name AS name, f.text AS body,
                           cl.name AS clientName, coalesce(cl.crossClient, false) AS crossClient
                    LIMIT 6
                    """,
                    userId=user_id, did=item["id"]
                ))
            if rows:
                m_lines = []
                for r in rows:
                    line = f"- {r['name'] or 'Unnamed'}: {_snippet(r['body'])}"
                    # Prefer the pre-cleared snapshot — see _capture_scope_snapshot.
                    client_name = (scope_snapshot or {}).get(r["fid"]) or r["clientName"]
                    if client_name and not r["crossClient"]:
                        line += f" [client: {client_name}]"
                    m_lines.append(line)
                parts.append("MENTIONS:\n" + "\n".join(m_lines))
        except Exception as exc:
            logger.debug(f"[scope_backfill] mentions fetch failed for {item.get('id')}: {exc}")
    return "\n".join(p for p in parts if p)


def _scope_signature(clients: list) -> str:
    """Fingerprint of the client/context set — null verdicts are stamped with this.

    Boot skips items already checked against the current signature, so permanently
    generic items are classified once, but any client/context add/rename triggers
    a retry. Full reclassification clears stamps, retrying everything.
    """
    parts = []
    for c in sorted(clients, key=lambda x: x.get("name", "")):
        ctxs = sorted(x.get("name", "") for x in c.get("contexts", []))
        parts.append(c.get("name", "") + "|" + ",".join(ctxs))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _stamp_scope_checked(node_id: str, label: str, user_id: str, neo4j_driver, scope_sig) -> None:
    """Record a null verdict so boot won't reclassify this item until clients change."""
    if neo4j_driver is None or not scope_sig:
        return
    try:
        with neo4j_driver.session() as s:
            s.run(
                f"MATCH (n:{label} {{id: $id, userId: $userId}}) SET n.scopeCheckedSig = $sig",
                id=node_id, userId=user_id, sig=scope_sig,
            )
    except Exception as exc:
        logger.debug(f"[scope_backfill] scope-stamp failed for {node_id}: {exc}")


async def _classify_and_link_fact(item: dict, clients: list, user_id: str, sem: asyncio.Semaphore,
                                  neo4j_driver=None, scope_sig=None, scope_snapshot=None) -> bool:
    """Classify one fact and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    async with sem:
        body = _enriched_fact_text(item, neo4j_driver, user_id, scope_snapshot)
        client_name, context_name, ok = await _classify_scope(body, clients)
    if not client_name:
        # Only a *successful* "this item is generic" verdict may be stamped. A
        # failed call leaves the item unstamped so the next boot retries it.
        if ok:
            _stamp_scope_checked(item["id"], "Fact", user_id, neo4j_driver, scope_sig)
        return False
    c = db_resolve_client(client_name, user_id)
    if not c:
        return False
    await link_fact_to_client(item["id"], c["id"], user_id)
    if context_name:
        cx = db_resolve_context(context_name, c["id"], user_id)
        if cx:
            await link_fact_to_context(item["id"], cx["id"], user_id)
    return True


# ---------------------------------------------------------------------------
# People extraction helper: extract person names from diary content and
# link matching People facts via MENTIONS before scope classification.
# ---------------------------------------------------------------------------
_PEOPLE_SYSTEM = (
    "You are a named-entity extractor. Extract the full names of every person "
    "explicitly mentioned in the text. "
    "Return ONLY a JSON array of strings, e.g. [\"Alice Smith\", \"Bob Jones\"]. "
    "Return [] if no people are mentioned. Never add explanations. "
    "Ignore speaker labels such as SPEAKER1, SPEAKER 2, and SPEAKER#3; they are not names. "
    "IMPORTANT: ignore anything in parentheses — it is a role or description, not part of the name. "
    "For example, 'Alice Smith (host)' → extract only 'Alice Smith'."
)


async def _extract_people_names(content: str) -> list[str]:
    """Return a list of person name strings mentioned in diary content.

    Uses the scope LLM with a tight token budget. Returns [] on any failure.
    """
    snippet = content[:2000] if len(content) > 2000 else content
    logger.debug(f"[people_extract] started (content_len={len(content)}, model={SCOPE_MODEL})")
    try:
        raw = await get_llm_response(snippet, system=_PEOPLE_SYSTEM,
                                     model=SCOPE_MODEL, num_predict=200)
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            logger.debug("[people_extract] model response contained no JSON array")
            return []
        names = json.loads(m.group())
        if isinstance(names, list):
            extracted = clean_extracted_people_names(names)
            logger.debug(f"[people_extract] completed (count={len(extracted)})")
            return extracted
        logger.debug("[people_extract] model response JSON was not a list")
        return []
    except Exception as exc:
        logger.debug(f"[people_extract] failed: {exc}")
        return []


def _existing_auto_people(diary_id: str, user_id: str, neo4j_driver) -> list:
    """People facts already auto-linked to this entry, as minimal candidate dicts.

    Seeding the candidate set with these keeps reclassification non-destructive.
    ``db_find_people_matches`` is deliberately strict, so a re-run can fail to
    re-find a link that is in fact correct. The resolver then sees the full
    picture and may keep or drop each one, instead of the entry silently losing
    every auto link it had.
    """
    if neo4j_driver is None:
        return []
    try:
        with neo4j_driver.session() as s:
            rows = list(s.run(
                """
                MATCH (d:DiaryEntry {id: $did, userId: $userId})-[r:MENTIONS]->(f:Fact)
                WHERE coalesce(r.source, 'manual') = 'auto'
                RETURN f.id AS id, f.name AS name, f.text AS text
                """,
                did=diary_id, userId=user_id,
            ))
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug(f"[people_extract] existing-link read failed for {diary_id}: {exc}")
        return []


async def _link_missing_people(diary_id: str, names: list[str], user_id: str,
                               neo4j_driver, content: str = "") -> int:
    """Reconcile automatically detected People links for a diary entry."""
    if neo4j_driver is None:
        return 0
    from fact_manager import db_find_people_matches

    people_matches = await db_find_people_matches(names, user_id)
    # Fold in links that already exist so they are adjudicated, not dropped.
    found_ids = {p.get("id") for p in people_matches}
    for existing in _existing_auto_people(diary_id, user_id, neo4j_driver):
        if existing.get("id") and existing["id"] not in found_ids:
            people_matches.append(existing)
            found_ids.add(existing["id"])
    people_matches = await resolve_people_candidates(
        names, content, people_matches, get_llm_response
    )
    person_ids = [person["id"] for person in people_matches]
    created = 0
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (d:DiaryEntry {id: $did, userId: $userId})-[r:MENTIONS]->(f:Fact)
            WHERE coalesce(r.source, 'manual') = 'auto'
              AND NOT f.id IN $personIds
            DELETE r
            """,
            did=diary_id, userId=user_id, personIds=person_ids,
        )
        for person in people_matches:
            row = s.run(
                """
                MATCH (d:DiaryEntry {id: $did, userId: $userId})
                MATCH (f:Fact {id: $fid, userId: $userId})
                WHERE NOT (d)-[:MENTIONS]->(f)
                MERGE (d)-[r:MENTIONS]->(f)
                SET r.source = 'auto'
                RETURN count(f) AS n
                """,
                did=diary_id, userId=user_id, fid=person["id"],
            ).single()
            if row:
                created += row["n"]
    if created:
        logger.debug(f"[people_extract] diary {diary_id}: linked {created} new People fact(s)")
    return created


def _diary_has_mentions(diary_id: str, user_id: str, neo4j_driver) -> bool:
    """Return True if the diary entry already has at least one MENTIONS edge."""
    if neo4j_driver is None:
        return False
    try:
        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (d:DiaryEntry {id: $did, userId: $userId})-[:MENTIONS]->() "
                "RETURN count(*) AS n LIMIT 1",
                did=diary_id, userId=user_id
            ).single()
            return bool(row and row["n"] > 0)
    except Exception:
        return False


async def _classify_and_link_diary(item: dict, clients: list, user_id: str, sem: asyncio.Semaphore,
                                   neo4j_driver=None, scope_sig=None, scope_snapshot=None) -> bool:
    """Classify one diary entry and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    # Reconcile People links even when the entry already has mentions; edits can
    # remove names or replace one person with another.
    async with sem:
        names = await _extract_people_names(item.get("content", "") or "")
    await _link_missing_people(
        item["id"], names, user_id, neo4j_driver, item.get("content", "") or ""
    )

    # Fast path: unanimous MENTIONS client or explicit **Client:** header — no LLM needed.
    client_name, context_name = _fast_diary_scope(item, clients, neo4j_driver, user_id)
    if not client_name:
        async with sem:
            body = _enriched_diary_text(item, neo4j_driver, user_id, scope_snapshot)
            client_name, context_name, ok = await _classify_scope(body, clients)
    else:
        ok = True
    if not client_name:
        # See _classify_and_link_fact: never stamp a transient failure.
        if ok:
            _stamp_scope_checked(item["id"], "DiaryEntry", user_id, neo4j_driver, scope_sig)
        return False
    c = db_resolve_client(client_name, user_id)
    if not c:
        return False
    await link_diary_to_client(item["id"], c["id"], user_id)
    if context_name:
        cx = db_resolve_context(context_name, c["id"], user_id)
        if cx:
            await link_diary_to_context(item["id"], cx["id"], user_id)
    return True


async def llm_backfill_scope():
    """LLM pass over all unlinked facts + diary entries, linking them to existing clients.

    Self-limiting: only items without a FOR_CLIENT link are classified, so after
    the first full pass each boot only handles new items. Disable entirely with
    MEM_SCOPE_BACKFILL=0.
    """
    if not SCOPE_BACKFILL_ENABLED:
        logger.info("scope_backfill: disabled via MEM_SCOPE_BACKFILL=0, skipping")
        return

    neo4j_driver = get_neo4j()
    qdrant = await get_qdrant()
    if not neo4j_driver or not qdrant:
        logger.warning("scope_backfill: DB not available, skipping")
        return

    with neo4j_driver.session() as s:
        user_rows = list(s.run(
            "MATCH (f:Fact) RETURN DISTINCT f.userId AS userId "
            "UNION "
            "MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS userId"
        ))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        return

    sem = asyncio.Semaphore(max(1, SCOPE_BACKFILL_CONCURRENCY))
    for user_id in user_ids:
        clients = db_list_clients(user_id)
        if not clients:
            logger.info(f"scope_backfill [{user_id}]: no clients, skipping")
            continue
        sig = _scope_signature(clients)

        with neo4j_driver.session() as s:
            facts = list(s.run(
                """
                MATCH (f:Fact {userId: $userId})
                WHERE NOT (f)-[:FOR_CLIENT]->(:Client)
                  AND toLower(f.category) <> 'client'
                  AND (f.scopeCheckedSig IS NULL OR f.scopeCheckedSig <> $sig)
                RETURN f.id AS id, f.name AS name, f.text AS text, f.category AS category
                """,
                userId=user_id, sig=sig
            ))
            diaries = list(s.run(
                """
                MATCH (d:DiaryEntry {userId: $userId})
                WHERE NOT (d)-[:FOR_CLIENT]->(:Client)
                  AND (d.scopeCheckedSig IS NULL OR d.scopeCheckedSig <> $sig)
                RETURN d.id AS id, d.name AS name, d.content AS content, d.keywords AS keywords
                """,
                userId=user_id, sig=sig
            ))

        if not facts and not diaries:
            logger.info(f"scope_backfill [{user_id}]: nothing unlinked, skipping")
            continue

        logger.info(
            f"scope_backfill [{user_id}]: classifying {len(facts)} facts + "
            f"{len(diaries)} diary entries with {SCOPE_MODEL}"
        )
        linked = 0
        total = len(facts) + len(diaries)
        done = 0

        # Backfill only touches already-unlinked items, so the snapshot is mostly
        # redundant here — but it keeps the neighbour evidence identical whether
        # the pass runs at boot or from the UI, and it costs one query.
        scope_snapshot = await asyncio.to_thread(_capture_scope_snapshot, user_id, neo4j_driver)

        async def _track(coro):
            nonlocal linked, done
            ok = await coro
            done += 1
            if ok:
                linked += 1
            if done % 25 == 0 or done == total:
                logger.info(f"scope_backfill [{user_id}]: {done}/{total} classified, {linked} linked")
            return ok

        await asyncio.gather(*[
            _track(_classify_and_link_fact(dict(r), clients, user_id, sem, neo4j_driver, sig, scope_snapshot))
            for r in facts
        ])
        await asyncio.gather(*[
            _track(_classify_and_link_diary(dict(r), clients, user_id, sem, neo4j_driver, sig, scope_snapshot))
            for r in diaries
        ])

        # Push the new links into Qdrant payloads for filtered search.
        await _backfill_qdrant(user_id, neo4j_driver, qdrant)
        logger.info(f"scope_backfill [{user_id}]: done, {linked}/{total} linked to clients")


async def restore_scope_links():
    """Re-create Neo4j FOR_CLIENT / IN_CONTEXT links from Qdrant payloads.

    Repairs scope links if a janitor query ever deletes them (MERGE = idempotent).
    Runs before llm_backfill_scope so the LLM pass finds nothing to re-classify.
    """
    neo4j_driver = get_neo4j()
    qdrant = await get_qdrant()
    if not neo4j_driver or not qdrant:
        logger.warning("restore_scope_links: DB not available, skipping")
        return

    with neo4j_driver.session() as s:
        user_rows = list(s.run("MATCH (c:Client) RETURN DISTINCT c.userId AS userId"))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        return

    for user_id in user_ids:
        restored = 0
        for collection, is_diary in ((COLLECTION_NAME, False), (DIARY_COLLECTION, True)):
            offset = None
            while True:
                points, next_offset = await qdrant.scroll(
                    collection_name=collection,
                    limit=1000,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                    scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
                )
                for p in points:
                    payload = p.payload or {}
                    client_id = payload.get("clientId")
                    if not client_id:
                        continue
                    if _resolve_client_by_id(client_id, user_id) is None:
                        continue
                    node_id = str(p.id)
                    if is_diary:
                        await link_diary_to_client(node_id, client_id, user_id)
                    else:
                        await link_fact_to_client(node_id, client_id, user_id)
                    context_id = payload.get("contextId")
                    if context_id and _resolve_context_by_id(context_id, user_id) is not None:
                        if is_diary:
                            await link_diary_to_context(node_id, context_id, user_id)
                        else:
                            await link_fact_to_context(node_id, context_id, user_id)
                    restored += 1
                if next_offset is None:
                    break
                offset = next_offset
        if restored:
            logger.info(f"restore_scope_links [{user_id}]: restored {restored} scope links from Qdrant payloads")


_SCOPE_PROP_KEYS = ("clientId", "clientName", "contextId", "contextName")


async def strip_scope_properties():
    """Remove legacy scope keys from node properties (links are the source of truth).

    Scope used to be surfaced inside `metadata`; any copies baked onto Fact nodes
    (or into DiaryEntry metadata JSON, e.g. via the metadata editor) are stripped
    here. Links, Qdrant payloads and the scopeCheckedSig boot stamp are untouched —
    run before restore_scope_links().
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        logger.warning("strip_scope_properties: DB not available, skipping")
        return
    stripped_facts = 0
    stripped_diary = 0
    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (f:Fact)
            WHERE f.clientId IS NOT NULL OR f.clientName IS NOT NULL
               OR f.contextId IS NOT NULL OR f.contextName IS NOT NULL
            REMOVE f.clientId, f.clientName, f.contextId, f.contextName
            RETURN count(*) as n
            """
        ).single()
        stripped_facts = res["n"] if res else 0
        # DiaryEntry metadata is a JSON string — prune scope keys inside it.
        rows = list(s.run(
            "MATCH (d:DiaryEntry) WHERE d.metadata IS NOT NULL RETURN d.id AS id, d.metadata AS metadata"
        ))
    for row in rows:
        raw = row["metadata"]
        try:
            meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            continue
        if not isinstance(meta, dict) or not any(k in meta for k in _SCOPE_PROP_KEYS):
            continue
        pruned = {k: v for k, v in meta.items() if k not in _SCOPE_PROP_KEYS}
        with neo4j_driver.session() as s:
            s.run(
                "MATCH (d:DiaryEntry {id: $id}) SET d.metadata = $metadata",
                id=row["id"], metadata=json.dumps(pruned),
            )
        stripped_diary += 1
    if stripped_facts or stripped_diary:
        logger.info(f"strip_scope_properties: stripped scope props from {stripped_facts} facts, {stripped_diary} diary entries")


# ---------------------------------------------------------------------------
# UI-triggered full reclassification: re-run the (enriched) Ollama classifier
# over EVERY fact + diary entry, replacing existing scope links. Runs as a
# background asyncio task on the server loop (non-blocking for GUI/MCP) —
# a real OS thread is unsafe here because the shared AsyncQdrantClient and
# LLM http client are bound to the server's event loop. Progress is exposed
# via start_reclassify_scope() / get_reclassify_status() for the GUI.
# ---------------------------------------------------------------------------
_RECLASSIFY_JOBS: dict = {}
_SCOPE_PAYLOAD_KEYS = ("clientId", "clientName", "contextId", "contextName")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def clear_scope_links(node_id: str, user_id: str, neo4j_driver) -> None:
    """Reset scope state of one fact or diary node: links + checked signature."""
    clear_scope_links_batch([node_id], user_id, neo4j_driver)


def clear_scope_links_batch(node_ids: list, user_id: str, neo4j_driver) -> int:
    """Reset scope state for many nodes in one round trip.

    Reclassification used to issue one blocking query per item from the event
    loop, which stalled the whole server for the length of the run. Batching
    keeps the round trips proportional to the vault size rather than to the item
    count times a per-item constant.
    """
    ids = [nid for nid in dict.fromkeys(node_ids) if nid]
    if not ids or neo4j_driver is None:
        return 0
    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (n {id: $ids, userId: $userId})
            OPTIONAL MATCH (n)-[r:FOR_CLIENT|IN_CONTEXT]->()
            FOREACH (x IN CASE WHEN r IS NULL THEN [] ELSE [x] END | DELETE x)
            REMOVE n.scopeCheckedSig
            RETURN count(n) AS cleared
            """,
            ids=ids, userId=user_id,
        )
        row = result.single()
    return int(row["cleared"]) if row and row["cleared"] else 0


async def clear_scope_links_async(node_ids: list, user_id: str, neo4j_driver) -> int:
    """Batched clear off the event loop so the UI/MCP stay responsive during a run."""
    return await asyncio.to_thread(clear_scope_links_batch, node_ids, user_id, neo4j_driver)


_RECLASSIFY_JOB_HISTORY = 8
_CLEAR_BATCH = 250


def _prune_reclassify_jobs(keep: int = _RECLASSIFY_JOB_HISTORY) -> None:
    """Keep the registry bounded — it is keyed by user and never expires on its own."""
    if len(_RECLASSIFY_JOBS) <= keep:
        return
    finished = [
        (job.get("finished_at") or job.get("started_at") or "", user_id)
        for user_id, job in _RECLASSIFY_JOBS.items()
        if job.get("state") != "running"
    ]
    finished.sort()
    for _, user_id in finished[:len(_RECLASSIFY_JOBS) - keep]:
        _RECLASSIFY_JOBS.pop(user_id, None)


def _public_reclassify_job(job) -> dict:
    if not job:
        return {"state": "idle", "total": 0, "done": 0, "linked": 0, "unlinked": 0,
                "errors": 0, "started_at": None, "finished_at": None, "error": None}
    return {k: v for k, v in job.items() if k != "task"}


def start_reclassify_scope(user_id: str) -> dict:
    """Start a full reclassification background job. Returns {started, job}."""
    job = _RECLASSIFY_JOBS.get(user_id)
    if job and job.get("state") == "running":
        return {"started": False, "job": _public_reclassify_job(job)}
    if not claim_maintenance(user_id, "reclassify"):
        logger.warning(f"reclassify [{user_id}]: refused, another maintenance job holds the lock")
        return {"started": False, "job": _public_reclassify_job(job), "conflict": "maintenance"}
    job = {"state": "running", "total": 0, "done": 0, "linked": 0, "unlinked": 0,
           "errors": 0, "started_at": _utcnow(), "finished_at": None, "error": None}
    # Prune after inserting: pruning first would evict one entry only to add a
    # new one, leaving the registry permanently one over the limit.
    _RECLASSIFY_JOBS[user_id] = job
    _prune_reclassify_jobs(keep=_RECLASSIFY_JOB_HISTORY - 1)
    job["task"] = asyncio.create_task(_reclassify_all_scope(user_id, job))
    logger.info(f"reclassify [{user_id}]: full reclassification started in background")
    return {"started": True, "job": _public_reclassify_job(job)}


def get_reclassify_status(user_id: str) -> dict:
    """Return the current (or last) reclassification job status for a user."""
    return _public_reclassify_job(_RECLASSIFY_JOBS.get(user_id))


async def _reclassify_all_scope(user_id: str, job: dict) -> None:
    """Classify ALL facts + diary entries (enriched input), replacing scope links."""
    try:
        neo4j_driver = get_neo4j()
        qdrant = await get_qdrant()
        if not neo4j_driver or not qdrant:
            job.update(state="error", finished_at=_utcnow(), error="DB not available")
            return

        clients = db_list_clients(user_id)
        if not clients:
            job.update(state="error", finished_at=_utcnow(), error="No clients defined")
            return
        sig = _scope_signature(clients)

        with neo4j_driver.session() as s:
            facts = list(s.run(
                """
                MATCH (f:Fact {userId: $userId})
                WHERE toLower(f.category) <> 'client'
                RETURN f.id AS id, f.name AS name, f.text AS text, f.category AS category
                """,
                userId=user_id
            ))
            diaries = list(s.run(
                """
                MATCH (d:DiaryEntry {userId: $userId})
                RETURN d.id AS id, d.name AS name, d.content AS content, d.keywords AS keywords
                """,
                userId=user_id
            ))

        job["total"] = len(facts) + len(diaries)
        logger.info(
            f"reclassify [{user_id}]: classifying {len(facts)} facts + "
            f"{len(diaries)} diary entries with {SCOPE_MODEL}"
        )

        # Capture the current scope BEFORE clearing. The classifier's strongest
        # signal is the [client: X] tag on neighbouring facts, and those tags
        # come from FOR_CLIENT links this run is about to remove. Clearing
        # up front and feeding the snapshot keeps that evidence constant for
        # every item instead of decaying as the run progresses.
        scope_snapshot = await asyncio.to_thread(_capture_scope_snapshot, user_id, neo4j_driver)
        if scope_snapshot:
            logger.info(f"reclassify [{user_id}]: captured scope for {len(scope_snapshot)} items")

        all_ids = [r["id"] for r in list(facts) + list(diaries) if r.get("id")]
        cleared = 0
        for start in range(0, len(all_ids), _CLEAR_BATCH):
            cleared += await clear_scope_links_async(
                all_ids[start:start + _CLEAR_BATCH], user_id, neo4j_driver
            )
        logger.info(f"reclassify [{user_id}]: cleared scope links on {cleared} items")

        sem = asyncio.Semaphore(max(1, SCOPE_BACKFILL_CONCURRENCY))

        async def _process(kind: str, item: dict) -> None:
            try:
                if kind == "fact":
                    ok = await _classify_and_link_fact(
                        item, clients, user_id, sem, neo4j_driver, sig, scope_snapshot)
                else:
                    ok = await _classify_and_link_diary(
                        item, clients, user_id, sem, neo4j_driver, sig, scope_snapshot)
            except Exception as exc:
                # One bad item must not abort the run, and must not be stamped:
                # leaving it unstamped means the next boot retries it.
                logger.warning(f"reclassify [{user_id}] {kind} {item.get('id')}: {exc}")
                job["errors"] += 1
                job["done"] += 1
                return
            job["done"] += 1
            if ok:
                job["linked"] += 1
            else:
                job["unlinked"] += 1
            if job["done"] % 25 == 0 or job["done"] == job["total"]:
                logger.info(f"reclassify [{user_id}]: {job['done']}/{job['total']} classified, "
                            f"{job['linked']} linked, {job['errors']} errored")

        await asyncio.gather(*[_process("fact", dict(r)) for r in facts])
        await asyncio.gather(*[_process("diary", dict(r)) for r in diaries])

        # Push the new links into Qdrant payloads; the diff-based backfill also
        # strips stale scope keys off items that are now generic.
        await _backfill_qdrant(user_id, neo4j_driver, qdrant)

        job.update(state="done", finished_at=_utcnow())
        logger.info(
            f"reclassify [{user_id}]: done, {job['linked']}/{job['total']} linked to clients, "
            f"{job['errors']} errored (left for retry)"
        )
        await publish_db_event(user_id, "reclassify_done", {
            "total": job["total"], "linked": job["linked"], "unlinked": job["unlinked"],
            "errors": job["errors"],
        })
    except Exception as exc:
        logger.exception(f"reclassify [{user_id}]: failed: {exc}")
        job.update(state="error", finished_at=_utcnow(), error=str(exc))
    finally:
        # The lock is what stops a restore from interleaving with this run.
        release_maintenance(user_id)


# ---------------------------------------------------------------------------
# Single-item reclassification — triggered from the UI hamburger menu.
# Clears existing scope links, re-runs the enriched classifier, syncs Qdrant.
# Returns True if the item was linked to a client, False if left unscoped.
# ---------------------------------------------------------------------------

async def reclassify_single_fact(item_id: str, user_id: str) -> bool:
    """Clear and re-classify scope for a single fact. Returns True if linked."""
    if not claim_maintenance(user_id, "reclassify_single"):
        raise RuntimeError("Another maintenance operation (backup/restore/reclassify) is running")
    try:
        return await _reclassify_single_fact(item_id, user_id)
    finally:
        release_maintenance(user_id)


async def _reclassify_single_fact(item_id: str, user_id: str) -> bool:
    neo4j_driver = get_neo4j()
    qdrant = await get_qdrant()
    if not neo4j_driver or not qdrant:
        raise RuntimeError("DB not available")

    with neo4j_driver.session() as s:
        rows = list(s.run(
            "MATCH (f:Fact {id: $id, userId: $userId}) "
            "RETURN f.id AS id, f.name AS name, f.text AS text, f.category AS category",
            id=item_id, userId=user_id,
        ))
    if not rows:
        raise ValueError(f"Fact {item_id!r} not found for user {user_id!r}")
    item = dict(rows[0])

    clients = db_list_clients(user_id)
    if not clients:
        return False

    sig = _scope_signature(clients)
    # Snapshot before clearing so the neighbour evidence is the same as it is
    # during a full run rather than a post-clear graph.
    scope_snapshot = await asyncio.to_thread(_capture_scope_snapshot, user_id, neo4j_driver)
    await clear_scope_links_async([item_id], user_id, neo4j_driver)
    sem = asyncio.Semaphore(1)
    linked = await _classify_and_link_fact(
        item, clients, user_id, sem, neo4j_driver, sig, scope_snapshot)
    await _backfill_qdrant(user_id, neo4j_driver, qdrant)
    logger.info(f"reclassify_single_fact [{user_id}] {item_id}: linked={linked}")
    return linked


async def reclassify_single_diary(item_id: str, user_id: str) -> bool:
    """Clear and re-classify scope for a single diary entry. Returns True if linked."""
    if not claim_maintenance(user_id, "reclassify_single"):
        raise RuntimeError("Another maintenance operation (backup/restore/reclassify) is running")
    try:
        return await _reclassify_single_diary(item_id, user_id)
    finally:
        release_maintenance(user_id)


async def _reclassify_single_diary(item_id: str, user_id: str) -> bool:
    neo4j_driver = get_neo4j()
    qdrant = await get_qdrant()
    if not neo4j_driver or not qdrant:
        raise RuntimeError("DB not available")

    with neo4j_driver.session() as s:
        rows = list(s.run(
            "MATCH (d:DiaryEntry {id: $id, userId: $userId}) "
            "RETURN d.id AS id, d.name AS name, d.content AS content, d.keywords AS keywords",
            id=item_id, userId=user_id,
        ))
    if not rows:
        raise ValueError(f"DiaryEntry {item_id!r} not found for user {user_id!r}")
    item = dict(rows[0])

    clients = db_list_clients(user_id)
    if not clients:
        return False

    sig = _scope_signature(clients)
    scope_snapshot = await asyncio.to_thread(_capture_scope_snapshot, user_id, neo4j_driver)
    await clear_scope_links_async([item_id], user_id, neo4j_driver)
    sem = asyncio.Semaphore(1)
    linked = await _classify_and_link_diary(
        item, clients, user_id, sem, neo4j_driver, sig, scope_snapshot)
    await _backfill_qdrant(user_id, neo4j_driver, qdrant)
    logger.info(f"reclassify_single_diary [{user_id}] {item_id}: linked={linked}")
    return linked
