"""
diary_manager.py – Diary entry management, search, and automatic link generation.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

import re
from common import (
    get_qdrant, get_neo4j, logger, get_embedding, get_llm_response, publish_db_event,
    DIARY_COLLECTION, QDRANT_URL, clean_extracted_people_names, EXTRACT_MODEL
)
from matching_utils import (
    parse_people_name_array,
    resolve_people_candidates,
    scope_axis_tier,
    scope_context_tier,
    scope_strength,
    text_windows,
)
from chunking import (
    CHUNK_FETCH_MULTIPLIER,
    build_chunk_payloads,
    chunk_text_of,
    normalize_text,
    parent_of,
    strip_chunk_meta,
)
from client_manager import (
    link_diary_to_client, link_diary_to_context,
    _resolve_client_by_id, _resolve_context_by_id, _stamp_manual_scope,
    infer_scope_from_text, db_get_client_status_map, db_plan_search_scope,
    INFERRED_SCOPE_BOOST, INACTIVE_PENALTY
)

# ---------------------------------------------------------------------------
# Diary helpers
# ---------------------------------------------------------------------------

def _diary_id(user_id: str, timestamp: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"diary_{user_id}_{timestamp}"))


# ---------------------------------------------------------------------------
# Chunked vector writes
#
# Same rationale as facts: one vector over a long diary entry is a lossy
# average, so a long entry gets one point per chunk. Chunk 0 keeps the entry
# id, so short entries are stored exactly as before.
# ---------------------------------------------------------------------------
async def _upsert_diary_points(qdrant, entry_id: str, content: str, base_payload: dict,
                                prefix: Optional[str] = None, replace: bool = False) -> int:
    """Write one point, or N when the entry needs chunking. Returns the count."""
    parts = build_chunk_payloads(entry_id, content, strip_chunk_meta(base_payload))
    if len(parts) == 1:
        vector = await get_embedding(f"{prefix}: {content}" if prefix else content)
        if replace:
            await _delete_diary_chunks(entry_id)
        await qdrant.upsert(
            collection_name=DIARY_COLLECTION,
            points=[PointStruct(id=entry_id, vector=vector, payload=parts[0]["payload"])],
        )
        return 1

    points = []
    for part in parts:
        body = chunk_text_of(part["payload"])
        vector = await get_embedding(f"{prefix}: {body}" if prefix else body)
        points.append(PointStruct(id=part["id"], vector=vector, payload=part["payload"]))

    if replace:
        await _delete_diary_chunks(entry_id)
    await qdrant.upsert(collection_name=DIARY_COLLECTION, points=points)
    logger.info(f"[chunking] diary {entry_id}: {len(points)} chunk points written")
    return len(points)


async def find_diary_chunk_family(qdrant, entry_id: str) -> list:
    """Every Qdrant point id for a diary entry, chunked or not."""
    ids = [str(entry_id)]
    offset = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=DIARY_COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="parentId", match=MatchValue(value=str(entry_id)))]),
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for point in points:
            pid = str(point.id)
            if pid not in ids:
                ids.append(pid)
        if offset is None:
            break
    return ids


async def _delete_diary_chunks(entry_id: str) -> None:
    """Delete every point for a diary entry via raw HTTP (avoids shard_key: null)."""
    qdrant = await get_qdrant()
    family = await find_diary_chunk_family(qdrant, entry_id)
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{QDRANT_URL}/collections/{DIARY_COLLECTION}/points/delete",
            json={"points": family},
            params={"wait": "true"},
        )


_KEYWORD_EXTRACT_SYSTEM = (
    "You are a keyword extractor. Given a diary entry, extract the most important "
    "searchable keywords and short phrases (people, places, projects, topics, events). "
    "Return ONLY a JSON object: {\"keywords\": [\"kw1\", \"kw2\", ...]}. "
    "Max 10 items, lowercase, 1-3 words each, no generic words like 'meeting' or 'today'."
)

# The old implementation sent text[:1500], so a 40k transcription got keywords
# from its opening only and a query about anything in the last thirty pages
# scored as though the entry had never mentioned it. The window is a budget, not
# a slice that could cut the only mention of a project in half.
KEYWORD_EXTRACT_WINDOW = max(1000, int(os.getenv("MEM_KEYWORD_WINDOW", "8000")))
KEYWORD_EXTRACT_OVERLAP = max(0, min(1000, int(os.getenv("MEM_KEYWORD_OVERLAP", "500"))))
# A warning about cost, deliberately not a limit: a cap would reintroduce the
# exact silent tail-drop the windowing replaced.
KEYWORD_EXTRACT_WARN_WINDOWS = 6
# Cap on the union. Keywords are a *boost* (any match adds a fixed amount and
# then breaks), so a longer list widens recall and slightly widens the set of
# things that can spuriously match. Bounded so the payload stays small -- it is
# duplicated onto every chunk of the entry.
KEYWORD_LIMIT = max(5, int(os.getenv("MEM_KEYWORD_LIMIT", "20")))


def _clean_keywords(values, limit: int = 0) -> list:
    """Lowercase, strip, dedupe case-insensitively, keep first-seen order.

    Order matters: the per-window prompt is asked for the *most important*
    keywords first, so truncating to the limit keeps the best ones rather than
    an arbitrary slice of a set.
    """
    seen = set()
    out = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        keyword = value.strip().lower()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        out.append(keyword)
    return out[:limit] if limit else out


async def extract_diary_keywords(name: str, content: str) -> list:
    """Use the query LLM to extract searchable keywords from a diary entry.

    Runs once per window (see text_windows) and unions the results, so keywords
    come from the whole entry rather than its first paragraph.

    Returns a list of lowercase keyword strings, capped at KEYWORD_LIMIT.
    Falls back to an empty list on any error so saves are never blocked.
    """
    windows = text_windows(content, KEYWORD_EXTRACT_WINDOW, KEYWORD_EXTRACT_OVERLAP)
    logger.debug(
        f"[extract_diary_keywords] started (content_len={len(content)}, windows={len(windows)})"
    )
    if len(windows) > KEYWORD_EXTRACT_WARN_WINDOWS:
        logger.warning(
            f"[extract_diary_keywords] entry is {len(content)} chars, so keyword extraction "
            f"runs {len(windows)} times — raise MEM_KEYWORD_WINDOW to trade recall for cost"
        )
    found = []
    for index, window in enumerate(windows):
        # The entry name goes on every window, not just the first: a window from
        # the middle of a long entry has no other way to know which entry it is
        # a fragment of, and the name is one of the better keywords anyway.
        text = f"{name}\n{window}" if name else window
        try:
            raw = await get_llm_response(text, system=_KEYWORD_EXTRACT_SYSTEM,
                                         model=EXTRACT_MODEL)
            raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
            json_match = re.search(r'\{[^{}]*"keywords"[^{}]*\}', raw, re.DOTALL)
            if not json_match:
                logger.debug(
                    f"[extract_diary_keywords] window {index + 1}/{len(windows)} had no JSON object"
                )
                continue
            data = json.loads(json_match.group())
            found.extend(_clean_keywords(data.get("keywords", [])))
        except Exception as exc:
            # One bad window must not discard the keywords the others found.
            logger.debug(
                f"[extract_diary_keywords] window {index + 1}/{len(windows)} failed: {exc}"
            )
    keywords = _clean_keywords(found, KEYWORD_LIMIT)
    logger.debug(f"[extract_diary_keywords] extracted: {keywords}")
    return keywords


# ---------------------------------------------------------------------------
# People auto-linking: extract person names from diary content and create
# MENTIONS edges to matching People facts.
# ---------------------------------------------------------------------------
_PEOPLE_EXTRACT_SYSTEM = (
    "You are a named-entity extractor. Extract the full names of every person "
    "explicitly mentioned in the text. "
    "Return ONLY a JSON array of strings, e.g. [\"Alice Smith\", \"Bob Jones\"]. "
    "Return [] if no people are mentioned. Never add explanations. "
    "Ignore speaker labels such as SPEAKER1, SPEAKER 2, and SPEAKER#3; they are not names. "
    "IMPORTANT: ignore anything in parentheses — it is a role or description, not part of the name. "
    "For example, 'Alice Smith (host)' → extract only 'Alice Smith'."
)

# Name extraction runs per window, so these control both the coverage of a long
# entry and the cost of scanning it. The window must fit the extractor's context
# or Ollama rejects the length; the overlap catches a name split across a
# boundary, which both halves would otherwise drop.
PEOPLE_EXTRACT_WINDOW = max(1000, int(os.getenv("MEM_PEOPLE_WINDOW", "6000")))
PEOPLE_EXTRACT_OVERLAP = max(0, min(1000, int(os.getenv("MEM_PEOPLE_OVERLAP", "600"))))
# Above this many windows an entry is worth telling the operator about: each
# window is its own LLM call. This warns, it does not truncate.
PEOPLE_EXTRACT_WARN_WINDOWS = 6

# Output budget for ONE window's JSON array. It is a budget, not a formality:
# a dense 6000-char window can easily hold thirty-odd names, and at 200 tokens
# the model hit the cap mid-array on a real 30k-char entry. The parse then
# found no JSON object, the window was skipped, and the names it had already
# emitted were lost with no error anywhere -- the silent-recall-loss shape this
# windowing exists to prevent. Raising it costs generation time only when a
# window really is that dense; a sparse window stops well short of it.
# Sizing rule of thumb: a name is ~4 tokens in JSON, so 800 covers ~200 names.
PEOPLE_EXTRACT_MAX_TOKENS = max(
    200, int(os.getenv("MEM_PEOPLE_EXTRACT_TOKENS") or 800)
)


def people_extract_windows(content: str, window: int = 0, overlap: int = 0) -> list:
    """Split diary content into overlapping windows for name extraction.

    The old code sent ``content[:2000]`` — one call, and only the opening of the
    entry. On a 40k-char transcription every person named after character 2000
    was silently missed, so their MENTIONS edge was never created. Splitting
    into overlapping windows is the fix; the overlap is what stops a name that
    straddles a boundary from being cut in half and lost by both halves.

    Windows are returned in order and a doc that fits the window is a single
    element, so short entries cost exactly what they always did.
    """
    size = window or PEOPLE_EXTRACT_WINDOW
    step = size - min(overlap or PEOPLE_EXTRACT_OVERLAP, size - 1)
    body = normalize_text(content or "")
    if not body.strip():
        return []
    if len(body) <= size:
        return [body]
    return [body[i:i + size] for i in range(0, len(body), step)]


async def _extract_people_names(content: str) -> list:
    """Return a list of person name strings extracted from diary content via LLM.

    Runs once per window (see people_extract_windows) and unions the results.
    Every name in the entry has to be seen by the extractor, not just the ones
    that happen to fall in the first paragraph.
    """
    windows = people_extract_windows(content)
    logger.debug(
        f"[extract_people_names] started (content_len={len(content)}, windows={len(windows)})"
    )
    if len(windows) > PEOPLE_EXTRACT_WARN_WINDOWS:
        # Every window is a separate LLM round trip, so a very long entry is
        # genuinely expensive. Warn rather than cap: capping would silently drop
        # the tail, which is the exact defect the windowing replaced.
        logger.warning(
            f"[extract_people_names] entry is {len(content)} chars, so name extraction "
            f"runs {len(windows)} times — raise MEM_PEOPLE_WINDOW to trade recall for cost"
        )
    found = []
    for index, window in enumerate(windows):
        try:
            raw = await get_llm_response(window, system=_PEOPLE_EXTRACT_SYSTEM,
                                         model=EXTRACT_MODEL,
                                         num_predict=PEOPLE_EXTRACT_MAX_TOKENS)
            names = parse_people_name_array(raw)
            if names is None:
                logger.debug(
                    f"[extract_people_names] window {index + 1}/{len(windows)} had no JSON array"
                )
                continue
            found.extend(clean_extracted_people_names(names))
        except Exception as exc:
            # One bad window must not discard the names the other windows found.
            logger.debug(f"[extract_people_names] window {index + 1}/{len(windows)} failed: {exc}")

    # clean_extracted_people_names already dedupes within a call, but each
    # window is a separate call and a name can span the overlap, so dedupe
    # across the union too.
    extracted = clean_extracted_people_names(found)
    logger.debug(f"[extract_people_names] completed (count={len(extracted)})")
    return extracted


async def find_people_candidates(entry_id: str, content: str, user_id: str) -> list:
    """Extract person names from content and return matching People facts as candidates.

    Returns a list of dicts: {id, name, text, score, already_linked}.
    Does NOT create any edges.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver or not content:
        return []
    names = await _extract_people_names(content)
    if not names:
        return []
    from fact_manager import db_find_people_matches

    candidates = []
    people_matches = await db_find_people_matches(names, user_id)
    with neo4j_driver.session() as s:
        for person in people_matches:
            linked = s.run(
                    """
                    MATCH (f:Fact {id: $fid, userId: $userId})
                    MATCH (d:DiaryEntry {id: $did, userId: $userId})
                    RETURN EXISTS((d)-[:MENTIONS]->(f)) AS already_linked
                    """,
                    userId=user_id, did=entry_id, fid=person["id"],
            ).single()
            candidates.append({
                "id": person["id"],
                "name": person["name"],
                "text": (person.get("text") or "")[:120],
                "score": person.get("score", 0),
                "already_linked": bool(linked and linked["already_linked"]),
            })
    # Deduplicate by fact id
    seen = set()
    unique = []
    for c in candidates:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)
    return sorted(unique, key=lambda c: (-c.get("score", 0), (c.get("name") or "").casefold()))


