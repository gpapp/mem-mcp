"""
chunking.py — Multi-vector chunking for long records.

Why
---
One embedding is one lossy average of the whole text. A 40k-character
transcription embeds to a single vector whose meaning is "this is a long
transcription", and no query can reach the part it actually needs. Truncating to
the embed budget (see ``_truncate_for_embed`` in ``common.py``) bounds the
damage but throws the middle away, so a query whose only signal is in the
truncated middle finds nothing.

Chunking stores several vectors per record — one per passage — so a query can
match the passage it is about instead of the average of everything.

The shape of a chunked record
-----------------------------
A chunked record occupies N Qdrant points. The **first** one keeps the record's
own id, so every id-based path in the app (delete, update, re-embed, keyword
reindex) keeps working for it and for the far more common unchunked record. The
rest get deterministic ids derived from the record id and their index, so
re-chunking a record replaces exactly the points it owns and never accumulates
orphans.

Every chunk carries the **full** record payload plus three extra keys. The extra
keys are what let the rest of the app stay ignorant of chunking:

    parentId     the record's id — the single source of truth for identity
    chunkIndex   0-based position; 0 is the point whose id is the record id
    chunkCount   N, so a reader knows the family is complete

Because the full payload rides on every chunk, a filter on category, client,
context, or keywords behaves identically whether it is matching chunk 0 or
chunk 7. That is the whole reason for the duplication — a chunk that could only
be matched by its own text would be invisible to every metadata filter.

An **unchunked** record is written exactly as it was before: one point, its own
id, no ``parentId``. Records below the threshold are therefore bit-identical to
their pre-chunking form, and existing data needs no migration to keep working.
"""

import os
import re
import uuid

# Namespace for derived chunk point ids. Fixed forever — changing it would give
# every previously-chunked record a fresh set of orphan points on the next save.
CHUNK_NS = uuid.UUID("6f1c8b2a-3d54-4e97-9a10-5b7d0e2c8f43")

CHUNK_TARGET_CHARS = max(200, int(os.getenv("MEM_CHUNK_CHARS", "3000")))
CHUNK_MAX = max(1, int(os.getenv("MEM_CHUNK_MAX", "16")))
# Overlap keeps a sentence that straddles a boundary retrievable from the next
# chunk. Sized to roughly a paragraph.
CHUNK_OVERLAP = max(0, min(500, int(os.getenv("MEM_CHUNK_OVERLAP", "200"))))
# Mirrors EMBED_MAX_CHARS in common.py. Duplicated rather than imported because
# common.py pulls in the DB drivers; a chunk larger than this is silently
# head+tail truncated by the embedder, so callers compare against it.
EMBED_CEILING = max(500, int(os.getenv("MEM_EMBED_MAX_CHARS", "12000")))

# Keys that exist only to describe the chunking itself. Stripped from chunk 0's
# payload so an unchunked record and chunk 0 of a chunked one are byte-identical.
CHUNK_META_KEYS = ("parentId", "chunkIndex", "chunkCount")
# The full text lives only on chunk 0. Chunks 1..N-1 carry `chunkText` and omit
# `text` outright, so a search that forgets to hydrate cannot accidentally return
# a fragment as if it were the record.
CHUNK_TEXT_KEY = "chunkText"
FULL_TEXT_KEYS = ("text", "content")

# A chunked record contributes one point per chunk to every vector query, so a
# result window sized for records silently holds chunks: a single long entry can
# occupy the whole top-N and no other record is ever seen. Search over-fetches by
# this factor before collapsing a family down to its best-scoring chunk.
CHUNK_FETCH_MULTIPLIER = max(1, int(os.getenv("MEM_CHUNK_FETCH_MULTIPLIER", "3")))

# Startup re-chunking. New and edited records are chunked on write, so this only
# ever applies to records that were already stored as a single point when
# chunking was switched on. It is a background task, not a blocking lifespan step,
# because a long vault costs one embedding call per chunk and nothing should wait
# on that. RECHUNK_LIMIT bounds the work per boot: the remainder is picked up by
# the next restart rather than by one boot that never finishes.
RECHUNK_ENABLED = os.getenv("MEM_RECHUNK_ENABLED", "1") == "1"
RECHUNK_LIMIT = max(0, int(os.getenv("MEM_RECHUNK_LIMIT", "200")))
RECHUNK_CONCURRENCY = max(1, int(os.getenv("MEM_RECHUNK_CONCURRENCY", "2")))

