"""
migrate_title_to_name.py

Renames the 'title' property to 'name' for Facts and DiaryEntries in both
Neo4j and Qdrant. Run this once after deploying the name rename.

Usage:
    python migrate_title_to_name.py [--dry-run]

The --dry-run flag shows what would be changed without making modifications.
"""

import asyncio
import argparse
import logging

from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Record, ScrollResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migration")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "ea_memories"


async def get_neo4j_driver():
    from dotenv import load_dotenv
    import os
    load_dotenv()
    password = os.getenv("NEO4J_PASSWORD") or os.getenv("MEM_NEO4J_PASSWORD")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD not set in environment")
    return AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, password))


async def migrate_neo4j_facts(dry_run: bool = False):
    driver = await get_neo4j_driver()
    migrated = 0

    async with driver.session() as s:
        # Migrate Fact nodes
        result = await s.run("""
            MATCH (f:Fact)
            WHERE f.title IS NOT NULL AND f.name IS NULL
            RETURN f.id as id, f.title as old_title
        """)
        records = await result.data()
        logger.info(f"Found {len(records)} Fact nodes to migrate in Neo4j")

        for rec in records:
            if dry_run:
                logger.info(f"  [DRY-RUN] Would rename Fact {rec['id']}: '{rec['old_title']}' -> name")
            else:
                await s.run("""
                    MATCH (f:Fact {id: $id})
                    SET f.name = f.title
                    REMOVE f.title
                """, id=rec["id"])
                logger.info(f"  Renamed Fact {rec['id']}: '{rec['old_title']}'")
            migrated += 1

        # Migrate DiaryEntry nodes
        result = await s.run("""
            MATCH (d:DiaryEntry)
            WHERE d.title IS NOT NULL AND d.name IS NULL
            RETURN d.id as id, d.title as old_title
        """)
        records = await result.data()
        logger.info(f"Found {len(records)} DiaryEntry nodes to migrate in Neo4j")

        for rec in records:
            if dry_run:
                logger.info(f"  [DRY-RUN] Would rename DiaryEntry {rec['id']}: '{rec['old_title']}' -> name")
            else:
                await s.run("""
                    MATCH (d:DiaryEntry {id: $id})
                    SET d.name = d.title
                    REMOVE d.title
                """, id=rec["id"])
                logger.info(f"  Renamed DiaryEntry {rec['id']}: '{rec['old_title']}'")
            migrated += 1

    await driver.close()
    return migrated


async def migrate_qdrant(dry_run: bool = False):
    client = AsyncQdrantClient(url=QDRANT_URL)
    migrated = 0
    offset = None

    while True:
        try:
            if offset:
                result: ScrollResponse = await client.scroll(
                    collection_name=COLLECTION_NAME,
                    scroll_filter=None,
                    limit=100,
                    offset=offset,
                    with_vectors=False,
                )
            else:
                result: ScrollResponse = await client.scroll(
                    collection_name=COLLECTION_NAME,
                    scroll_filter=None,
                    limit=100,
                    offset=None,
                    with_vectors=False,
                )
        except Exception as e:
            logger.error(f"Qdrant scroll failed: {e}")
            break

        points_to_update = []
        for point in result.points:
            payload = point.payload
            if "title" in payload and "name" not in payload:
                new_payload = dict(payload)
                new_payload["name"] = new_payload.pop("title")
                points_to_update.append(
                    Record(id=point.id, payload=new_payload, vector=payload.get("vector"))
                )
                if dry_run:
                    logger.info(f"  [DRY-RUN] Would rename Qdrant point {point.id}: title -> name")
                else:
                    logger.info(f"  Renamed Qdrant point {point.id}")

        if points_to_update and not dry_run:
            await client.upsert(
                collection_name=COLLECTION_NAME,
                points=points_to_update,
            )

        migrated += len(points_to_update)
        offset = result.next_page_offset
        if offset is None:
            break

    logger.info(f"Migrated {migrated} points in Qdrant collection '{COLLECTION_NAME}'")
    return migrated


async def main():
    parser = argparse.ArgumentParser(description="Migrate title -> name in Neo4j and Qdrant")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be changed without modifying")
    args = parser.parse_args()

    mode = "[DRY-RUN] " if args.dry_run else ""
    logger.info(f"{mode}Starting migration: title -> name")

    neo4j_count = await migrate_neo4j_facts(dry_run=args.dry_run)
    qdrant_count = await migrate_qdrant(dry_run=args.dry_run)

    logger.info(f"{mode}Migration complete: {neo4j_count} Neo4j nodes, {qdrant_count} Qdrant points updated")


if __name__ == "__main__":
    asyncio.run(main())