async def _auto_link_people(entry_id: str, content: str, user_id: str,
                             fact_ids: Optional[list] = None) -> int:
    """Reconcile automatically detected People links for a diary entry.

    If fact_ids is provided, link exactly those facts (skipping LLM extraction).
    Otherwise extract names from content and link resolved People facts.
    Returns count of new edges created.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return 0
    if fact_ids is None:
        # Auto mode: extract names, resolve candidates, and remove stale auto links.
        names = await _extract_people_names(content) if content else []
        created = 0
        from fact_manager import db_find_people_matches

        people_matches = await db_find_people_matches(names, user_id)
        people_matches = await resolve_people_candidates(
            names, content, people_matches, get_llm_response
        )
        person_ids = [person["id"] for person in people_matches]
        with neo4j_driver.session() as s:
            s.run(
                """
                MATCH (d:DiaryEntry {id: $did, userId: $userId})-[r:MENTIONS]->(f:Fact)
                WHERE coalesce(r.source, 'manual') = 'auto'
                  AND NOT f.id IN $personIds
                DELETE r
                """,
                did=entry_id, userId=user_id, personIds=person_ids,
            )
            for person in people_matches:
                result = s.run(
                        """
                        MATCH (d:DiaryEntry {id: $did, userId: $userId})
                        MATCH (f:Fact {id: $fid, userId: $userId})
                        WHERE NOT (d)-[:MENTIONS]->(f)
                        MERGE (d)-[r:MENTIONS]->(f)
                        SET r.source = 'auto'
                        RETURN count(f) AS n
                        """,
                        did=entry_id, userId=user_id, fid=person["id"],
                ).single()
                if result:
                    created += result["n"]
    else:
        # Explicit mode: link only the given fact IDs
        created = 0
        with neo4j_driver.session() as s:
            for fid in fact_ids:
                result = s.run(
                    """
                    MATCH (d:DiaryEntry {id: $did, userId: $userId})
                    MATCH (f:Fact {id: $fid, userId: $userId})
                    WHERE NOT (d)-[:MENTIONS]->(f)
                    MERGE (d)-[r:MENTIONS]->(f)
                    SET r.source = 'manual'
                    RETURN count(f) AS n
                    """,
                    did=entry_id, userId=user_id, fid=fid
                )
                row = result.single()
                if row:
                    created += row["n"]
    if created:
        logger.debug(f"[auto_link_people] diary {entry_id}: linked {created} new People fact(s)")
    return created


async def db_save_diary(content: str, user_id: str, timestamp: str, name: str, metadata: Optional[dict] = None, linked_facts: Optional[list] = None, client_id: Optional[str] = None, context_id: Optional[str] = None) -> str:
    """Upsert a diary entry keyed by user + timestamp. Returns the ISO timestamp string.
    Optional linked_facts is a list of fact IDs to create MENTIONS relationships.
    Optional client_id and context_id link the entry to a Client and/or Context.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # Derive a stable ID from user + timestamp so the same timestamp always maps to the same node
    doc_id   = _diary_id(user_id, timestamp)
    # Keep a plain date string for display / grouping purposes
    entry_date = timestamp[:10]

    # Extract keywords asynchronously — non-blocking; empty list on failure
    keywords = await extract_diary_keywords(name or "", content)

    payload = {"content": content, "name": name, "date": entry_date, "timestamp": timestamp, "userId": user_id}
    if keywords:
        payload["keywords"] = keywords
        if not metadata:
            metadata = {}
        metadata["keywords"] = ", ".join(keywords)
    if metadata:
        payload["metadata"] = metadata

    # Add client/context info to payload if provided
    if client_id:
        client_info = _resolve_client_by_id(client_id, user_id)
        if client_info:
            payload["clientId"] = client_id
            payload["clientName"] = client_info["name"]
    if context_id:
        ctx_info = _resolve_context_by_id(context_id, user_id)
        if ctx_info:
            payload["contextId"] = context_id
            payload["contextName"] = ctx_info["name"]

    # Qdrant — upsert by the stable doc_id so re-saving replaces the vector.
    # Long entries are split into chunk points; embedding happens inside here and
    # before the Neo4j MERGE below, so a failed embed leaves no entry behind.
    await _upsert_diary_points(qdrant, doc_id, content, payload, prefix=name)

    neo4j_props = "d.date = $date, d.timestamp = $timestamp, d.content = $content, d.name = $name"
    if keywords:
        neo4j_props += ", d.keywords = $keywords"
    if metadata:
        neo4j_props += ", d.metadata = $metadata"

    with neo4j_driver.session() as s:
        params = dict(userId=user_id, id=doc_id, date=entry_date, timestamp=timestamp, content=content, name=name)
        if keywords:
            params["keywords"] = keywords
        if metadata:
            params["metadata"] = json.dumps(metadata)
        s.run(
            f"""
            MERGE (u:User {{id: $userId}})
            MERGE (d:DiaryEntry {{id: $id, userId: $userId}})
            SET {neo4j_props}
            MERGE (u)-[:WROTE_DIARY]->(d)
            """,
            **params
        )

        # Link to Client if provided
        if client_id:
            await link_diary_to_client(doc_id, client_id, user_id)

        # Link to Context if provided
        if context_id:
            await link_diary_to_context(doc_id, context_id, user_id)

        # A client/project handed to the save form is the user's answer, not a
        # hint. Without this stamp nothing downstream can tell it apart from a
        # guess, and a full reclassify would clear the links and re-derive them.
        if client_id or context_id:
            _stamp_manual_scope(doc_id, "DiaryEntry", user_id)

        # Sync linked facts: only modify MENTIONS if explicitly provided
        if linked_facts is not None:
            if linked_facts:
                # Remove MENTIONS to facts NOT in the incoming list
                s.run(
                    """
                    MATCH (d:DiaryEntry {id: $id, userId: $userId})-[r:MENTIONS]->(f:Fact)
                    WHERE NOT f.id IN $factIds
                    DELETE r
                    """,
                    id=doc_id, userId=user_id, factIds=linked_facts
                )
                # Add MENTIONS for links not yet present
                s.run(
                    """
                    MATCH (d:DiaryEntry {id: $id, userId: $userId})
                    MATCH (f:Fact) WHERE f.id IN $factIds
                    MERGE (d)-[r:MENTIONS]->(f)
                    SET r.source = 'manual'
                    """,
                    id=doc_id, userId=user_id, factIds=linked_facts
                )
            else:
                # Explicit empty list: clear all MENTIONS
                s.run(
                    "MATCH (d:DiaryEntry {id: $id, userId: $userId})-[r:MENTIONS]->() DELETE r",
                    id=doc_id, userId=user_id
                )

    await publish_db_event(user_id, "diary_changed", {
        "action": "add",
        "id": doc_id,
        "date": entry_date,
        "timestamp": timestamp
    })
    # Auto-link People facts mentioned by name (add-only, fire-and-forget)
    await _auto_link_people(doc_id, content, user_id)
    return timestamp


