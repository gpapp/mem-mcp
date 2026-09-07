"""
migrate_client_context.py – One-time migration: Client-category facts → Client nodes,
Project-category facts → Context nodes. Idempotent (MERGE throughout).
"""

from common import get_neo4j, get_qdrant, logger, COLLECTION_NAME, DIARY_COLLECTION
from qdrant_client.models import PointStruct
from client_manager import db_create_client, db_create_context


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
