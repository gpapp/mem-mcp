"""
migrate_client_context.py – One-time migration: Client-category facts → Client nodes,
Project-category facts → Context nodes. Idempotent (MERGE throughout).
"""

import asyncio
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
    with neo4j_driver.session() as s:
        for fact_id, (client_id, cname) in client_map.items():
            s.run(
                """
                MATCH (d:DiaryEntry {userId: $userId})-[:MENTIONS]->(cf:Fact {id: $factId, userId: $userId})
                MERGE (c:Client {id: $clientId, userId: $userId})
                MERGE (d)-[:FOR_CLIENT]->(c)
                SET c.lastMentioned = datetime()
                """,
                userId=user_id, factId=fact_id, clientId=client_id
            )

    # 5. Backfill Qdrant payloads with denormalized client/context names
    await _backfill_qdrant(user_id, neo4j_driver, qdrant)
    logger.info(f"migrate_client_context [{user_id}]: done ({len(client_map)} clients)")


async def _backfill_qdrant(user_id: str, neo4j_driver, qdrant):
    """Copy FOR_CLIENT / IN_CONTEXT links into Qdrant payloads for fast filtering."""
    with neo4j_driver.session() as s:
        rows = list(s.run(
            """
            MATCH (f:Fact {userId: $userId})
            OPTIONAL MATCH (f)-[:FOR_CLIENT]->(c:Client)
            OPTIONAL MATCH (f)-[:IN_CONTEXT]->(ctx:Context)
            WHERE c IS NOT NULL OR ctx IS NOT NULL
            RETURN f.id AS id, c.id AS clientId, c.name AS clientName,
                   ctx.id AS contextId, ctx.name AS contextName
            """,
            userId=user_id
        ))
    for r in rows:
        try:
            existing = await qdrant.retrieve(collection_name=COLLECTION_NAME, ids=[r["id"]], with_payload=True)
            if not existing:
                continue
            payload = dict(existing[0].payload or {})
            if r["clientId"]:
                payload["clientId"] = r["clientId"]
                payload["clientName"] = r["clientName"]
            if r["contextId"]:
                payload["contextId"] = r["contextId"]
                payload["contextName"] = r["contextName"]
            # Re-embed is expensive; preserve existing vector by retrieving it
            with_vec = await qdrant.retrieve(collection_name=COLLECTION_NAME, ids=[r["id"]], with_vectors=True)
            vec = with_vec[0].vector if with_vec else None
            if vec is None:
                continue
            await qdrant.upsert(
                collection_name=COLLECTION_NAME,
                points=[PointStruct(id=r["id"], vector=vec, payload=payload)],
            )
        except Exception as e:
            logger.warning(f"migrate_client_context [{user_id}]: Qdrant backfill failed for {r['id']}: {e}")

    # Diary entries
    with neo4j_driver.session() as s:
        drows = list(s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            OPTIONAL MATCH (d)-[:FOR_CLIENT]->(c:Client)
            OPTIONAL MATCH (d)-[:IN_CONTEXT]->(ctx:Context)
            WHERE c IS NOT NULL OR ctx IS NOT NULL
            RETURN d.id AS id, c.id AS clientId, c.name AS clientName,
                   ctx.id AS contextId, ctx.name AS contextName
            """,
            userId=user_id
        ))
    for r in drows:
        try:
            existing = await qdrant.retrieve(collection_name=DIARY_COLLECTION, ids=[r["id"]], with_payload=True)
            if not existing:
                continue
            payload = dict(existing[0].payload or {})
            if r["clientId"]:
                payload["clientId"] = r["clientId"]
                payload["clientName"] = r["clientName"]
            if r["contextId"]:
                payload["contextId"] = r["contextId"]
                payload["contextName"] = r["contextName"]
            with_vec = await qdrant.retrieve(collection_name=DIARY_COLLECTION, ids=[r["id"]], with_vectors=True)
            vec = with_vec[0].vector if with_vec else None
            if vec is None:
                continue
            await qdrant.upsert(
                collection_name=DIARY_COLLECTION,
                points=[PointStruct(id=r["id"], vector=vec, payload=payload)],
            )
        except Exception as e:
            logger.warning(f"migrate_client_context [{user_id}]: diary backfill failed for {r['id']}: {e}")


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
    "return nulls when the item is generic/shared knowledge or matches no client."
)


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
                    RETURN DISTINCT f.name AS name, f.text AS body
                    LIMIT 6
                    """,
                    userId=user_id, did=item["id"]
                ))
            if rows:
                m_lines = [
                    f"- {r['name'] or 'Unnamed'}: {_snippet(r['body'])}"
                    for r in rows
                ]
                parts.append("MENTIONS:\n" + "\n".join(m_lines))
        except Exception as exc:
            logger.debug(f"[scope_backfill] mentions fetch failed for {item.get('id')}: {exc}")
    return "\n".join(p for p in parts if p)