# Blank lines, then any newline, then sentence ends — tried in that order because
# a paragraph boundary is the cheapest good split, a line boundary is acceptable
# and a sentence boundary is the fallback for text with no line structure at all.
# Wrapped in a capture group so the separator is kept: dropping it would discard
# the newlines that give the next chunk its structure.
_SEPARATORS = (r"\n[ \t]*\n+", r"\n", r"(?<=[.!?])[ \t]*\n?[ \t]+")
_SEPARATOR_RES = tuple(re.compile(f"({pattern})") for pattern in _SEPARATORS)


def strip_chunk_meta(payload: dict) -> dict:
    """A copy of ``payload`` with the chunking-only keys removed.

    Chunk 0 must carry the record's payload verbatim, otherwise an unchunked
    record and chunk 0 would differ and every payload comparison downstream would
    need to know about chunking.
    """
    return {k: v for k, v in payload.items() if k not in CHUNK_META_KEYS and k != CHUNK_TEXT_KEY}


def chunk_point_id(parent_id: str, index: int) -> str:
    """The Qdrant point id for chunk ``index`` of a record.

    Index 0 is the record's own id. Higher indices are derived, so writing the
    same record twice produces the same ids and re-saving is idempotent.
    """
    if index <= 0:
        return str(parent_id)
    return str(uuid.uuid5(CHUNK_NS, f"{parent_id}:{index}"))


def parent_of(point_id, payload: dict = None) -> str:
    """The record id a point belongs to.

    Prefers the payload's ``parentId`` and falls back to the point id, which is
    exactly right for an unchunked record. Every reconciliation pass should use
    this instead of ``str(point.id)`` — a derived chunk id is not a node id, and
    treating it as one deletes a live record or links a nonexistent one.
    """
    if payload:
        parent = payload.get("parentId")
        if parent:
            return str(parent)
    return str(point_id)


def is_chunk_of(point_id, payload: dict) -> bool:
    """True when this point is a *non-primary* chunk (id is not the record id)."""
    if not payload:
        return False
    return bool(payload.get("parentId")) and str(point_id) != str(payload.get("parentId"))


def rechunk_candidates(records: list, chunked_ids: set) -> list:
    """The records worth re-chunking, biggest benefit first.

    Two filters, and both matter:

    * **Large** — a record that would not be split into more than one chunk is
      already stored correctly. Re-writing it would cost an embedding call to
      produce the identical single point.
    * **Not already chunked** — re-chunking a chunked record is pure waste: the
      work is already done, and it costs an embedding call per chunk to
      rediscover the same vectors.

    Sorted longest-first so a boot that hits its limit does the records with the
    worst recall problem rather than the first ones it happened to find.
    """
    wanted = []
    for record in records:
        text = record.get("text") or ""
        if not needs_chunking(text):
            continue
        record_id = record.get("id")
        if record_id is None:
            continue
        if parent_of(record_id) in chunked_ids:
            continue
        wanted.append(record)
    wanted.sort(key=lambda r: len(r.get("text") or ""), reverse=True)
    return wanted




def needs_chunking(text: str) -> bool:
    """Whether a record is long enough to be worth more than one vector.

    The threshold is the target chunk size, not the embed ceiling: splitting
    below it produces chunks too small to carry a distinct meaning, which costs
    an embedding call per chunk and buys nothing.
    """
    return needs_chunking_with(text)


def needs_chunking_with(text: str, target: int = None) -> bool:
    """``needs_chunking`` against an explicit target rather than the config."""
    limit = CHUNK_TARGET_CHARS if target is None else int(target)
    return bool(text) and len(text) > limit


def normalize_text(text: str) -> str:
    """CRLF to LF. A Windows-authored transcript would otherwise split on ``\\n``
    and leave a stray ``\\r`` on the end of every chunk."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _hard_split(text: str, size: int) -> list:
    """Last resort for a run with no separator — a base64 blob, a data table."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def _split_at(text: str, size: int, level: int) -> list:
    """Pack ``text`` into parts of at most ``size`` using separator ``level``.

    ``"".join(_split_at(t, n, level)) == t`` for any size, so no character of the
    input is ever lost to the split itself.
    """
    parts, buffer, length = [], [], 0
    for unit in _units(text, level):
        if length and length + len(unit) > size:
            parts.append("".join(buffer))
            buffer, length = [], 0
        if len(unit) > size:
            if buffer:
                parts.append("".join(buffer))
                buffer, length = [], 0
            parts.extend(_hard_split(unit, size))
            continue
        buffer.append(unit)
        length += len(unit)
    if buffer:
        parts.append("".join(buffer))
    return _merge_blank(parts)


