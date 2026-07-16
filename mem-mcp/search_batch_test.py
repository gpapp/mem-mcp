"""
Batch test script — run a suite of diagnostic queries against fact search.
Outputs a summary table of pass/fail and scores.

Usage:
    python -m search_batch_test
    python -m search_batch_test --threshold 0.5
    python -m search_batch_test --user myuser
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from search_diagnostics import diagnose_search_facts, diagnose_search_diary, LOG_FILE


# Diverse test queries covering common failure modes
TEST_QUERIES = [
    # Exact name (should work)
    ("Alice", "exact_name"),
    ("Bob Smith", "exact_name"),

    # Semantic concept (likely fails with current system)
    ("who do I work with on AI projects", "semantic_concept"),
    ("things I need to follow up on", "semantic_concept"),
    ("my cooking hobby", "semantic_concept"),

    # Fuzzy / misspelled
    ("Alyce", "fuzzy_name"),
    ("Jonh", "fuzzy_name"),

    # Cross-cutting (multiple categories)
    ("people and projects from last month", "cross_category"),
    ("what did I discuss with the client", "cross_category"),

    # Date-bound
    ("meeting last Tuesday", "date_bound"),
    ("tasks from May 2026", "date_bound"),

    # Abstract / no single match
    ("learning goals", "abstract"),
    ("career aspirations", "abstract"),
    ("what am I passionate about", "abstract"),

    # Overloaded (many possible matches)
    ("work", "overloaded"),
    ("project", "overloaded"),

    # Negated / excludes
    ("not about meetings", "negated"),

    # Relationship traversal
    ("who introduced me to the project", "relationship"),
    ("what does Alice think about Bob", "relationship"),
]


async def run_batch(threshold: float = 0.7, user_id: str = "default"):
    results = []
    for query, category in TEST_QUERIES:
        print(f"\n{'#'*60}")
        print(f"# Testing: {query!r} (type={category})")
        print(f"{'#'*60}")

        final = await diagnose_search_facts(query, user_id, top_p=threshold, limit=3)

        top_score = final[0]["boosted_score"] if final else 0
        passed = any(r["boosted_score"] >= threshold for r in (final or []))
        results.append({
            "query": query,
            "type": category,
            "top_score": round(top_score, 4),
            "passed": passed,
        })

    # Summary
    print(f"\n\n{'='*60}")
    print(f"BATCH RESULTS — threshold={threshold}")
    print(f"{'='*60}")
    print(f"{'Query':<45} {'Type':<20} {'Top':<8} {'Pass':<5}")
    print(f"{'-'*45} {'-'*20} {'-'*8} {'-'*5}")
    for r in results:
        status = "OK" if r["passed"] else "FAIL"
        print(f"{r['query']:<45} {r['type']:<20} {r['top_score']:<8} {status:<5}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed ({passed/len(results)*100:.0f}%)")

    # Group by type
    by_type = {}
    for r in results:
        by_type.setdefault(r["type"], []).append(r)
    print(f"\n  By query type:")
    for t, items in sorted(by_type.items()):
        p = sum(1 for i in items if i["passed"])
        print(f"    {t}: {p}/{len(items)} passed")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", "-t", type=float, default=0.7)
    parser.add_argument("--user", "-u", default="default")
    args = parser.parse_args()
    asyncio.run(run_batch(args.threshold, args.user))
