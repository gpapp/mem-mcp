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
    get_neo4j, get_qdrant, get_llm_response, logger,
    COLLECTION_NAME, DIARY_COLLECTION,
    SCOPE_MODEL, SCOPE_BACKFILL_ENABLED, SCOPE_BACKFILL_CONCURRENCY,
)
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
from client_manager import (
    db_create_client, db_create_context, db_list_clients,
    db_resolve_client, db_resolve_context,
    _resolve_client_by_id, _resolve_context_by_id,
    link_fact_to_client, link_fact_to_context,
    link_diary_to_client, link_diary_to_context,
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
    with neo4j_driver.session() as s:
        for fact_id, (client_id, cname) in client_map.items():
            s.run(
                """
                MATCH (d:DiaryEntry {userId: $userId})-[:MENTIONS]->(cf:Fact {id: $factId, userId: $userId})
                WHERE NOT (d)-[:FOR_CLIENT]->(:Client)
                MERGE (c:Client {id: $clientId, userId: $userId})
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
    "IMPORTANT: if the item content contains an explicit '**Client:**' or 'Client:' header, "
    "that declaration is authoritative — use it and do not override it with content keywords."
)


# ---------------------------------------------------------------------------
# Fast (no-LLM) scope resolution for diary entries.
# Returns (client_name, context_name) or (None, None) if no hard signal found.
# ---------------------------------------------------------------------------
_CLIENT_HEADER_RE = re.compile(
    r"(?:^\*{0,2}Client\*{0,2}|^Client)\s*[:\-]\s*(.+)",
    re.MULTILINE | re.IGNORECASE,
)


def _fast_diary_scope(item: dict, clients: list, neo4j_driver, user_id: str) -> tuple:
    """Return (client_name, None) when a hard deterministic signal is found.

    Two signals checked in priority order:
    1. All MENTIONS-linked facts that have a client agree on ONE client.
    2. The diary content contains an explicit '**Client:** <name>' header line.

    Returns (None, None) when no hard signal is present — caller falls back to LLM.
    """
    # Signal 1: unanimous MENTIONS client
    if neo4j_driver is not None:
        try:
            with neo4j_driver.session() as s:
                rows = list(s.run(
                    """
                    MATCH (d:DiaryEntry {userId: $userId, id: $did})-[:MENTIONS]->(f:Fact)
                    MATCH (f)-[:FOR_CLIENT]->(cl:Client)
                    RETURN DISTINCT cl.name AS clientName
                    """,
                    userId=user_id, did=item["id"]
                ))
            scoped_clients = [r["clientName"] for r in rows if r["clientName"]]
            unique = set(scoped_clients)
            if len(unique) == 1:
                cname = unique.pop()
                matched = next((c["name"] for c in clients if c["name"].lower() == cname.lower()), None)
                if matched:
                    logger.debug(f"[scope_fast] diary {item.get('id')}: unanimous MENTIONS → {matched}")
                    return matched, None
        except Exception as exc:
            logger.debug(f"[scope_fast] MENTIONS lookup failed for {item.get('id')}: {exc}")

    # Signal 2: explicit **Client:** header in content
    content = item.get("content", "") or ""
    m = _CLIENT_HEADER_RE.search(content)
    if m:
        raw = m.group(1).strip().rstrip("*").strip()
        matched = next((c["name"] for c in clients if c["name"].lower() == raw.lower()), None)
        if matched:
            logger.debug(f"[scope_fast] diary {item.get('id')}: content header → {matched}")
            return matched, None

    return None, None


async def _classify_scope(item_text: str, clients: list) -> tuple:
    """Ask Ollama which existing client/context an item belongs to.

    Returns (client_name|None, context_name|None), validated against `clients`.
    Empty tuple parts on any error — classification must never block migration.
    """
    scope_lines = []
    for c in clients:
        ctxs = ", ".join(x["name"] for x in c.get("contexts", [])) or "(no contexts)"
        scope_lines.append(f"- {c['name']} [contexts: {ctxs}]")
    scope_block = "\n".join(scope_lines)

    text = item_text if len(item_text) <= 1500 else item_text[:1500] + "…"
    prompt = f"KNOWN CLIENTS:\n{scope_block}\n\nITEM:\n{text}"

    try:
        raw = await get_llm_response(prompt, system=_SCOPE_SYSTEM, model=SCOPE_MODEL)
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if not m:
            return None, None
        data = json.loads(m.group())
        client_name = (data.get("client") or "").strip() or None
        context_name = (data.get("context") or "").strip() or None

        # Validate against known names (case-insensitive); reject inventions.
        matched_client = None
        if client_name:
            for c in clients:
                if c["name"].lower() == client_name.lower():
                    matched_client = c
                    break
        if not matched_client:
            return None, None
        if context_name:
            ok = any(x["name"].lower() == context_name.lower()
                     for x in matched_client.get("contexts", []))
            if not ok:
                context_name = None
            else:
                # Normalize to the canonical stored spelling.
                for x in matched_client.get("contexts", []):
                    if x["name"].lower() == context_name.lower():
                        context_name = x["name"]
                        break
        return matched_client["name"], context_name
    except Exception as exc:
        logger.warning(f"[scope_backfill] classification failed: {type(exc).__name__}: {exc}")
        return None, None


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


def _enriched_fact_text(item: dict, neo4j_driver, user_id: str) -> str:
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
                    WHERE n:Fact OR n:DiaryEntry
                    RETURN DISTINCT type(r) AS rel, n.name AS name,
                           coalesce(n.text, n.content, '') AS body
                    LIMIT 6
                    """,
                    userId=user_id, fid=item["id"]
                ))
            if rows:
                rel_lines = [
                    f"- [{r['rel']}] {r['name'] or 'Unnamed'}: {_snippet(r['body'])}"
                    for r in rows
                ]
                parts.append("RELATED:\n" + "\n".join(rel_lines))
        except Exception as exc:
            logger.debug(f"[scope_backfill] neighbor fetch failed for {item.get('id')}: {exc}")
    return "\n".join(p for p in parts if p)