def _merge_blank(parts: list) -> list:
    """Fold a whitespace-only part into the part that follows it.

    A part can be nothing but the separator that preceded an oversized run. Left
    alone it would become its own chunk, which is pure noise.
    """
    merged = []
    for part in parts:
        if not part.strip():
            continue
        if merged and not merged[-1].strip():
            merged[-1] += part
        else:
            merged.append(part)
    return merged


def _units(text: str, level: int) -> list:
    """Split at separator ``level``, keeping each separator glued to what follows.

    The separator is part of the text and must survive the split, otherwise the
    newlines that give a chunk its structure are exactly the characters dropped.

    Mixing separator kinds within one text produces ragged chunks — a paragraph
    boundary in one place and a mid-sentence cut in another. Fixing a level and
    using it throughout is what keeps the boundaries legible.
    """
    if level >= len(_SEPARATOR_RES):
        return [text]
    pieces = _SEPARATOR_RES[level].split(text)  # [body, sep, body, sep, ..., body]
    units = [pieces[0]] if pieces else []
    for index in range(1, len(pieces), 2):
        units.append(pieces[index] + (pieces[index + 1] if index + 1 < len(pieces) else ""))
    return units


def max_part_size() -> int:
    """The widest a single part may be.

    A chunk is its part plus the overlap it inherits, and anything past the embed
    ceiling is head+tail truncated by the embedder — so the middle of that chunk
    becomes unreachable, which is the exact failure chunking exists to remove.
    """
    return max(1, EMBED_CEILING - CHUNK_OVERLAP)


