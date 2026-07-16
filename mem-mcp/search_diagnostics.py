"""
Search Diagnostics — run on the server to analyze failing queries.

Usage:
    python -m search_diagnostics "your query here"
    python -m search_diagnostics --interactive
    python -m search_diagnostics --from-log search_log.jsonl

Captures:
    - Raw query characteristics
    - Neo4j substring match results + scores
    - Qdrant raw vector scores (before boosting)
    - Qdrant boosted scores
    - Final merged results
    - Threshold pass/fail per result
"""

import asyncio
import json
import sys
import os
import time
from datetime import datetime
from typing import Optional

# Add parent dir so we can import mem-mcp modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import get_qdrant, get_neo4j, get_embedding, COLLECTION_NAME, DIARY_COLLECTION
from qdrant_client.models import Filter, FieldCondition, MatchValue
import difflib


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_log.jsonl")


def log_query(entry: dict):
    """Append a diagnostic entry to the JSONL log."""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(f"  [logged to {LOG_FILE}]")


def classify_query(query: str) -> dict:
    """Classify the query type for pattern analysis."""
    q = query.strip()
    words = q.split()
    return {
        "length": len(q),
        "word_count": len(words),
        "has_quotes": '"' in q or "'" in q,
        "is_question": q.endswith("?") or q.lower().startswith(("what", "who", "where", "when", "why", "how")),
        "is_person_query": any(w.istitle() for w in words) and len(words) <= 4,
        "is_date_query": any(c.isdigit() for c in q) and len(words) <= 5,
        "is_concept_query": not any(c.isdigit() for c in q) and len(words) >= 2,
        "is_broad_query": len(words) <= 2 and len(q) <= 15,
        "is_specific_query": len(words) >= 4,
        "lowercase": q.lower(),
    }