async def db_search_diary(query: str, user_id: str, limit: int = 3, top_p: float = 0.4, client: Optional[str] = None, context: Optional[str] = None) -> list:
    """Vector-similarity search across the diary collection with mention enrichment.

    Uses LLM query rewriting for multi-variant search and boosts results whose
    stored keywords match the query terms.

    ``client``/``context`` **prioritise and never exclude**, exactly as in
    ``db_search_memories``: they are resolved to canonical stored names and turned
    into a ``scopeStrength`` ranking, while every entry stays a candidate. A
    ``FieldCondition`` on ``clientName`` did both wrong things at once -- it
    compared a raw user string against the stored name (so ``"DB"`` matched
    nothing at all and the search came back empty), and it deleted the very
    entries ``scopeStrength`` needs in order to rank the rest.
    """
    from fact_manager import rewrite_search_query, _db_relevant_scope

    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # 0. Resolve the requested scope up front, so the query rewriter and the
    #    ranking both work off the stored spellings rather than whatever the
    #    caller typed. An unresolvable name resolves to None, which means "no
    #    scope signal" -- never "no results".
    scope = db_plan_search_scope(client, context, user_id)
    want_client = scope["client"]
    want_context = scope["context"]

    # 1. Rewrite query into keyword variants for better semantic coverage
    query_variants = await rewrite_search_query(query, client=want_client, context=want_context)
    # A chunked entry contributes one point per chunk, so each entry can appear
    # several times in a single result set. Over-fetch or a long entry crowds
    # every other entry out of the window before the merge below can collapse it.
    fetch_limit = max(limit * 4 * CHUNK_FETCH_MULTIPLIER, 20)
    conditions = [FieldCondition(key="userId", match=MatchValue(value=user_id))]
    filt = Filter(must=conditions)

    # 2. Multi-variant vector search — merge by best score per entry
    all_results: dict = {}  # id -> {payload, score}
    for variant_query, weight in query_variants:
        vec = await get_embedding(variant_query)
        result = await qdrant.query_points(
            collection_name=DIARY_COLLECTION,
            query=vec,
            query_filter=filt,
            limit=fetch_limit,
            with_payload=True,
            score_threshold=0.0,  # apply threshold after merging and boosting
        )
        variant_lower = variant_query.lower()
        for r in result.points:
            score = r.score * weight

            # Boost if variant matches the entry name
            name = r.payload.get("name") or ""
            if name:
                name_lower = name.lower()
                if variant_lower == name_lower:
                    score += 0.5
                elif variant_lower in name_lower or name_lower in variant_lower:
                    score += 0.2

            # Boost if variant matches any stored keyword
            stored_kws = r.payload.get("keywords") or []
            if not stored_kws and "metadata" in r.payload and r.payload["metadata"] and "keywords" in r.payload["metadata"]:
                meta_kws = r.payload["metadata"]["keywords"]
                if isinstance(meta_kws, str):
                    stored_kws = [kw.strip() for kw in meta_kws.split(",") if kw.strip()]
                elif isinstance(meta_kws, list):
                    stored_kws = meta_kws

            for kw in stored_kws:
                kw_lower = kw.lower()
                if variant_lower == kw_lower:
                    score += 0.4
                    break
                elif variant_lower in kw_lower or kw_lower in variant_lower:
                    score += 0.15
                    break

            # Key by the entry, not the point: a chunked entry owns one point per
            # chunk and all of them carry the same payload keys, so the filters
            # above and the boosts here behave identically on every chunk.
            entry_id = parent_of(r.id, r.payload)
            if entry_id not in all_results or score > all_results[entry_id]["score"]:
                all_results[entry_id] = {"point": r, "score": score}

    # 3. Scope ranking, before the top_p cut. Ordering has to happen first so the
    #    limit is spent on the entries the caller asked for; the threshold still
    #    runs on the blended `score` alone, which is this path's existing
    #    (inconsistent with fact search) behaviour and is deliberately unchanged.
    relevant_by_id = _db_relevant_scope(neo4j_driver, user_id, list(all_results.keys()))
    status_map = db_get_client_status_map(user_id)
    for rid, v in all_results.items():
        p = v["point"].payload or {}
        meta = p.get("metadata") or {}
        cname = p.get("clientName") or meta.get("clientName", "")
        xname = p.get("contextName") or meta.get("contextName", "")
        rel = relevant_by_id.get(rid, {})
        text = p.get("content") or p.get("chunkText") or p.get("name") or ""
        v["scopeStrength"] = scope_strength(
            want_client,
            want_context,
            result_client=cname,
            result_context=xname,
            relevant_client_names=rel.get("clients", ()),
            relevant_context_names=rel.get("contexts", ()),
            record_text=text,
        )
        v["scopeClientTier"] = scope_axis_tier(want_client, cname, rel.get("clients", ()), text)
        v["scopeContextTier"] = scope_context_tier(want_context, xname, rel.get("contexts", ()), text)

    # Inactive clients are demoted -- unless the requested scope IS that client,
    # in which case the caller has said what they want and a hand-pinned inactive
    # status must not empty the answer.
    if status_map:
        for v in all_results.values():
            p = v["point"].payload or {}
            cname = (p.get("clientName") or (p.get("metadata") or {}).get("clientName", "") or "").lower()
            if not cname or cname == (want_client or "").lower():
                continue
            if status_map.get(cname) is False:
                v["score"] -= INACTIVE_PENALTY

    # Query-inferred scope is a score boost, not a ranking signal: it fires only
    # when the query text names a client and the caller asked for no scope.
    if not want_client:
        inferred_client, _ = infer_scope_from_text(query, user_id)
        if inferred_client:
            for v in all_results.values():
                p = v["point"].payload or {}
                cname = (p.get("clientName") or (p.get("metadata") or {}).get("clientName", "") or "").lower()
                if cname == inferred_client.lower() and status_map.get(cname) is not False:
                    v["score"] += INFERRED_SCOPE_BOOST

    passing = {rid: v for rid, v in all_results.items() if v["score"] >= top_p}

    # The best-scoring chunk of a long entry is usually not chunk 0, and only
    # chunk 0 carries the full content — the others deliberately store just
    # their slice. One batched retrieve fills them in, so the caller gets whole
    # entries rather than whichever fragment happened to match best.
    missing_content = {
        rid for rid, v in passing.items()
        if not (v["point"].payload or {}).get("content")
    }
    if missing_content:
        try:
            fetched = await qdrant.retrieve(
                collection_name=DIARY_COLLECTION,
                ids=list(missing_content),
                with_payload=True,
                with_vectors=False,
            )
            for point in fetched:
                item = passing.get(str(point.id))
                if not item:
                    continue
                full = (point.payload or {}).get("content")
                if full:
                    item["point"].payload = dict(item["point"].payload or {}, content=full)
                item["matchedChunk"] = (item["point"].payload or {}).get("chunkText")
        except Exception as exc:
            # Degrade to the fragment we already have rather than losing the hit.
            logger.warning(f"diary search: could not hydrate chunked entries: {exc}")

    # 4. Enrich with Neo4j MENTIONS and format output
    entries = []
    for rid, v in passing.items():
        r = v["point"]
        score = v["score"]
        date = r.payload.get("date")
        entry_ts = r.payload.get("timestamp", date)
        content = r.payload.get("content")
        name = r.payload.get("name")

        mentions = []
        with neo4j_driver.session() as s:
            m_res = s.run(
                "MATCH (d:DiaryEntry {id: $id, userId: $userId})-[:MENTIONS]->(f:Fact) RETURN f.id as id, f.text as text",
                id=str(rid), userId=user_id
            )
            mentions = [{"id": mr["id"], "text": mr["text"]} for mr in m_res]

        entry = {
            "id": rid,
            "date": date,
            "timestamp": entry_ts,
            "content": content,
            "name": name,
            "score": score,
            "keywords": r.payload.get("keywords") or [],
            "clientName": r.payload.get("clientName"),
            "contextName": r.payload.get("contextName"),
            "metadata": r.payload.get("metadata") or {},
            "mentions": mentions,
            "scopeStrength": v.get("scopeStrength", 0.0),
            "scope": {
                "requestedClient": want_client,
                "requestedContext": want_context,
                "strength": v.get("scopeStrength", 0.0),
                "clientTier": v.get("scopeClientTier"),
                "contextTier": v.get("scopeContextTier"),
                "unresolvedClient": scope["unresolvedClient"],
                "unresolvedContext": scope["unresolvedContext"],
            },
        }
        if v.get("matchedChunk"):
            entry["matchedChunk"] = v["matchedChunk"]
        entries.append(entry)

    # Scope is the primary key and score the tiebreak within a scope tier, so the
    # limit is spent on the entries the caller asked for rather than on whichever
    # vector similarity happened to lead. Everything still returns: an entry
    # assigned elsewhere only ranks lower.
    entries.sort(key=lambda x: (x["scopeStrength"], x["score"]), reverse=True)
    return entries[:limit]


