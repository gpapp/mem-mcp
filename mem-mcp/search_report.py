"""
Generate a markdown report from the search diagnostic log.

Usage:
    python -m search_report > search_report.md
"""

import json
import os
import sys
from collections import Counter

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_log.jsonl")


def main():
    if not os.path.exists(LOG_FILE):
        print("No log file found. Run search_diagnostics first.")
        sys.exit(1)

    entries = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    total = len(entries)
    empty = [e for e in entries if e.get("returned_empty")]
    low = [e for e in entries if e.get("top_score", 0) < 0.5]

    # Type breakdown
    types = {}
    for e in entries:
        qi = e.get("query_info", {})
        for key in ["is_question", "is_person_query", "is_date_query", "is_concept_query", "is_broad_query", "is_specific_query"]:
            if qi.get(key):
                types.setdefault(key, []).append(e)

    print("# Search Diagnostics Report")
    print(f"\n**Generated:** {len(entries)} queries logged\n")

    print("## Summary\n")
    print(f"| Metric | Count | % |")
    print(f"|--------|-------|---|")
    print(f"| Total queries | {total} | 100% |")
    print(f"| Empty results (fail) | {len(empty)} | {len(empty)/total*100:.0f}% |")
    print(f"| Top score < 0.5 | {len(low)} | {len(low)/total*100:.0f}% |")

    print("\n## By Query Type\n")
    print("| Type | Count | Failures | Fail % |")
    print("|------|-------|----------|--------|")
    for t, matching in sorted(types.items(), key=lambda x: -len(x[1])):
        fail_count = sum(1 for e in matching if e.get("returned_empty"))
        print(f"| {t} | {len(matching)} | {fail_count} | {fail_count/len(matching)*100:.0f}% |")

    print("\n## All Failed Queries\n")
    print("| Query | Type | Top Score | Neo4j | Qdrant |")
    print("|-------|------|-----------|-------|--------|")
    for e in empty:
        qi = e.get("query_info", {})
        qtype = next((k for k in ["is_question", "is_person_query", "is_date_query", "is_concept_query", "is_broad_query", "is_specific_query"] if qi.get(k)), "?")
        print(f"| {e['query']} | {qtype} | {e.get('top_score', 0):.4f} | {e.get('neo4j_matches', '-')} | {e.get('qdrant_raw_count', '-')} |")

    print("\n## Recommended Thresholds\n")
    # Suggest thresholds based on score distribution
    scores = sorted([e.get("top_score", 0) for e in entries], reverse=True)
    if scores:
        p50 = scores[len(scores)//2]
        p25 = scores[len(scores)//4]
        print(f"- Current p50 top score: {p50:.4f}")
        print(f"- Current p25 top score: {p25:.4f}")
        print(f"- Suggested `search_facts` threshold: `{max(p50 - 0.1, 0.3):.2f}`")
        print(f"- Suggested `diary_search` threshold: `{max(p25 - 0.1, 0.2):.2f}`")

    print("\n## Query Patterns to Investigate\n")
    print("Review these manually to understand what users actually search for:")
    seen = set()
    for e in entries:
        q = e["query"]
        if q not in seen and e.get("returned_empty"):
            print(f"- `{q}`")
            seen.add(q)


if __name__ == "__main__":
    main()