def effective_target(text: str, target: int = None, max_chunks: int = None) -> int:
    """The chunk size to start searching from.

    A first guess of ``ceil(len(text) / max_chunks)`` assumes perfect packing.
    Packing is greedy over whole units, so it is a starting point, not a
    guarantee — ``_best_split`` is what actually finds a size that works.
    """
    target = CHUNK_TARGET_CHARS if target is None else max(1, int(target))
    max_chunks = CHUNK_MAX if max_chunks is None else max(1, int(max_chunks))
    if not text:
        return target
    return max(target, -(-len(text) // max_chunks))


def chunk_limit(text: str, max_chunks: int = None) -> int:
    """How many chunks a record may actually use.

    ``CHUNK_MAX`` is a *call budget* — it keeps a routine save from turning into
    fifty embedding round trips. It is not a coverage limit, because the number
    of chunks needed to keep every part within the embed ceiling is a fact about
    the text. So the budget is raised to whatever that requires. A one-megabyte
    record costs 84 calls here; the alternative is storing it and never being
    able to search it.
    """
    cap = CHUNK_MAX if max_chunks is None else max(1, int(max_chunks))
    if not text:
        return cap
    return max(cap, -(-len(text) // max_part_size()))


def _candidate_sizes(length: int, target: int, limit: int, ceiling: int) -> list:
    """Sizes to try, coarsening from the first guess up to the ceiling.

    Only ever upwards: a bigger size means fewer parts, and an over-budget part
    count is the failure being corrected. Each size is tried at every separator
    level, so the search is at most a few dozen splits of an in-memory string.
    """
    preferred = min(ceiling, max(target, -(-length // limit)))
    sizes = {preferred}
    for factor in (1.05, 1.15, 1.35, 1.75, 2.5):
        sizes.add(min(ceiling, int(preferred * factor) + 1))
    return sorted(size for size in sizes if size > 0)


def _best_split(body: str, target: int, limit: int) -> list:
    """A split that respects both the call budget and the embed ceiling.

    Greedy packing at one separator kind is not always able to hit the budget: a
    document of thirty 7000-character paragraphs cannot be packed into eighteen
    11800-character chunks without cutting a paragraph in half, no matter how the
    size is chosen. So the search widens the size *and* drops to a finer
    separator, which yields smaller units that pack cleanly. If nothing reaches
    the budget, the smallest count found wins — coverage outranks the budget.
    """
    ceiling = max_part_size()
    sizes = _candidate_sizes(len(body), target, limit, ceiling)
    best = None
    for level in range(len(_SEPARATOR_RES)):
        for size in sizes:
            parts = _split_at(body, size, level)
            if best is None or len(parts) < len(best):
                best = parts
            if len(parts) <= limit:
                return parts
    if len(best) > limit:
        # Unreachable for text below limit * ceiling chars. Kept so the function
        # has a defined return rather than silently exceeding a limit its caller
        # was promised; the tail is merged, never dropped.
        best = best[:limit - 1] + ["".join(best[limit - 1:])]
    return best


def plan_chunks(text: str, target: int = None, max_chunks: int = None) -> dict:
    """Split ``text`` and report how it was done.

    Returns the overlapped ``chunks`` (what gets embedded) alongside the
    non-overlapping ``parts`` they were built from. The two together are what
    makes the split verifiable: ``"".join(parts)`` is the source exactly, so a
    test can prove nothing was dropped without reverse-engineering the boundaries
    back out of the chunks — which is impossible for text like "x" * 25000,
    where every chunk matches at a thousand offsets.
    """
    body = normalize_text(text).strip()
    target = CHUNK_TARGET_CHARS if target is None else max(1, int(target))
    max_chunks = CHUNK_MAX if max_chunks is None else max(1, int(max_chunks))
    limit = chunk_limit(body, max_chunks)
    if not body or len(body) <= target:
        return {"body": body, "chunks": [body] if body else [], "parts": [body] if body else [],
                "limit": limit, "ceiling": max_part_size()}
    parts = _best_split(body, target, limit)
    return {"body": body, "chunks": _add_overlap(parts), "parts": parts,
            "limit": limit, "ceiling": max_part_size()}


def split_chunks(text: str, target: int = None, max_chunks: int = None) -> list:
    """Split ``text`` into overlapping passages, in order, with nothing dropped.

    Text already within ``target`` is returned verbatim as a single chunk.

    Each chunk after the first begins with ``CHUNK_OVERLAP`` characters of its
    predecessor, so a sentence split across a boundary is still whole in one of
    them.
    """
    return plan_chunks(text, target, max_chunks)["chunks"]


def _add_overlap(parts: list) -> list:
    """Prepend each part's predecessor's tail.

    The prefix is spliced on verbatim rather than rejoined with a separator, so
    ``chunk[i][-len(parts[i]):] == parts[i]`` and ``chunk[i]`` is a contiguous
    slice of the original text. Inserting a joining space would read better at
    the seam but would mean the chunk is no longer a substring of its source.
    """
    if CHUNK_OVERLAP <= 0 or len(parts) < 2:
        return list(parts)
    out = [parts[0]]
    for index in range(1, len(parts)):
        previous = parts[index - 1]
        out.append(previous[-CHUNK_OVERLAP:] + parts[index])
    return out


def build_chunk_payloads(record_id: str, text: str, base_payload: dict,
                         target: int = None, max_chunks: int = None) -> list:
    """Every Qdrant point for a record, chunked or not.

    This is the single place that decides the on-disk shape. Unchunked records
    return exactly one point with the untouched payload and the record's own id,
    so nothing downstream can tell they exist.
    """
    clean = strip_chunk_meta(base_payload)
    text = normalize_text(text).strip()

    if not needs_chunking_with(text, target):
        return [{"id": str(record_id), "payload": clean}]

    chunks = split_chunks(text, target, max_chunks)
    count = len(chunks)
    points = []
    for index, chunk in enumerate(chunks):
        payload = dict(clean)
        payload.update({
            "parentId": str(record_id),
            "chunkIndex": index,
            "chunkCount": count,
        })
        if index:
            # Only chunk 0 carries the full text. Keeping a partial `text` on the
            # others would let a search return a fragment as though it were the
            # whole record.
            for key in FULL_TEXT_KEYS:
                payload.pop(key, None)
            payload[CHUNK_TEXT_KEY] = chunk
        points.append({"id": chunk_point_id(record_id, index), "payload": payload})
    return points


def chunk_text_of(payload: dict) -> str:
    """The text a point actually represents — the full record or the passage."""
    if not payload:
        return ""
    if payload.get(CHUNK_TEXT_KEY):
        return str(payload[CHUNK_TEXT_KEY])
    for key in FULL_TEXT_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def merge_by_parent(points: list) -> dict:
    """Collapse a result set to one entry per record, keeping the best.

    A record is represented by up to ``chunkCount`` points and the most relevant
    passage is the one the search should return. Without this the user sees the
    same record three times, and the low-scoring chunks dilute the ranking of
    the genuine hit.
    """
    best = {}
    for point in points or []:
        payload = getattr(point, "payload", None)
        if payload is None and isinstance(point, dict):
            payload = point.get("payload")
        score = getattr(point, "score", None)
        if score is None and isinstance(point, dict):
            score = point.get("score")
        key = parent_of(getattr(point, "id", None) or (point.get("id") if isinstance(point, dict) else None),
                        payload or {})
        current = best.get(key)
        if current is None or (score or 0) > (current[0] or 0):
            best[key] = (score, point)
    return {key: value[1] for key, value in best.items()}