async def db_update_diary(entry_id: str, user_id: str, content: Optional[str] = None, name: Optional[str] = None, timestamp: Optional[str] = None, metadata: Optional[dict] = None, linked_facts: Optional[list] = None, client_id: Optional[str] = None, context_id: Optional[str] = None) -> bool:
    """Update a diary entry's content, name, timestamp, metadata, and optionally replace linked facts.
    If linked_facts is provided, existing MENTIONS relationships are cleared and new ones are created.

    client_id / context_id set the scope when given; passing neither leaves the
    entry's existing FOR_CLIENT / IN_CONTEXT links alone. They cannot be cleared
    through this path -- db_set_diary_scope is the one that drops a side.
    """
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    with neo4j_driver.session() as s:
        res = s.run(
            """
            MATCH (d:DiaryEntry {id: $id, userId: $userId})
            OPTIONAL MATCH (d)-[:FOR_CLIENT]->(c:Client)
            WITH d, head(collect({id: c.id, name: c.name})) AS cl
            OPTIONAL MATCH (d)-[:IN_CONTEXT]->(cx:Context)
            RETURN d.content as content, d.name as name, d.timestamp as timestamp, d.metadata as metadata,
                   cl.id as clientId, cl.name as clientName, cx.id as contextId, cx.name as contextName
            """,
            id=entry_id, userId=user_id
        )
        existing = res.single()
        if not existing:
            return False

        new_content = content if content is not None else existing["content"]
        new_name = name if name is not None else existing["name"]
        new_ts = timestamp if timestamp is not None else existing["timestamp"]
        raw_meta = existing.get("metadata")
        existing_meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        new_metadata = metadata if metadata is not None else existing_meta
        entry_date = new_ts[:10]

        # Scope is a property of the record, and the links are the source of
        # truth. The payload below is rebuilt from scratch and the write uses
        # replace=True, so the four scope keys have to be carried over here or
        # every edit would erase the entry's client/project from the vector
        # store: the graph filter would still show it while a client-filtered
        # search silently stopped returning it.
        scope = {
            "clientId": client_id or existing.get("clientId"),
            "clientName": None,
            "contextId": context_id or existing.get("contextId"),
            "contextName": None,
        }
        if client_id:
            info = _resolve_client_by_id(client_id, user_id)
            scope["clientName"] = info["name"] if info else None
            if not info:
                scope["clientId"] = None
        elif existing.get("clientName"):
            scope["clientName"] = existing["clientName"]
        if context_id:
            info = _resolve_context_by_id(context_id, user_id)
            scope["contextName"] = info["name"] if info else None
            if not info:
                scope["contextId"] = None
        elif existing.get("contextName"):
            scope["contextName"] = existing["contextName"]

        # Extract/regenerate keywords asynchronously — non-blocking; empty list on failure
        keywords = await extract_diary_keywords(new_name or "", new_content)
        if keywords:
            if new_metadata is None:
                new_metadata = {}
            new_metadata["keywords"] = ", ".join(keywords)

        # Embed and write to Qdrant BEFORE the Neo4j SET below, matching
        # db_save_diary/db_add_memory/db_update_memory. A failed embed then
        # leaves both stores still describing the old text. The other order
        # leaves the canonical text in Neo4j ahead of the vector store, and the
        # entry is only findable by the text it no longer has.
        payload = {"content": new_content, "name": new_name, "date": entry_date, "timestamp": new_ts, "userId": user_id}
        if keywords:
            payload["keywords"] = keywords
        if new_metadata is not None:
            payload["metadata"] = new_metadata
        payload.update({k: v for k, v in scope.items() if v})
        # replace=True because shortening the text can leave fewer chunks than
        # before, and the stale high-index points would otherwise keep answering
        # searches for text that no longer exists.
        await _upsert_diary_points(qdrant, entry_id, new_content, payload,
                                   prefix=new_name, replace=True)

        neo4j_props = "d.content = $content, d.name = $name, d.timestamp = $ts, d.date = $date"
        params = dict(id=entry_id, userId=user_id, content=new_content, name=new_name, ts=new_ts, date=entry_date)
        if keywords:
            neo4j_props += ", d.keywords = $keywords"
            params["keywords"] = keywords
        if new_metadata is not None:
            neo4j_props += ", d.metadata = $metadata"
            params["metadata"] = json.dumps(new_metadata)

        s.run(
            f"""
            MATCH (d:DiaryEntry {{id: $id, userId: $userId}})
            SET {neo4j_props}
            """,
            **params
        )

        # Sync linked facts: remove stale, add new
        if linked_facts is not None:
            # Remove MENTIONS to facts NOT in the incoming list
            s.run(
                """
                MATCH (d:DiaryEntry {id: $id, userId: $userId})-[r:MENTIONS]->(f:Fact)
                WHERE NOT f.id IN $factIds
                DELETE r
                """,
                id=entry_id, userId=user_id, factIds=linked_facts
            )
            # Add MENTIONS for links not yet present
            if linked_facts:
                s.run(
                    """
                    MATCH (d:DiaryEntry {id: $id, userId: $userId})
                    MATCH (f:Fact) WHERE f.id IN $factIds
                    MERGE (d)-[r:MENTIONS]->(f)
                    SET r.source = 'manual'
                    """,
                    id=entry_id, userId=user_id, factIds=linked_facts
                )

        # A scope named on the edit form is a decision like any other, so it is
        # linked and stamped the same way db_save_diary does it. MERGE, so
        # re-saving an entry with the scope it already has changes nothing.
        if client_id:
            await link_diary_to_client(entry_id, client_id, user_id)
        if context_id:
            await link_diary_to_context(entry_id, context_id, user_id)
        if client_id or context_id:
            _stamp_manual_scope(entry_id, "DiaryEntry", user_id)

    await publish_db_event(user_id, "diary_changed", {"action": "update", "id": entry_id, "date": entry_date})
    # Auto-link People facts mentioned by name (add-only, fire-and-forget)
    await _auto_link_people(entry_id, new_content, user_id)
    return True