async def diagnose_search_facts(query: str, user_id: str, top_p: float = 0.7, limit: int = 5, category: Optional[str] = None):
    """Run fact search with full diagnostic output."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        print("ERROR: Databases not connected.")
        return

    query_info = classify_query(query)
    print(f"\n{'='*60}")
    print(f"QUERY: {query!r}")
    print(f"Classification: {json.dumps(query_info, indent=2)}")
    print(f"Threshold (top_p): {top_p}")
    print(f"Category filter: {category or 'none'}")
    print(f"{'='*60}")

    # --- Neo4j substring match ---
    print(f"\n--- NEO4J SUBSTRING MATCH ---")
    exact_matches = []
    query_lower = query.lower()

    with neo4j_driver.session() as s:
        if len(query) > 3:
            cypher = """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.name) CONTAINS toLower($query_str)
               OR (size(f.name) > 3 AND toLower($query_str) CONTAINS toLower(f.name))
               OR toLower(f.text) CONTAINS toLower($query_str)
            """
        else:
            cypher = """
            MATCH (f:Fact {userId: $userId})
            WHERE toLower(f.name) CONTAINS toLower($query_str)
            """
        if category:
            cypher += " AND toLower(f.category) = toLower($category)"
        cypher += " RETURN f LIMIT 100"

        params = {"userId": user_id, "query_str": query}
        if category:
            params["category"] = category.strip()

        neo_result = s.run(cypher, **params)
        for r in neo_result:
            f = r["f"]
            meta = {k: v for k, v in f.items() if k not in {"id", "text", "name", "category", "timestamp", "userId"}}
            score = 1.0
            name = f.get("name", "")
            if name:
                name_lower = name.lower()
                query_words = query_lower.split()
                if query_lower == name_lower:
                    score = 2.5
                elif name_lower in query_lower:
                    score = 2.0
                elif all(w in name_lower for w in query_words):
                    score = 1.7
                elif query_lower in name_lower:
                    score = 1.5
                name_words = name_lower.split()
                if len(query_words) == 1 and len(name_words) >= 1:
                    for tw in name_words:
                        s_val = difflib.SequenceMatcher(None, query_lower, tw).ratio()
                        if s_val >= 0.6:
                            score = max(score, 1.4 * s_val)
                            break
            first = (meta.get("first_name") or "").lower()
            last = (meta.get("last_name") or "").lower()
            if first and query_lower == first:
                score = max(score, 2.3)
            elif first and first in query_lower:
                score = max(score, 1.9)
            elif last and query_lower == last:
                score = max(score, 2.3)
            elif last and last in query_lower:
                score = max(score, 1.9)
            if len(query_words) >= 2:
                query_surname = query_words[-1]
                if last:
                    s_val = difflib.SequenceMatcher(None, query_surname, last).ratio()
                    if s_val >= 0.7:
                        score = max(score, 1.4 + s_val * 0.6)

            exact_matches.append({
                "id": f["id"],
                "name": f.get("name"),
                "category": f.get("category"),
                "score": score,
            })

    print(f"  Found {len(exact_matches)} Neo4j matches")
    for m in sorted(exact_matches, key=lambda x: x["score"], reverse=True)[:10]:
        print(f"    [{m['score']:.2f}] {m['name']} ({m['category']})")

    # --- Qdrant vector search ---
    print(f"\n--- QDRANT VECTOR SEARCH ---")
    vec = await get_embedding(query)
    conditions = [FieldCondition(key="userId", match=MatchValue(value=user_id))]
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category.strip().capitalize())))

    filt = Filter(must=conditions)
    fetch_limit = max(limit * 5, 50)

    t0 = time.time()
    result = await qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=vec,
        query_filter=filt,
        limit=fetch_limit,
        score_threshold=0.0,  # Use 0.0 to see ALL results, we'll apply threshold later
    )
    elapsed = time.time() - t0
    print(f"  Qdrant returned {len(result.points)} results in {elapsed:.3f}s")

    # Show raw scores
    print(f"\n  Raw Qdrant scores (before boosting):")
    raw_scores = []
    for r in result.points[:20]:
        name = r.payload.get("name", "?")
        raw_scores.append({"id": r.id, "name": name, "raw_score": r.score})
        print(f"    [{r.score:.4f}] {name}")

    # Apply boosting
    boosted_results = []
    for r in result.points:
        score = r.score
        metadata = r.payload.get("metadata", {})
        name = r.payload.get("name")
        aliases = metadata.get("aliases", {})

        if name:
            name_lower = name.lower()
            query_words = query_lower.split()
            if query_lower == name_lower:
                score += 1.0
            elif name_lower in query_lower:
                score += 0.5
            elif all(w in name_lower for w in query_words):
                score += 0.4
            elif query_lower in name_lower:
                score += 0.2
            name_words = name_lower.split()
            if len(query_words) == 1 and len(name_words) >= 1:
                for tw in name_words:
                    s_val = difflib.SequenceMatcher(None, query_lower, tw).ratio()
                    if s_val >= 0.6:
                        score += 0.35 * s_val
                        break
            first = (metadata.get("first_name") or "").lower()
            last = (metadata.get("last_name") or "").lower()
            if first:
                s_val = difflib.SequenceMatcher(None, query_lower, first).ratio()
                if query_lower == first:
                    score += 0.8
                elif first in query_lower:
                    score += 0.4
                elif s_val >= 0.7:
                    score += s_val * 0.6
            if last:
                s_val = difflib.SequenceMatcher(None, query_lower, last).ratio()
                if query_lower == last:
                    score += 0.8
                elif last in query_lower:
                    score += 0.4
                elif s_val >= 0.7:
                    score += s_val * 0.6
            if name:
                name_norm = name.lower().strip()
                s_val = difflib.SequenceMatcher(None, query_lower, name_norm).ratio()
                if s_val >= 0.7:
                    score += s_val * 0.8
                name_words = name_norm.split()
                if len(name_words) >= 2:
                    surname = name_words[-1]
                    query_words_l = query_lower.split()
                    if len(query_words_l) >= 2:
                        query_surname = query_words_l[-1]
                        s_surname = difflib.SequenceMatcher(None, query_surname, surname).ratio()
                        if s_surname >= 0.7:
                            score += s_surname * 0.8
            if aliases and isinstance(aliases, dict):
                matched_query_words = set()
                best_ratio = 0
                for alias, confidence in aliases.items():
                    alias_words = alias.lower().split()
                    for qw in query_words:
                        if qw in alias.lower():
                            continue
                        for aw in alias_words:
                            ratio = difflib.SequenceMatcher(None, qw, aw).ratio()
                            if ratio >= 0.6 and ratio > best_ratio:
                                best_ratio = ratio
                                matched_query_words.add(qw)
                    if query_lower == alias.lower():
                        try: score += (float(confidence) * 0.2)
                        except: pass
                    elif query_lower in alias.lower() or alias.lower() in query_lower:
                        try: score += (float(confidence) * 0.05)
                        except: pass
                if matched_query_words:
                    score += 0.1 * best_ratio

        boosted_results.append({
            "id": r.id,
            "name": r.payload.get("name"),
            "category": r.payload.get("category"),
            "raw_score": r.score,
            "boosted_score": score,
            "above_threshold": score >= top_p,
        })

    # Sort by boosted score
    boosted_results.sort(key=lambda x: x["boosted_score"], reverse=True)

    above = [r for r in boosted_results if r["above_threshold"]]
    below = [r for r in boosted_results if not r["above_threshold"]]

    print(f"\n  Boosted results ABOVE threshold ({top_p}): {len(above)}")
    for r in above[:10]:
        delta = r["boosted_score"] - r["raw_score"]
        print(f"    [{r['boosted_score']:.4f}] (raw={r['raw_score']:.4f}, boost=+{delta:.4f}) {r['name']}")

    print(f"\n  Boosted results BELOW threshold ({top_p}): {len(below)}")
    for r in below[:10]:
        delta = r["boosted_score"] - r["raw_score"]
        print(f"    [{r['boosted_score']:.4f}] (raw={r['raw_score']:.4f}, boost=+{delta:.4f}) {r['name']}")

    # --- Merge ---
    merged = {}
    for r in boosted_results:
        merged[r["id"]] = r
    for m in exact_matches:
        if m["id"] in merged:
            merged[m["id"]]["boosted_score"] = max(merged[m["id"]]["boosted_score"], m["score"])
        else:
            merged[m["id"]] = {"id": m["id"], "name": m["name"], "category": m["category"],
                               "raw_score": 0, "boosted_score": m["score"], "above_threshold": m["score"] >= top_p}

    final = sorted(merged.values(), key=lambda x: x["boosted_score"], reverse=True)
    if category:
        final = [r for r in final if (r.get("category") or "").lower() == category.lower()]

    print(f"\n--- FINAL MERGED RESULTS (top {limit}) ---")
    for i, r in enumerate(final[:limit]):
        status = "PASS" if r["boosted_score"] >= top_p else "FAIL"
        print(f"  {i+1}. [{r['boosted_score']:.4f}] [{status}] {r.get('name', '?')}")

    if not final or final[0]["boosted_score"] < top_p:
        print(f"\n  !! TOP RESULT BELOW THRESHOLD — search would return empty or weak results")

    # Log
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "query_info": query_info,
        "top_p": top_p,
        "category": category,
        "neo4j_matches": len(exact_matches),
        "qdrant_raw_count": len(result.points),
        "qdrant_above_threshold": len(above),
        "final_results": len(final[:limit]),
        "top_score": final[0]["boosted_score"] if final else 0,
        "returned_empty": not any(r["boosted_score"] >= top_p for r in final),
        "results_preview": [{"name": r.get("name"), "score": r["boosted_score"]} for r in final[:5]],
    }
    log_query(log_entry)

    return final


async def diagnose_search_diary(query: str, user_id: str, top_p: float = 0.4, limit: int = 3):
    """Run diary search with full diagnostic output."""
    qdrant = await get_qdrant()
    if not qdrant:
        print("ERROR: Qdrant not connected.")
        return

    query_info = classify_query(query)
    print(f"\n{'='*60}")
    print(f"DIARY QUERY: {query!r}")
    print(f"Classification: {json.dumps(query_info, indent=2)}")
    print(f"Threshold (top_p): {top_p}")
    print(f"{'='*60}")

    vec = await get_embedding(query)
    filt = Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))])

    t0 = time.time()
    result = await qdrant.query_points(
        collection_name=DIARY_COLLECTION,
        query=vec,
        query_filter=filt,
        limit=limit * 5,  # fetch more to see full picture
        with_payload=True,
        score_threshold=0.0,
    )
    elapsed = time.time() - t0
    print(f"\n  Qdrant returned {len(result.points)} results in {elapsed:.3f}s")

    entries = []
    query_lower = query.lower()
    for r in result.points:
        score = r.score
        name = r.payload.get("name")

        if name:
            if query_lower == name.lower():
                score += 0.5
            elif query_lower in name.lower() or name.lower() in query_lower:
                score += 0.2

        entries.append({
            "id": r.id,
            "name": name,
            "date": r.payload.get("date"),
            "raw_score": r.score,
            "boosted_score": score,
            "above_threshold": score >= top_p,
        })

    entries.sort(key=lambda x: x["boosted_score"], reverse=True)

    above = [e for e in entries if e["above_threshold"]]
    below = [e for e in entries if not e["above_threshold"]]

    print(f"\n  Results ABOVE threshold ({top_p}): {len(above)}")
    for e in above[:10]:
        print(f"    [{e['boosted_score']:.4f}] {e['name']} ({e['date']})")

    print(f"\n  Results BELOW threshold ({top_p}): {len(below)}")
    for e in below[:10]:
        print(f"    [{e['boosted_score']:.4f}] {e['name']} ({e['date']})")

    if not above:
        print(f"\n  !! NO RESULTS ABOVE THRESHOLD")

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "query_info": query_info,
        "top_p": top_p,
        "type": "diary",
        "qdrant_raw_count": len(result.points),
        "above_threshold": len(above),
        "top_score": entries[0]["boosted_score"] if entries else 0,
        "returned_empty": not above,
        "results_preview": [{"name": e["name"], "date": e["date"], "score": e["boosted_score"]} for e in entries[:5]],
    }
    log_query(log_entry)

    return entries


def analyze_log():
    """Analyze the search log to find patterns in failing queries."""
    if not os.path.exists(LOG_FILE):
        print(f"No log file found at {LOG_FILE}")
        return

    entries = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    if not entries:
        print("Log is empty.")
        return

    total = len(entries)
    empty = [e for e in entries if e.get("returned_empty")]
    low_score = [e for e in entries if e.get("top_score", 0) < 0.5]

    print(f"\n{'='*60}")
    print(f"SEARCH LOG ANALYSIS — {total} queries logged")
    print(f"{'='*60}")
    print(f"  Queries returning empty/no results: {len(empty)} ({len(empty)/total*100:.0f}%)")
    print(f"  Queries with top score < 0.5:       {len(low_score)} ({len(low_score)/total*100:.0f}%)")

    # Query type breakdown
    types = {}
    for e in entries:
        qi = e.get("query_info", {})
        for key in ["is_question", "is_person_query", "is_date_query", "is_concept_query", "is_broad_query", "is_specific_query"]:
            if qi.get(key):
                types.setdefault(key, []).append(e)

    print(f"\n  By query type:")
    for t, matching in sorted(types.items(), key=lambda x: -len(x[1])):
        fail_count = sum(1 for e in matching if e.get("returned_empty"))
        print(f"    {t}: {len(matching)} queries, {fail_count} failures ({fail_count/len(matching)*100:.0f}%)")

    # Show worst failures
    if empty:
        print(f"\n  FAILED QUERIES (returning empty):")
        for e in empty[:20]:
            print(f"    \"{e['query']}\" (top_score={e.get('top_score', 0):.4f}, "
                  f"neo4j={e.get('neo4j_matches', '?')}, qdrant={e.get('qdrant_raw_count', '?')})")


async def inspect_collections():
    """Inspect Qdrant collections: point counts, unique user IDs, sample data."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant:
        print("ERROR: Qdrant not connected.")
        return

    for coll_name in [COLLECTION_NAME, DIARY_COLLECTION]:
        print(f"\n{'='*60}")
        print(f"COLLECTION: {coll_name}")
        print(f"{'='*60}")

        try:
            info = await qdrant.get_collection(coll_name)
            print(f"  Points count: {info.points_count}")
            print(f"  Vectors size: {info.config.params.vectors.size}")
            print(f"  Status: {info.status}")
        except Exception as e:
            print(f"  ERROR getting collection info: {e}")
            continue

        if info.points_count == 0:
            print(f"  ** COLLECTION IS EMPTY **")
            continue

        # Sample points to find user IDs
        print(f"\n  Sampling points to find user IDs...")
        try:
            samples = await qdrant.scroll(
                collection_name=coll_name,
                limit=20,
                with_payload=True,
            )
            user_ids = set()
            for point in samples[0]:
                uid = point.payload.get("userId", "MISSING")
                user_ids.add(uid)

            print(f"  Unique userIds found in sample: {user_ids}")

            # Show sample payloads
            print(f"\n  Sample payloads:")
            for i, point in enumerate(samples[0][:5]):
                name = point.payload.get("name", "?")
                cat = point.payload.get("category", "?")
                date = point.payload.get("date", "?")
                uid = point.payload.get("userId", "?")
                text = (point.payload.get("text") or "")[:80]
                print(f"    [{i}] id={point.id}")
                print(f"        userId={uid}")
                print(f"        name={name}")
                if coll_name == DIARY_COLLECTION:
                    print(f"        date={date}")
                else:
                    print(f"        category={cat}")
                    print(f"        text={text}...")
        except Exception as e:
            print(f"  ERROR scrolling collection: {e}")

    # Also check Neo4j fact counts
    if neo4j_driver:
        print(f"\n{'='*60}")
        print(f"NEO4J FACTS")
        print(f"{'='*60}")
        with neo4j_driver.session() as s:
            result = s.run("MATCH (f:Fact) RETURN f.userId as uid, count(f) as cnt ORDER BY cnt DESC")
            for r in result:
                print(f"  userId={r['uid']}: {r['cnt']} facts")

        print(f"\n{'='*60}")
        print(f"NEO4J DIARY ENTRIES")
        print(f"{'='*60}")
        with neo4j_driver.session() as s:
            result = s.run("MATCH (d:DiaryEntry) RETURN d.userId as uid, count(d) as cnt ORDER BY cnt DESC")
            for r in result:
                print(f"  userId={r['uid']}: {r['cnt']} entries")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Search diagnostics")
    parser.add_argument("query", nargs="?", help="Query to test")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--from-log", action="store_true", help="Analyze existing log file")
    parser.add_argument("--diary", action="store_true", help="Test diary search instead of facts")
    parser.add_argument("--threshold", "-t", type=float, default=0.5, help="Override top_p threshold (default 0.5)")
    parser.add_argument("--user", "-u", default="default", help="User ID")
    parser.add_argument("--inspect", action="store_true", help="Inspect collection contents and user IDs")
    args = parser.parse_args()

    if args.inspect:
        await inspect_collections()
        return

    if args.from_log:
        analyze_log()
        return

    if args.interactive:
        print("Search Diagnostics — Interactive Mode")
        print("Type a query to test, 'quit' to exit, 'analyze' to review log.\n")
        while True:
            try:
                q = input("query> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q == "quit":
                break
            if q == "analyze":
                analyze_log()
                continue
            if not q:
                continue
            if args.diary:
                await diagnose_search_diary(q, args.user, top_p=args.threshold or 0.4)
            else:
                await diagnose_search_facts(q, args.user, top_p=args.threshold or 0.5)
        return

    if args.query:
        if args.diary:
            await diagnose_search_diary(args.query, args.user, top_p=args.threshold or 0.4)
        else:
            await diagnose_search_facts(args.query, args.user, top_p=args.threshold or 0.5)
        return

    parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