async def _classify_and_link_fact(item: dict, clients: list, user_id: str, sem: asyncio.Semaphore,
                                  neo4j_driver=None) -> bool:
    """Classify one fact and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    async with sem:
        body = _enriched_fact_text(item, neo4j_driver, user_id)
        client_name, context_name = await _classify_scope(body, clients)
    if not client_name:
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
                                   neo4j_driver=None) -> bool:
    """Classify one diary entry and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    async with sem:
        body = _enriched_diary_text(item, neo4j_driver, user_id)
        client_name, context_name = await _classify_scope(body, clients)
    if not client_name:
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

        with neo4j_driver.session() as s:
            facts = list(s.run(
                """
                MATCH (f:Fact {userId: $userId})
                WHERE NOT (f)-[:FOR_CLIENT]->(:Client)
                  AND toLower(f.category) <> 'client'
                RETURN f.id AS id, f.name AS name, f.text AS text, f.category AS category
                """,
                userId=user_id
            ))
            diaries = list(s.run(
                """
                MATCH (d:DiaryEntry {userId: $userId})
                WHERE NOT (d)-[:FOR_CLIENT]->(:Client)
                RETURN d.id AS id, d.name AS name, d.content AS content, d.keywords AS keywords
                """,
                userId=user_id
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
            _track(_classify_and_link_fact(dict(r), clients, user_id, sem, neo4j_driver))
            for r in facts
        ])
        await asyncio.gather(*[
            _track(_classify_and_link_diary(dict(r), clients, user_id, sem, neo4j_driver))
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
    """Remove FOR_CLIENT / IN_CONTEXT links of one fact or diary node."""
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (n {id: $id, userId: $userId})-[r:FOR_CLIENT|IN_CONTEXT]->() DELETE r",
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
        unlinked_fact_ids: list = []
        unlinked_diary_ids: list = []

        async def _process(kind: str, item: dict) -> None:
            # Clear-then-classify per item: interrupt-safe, and a null verdict
            # correctly leaves the item generic (unlinked).
            clear_scope_links(item["id"], user_id, neo4j_driver)
            if kind == "fact":
                ok = await _classify_and_link_fact(item, clients, user_id, sem, neo4j_driver)
            else:
                ok = await _classify_and_link_diary(item, clients, user_id, sem, neo4j_driver)
            job["done"] += 1
            if ok:
                job["linked"] += 1
            else:
                job["unlinked"] += 1
                (unlinked_fact_ids if kind == "fact" else unlinked_diary_ids).append(item["id"])
            if job["done"] % 25 == 0 or job["done"] == job["total"]:
                logger.info(f"reclassify [{user_id}]: {job['done']}/{job['total']} classified, "
                            f"{job['linked']} linked")

        await asyncio.gather(*[_process("fact", dict(r)) for r in facts])
        await asyncio.gather(*[_process("diary", dict(r)) for r in diaries])

        # Push the new links into Qdrant payloads, and scrub stale scope keys
        # off items that are now generic (backfill only ever adds keys).
        await _backfill_qdrant(user_id, neo4j_driver, qdrant)
        for collection, ids in ((COLLECTION_NAME, unlinked_fact_ids),
                                (DIARY_COLLECTION, unlinked_diary_ids)):
            if not ids:
                continue
            try:
                await qdrant.delete_payload(
                    collection_name=collection,
                    keys=list(_SCOPE_PAYLOAD_KEYS),
                    points=ids,
                )
            except Exception as exc:
                logger.warning(f"reclassify [{user_id}]: stale scope-key cleanup failed "
                               f"for {len(ids)} {collection} points: {exc}")

        job.update(state="done", finished_at=_utcnow())
        logger.info(f"reclassify [{user_id}]: done, {job['linked']}/{job['total']} linked to clients")
    except Exception as exc:
        logger.exception(f"reclassify [{user_id}]: failed: {exc}")
        job.update(state="error", finished_at=_utcnow(), error=str(exc))