async def db_link_diary_mention(entry_id: str, fact_id: str, user_id: str):
    """Create a MENTIONS relationship from a diary entry to a fact."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})
            MATCH (f:Fact {id: $factId, userId: $userId})
            MERGE (d)-[r:MENTIONS]->(f)
            SET r.source = 'manual'
            """,
            entryId=entry_id, factId=fact_id, userId=user_id
        )
    await publish_db_event(user_id, "diary_changed", {"action": "link", "id": entry_id})


async def db_unlink_diary_mention(entry_id: str, fact_id: str, user_id: str):
    """Remove a MENTIONS relationship from a diary entry to a fact."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})-[r:MENTIONS]->(f:Fact {id: $factId, userId: $userId})
            DELETE r
            """,
            entryId=entry_id, factId=fact_id, userId=user_id
        )
    await publish_db_event(user_id, "diary_changed", {"action": "unlink", "id": entry_id})
    await publish_db_event(user_id, "memory_changed", {"action": "update", "id": fact_id})


async def db_add_diary_relevant(entry_id: str, client_id: str, user_id: str):
    """Create a RELEVANT_TO relationship from a diary entry to a client or project.

    Same RELEVANT_TO type for both target kinds, so existing links keep working.
    """
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})
            MATCH (c {id: $clientId, userId: $userId})
            WHERE c:Client OR c:Context
            MERGE (d)-[:RELEVANT_TO]->(c)
            """,
            entryId=entry_id, clientId=client_id, userId=user_id
        )
    await publish_db_event(user_id, "diary_changed", {"action": "relevant_add", "id": entry_id})


async def db_remove_diary_relevant(entry_id: str, client_id: str, user_id: str):
    """Remove a RELEVANT_TO relationship from a diary entry to a client or project."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")
    with neo4j_driver.session() as s:
        s.run(
            """
            MATCH (d:DiaryEntry {id: $entryId, userId: $userId})-[r:RELEVANT_TO]->(c {id: $clientId, userId: $userId})
            WHERE c:Client OR c:Context
            DELETE r
            """,
            entryId=entry_id, clientId=client_id, userId=user_id
        )
    await publish_db_event(user_id, "diary_changed", {"action": "relevant_remove", "id": entry_id})


