"""
migrate_client_context.py – One-time migration: Client-category facts → Client nodes,
Project-category facts → Context nodes. Idempotent (MERGE throughout).
"""

import asyncio
import json
import re

from common import (
    get_neo4j, get_qdrant, get_llm_response, logger,
    COLLECTION_NAME, DIARY_COLLECTION,
    SCOPE_MODEL, SCOPE_BACKFILL_ENABLED, SCOPE_BACKFILL_CONCURRENCY,
)
from qdrant_client.models import PointStruct
from client_manager import (
    db_create_client, db_create_context, db_list_clients,
    db_resolve_client, db_resolve_context,
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


async def _classify_and_link_fact(item: dict, clients: list, user_id: str, sem: asyncio.Semaphore) -> bool:
    """Classify one fact and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    async with sem:
        label = item.get("name") or ""
        body = f"{label}\n{item.get('text', '')}" if label else item.get("text", "")
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


async def _classify_and_link_diary(item: dict, clients: list, user_id: str, sem: asyncio.Semaphore) -> bool:
    """Classify one diary entry and create FOR_CLIENT / IN_CONTEXT links. Returns True if linked."""
    async with sem:
        label = item.get("name") or ""
        body = f"{label}\n{item.get('content', '')}" if label else item.get("content", "")
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
                RETURN f.id AS id, f.name AS name, f.text AS text
                """,
                userId=user_id
            ))
            diaries = list(s.run(
                """
                MATCH (d:DiaryEntry {userId: $userId})
                WHERE NOT (d)-[:FOR_CLIENT]->(:Client)
                RETURN d.id AS id, d.name AS name, d.content AS content
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
            _track(_classify_and_link_fact(dict(r), clients, user_id, sem))
            for r in facts
        ])
        await asyncio.gather(*[
            _track(_classify_and_link_diary(dict(r), clients, user_id, sem))
            for r in diaries
        ])

        # Push the new links into Qdrant payloads for filtered search.
        await _backfill_qdrant(user_id, neo4j_driver, qdrant)
        logger.info(f"scope_backfill [{user_id}]: done, {linked}/{total} linked to clients")
