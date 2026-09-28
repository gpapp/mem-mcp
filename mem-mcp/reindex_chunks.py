"""
reindex_chunks.py — Split existing long records into per-chunk vector points.

Usage:
    python reindex_chunks.py [OPTIONS]

Options:
    -u / --user USER_ID     Reindex only this user (default: all users)
    -d / --dry-run          Report what would be rewritten, write nothing
    -c / --concurrency N    Max records processed at once (default: 2)
    --env FILE              Load environment from .env file (default: .env)

Why this exists
---------------
New and edited long facts/diary entries are chunked on write, so this script is
only needed for records that were already stored as a single point. Such a
record is searchable but its vector averages the whole document, so a query
about a detail in the middle of it scores poorly against everything else.

Rewriting is idempotent: a record that already has chunks is reported as
``skipped``, and a record too short to chunk is left alone. Each long record
costs one embedding call per chunk, so a large vault is not free — ``--dry-run``
first is worth the five seconds.

Examples:
    python reindex_chunks.py --dry-run
    python reindex_chunks.py -u memories
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load .env before importing the modules that need it
# ---------------------------------------------------------------------------

def _load_env(env_file: str) -> None:
    p = Path(env_file)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Reading the records to consider
# ---------------------------------------------------------------------------

async def _fetch_facts(neo4j_driver, user_id: str) -> list:
    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (f:Fact {userId: $userId})
            WHERE f.text IS NOT NULL AND f.text <> ''
            RETURN f.id AS id, f.text AS text, f.name AS name
            """,
            userId=user_id,
        )
        return [dict(r) for r in res]


async def _fetch_diary(neo4j_driver, user_id: str) -> list:
    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            WHERE d.content IS NOT NULL AND d.content <> ''
            RETURN d.id AS id, d.content AS text, d.name AS name
            """,
            userId=user_id,
        )
        return [dict(r) for r in res]


async def _get_all_users(neo4j_driver) -> list:
    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (f:Fact) RETURN DISTINCT f.userId AS uid
            UNION
            MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS uid
            """
        )
        return [r["uid"] for r in res if r["uid"]]


# ---------------------------------------------------------------------------
# Per-record work
# ---------------------------------------------------------------------------

def _classify(record: dict) -> str:
    """chunk | skip-short | skip-chunked, without touching Qdrant."""
    from chunking import needs_chunking, plan_chunks

    text = record.get("text") or ""
    if not needs_chunking(text):
        return "skip-short"
    if plan_chunks(text)["chunks"] <= 1:
        return "skip-short"
    return "chunk"


async def _already_chunked(qdrant, record_id: str, collection: str) -> bool:
    """True when extra points exist beyond the record's own point."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    points, _ = await qdrant.scroll(
        collection_name=collection,
        scroll_filter=Filter(must=[
            FieldCondition(key="parentId", match=MatchValue(value=str(record_id)))
        ]),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return bool(points)


async def _process_fact(sem, qdrant, record: dict, dry_run: bool) -> dict:
    from chunking import plan_chunks
    from common import COLLECTION_NAME
    from fact_manager import _upsert_fact_points

    verdict = _classify(record)
    if verdict != "chunk":
        return {"id": record["id"], "status": verdict, "chunks": 1}

    plan = plan_chunks(record["text"])
    if not dry_run:
        async with sem:
            if await _already_chunked(qdrant, record["id"], COLLECTION_NAME):
                return {"id": record["id"], "status": "skip-chunked", "chunks": len(plan["chunks"])}
            # replace=True: the single point being rewritten is deleted as part
            # of the write, so a failure here leaves the old vector searchable.
            await _upsert_fact_points(
                qdrant, record["id"], record["text"], {},
                prefix=record.get("name"), replace=True,
            )
    return {"id": record["id"], "status": "dry-run" if dry_run else "chunked",
            "chunks": len(plan["chunks"])}


async def _process_diary(sem, qdrant, record: dict, dry_run: bool) -> dict:
    from chunking import plan_chunks
    from common import DIARY_COLLECTION
    from diary_manager import _upsert_diary_points

    verdict = _classify(record)
    if verdict != "chunk":
        return {"id": record["id"], "status": verdict, "chunks": 1}

    plan = plan_chunks(record["text"])
    if not dry_run:
        async with sem:
            if await _already_chunked(qdrant, record["id"], DIARY_COLLECTION):
                return {"id": record["id"], "status": "skip-chunked", "chunks": len(plan["chunks"])}
            await _upsert_diary_points(
                qdrant, record["id"], record["text"], {},
                prefix=record.get("name"), replace=True,
            )
    return {"id": record["id"], "status": "dry-run" if dry_run else "chunked",
            "chunks": len(plan["chunks"])}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    _load_env(args.env)

    from common import get_qdrant, get_neo4j, wait_for_service, OLLAMA_URL

    if not wait_for_service(OLLAMA_URL, label="Ollama"):
        print("ERROR: Ollama is not reachable. Make sure the stack is running.", file=sys.stderr)
        sys.exit(1)

    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        print("ERROR: Could not connect to Qdrant or Neo4j.", file=sys.stderr)
        sys.exit(1)

    users = [args.user] if args.user else await _get_all_users(neo4j_driver)
    if not users:
        print("No facts or diary entries found.")
        return

    print(f"Users to process: {users}")
    if args.dry_run:
        print("DRY RUN — nothing will be written.\n")

    sem = asyncio.Semaphore(args.concurrency)
    totals = {"chunked": 0, "dry-run": 0, "skip-short": 0, "skip-chunked": 0, "error": 0}

    for user_id in users:
        t_start = time.monotonic()
        for label, fetcher, worker in (
            ("facts", _fetch_facts, _process_fact),
            ("diary", _fetch_diary, _process_diary),
        ):
            records = await fetcher(neo4j_driver, user_id)
            if not records:
                continue
            print(f"[{user_id}] {label}: {len(records)} records")

            results = await asyncio.gather(
                *(worker(sem, qdrant, r, args.dry_run) for r in records),
                return_exceptions=True,
            )
            for record, res in zip(records, results):
                if isinstance(res, Exception):
                    print(f"  ERROR  {record['id'][:8]}…  — {res}")
                    totals["error"] += 1
                    continue
                status = res["status"]
                totals[status] = totals.get(status, 0) + 1
                if status in ("chunked", "dry-run"):
                    name = (record.get("name") or "")[:50]
                    print(f"  {status:8}  {name!r}  -> {res['chunks']} chunks")

        print(f"[{user_id}] done in {time.monotonic() - t_start:.1f}s\n")

    print(
        "=== TOTAL: "
        f"{totals['chunked']} rewritten, {totals['dry-run']} pending, "
        f"{totals['skip-short']} too short, {totals['skip-chunked']} already chunked, "
        f"{totals['error']} errors ==="
    )
    if args.dry_run and totals["dry-run"]:
        print("Re-run without --dry-run to write.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split existing long records into per-chunk vector points.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-u", "--user", default="", help="Reindex only this user ID")
    parser.add_argument("-d", "--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("-c", "--concurrency", type=int, default=2, help="Records at once (default 2)")
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
