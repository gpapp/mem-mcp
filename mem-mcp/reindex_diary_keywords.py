"""
reindex_diary_keywords.py – Backfill keyword extraction for existing diary entries.

Usage:
    python reindex_diary_keywords.py [OPTIONS]

Options:
    -u / --user USER_ID     Reindex only this user (default: all users)
    -f / --force            Re-extract even if keywords already exist
    -d / --dry-run          Show what would be processed, make no changes
    -c / --concurrency N    Max parallel LLM calls (default: 3)
    --env FILE              Load environment from .env file (default: .env)

Examples:
    python reindex_diary_keywords.py
    python reindex_diary_keywords.py -u memories --force
    python reindex_diary_keywords.py --dry-run
"""

import asyncio
import argparse
import os
import sys
import time
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load .env before importing common/diary_manager
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
# Helpers
# ---------------------------------------------------------------------------

async def _get_all_users(neo4j_driver) -> list:
    """Return every user_id that has at least one DiaryEntry in Neo4j."""
    with neo4j_driver.session() as s:
        res = s.run("MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS uid")
        return [r["uid"] for r in res if r["uid"]]


async def _fetch_entries_neo4j(neo4j_driver, user_id: str) -> list:
    """Fetch id, name, content, timestamp for all diary entries of a user."""
    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            RETURN d.id AS id, d.name AS name, d.content AS content,
                   d.timestamp AS timestamp, d.keywords AS keywords
            ORDER BY d.timestamp ASC
            """,
            userId=user_id,
        )
        return [dict(r) for r in res]


async def _fetch_qdrant_payload(qdrant, entry_id: str, collection: str) -> dict:
    """Fetch the current Qdrant payload for a single point."""
    from qdrant_client.models import PointIdsList
    result = await qdrant.retrieve(
        collection_name=collection,
        ids=[entry_id],
        with_payload=True,
        with_vectors=False,
    )
    if result:
        return result[0].payload
    return {}


async def _patch_entry(qdrant, neo4j_driver, entry: dict, keywords: list, collection: str, dry_run: bool) -> None:
    """Write extracted keywords back to Qdrant payload and Neo4j node."""
    if dry_run:
        return

    entry_id = entry["id"]

    # --- Qdrant: fetch existing payload, merge keywords, upsert ---
    existing_payload = await _fetch_qdrant_payload(qdrant, entry_id, collection)
    if existing_payload:
        updated_payload = dict(existing_payload)
        updated_payload["keywords"] = keywords
        from qdrant_client.models import SetPayload
        await qdrant.set_payload(
            collection_name=collection,
            payload={"keywords": keywords},
            points=[entry_id],
        )

    # --- Neo4j: set d.keywords ---
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (d:DiaryEntry {id: $id}) SET d.keywords = $keywords",
            id=entry_id,
            keywords=keywords,
        )


async def _process_entry(sem: asyncio.Semaphore, qdrant, neo4j_driver,
                         entry: dict, collection: str,
                         force: bool, dry_run: bool) -> dict:
    """Process a single entry: extract keywords and patch storage."""
    from diary_manager import extract_diary_keywords

    existing_kws = entry.get("keywords")
    has_keywords = bool(existing_kws)

    if has_keywords and not force:
        return {"id": entry["id"], "status": "skipped", "keywords": existing_kws}

    name = entry.get("name") or ""
    content = entry.get("content") or ""

    async with sem:
        t0 = time.monotonic()
        keywords = await extract_diary_keywords(name, content)
        elapsed = time.monotonic() - t0

    if not dry_run:
        await _patch_entry(qdrant, neo4j_driver, entry, keywords, collection, dry_run)

    return {
        "id": entry["id"],
        "status": "extracted" if not dry_run else "dry-run",
        "keywords": keywords,
        "elapsed": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    _load_env(args.env)

    # Import after env is loaded
    from common import get_qdrant, get_neo4j, wait_for_service, OLLAMA_URL, DIARY_COLLECTION

    print(f"Connecting to services…")
    if not wait_for_service(OLLAMA_URL, label="Ollama"):
        print("ERROR: Ollama is not reachable. Make sure the stack is running.", file=sys.stderr)
        sys.exit(1)

    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        print("ERROR: Could not connect to Qdrant or Neo4j.", file=sys.stderr)
        sys.exit(1)

    # Determine users to process
    if args.user:
        users = [args.user]
    else:
        users = await _get_all_users(neo4j_driver)
        if not users:
            print("No diary entries found.")
            return

    print(f"Users to process: {users}")
    if args.dry_run:
        print("DRY RUN — no changes will be written.")
    print()

    sem = asyncio.Semaphore(args.concurrency)
    grand_total = grand_extracted = grand_skipped = 0

    for user_id in users:
        entries = await _fetch_entries_neo4j(neo4j_driver, user_id)
        total = len(entries)
        print(f"[{user_id}] {total} entries found")

        if total == 0:
            continue

        to_process = [e for e in entries if not e.get("keywords") or args.force]
        will_skip = total - len(to_process)
        print(f"[{user_id}] {len(to_process)} to extract  |  {will_skip} already tagged (use --force to redo)")

        tasks = [
            _process_entry(sem, qdrant, neo4j_driver, entry, DIARY_COLLECTION, args.force, args.dry_run)
            for entry in entries
        ]

        extracted = 0
        skipped = 0
        errors = 0
        t_start = time.monotonic()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, res in enumerate(results):
            entry = entries[i]
            ts = entry.get("timestamp", "")[:10]
            name = (entry.get("name") or "")[:60]

            if isinstance(res, Exception):
                print(f"  ERROR  {ts}  {name!r}  — {res}")
                errors += 1
            elif res["status"] == "skipped":
                skipped += 1
            else:
                kw_str = ", ".join(res.get("keywords") or [])
                elapsed = res.get("elapsed", 0)
                print(f"  {'DRY ' if args.dry_run else ''}OK  {ts}  {name!r}  [{elapsed}s]  → {kw_str}")
                extracted += 1

        elapsed_total = time.monotonic() - t_start
        print(f"[{user_id}] Done: {extracted} extracted, {skipped} skipped, {errors} errors  ({elapsed_total:.1f}s)\n")
        grand_total += total
        grand_extracted += extracted
        grand_skipped += skipped

    print(f"=== TOTAL: {grand_total} entries  |  {grand_extracted} extracted  |  {grand_skipped} skipped ===")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill LLM keyword extraction for existing diary entries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-u", "--user", default="", help="Reindex only this user ID")
    parser.add_argument("-f", "--force", action="store_true", help="Re-extract even if keywords exist")
    parser.add_argument("-d", "--dry-run", action="store_true", help="Show plan without writing changes")
    parser.add_argument("-c", "--concurrency", type=int, default=3, help="Parallel LLM calls (default 3)")
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