def _enriched_diary_text(item: dict, neo4j_driver, user_id: str) -> str:
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
                    OPTIONAL MATCH (f)-[:FOR_CLIENT]->(cl:Client)
                    RETURN DISTINCT f.name AS name, f.text AS body, cl.name AS clientName
                    LIMIT 6
                    """,
                    userId=user_id, did=item["id"]
                ))
            if rows:
                m_lines = []
                for r in rows:
                    line = f"- {r['name'] or 'Unnamed'}: {_snippet(r['body'])}"
                    if r["clientName"]:
                        line += f" [client: {r['clientName']}]"
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
                                  neo4j_driver=None, scope_sig=None) -> bool:
    """Classify one fact and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    async with sem:
        body = _enriched_fact_text(item, neo4j_driver, user_id)
        client_name, context_name = await _classify_scope(body, clients)
    if not client_name:
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


async def _classify_and_link_diary(item: dict, clients: list, user_id: str, sem: asyncio.Semaphore,
                                   neo4j_driver=None, scope_sig=None) -> bool:
    """Classify one diary entry and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    # Fast path: unanimous MENTIONS client or explicit **Client:** header — no LLM needed.
    client_name, context_name = _fast_diary_scope(item, clients, neo4j_driver, user_id)
    if not client_name:
        async with sem:
            body = _enriched_diary_text(item, neo4j_driver, user_id)
            client_name, context_name = await _classify_scope(body, clients)
    if not client_name:
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
            _track(_classify_and_link_fact(dict(r), clients, user_id, sem, neo4j_driver, sig))
            for r in facts
        ])
        await asyncio.gather(*[
            _track(_classify_and_link_diary(dict(r), clients, user_id, sem, neo4j_driver, sig))
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
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (n {id: $id, userId: $userId})
            OPTIONAL MATCH (n)-[r:FOR_CLIENT|IN_CONTEXT]->()
            FOREACH (x IN CASE WHEN r IS NULL THEN [] ELSE [r] END | DELETE x)
            REMOVE n.scopeCheckedSig
            """,
            id=node_id, userId=user_id,
        )


def _public_reclassify_job(job) -> dict:
    if not job:
        return {"state": "idle", "total": 0, "done": 0, "linked": 0, "unlinked": 0,
                "started_at": None, "finished_at": None, "error": None}
    return {k: v for k, v in job.items() if k != "task"}


def start_reclassify_scope(user_id: str) -> dict:
    """Start a full reclassification background job. Returns {started, job}."""
    job = _RECLASSIFY_JOBS.get(user_id)
    if job and job.get("state") == "running":
        return {"started": False, "job": _public_reclassify_job(job)}
    job = {"state": "running", "total": 0, "done": 0, "linked": 0, "unlinked": 0,
           "started_at": _utcnow(), "finished_at": None, "error": None}
    _RECLASSIFY_JOBS[user_id] = job
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
        sem = asyncio.Semaphore(max(1, SCOPE_BACKFILL_CONCURRENCY))

        async def _process(kind: str, item: dict) -> None:
            # Clear-then-classify per item: interrupt-safe, and a null verdict
            # correctly leaves the item generic (unlinked).
            clear_scope_links(item["id"], user_id, neo4j_driver)
            if kind == "fact":
                ok = await _classify_and_link_fact(item, clients, user_id, sem, neo4j_driver, sig)
            else:
                ok = await _classify_and_link_diary(item, clients, user_id, sem, neo4j_driver, sig)
            job["done"] += 1
            if ok:
                job["linked"] += 1
            else:
                job["unlinked"] += 1
            if job["done"] % 25 == 0 or job["done"] == job["total"]:
                logger.info(f"reclassify [{user_id}]: {job['done']}/{job['total']} classified, "
                            f"{job['linked']} linked")

        await asyncio.gather(*[_process("fact", dict(r)) for r in facts])
        await asyncio.gather(*[_process("diary", dict(r)) for r in diaries])

        # Push the new links into Qdrant payloads; the diff-based backfill also
        # strips stale scope keys off items that are now generic.
        await _backfill_qdrant(user_id, neo4j_driver, qdrant)

        job.update(state="done", finished_at=_utcnow())
        logger.info(f"reclassify [{user_id}]: done, {job['linked']}/{job['total']} linked to clients")
    except Exception as exc:
        logger.exception(f"reclassify [{user_id}]: failed: {exc}")
        job.update(state="error", finished_at=_utcnow(), error=str(exc))