async def db_delete_diary(entry_id: str, user_id: str) -> bool:
    """Delete a single diary entry by id. Returns True if the entry existed and was deleted."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        raise RuntimeError("Database connections not established.")

    # Verify ownership before deleting
    with neo4j_driver.session() as s:
        result = s.run(
            "MATCH (d:DiaryEntry {id: $id, userId: $userId}) RETURN d.id as id",
            id=entry_id, userId=user_id
        )
        if not result.single():
            return False

        # Delete from Neo4j (detach removes all relationships)
        s.run(
            "MATCH (d:DiaryEntry {id: $id, userId: $userId}) DETACH DELETE d",
            id=entry_id, userId=user_id
        )

    # Delete from Qdrant (direct HTTP call to avoid serializing shard_key: null).
    # A long entry owns one point per chunk, so the whole family must go or the
    # leftover chunks stay searchable for an entry the user just deleted.
    await _delete_diary_chunks(entry_id)

    await publish_db_event(user_id, "diary_changed", {"action": "delete", "id": entry_id})
    return True


def db_list_diary_entries(user_id: str, from_ts: Optional[str] = None, to_ts: Optional[str] = None) -> list:
    """Return diary entries as (id, timestamp, name) tuples within optional time range.
    Defaults to last month if timestamps not provided."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    now = datetime.now(timezone.utc)
    default_from = (now - timedelta(days=30)).isoformat()
    default_to = now.isoformat()
    from_clause = from_ts or default_from
    to_clause = to_ts or default_to

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            WHERE d.timestamp >= $fromTs AND d.timestamp <= $toTs
            RETURN d.id as id, d.timestamp as timestamp, d.name as name, d.metadata as metadata
            ORDER BY d.timestamp DESC
            """,
            userId=user_id,
            fromTs=from_clause,
            toTs=to_clause,
        )
        return [(
            r["id"],
            str(r["timestamp"]) if r["timestamp"] else None,
            r.get("name") or "Unnamed",
            json.loads(r.get("metadata") or "{}").get("original_file", ""),
        ) for r in result]


def db_list_diary(user_id: str) -> list:
    """Return all diary entries for a user from Neo4j with mention links, grouped by date."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    def format_ts_for_picker(ts):
        """Format timestamp to YYYY-MM-DDTHH:MM for browser date picker compatibility."""
        if not ts: return None
        # Handle Neo4j datetime or strings
        s_ts = ts.iso_format() if hasattr(ts, "iso_format") else str(ts)
        # Return YYYY-MM-DDTHH:MM (first 16 characters)
        return s_ts[:16]

    with neo4j_driver.session() as s:
        result = s.run(
            """
            MATCH (d:DiaryEntry {userId: $userId})
            OPTIONAL MATCH (d)-[:FOR_CLIENT]->(cl:Client)
            OPTIONAL MATCH (d)-[:IN_CONTEXT]->(ctx:Context)
            WITH d, cl, ctx
            OPTIONAL MATCH (d)-[:MENTIONS]->(f:Fact)
            OPTIONAL MATCH (d)-[:RELEVANT_TO]->(rc)
            WHERE rc IS NULL OR rc:Client OR rc:Context
            RETURN d.id as id, d.date as date, d.content as content, d.timestamp as timestamp, d.name as name,
                   d.metadata as metadata, d.keywords as keywords,
                   cl.name as clientName, cl.id as clientId,
                   ctx.name as contextName, ctx.id as contextId,
                   collect(DISTINCT {id: f.id, text: f.text, name: f.name}) as mentions,
                   collect(DISTINCT {id: rc.id, name: rc.name,
                                     kind: CASE WHEN rc:Context THEN 'context' ELSE 'client' END}) as relevantClients,
                   [x IN [(d)-[:RELATED_TO]-(e:DiaryEntry) | {id: e.id, name: e.name, date: e.date,
                                                                timestamp: e.timestamp}] WHERE x.id <> d.id] as entryLinks
            ORDER BY d.date DESC, d.timestamp DESC
            """,
            userId=user_id,
        )
        return [
            {
                "id": r["id"],
                "date": r["date"],
                "content": r["content"],
                "name": r.get("name") or "Unnamed Entry",
                "timestamp": format_ts_for_picker(r.get("timestamp")),
                "metadata": (json.loads(r["metadata"]) if isinstance(r.get("metadata"), str) else (r.get("metadata") or {})),
                "mentions": [m for m in r["mentions"] if m.get("id")],
                "keywords": r.get("keywords") or [],
                "clientName": r.get("clientName"),
                "clientId": r.get("clientId"),
                "contextName": r.get("contextName"),
                "contextId": r.get("contextId"),
                "relevantClients": [rc for rc in r["relevantClients"] if rc.get("id")],
                "entryLinks": [
                    {"id": el["id"], "name": el.get("name"), "date": el.get("date"),
                     "timestamp": format_ts_for_picker(el.get("timestamp"))}
                    for el in (r.get("entryLinks") or []) if el.get("id")
                ],
            } for r in result
        ]


async def _scroll_diary_ids(qdrant, user_id: str) -> set:
    """Scroll the entry IDs a user has in the diary collection.

    Counts records, not points: a chunked entry owns one point per chunk and the
    extra ids exist in no Neo4j node, so collecting raw point ids here would
    report every long entry as an orphan and inflate the count.
    """
    ids = set()
    try:
        offset = None
        while True:
            result = await qdrant.scroll(
                collection_name=DIARY_COLLECTION,
                limit=1000,
                offset=offset,
                with_payload=["parentId"],
                with_vectors=False,
                scroll_filter=Filter(must=[FieldCondition(key="userId", match=MatchValue(value=user_id))]),
            )
            points, next_offset = result
            for p in points:
                ids.add(parent_of(p.id, p.payload))
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:
        logger.error(f"consistency: Qdrant diary scroll failed for {user_id}: {e}")
    return ids


async def run_diary_consistency_checks():
    """Read-only diary-specific consistency checks across Neo4j and Qdrant. Logs all discrepancies."""
    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        logger.warning("consistency: DB not available, skipping diary checks")
        return

    with neo4j_driver.session() as s:
        user_rows = list(s.run("MATCH (d:DiaryEntry) RETURN DISTINCT d.userId AS userId"))
    user_ids = [r["userId"] for r in user_rows if r["userId"]]
    if not user_ids:
        logger.info("consistency: no diary users found")
        return

    issues_found = False
    dangling_links = 0

    for user_id in sorted(user_ids):
        # --- Diary counts: Neo4j vs Qdrant ---
        with neo4j_driver.session() as s:
            diary_rows = list(s.run(
                "MATCH (d:DiaryEntry {userId: $userId}) RETURN d.id AS id",
                userId=user_id
            ))
        neo4j_ids = {r["id"] for r in diary_rows}
        neo4j_count = len(neo4j_ids)

        qdrant_ids = await _scroll_diary_ids(qdrant, user_id)
        qdrant_count = len(qdrant_ids)

        if neo4j_count != qdrant_count:
            issues_found = True
            logger.warning(
                f"consistency [{user_id}]: Diary count mismatch — "
                f"Neo4j: {neo4j_count}, Qdrant: {qdrant_count}"
            )
            only_neo4j = neo4j_ids - qdrant_ids
            only_qdrant = qdrant_ids - neo4j_ids
            if only_neo4j:
                logger.warning(
                    f"consistency [{user_id}]: {len(only_neo4j)} diary(s) in Neo4j only: "
                    f"{','.join(sorted(only_neo4j)[:20])}"
                )
            if only_qdrant:
                logger.warning(
                    f"consistency [{user_id}]: {len(only_qdrant)} diary(s) in Qdrant only: "
                    f"{','.join(sorted(only_qdrant)[:20])}"
                )
        else:
            logger.info(f"consistency [{user_id}]: Diary OK ({neo4j_count})")

        # --- Dangling MENTIONS ---
        with neo4j_driver.session() as s:
            bad = list(s.run(
                "MATCH (d:DiaryEntry {userId: $userId})-[r:MENTIONS]->(target) "
                "WHERE NOT target:Fact RETURN count(*) AS count",
                userId=user_id
            ))
        count = bad[0]["count"] if bad else 0
        if count:
            issues_found = True
            dangling_links += count
            logger.warning(f"consistency [{user_id}]: {count} MENTIONS link(s) to non-Fact nodes")

    # --- Cross-user diary checks ---
    with neo4j_driver.session() as s:
        no_user = list(s.run("MATCH (d:DiaryEntry) WHERE d.userId IS NULL RETURN count(*) AS count"))
    count = no_user[0]["count"] if no_user else 0
    if count:
        issues_found = True
        logger.warning(f"consistency: {count} diary entries have no userId")

    with neo4j_driver.session() as s:
        untitled = list(s.run(
            "MATCH (d:DiaryEntry) WHERE d.name IS NULL OR d.name = '' "
            "RETURN d.id AS id LIMIT 20"
        ))
    if untitled:
        issues_found = True
        logger.warning(
            f"consistency: {len(untitled)}+ diary entries without a title "
            f"(IDs: {', '.join(r['id'] for r in untitled)})"
        )

    with neo4j_driver.session() as s:
        bad_ts = list(s.run(
            "MATCH (d:DiaryEntry) WHERE d.timestamp IS NULL OR d.timestamp = '' "
            "RETURN d.id AS id LIMIT 20"
        ))
    if bad_ts:
        issues_found = True
        logger.warning(
            f"consistency: {len(bad_ts)}+ diary entries without a valid timestamp "
            f"(IDs: {', '.join(r['id'] for r in bad_ts)})"
        )

    if not issues_found:
        logger.info("consistency diary: All checks passed")
    else:
        logger.info(f"consistency diary: Summary — {len(user_ids)} users checked, {dangling_links} dangling links")


async def fix_diary_entries():
    """Auto-fix diary entries with missing title or timestamp."""
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        return

    fixed_any = False

    with neo4j_driver.session() as s:
        # Fix missing titles
        result = s.run(
            "MATCH (d:DiaryEntry) WHERE d.name IS NULL OR d.name = '' "
            "SET d.name = 'Untitled Diary Entry' "
            "RETURN count(*) AS count"
        )
        count = result.single()["count"]
        if count:
            fixed_any = True
            logger.info(f"fix: Set title on {count} untitled diary entries")

    with neo4j_driver.session() as s:
        # Fix missing timestamps
        now = datetime.now(timezone.utc).isoformat()
        result = s.run(
            "MATCH (d:DiaryEntry) WHERE d.timestamp IS NULL OR d.timestamp = '' "
            "SET d.timestamp = $now "
            "RETURN count(*) AS count",
            now=now
        )
        count = result.single()["count"]
        if count:
            fixed_any = True
            logger.info(f"fix: Set timestamp on {count} diary entries to {now}")

    if not fixed_any:
        logger.info("fix diary: Nothing to fix")
