"""live_manager.py – storage for Live notes transcriptions and their note links.

The raw transcript is a first-class record in Neo4j, not a Fact:

    (:Transcription)-[:HAS_SEGMENT]->(:TranscriptionSegment)

The note a transcription produces is an ordinary ``:Fact`` in category
``Notes``. It is written through ``db_add_memory`` / ``db_update_memory`` so it
is embedded, chunked, searchable, visible in Memories and backed up like every
other record. The links that make it a *live* note are one-way:

    (:Fact)-[:FROM_TRANSCRIPTION]->(:Transcription)
    (:Fact)-[:REFERENCES]->(:Fact)

``REFERENCES`` is written here rather than through ``db_link_facts`` because
that helper creates a Fact↔Fact relationship in both directions, and a note
referencing a fact is not the same statement as that fact referencing the note.
It would put the note on every referenced fact's card.

Raw transcripts are deliberately **not** written to Qdrant. A transcript is
large, is not a search target in its own right, and its value is in the note it
produces; indexing every segment would multiply the collection for no recall.

Nothing in this module imports ``fact_manager`` at module level: the note
deletion hook needs to call back into this file, and the two modules would form
a cycle. The one import is function-local, where it is needed.
"""

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from common import (
    LIVE_MODEL, get_llm_response, get_neo4j, logger, publish_db_event,
)
from client_manager import _resolve_client_by_id, _resolve_context_by_id
from live_notes import (
    COMPACT_SYSTEM_PROMPT,
    LIVE_FACT_LIMIT,
    LIVE_NOTE_MAX_CHARS,
    LIVE_SAMPLING,
    LIVE_SAMPLING_RETRY,
    LIVE_SYSTEM_PROMPT,
    LiveNoteError,
    LiveNoteTooLong,
    LivePriorityGate,
    RecordingActiveError,
    build_compaction_prompt,
    build_live_prompt,
    is_repeat_abort,
    live_output_budget,
    parse_note_json,
    rank_fact_candidates,
    segment_windows,
)

NOTE_CATEGORY = "Notes"

# One gate for the process: the WS route begins recording on it and the job
# layer registers its LLM-phase cancel hook on it. A per-request instance would
# let a second tab record while a job from the first was still committing.
LIVE_GATE = LivePriorityGate()

TRANSCRIPTION_STATUSES = (
    "idle", "recording", "paused", "processing", "deferred", "error",
)

_CANDIDATE_SCAN_LIMIT = 2000

# Kept separate from the query budget on purpose: the UI shows the segment
# count, and an unconsumed count is the number that tells the user whether a
# pause still has work waiting for it.
_TRANSCRIPTION_RETURN = """
    t, segmentCount, lastEnd, unconsumed,
    n.id AS noteId,
    coalesce(n.name, n.text) AS noteTitle
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(value):
    """A Neo4j temporal (or plain value) as an ISO string for JSON."""
    if value is None:
        return None
    if hasattr(value, "iso_format"):
        return value.iso_format()
    return str(value)


def _require_driver():
    driver = get_neo4j()
    if not driver:
        raise RuntimeError("Neo4j not connected.")
    return driver


def _transcription_dict(row) -> dict:
    node = row["t"]
    return {
        "id": node["id"],
        "userId": node.get("userId"),
        "name": node.get("name"),
        "status": node.get("status") or "idle",
        "language": node.get("language") or "auto",
        "createdAt": _stamp(node.get("createdAt")),
        "updatedAt": _stamp(node.get("updatedAt")),
        "clientId": node.get("clientId"),
        "clientName": node.get("clientName"),
        "contextId": node.get("contextId"),
        "contextName": node.get("contextName"),
        "noteId": row.get("noteId") or node.get("noteId"),
        "noteTitle": row.get("noteTitle"),
        "segmentCount": int(row.get("segmentCount") or 0),
        "unconsumedCount": int(row.get("unconsumed") or 0),
        "lastEndSec": float(row.get("lastEnd") or 0.0),
    }


def _segment_dict(node) -> dict:
    return {
        "id": node["id"],
        "transcriptionId": node.get("transcriptionId"),
        "seq": int(node.get("seq") or 0),
        "startSec": float(node.get("startSec") or 0.0),
        "endSec": float(node.get("endSec") or 0.0),
        "speaker": node.get("speaker") or "",
        "text": node.get("text") or "",
        "createdAt": _stamp(node.get("createdAt")),
        "updatedAt": _stamp(node.get("updatedAt")),
        "editedAt": _stamp(node.get("editedAt")),
        "consumedBy": node.get("consumedBy"),
        "consumedAt": _stamp(node.get("consumedAt")),
    }


def _resolve_scope(client_id: Optional[str], context_id: Optional[str],
                   user_id: str) -> tuple:
    """Resolve and cross-check a client/project pair.

    Returns ``(client, context)`` as ``{"id","name"}`` dicts or ``None``.
    A project given without a client implies its parent, because an entry
    filed under a project no client filter can find is not scoped. A project
    belonging to a different client is refused rather than silently rebuilt:
    the caller picked two things that disagree.
    """
    client = _resolve_client_by_id(client_id, user_id) if client_id else None
    if client_id and not client:
        raise ValueError(f"Unknown client '{client_id}'.")

    ctx = _resolve_context_by_id(context_id, user_id) if context_id else None
    if context_id and not ctx:
        raise ValueError(f"Unknown project '{context_id}'.")

    driver = _require_driver()
    if ctx and not client:
        with driver.session() as s:
            row = s.run(
                "MATCH (ctx:Context {id: $id, userId: $userId}) "
                "RETURN ctx.clientId AS clientId",
                id=ctx["id"], userId=user_id,
            ).single()
        if row and row["clientId"]:
            parent = _resolve_client_by_id(row["clientId"], user_id)
            if parent:
                client = parent

    if client and ctx:
        with driver.session() as s:
            row = s.run(
                "MATCH (ctx:Context {id: $id, userId: $userId}) "
                "RETURN ctx.clientId AS clientId",
                id=ctx["id"], userId=user_id,
            ).single()
        if row and row["clientId"] and row["clientId"] != client["id"]:
            raise ValueError(
                f"Project '{ctx['name']}' does not belong to client "
                f"'{client['name']}'."
            )
    return client, ctx


# ---------------------------------------------------------------------------
# Transcriptions
# ---------------------------------------------------------------------------

def db_create_transcription(user_id: str, name: Optional[str] = None,
                            language: str = "auto",
                            client_id: Optional[str] = None,
                            context_id: Optional[str] = None) -> dict:
    """Create a transcription session. Returns the stored record."""
    if not (user_id or "").strip():
        raise ValueError("user_id is required.")
    driver = _require_driver()
    client, ctx = _resolve_scope(client_id, context_id, user_id)

    transcription_id = str(uuid.uuid4())
    title = (name or "").strip() or (
        "Live notes — " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    )
    with driver.session() as s:
        row = s.run(
            """
            CREATE (t:Transcription {
                id: $id, userId: $userId, name: $name,
                status: 'idle', language: $language,
                createdAt: datetime(), updatedAt: datetime()
            })
            SET t.clientId = $clientId, t.clientName = $clientName,
                t.contextId = $contextId, t.contextName = $contextName
            RETURN t
            """,
            id=transcription_id, userId=user_id, name=title,
            language=(language or "auto"), clientId=client["id"] if client else None,
            clientName=client["name"] if client else None,
            contextId=ctx["id"] if ctx else None,
            contextName=ctx["name"] if ctx else None,
        ).single()
    return _transcription_dict({
        "t": row["t"], "segmentCount": 0, "lastEnd": 0.0,
        "unconsumed": 0, "noteId": None, "noteTitle": None,
    })


def db_list_transcriptions(user_id: str) -> list:
    """Every session for a user, newest first, with segment and note counts."""
    driver = _require_driver()
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (t:Transcription {userId: $userId})
            OPTIONAL MATCH (t)-[:HAS_SEGMENT]->(seg:TranscriptionSegment)
            WITH t, count(seg) AS segmentCount,
                 coalesce(max(seg.endSec), 0.0) AS lastEnd,
                 count(CASE WHEN seg.consumedBy IS NULL THEN 1 END) AS unconsumed
            OPTIONAL MATCH (n:Fact {userId: $userId}) WHERE n.id = t.noteId
            RETURN """ + _TRANSCRIPTION_RETURN + """
            ORDER BY t.createdAt DESC
            """,
            userId=user_id,
        ).data()
    return [_transcription_dict(row) for row in rows]


def db_get_transcription(transcription_id: str, user_id: str) -> Optional[dict]:
    """One session, or ``None`` when it is not the caller's."""
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
            OPTIONAL MATCH (t)-[:HAS_SEGMENT]->(seg:TranscriptionSegment)
            WITH t, count(seg) AS segmentCount,
                 coalesce(max(seg.endSec), 0.0) AS lastEnd,
                 count(CASE WHEN seg.consumedBy IS NULL THEN 1 END) AS unconsumed
            OPTIONAL MATCH (n:Fact {userId: $userId}) WHERE n.id = t.noteId
            RETURN """ + _TRANSCRIPTION_RETURN,
            id=transcription_id, userId=user_id,
        ).single()
    return _transcription_dict(row) if row else None


def db_update_transcription(transcription_id: str, user_id: str,
                            name: Optional[str] = None,
                            language: Optional[str] = None,
                            status: Optional[str] = None) -> bool:
    """Rename, change language, or set the job status of a session.

    Scope is changed through ``db_set_transcription_scope`` because that also
    has to move the note's own ``FOR_CLIENT`` / ``IN_CONTEXT`` edges.
    """
    if status is not None and status not in TRANSCRIPTION_STATUSES:
        raise ValueError(f"Unknown transcription status '{status}'.")
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
            SET t.name = coalesce($name, t.name),
                t.language = coalesce($language, t.language),
                t.status = coalesce($status, t.status),
                t.updatedAt = datetime()
            RETURN count(t) AS n
            """,
            id=transcription_id, userId=user_id, name=name,
            language=language, status=status,
        ).single()
    return bool(row and row["n"])


def db_set_transcription_scope(transcription_id: str, user_id: str,
                               client_id: Optional[str] = None,
                               context_id: Optional[str] = None) -> Optional[dict]:
    """Replace the session's client/project, or clear both with ``None``.

    This does not touch the note's scope: the job layer does that when a note
    already exists, because ``db_set_fact_scope`` is async and owns the Qdrant
    payload patch.
    """
    driver = _require_driver()
    client, ctx = _resolve_scope(client_id, context_id, user_id)
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
            SET t.clientId = $clientId, t.clientName = $clientName,
                t.contextId = $contextId, t.contextName = $contextName,
                t.updatedAt = datetime()
            RETURN t
            """,
            id=transcription_id, userId=user_id,
            clientId=client["id"] if client else None,
            clientName=client["name"] if client else None,
            contextId=ctx["id"] if ctx else None,
            contextName=ctx["name"] if ctx else None,
        ).single()
    if not row:
        return None
    return {
        "clientId": client["id"] if client else None,
        "clientName": client["name"] if client else None,
        "contextId": ctx["id"] if ctx else None,
        "contextName": ctx["name"] if ctx else None,
    }


async def db_delete_transcription(transcription_id: str, user_id: str,
                                  delete_note: bool = False) -> Optional[dict]:
    """Delete a session, its segments, and optionally its note.

    With ``delete_note=False`` the note survives as an ordinary Fact. Its
    ``FROM_TRANSCRIPTION`` edge is removed first, so the surviving note does
    not point at a transcription that no longer exists.
    """
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            "MATCH (t:Transcription {id: $id, userId: $userId}) "
            "RETURN t.noteId AS noteId",
            id=transcription_id, userId=user_id,
        ).single()
    if not row:
        return None

    note_id = row["noteId"]
    note_deleted = False
    if note_id and delete_note:
        from fact_manager import db_delete_memory
        note_deleted = await db_delete_memory(note_id, user_id)

    with driver.session() as s:
        # Keeping the note means dropping the link first: DETACH DELETE on the
        # transcription would remove the edge anyway, but doing it here makes
        # the intent explicit and covers a note whose transcription is about to
        # disappear under it.
        s.run(
            "MATCH (t:Transcription {id: $id, userId: $userId})"
            "<-[r:FROM_TRANSCRIPTION]-(n:Fact {userId: $userId}) "
            "DELETE r",
            id=transcription_id, userId=user_id,
        )
        s.run(
            "MATCH (t:Transcription {id: $id, userId: $userId})"
            "-[:HAS_SEGMENT]->(seg:TranscriptionSegment) DETACH DELETE seg",
            id=transcription_id, userId=user_id,
        )
        s.run(
            "MATCH (t:Transcription {id: $id, userId: $userId}) DETACH DELETE t",
            id=transcription_id, userId=user_id,
        )
    return {"deleted": True, "noteId": note_id, "noteDeleted": note_deleted}


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

def db_append_segment(transcription_id: str, user_id: str, text: str,
                      speaker: str = "", start_sec: float = 0.0,
                      end_sec: Optional[float] = None,
                      seq: Optional[int] = None) -> Optional[dict]:
    """Append one finalised ASR segment.

    Called by the streaming writer, never by the browser directly. ``seq`` is
    assigned from the stored maximum when omitted, so a reconnect resumes the
    numbering without the client having to remember it.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("A segment needs text.")
    start = max(0.0, float(start_sec or 0.0))
    end = max(start, float(end_sec if end_sec is not None else start))
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
            OPTIONAL MATCH (t)-[:HAS_SEGMENT]->(existing:TranscriptionSegment)
            WITH t, coalesce(max(existing.seq), 0) + 1 AS nextSeq
            WITH t, CASE WHEN $seq > 0 THEN $seq ELSE nextSeq END AS useSeq
            CREATE (seg:TranscriptionSegment {
                id: $segmentId, userId: $userId, transcriptionId: $id,
                seq: useSeq, startSec: $startSec, endSec: $endSec,
                speaker: $speaker, text: $text,
                createdAt: datetime(), updatedAt: datetime()
            })
            CREATE (t)-[:HAS_SEGMENT]->(seg)
            SET t.updatedAt = datetime()
            RETURN seg
            """,
            id=transcription_id, userId=user_id,
            segmentId=str(uuid.uuid4()),
            seq=int(seq or 0), startSec=start, endSec=end,
            speaker=(speaker or ""), text=body,
        ).single()
    return _segment_dict(row["seg"]) if row else None


def db_list_segments(transcription_id: str, user_id: str,
                     only_unconsumed: bool = False) -> list:
    """Segments in speaking order, optionally only those no note has used."""
    driver = _require_driver()
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
                  -[:HAS_SEGMENT]->(seg:TranscriptionSegment {userId: $userId})
            WHERE $onlyUnconsumed = false OR seg.consumedBy IS NULL
            RETURN seg
            ORDER BY seg.seq ASC
            """,
            id=transcription_id, userId=user_id,
            onlyUnconsumed=bool(only_unconsumed),
        ).data()
    return [_segment_dict(row["seg"]) for row in rows]


def db_transcription_state(transcription_id: str, user_id: str) -> Optional[dict]:
    """Counts and the highest offset, for the proxy and the UI."""
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
            OPTIONAL MATCH (t)-[:HAS_SEGMENT]->(seg:TranscriptionSegment)
            RETURN count(seg) AS segmentCount,
                   coalesce(max(seg.seq), 0) AS maxSeq,
                   coalesce(max(seg.endSec), 0.0) AS maxEnd,
                   count(CASE WHEN seg.consumedBy IS NULL THEN 1 END) AS unconsumed
            """,
            id=transcription_id, userId=user_id,
        ).single()
    if not row:
        return None
    return {
        "segmentCount": int(row["segmentCount"] or 0),
        "maxSeq": int(row["maxSeq"] or 0),
        "maxEndSec": float(row["maxEnd"] or 0.0),
        "unconsumedCount": int(row["unconsumed"] or 0),
    }


def db_update_segment(transcription_id: str, segment_id: str, user_id: str,
                      text: Optional[str] = None,
                      speaker: Optional[str] = None) -> Optional[dict]:
    """Edit a segment's text or speaker.

    This never rewrites the note. The note is a snapshot of what the model was
    shown at summarise time; rewriting it here would silently change a record
    the user may already have edited by hand, and deleting a segment is
    explicitly allowed to leave the note as it was.
    """
    if text is None and speaker is None:
        raise ValueError("Nothing to update.")
    if text is not None and not text.strip():
        raise ValueError("Segment text cannot be empty.")
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
                  -[:HAS_SEGMENT]->(seg:TranscriptionSegment {
                      id: $segmentId, userId: $userId
                  })
            SET seg.text = coalesce($text, seg.text),
                seg.speaker = coalesce($speaker, seg.speaker),
                seg.updatedAt = datetime(), seg.editedAt = datetime(),
                t.updatedAt = datetime()
            RETURN seg
            """,
            id=transcription_id, userId=user_id, segmentId=segment_id,
            text=text, speaker=speaker,
        ).single()
    return _segment_dict(row["seg"]) if row else None


def db_delete_segment(transcription_id: str, segment_id: str,
                      user_id: str) -> bool:
    """Delete one segment. The note keeps the text it was given."""
    driver = _require_driver()
    with driver.session() as s:
        found = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
                  -[:HAS_SEGMENT]->(seg:TranscriptionSegment {
                      id: $segmentId, userId: $userId
                  })
            RETURN count(seg) AS n
            """,
            id=transcription_id, userId=user_id, segmentId=segment_id,
        ).single()
        if not found or not found["n"]:
            return False
        s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
                  -[:HAS_SEGMENT]->(seg:TranscriptionSegment {
                      id: $segmentId, userId: $userId
                  })
            DETACH DELETE seg
            """,
            id=transcription_id, userId=user_id, segmentId=segment_id,
        )
        s.run(
            "MATCH (t:Transcription {id: $id, userId: $userId}) "
            "SET t.updatedAt = datetime()",
            id=transcription_id, userId=user_id,
        )
    return True


def db_mark_segments_consumed(transcription_id: str, segment_ids: list,
                              note_id: str, user_id: str) -> int:
    """Record which note has already used these segments.

    The marker is the only thing that stops the next pause re-summarising
    everything from the beginning. It is cleared by
    ``release_note_segments`` when the note is deleted.
    """
    ids = [str(sid) for sid in (segment_ids or []) if sid]
    if not ids:
        return 0
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
                  -[:HAS_SEGMENT]->(seg:TranscriptionSegment {userId: $userId})
            WHERE seg.id IN $segmentIds
            SET seg.consumedBy = $noteId, seg.consumedAt = datetime(),
                seg.updatedAt = datetime()
            WITH t, count(seg) AS marked
            SET t.updatedAt = datetime()
            RETURN marked
            """,
            id=transcription_id, userId=user_id, segmentIds=ids,
            noteId=note_id,
        ).single()
    return int(row["marked"] or 0) if row else 0


# ---------------------------------------------------------------------------
# Note ↔ transcription links
# ---------------------------------------------------------------------------

def db_get_note_for_transcription(transcription_id: str,
                                  user_id: str) -> Optional[dict]:
    """The running note Fact for a session, or ``None`` before the first pause."""
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (t:Transcription {id: $id, userId: $userId})
                  <-[:FROM_TRANSCRIPTION]-(n:Fact {userId: $userId})
            RETURN n LIMIT 1
            """,
            id=transcription_id, userId=user_id,
        ).single()
        if not row:
            # The edge is authoritative, but noteId is what the list query
            # uses. A savepoint restored between the two writes can leave the
            # property without the edge, and the note is still the note.
            row = s.run(
                """
                MATCH (t:Transcription {id: $id, userId: $userId})
                MATCH (n:Fact {id: t.noteId, userId: $userId})
                RETURN n LIMIT 1
                """,
                id=transcription_id, userId=user_id,
            ).single()
    if not row:
        return None
    node = row["n"]
    return {
        "id": node["id"],
        "name": node.get("name"),
        "text": node.get("text") or "",
        "category": node.get("category"),
    }


def db_set_note_transcription(note_id: str, transcription_id: str,
                              user_id: str) -> bool:
    """Point a note at its transcription, moving it off any previous one."""
    driver = _require_driver()
    with driver.session() as s:
        found = s.run(
            "MATCH (n:Fact {id: $noteId, userId: $userId}) "
            "MATCH (t:Transcription {id: $transcriptionId, userId: $userId}) "
            "RETURN count(n) AS n",
            noteId=note_id, transcriptionId=transcription_id, userId=user_id,
        ).single()
        if not found or not found["n"]:
            return False
        s.run(
            "MATCH (n:Fact {id: $noteId, userId: $userId}) "
            "-[r:FROM_TRANSCRIPTION]->() DELETE r",
            noteId=note_id, userId=user_id,
        )
        # A note can only have one running transcription. Clear the property
        # on any earlier one, or its list row would keep advertising a note
        # that now belongs to a different session.
        s.run(
            "MATCH (t:Transcription {userId: $userId}) "
            "WHERE t.noteId = $noteId AND t.id <> $transcriptionId "
            "REMOVE t.noteId SET t.updatedAt = datetime()",
            noteId=note_id, transcriptionId=transcription_id, userId=user_id,
        )
        s.run(
            """
            MATCH (n:Fact {id: $noteId, userId: $userId})
            MATCH (t:Transcription {id: $transcriptionId, userId: $userId})
            MERGE (n)-[:FROM_TRANSCRIPTION]->(t)
            SET t.noteId = $noteId, t.updatedAt = datetime(),
                n.updatedAt = datetime()
            """,
            noteId=note_id, transcriptionId=transcription_id, userId=user_id,
        )
    return True


def db_get_note_references(note_id: str, user_id: str) -> list:
    """Facts a note references, as titles only."""
    driver = _require_driver()
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (n:Fact {id: $noteId, userId: $userId})
                  -[:REFERENCES]->(f:Fact {userId: $userId})
            RETURN f.id AS id, coalesce(f.name, '') AS name
            ORDER BY name ASC
            """,
            noteId=note_id, userId=user_id,
        ).data()
    return [{"id": row["id"], "name": row["name"]} for row in rows
            if row.get("id")]


def db_set_note_references(note_id: str, fact_ids: list, user_id: str) -> int:
    """Replace a note's outgoing ``REFERENCES`` edges.

    One-way on purpose: the referenced fact must not grow a back-link, which
    would put the note on its card and make the reference look reciprocal.
    Unknown or foreign ids are ignored rather than raising, because the job
    validates them first and a reference that vanished between prompt and
    commit must not fail the whole note update.
    """
    ids = [str(fid) for fid in (fact_ids or []) if fid]
    driver = _require_driver()
    with driver.session() as s:
        exists = s.run(
            "MATCH (n:Fact {id: $noteId, userId: $userId}) RETURN count(n) AS n",
            noteId=note_id, userId=user_id,
        ).single()
        if not exists or not exists["n"]:
            return 0
        s.run(
            "MATCH (n:Fact {id: $noteId, userId: $userId})"
            "-[r:REFERENCES]->() DELETE r",
            noteId=note_id, userId=user_id,
        )
        if not ids:
            s.run(
                "MATCH (n:Fact {id: $noteId, userId: $userId}) "
                "SET n.updatedAt = datetime()",
                noteId=note_id, userId=user_id,
            )
            return 0
        row = s.run(
            """
            MATCH (n:Fact {id: $noteId, userId: $userId})
            UNWIND $ids AS fid
            MATCH (f:Fact {id: fid, userId: $userId})
            WHERE f.id <> n.id
            MERGE (n)-[:REFERENCES]->(f)
            SET f.updatedAt = datetime()
            WITH n, count(f) AS referenced
            SET n.updatedAt = datetime()
            RETURN referenced
            """,
            noteId=note_id, userId=user_id, ids=ids,
        ).single()
    return int(row["referenced"] or 0) if row else 0


def release_note_segments(note_id: str, user_id: str) -> int:
    """Make a deleted note's segments eligible for summarising again.

    Called from ``fact_manager.db_delete_memory``. With this missing, deleting
    a note from the Memories tab would leave every segment it used marked as
    consumed, and the next pause would summarise nothing — the vault would look
    as if the transcript had been lost.
    """
    driver = _require_driver()
    with driver.session() as s:
        row = s.run(
            """
            MATCH (seg:TranscriptionSegment {consumedBy: $noteId, userId: $userId})
            REMOVE seg.consumedBy, seg.consumedAt
            SET seg.updatedAt = datetime()
            RETURN count(seg) AS released
            """,
            noteId=note_id, userId=user_id,
        ).single()
        s.run(
            """
            MATCH (t:Transcription {noteId: $noteId, userId: $userId})
            REMOVE t.noteId
            SET t.updatedAt = datetime()
            """,
            noteId=note_id, userId=user_id,
        )
    return int(row["released"] or 0) if row else 0


# ---------------------------------------------------------------------------
# Reference candidates
# ---------------------------------------------------------------------------

def db_note_fact_candidates(transcription_id: str, user_id: str,
                            limit: Optional[int] = None,
                            segments: Optional[list] = None,
                            referenced_ids=()) -> list:
    """Titles of facts the note is allowed to reference.

    Only the identified client's own records are candidates: ``FOR_CLIENT`` to
    that client, plus facts filed under the selected project. A fact reached
    only through another client's ``RELEVANT_TO`` edge is deliberately not a
    candidate — that edge says the record *mentions* something, not that it
    belongs to this engagement, and a note is a statement about this client.

    No body is returned. The prompt only ever needs a title to decide whether
    the note talks about the same thing, and sending bodies would turn a small
    reference list into a second copy of the vault.
    """
    if not transcription_id or not user_id:
        return []
    driver = _require_driver()
    with driver.session() as s:
        session_row = s.run(
            "MATCH (t:Transcription {id: $id, userId: $userId}) "
            "RETURN t.clientId AS clientId, t.contextId AS contextId, "
            "t.noteId AS noteId",
            id=transcription_id, userId=user_id,
        ).single()
        if not session_row or not session_row.get("clientId"):
            return []
        client_id = session_row["clientId"]
        context_id = session_row.get("contextId")
        note_id = session_row.get("noteId")
        rows = s.run(
            """
            MATCH (f:Fact {userId: $userId})-[:FOR_CLIENT]->(:Client {
                id: $clientId, userId: $userId
            })
            WHERE coalesce(f.category, '') <> $noteCategory
              AND ($noteId = '' OR f.id <> $noteId)
            RETURN DISTINCT f.id AS id, coalesce(f.name, '') AS name
            LIMIT $scanLimit
            """,
            userId=user_id, clientId=client_id, noteCategory=NOTE_CATEGORY,
            noteId=note_id or "", scanLimit=_CANDIDATE_SCAN_LIMIT,
        ).data()
        if context_id:
            rows += s.run(
                """
                MATCH (f:Fact {userId: $userId})-[:IN_CONTEXT]->(:Context {
                    id: $contextId, userId: $userId
                })
                WHERE coalesce(f.category, '') <> $noteCategory
                  AND ($noteId = '' OR f.id <> $noteId)
                RETURN DISTINCT f.id AS id, coalesce(f.name, '') AS name
                LIMIT $scanLimit
                """,
                userId=user_id, contextId=context_id,
                noteCategory=NOTE_CATEGORY, noteId=note_id or "",
                scanLimit=_CANDIDATE_SCAN_LIMIT,
            ).data()
    seen = set()
    candidates = []
    for row in rows:
        if not row.get("id") or row["id"] in seen:
            continue
        title = (row.get("name") or "").strip()
        if not title:
            continue
        seen.add(row["id"])
        candidates.append({"id": row["id"], "name": title})

    # Ranking happens **before** the cap, not after it. The scan limit is a
    # safety net against a huge client; the live cap is how many titles fit in
    # the prompt, and taking those in graph order handed the model whichever
    # records Neo4j returned first rather than the ones this window mentions.
    if segments is not None:
        return rank_fact_candidates(
            candidates, segments, referenced_ids=referenced_ids, limit=limit,
        )
    cap = int(limit if limit is not None else LIVE_FACT_LIMIT)
    return candidates[:cap]


# ---------------------------------------------------------------------------
# Note jobs: summarise, rebuild, and the audio-first priority gate
# ---------------------------------------------------------------------------
#
# A pause hands the unconsumed segments to one background job. The job is a
# background task rather than work inside the WebSocket handler because the
# handler's reader loop must stay free to accept the next audio frame -- but
# the job is *not* independent of the recording: `LIVE_GATE` cancels its LLM
# phase the moment the microphone opens, and the generation is re-checked
# immediately before every write. Audio always wins.
#
# Jobs live in memory. A restart loses the status line, not the data: segments
# are consumed in the graph, and the route treats "no job" as idle. Keeping the
# registry here rather than in SQLite is deliberate -- a queued LLM call cannot
# survive a restart either, and persisting a `processing` flag would leave a
# transcription permanently stuck after every deploy.

_JOB_LOCK = threading.Lock()
_NOTE_JOBS = {}
_NOTE_USER_LOCKS = {}
_NOTE_LOCKS_GUARD = threading.Lock()
_TERMINAL_JOB_STATUSES = ("done", "error", "deferred")


class NoteJobDeferred(RuntimeError):
    """The job stopped before writing because audio was started.

    Deliberately not an error: nothing failed and every unconsumed segment is
    still eligible. The route maps it to a `deferred` status, and the next
    pause will pick the work up.
    """


class NoteJobBusy(RuntimeError):
    """A summarise/rebuild job is already running for this transcription."""


def _user_lock(user_id: str) -> asyncio.Lock:
    key = str(user_id or "")
    with _NOTE_LOCKS_GUARD:
        lock = _NOTE_USER_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _NOTE_USER_LOCKS[key] = lock
        return lock


def _job_public(job: dict) -> dict:
    return {
        "transcriptionId": job.get("transcriptionId"),
        "status": job.get("status"),
        "phase": job.get("phase"),
        "mode": job.get("mode"),
        "noteId": job.get("noteId"),
        "consumedSegments": int(job.get("consumedSegments") or 0),
        "changed": bool(job.get("changed")),
        "error": job.get("error"),
        "startedAt": job.get("startedAt"),
        "finishedAt": job.get("finishedAt"),
        "recording": bool(job.get("recording")),
    }


async def _publish_job_event(user_id: str, payload: dict) -> None:
    """Put one job snapshot on the event bus, never at the job's expense."""
    try:
        await publish_db_event(user_id, "live_note_job", payload)
    except Exception as exc:  # noqa: BLE001 -- a bus failure is not a job failure
        logger.warning("live notes: could not announce a job event: %s", exc)


def _announce(job: dict) -> None:
    """Tell open tabs that this job changed state.

    Fire and forget, deliberately: `publish_db_event` awaits a put per
    subscriber, and a browser that has stopped reading must not hold the
    coroutine that owns the note commit. Without this the only way to see a
    job finish was the 1.5 s status poll the dashboard used to run, which is
    why a note written here was invisible to every other tab.
    """
    payload = _job_public(job)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no loop: a sync caller or a unit test
        return
    loop.create_task(_publish_job_event(job.get("userId") or "", payload))


def _prune_jobs() -> None:
    """Keep terminal jobs bounded; a long-lived vault has one per pause."""
    with _JOB_LOCK:
        finished = [
            (job.get("finishedAt") or "", tid, job)
            for tid, job in _NOTE_JOBS.items()
            if job.get("status") in _TERMINAL_JOB_STATUSES
        ]
        if len(finished) <= 50:
            return
        finished.sort(key=lambda item: item[0])
        for _, tid, _job in finished[:-50]:
            _NOTE_JOBS.pop(tid, None)


def _still_current(job: dict) -> bool:
    """False once a recording has begun since this job captured its slot."""
    try:
        return (LIVE_GATE.current_generation(job["userId"])
                == job.get("generation")
                and not LIVE_GATE.is_recording(job["userId"]))
    except Exception:  # noqa: BLE001 -- a gate failure must not commit blind
        return False


def _set_job(job: dict, status: str = None, phase: str = None,
             error=None, recording=None) -> None:
    with _JOB_LOCK:
        if status is not None:
            job["status"] = status
        if phase is not None:
            job["phase"] = phase
        if error is not None:
            job["error"] = error
        if recording is not None:
            job["recording"] = bool(recording)
        job["updatedAt"] = _now()
        if status in _TERMINAL_JOB_STATUSES:
            job["finishedAt"] = _now()
    # Outside the lock: the payload is a snapshot, and the lock is held by
    # callers that may be mid-commit.
    _announce(job)


def _safe_transcription_status(transcription_id: str, user_id: str,
                               status: str) -> None:
    """Write a status without letting a status write fail the job."""
    try:
        db_update_transcription(transcription_id, user_id, status=status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("live notes: could not set transcription %s status %s: %s",
                       transcription_id, status, exc)


def _preflight_prompt(transcription: dict, note, segments: list,
                      referenced: list, candidates: list,
                      rebuilding: bool = False) -> dict:
    """Compute the first window's prompt and budget before any job starts.

    The refusal has to happen on the request, not inside the task: a prompt too
    large for the context window is a `400` naming the segment count and the
    estimate, and a background job can only report it as an error after the
    user has already been told the pause was accepted.
    """
    working = note
    if rebuilding:
        working = {"name": (note or {}).get("name") or "",
                   "text": ""}
    prompt = build_live_prompt(
        working, segments, referenced, candidates,
        client=transcription.get("clientName") or "",
        context=transcription.get("contextName") or "",
    )
    budget = live_output_budget(len(prompt))
    if not budget["ok"]:
        raise LiveNoteTooLong(
            f"{len(segments)} segment(s) do not fit the live note window: "
            f"{budget['reason']}"
        )
    return budget


def start_note_job(transcription_id: str, user_id: str,
                   mode: str = "unconsumed") -> Optional[dict]:
    """Start a summarise/rebuild job and return its status immediately.

    ``mode="unconsumed"`` is what a pause asks for: add the speech since the
    last note update. ``mode="all"`` rebuilds the running note in place from
    every segment, including ones already consumed. Both are background tasks;
    the caller polls `note_job_status`.
    """
    if mode not in ("unconsumed", "all"):
        raise ValueError("mode must be 'unconsumed' or 'all'")
    transcription = db_get_transcription(transcription_id, user_id)
    if not transcription:
        return None

    LIVE_GATE.assert_idle(user_id)

    with _JOB_LOCK:
        current = _NOTE_JOBS.get(transcription_id)
        if current and current.get("userId") == user_id \
                and current.get("status") in ("queued", "running"):
            return _job_public(current)

    note = db_get_note_for_transcription(transcription_id, user_id)
    segments = db_list_segments(
        transcription_id, user_id, only_unconsumed=(mode == "unconsumed"),
    )
    if not segments:
        raise ValueError(
            "There is no speech to summarise yet."
            if mode == "unconsumed" else
            "This transcription has no segments to rebuild from."
        )

    rebuilding = mode == "all"
    referenced = [] if rebuilding else db_get_note_references(
        note["id"], user_id) if note else []
    windows = segment_windows(segments)
    first_window = windows[0] if windows else []
    candidates = db_note_fact_candidates(
        transcription_id, user_id,
        segments=first_window,
        referenced_ids=[item["id"] for item in referenced],
    )
    _preflight_prompt(transcription, note, first_window, referenced,
                      candidates, rebuilding=rebuilding)

    job = {
        "transcriptionId": transcription_id,
        "userId": user_id,
        "mode": mode,
        "status": "queued",
        "phase": "queued",
        "generation": LIVE_GATE.current_generation(user_id),
        "baseStatus": transcription.get("status") or "idle",
        "noteId": note["id"] if note else None,
        "consumedSegments": 0,
        "consumedIds": [],
        "changed": False,
        "error": None,
        "createdAt": _now(),
        "startedAt": None,
        "finishedAt": None,
        "updatedAt": _now(),
        "recording": False,
    }
    _prune_jobs()
    with _JOB_LOCK:
        _NOTE_JOBS[transcription_id] = job
    # Announced before the task starts so a tab that opened between the POST
    # and the first `_set_job` still learns the job exists.
    _announce(job)
    job["task"] = asyncio.create_task(_run_note_job(job))
    return _job_public(job)


def note_job_status(transcription_id: str, user_id: str) -> Optional[dict]:
    """Current job status, or an idle status when none has run this boot."""
    transcription = db_get_transcription(transcription_id, user_id)
    if not transcription:
        return None
    with _JOB_LOCK:
        job = _NOTE_JOBS.get(transcription_id)
        if job and job.get("userId") == user_id:
            return _job_public(job)
    return {
        "transcriptionId": transcription_id,
        "status": "idle",
        "phase": None,
        "mode": None,
        "noteId": transcription.get("noteId"),
        "consumedSegments": 0,
        "changed": False,
        "error": None,
        "startedAt": None,
        "finishedAt": None,
        "recording": LIVE_GATE.is_recording(user_id),
    }


async def _call_live_llm(job: dict, prompt: str, system: str,
                         num_predict: int, parse=None):
    """One model call whose LLM phase the priority gate can cancel.

    The cancellation hook is registered **only around this call** and cleared
    in `finally`. Registering it for the whole job would let audio tear down a
    commit halfway through, which is the one moment the graph and the vector
    store must not be interrupted.

    `parse` is applied to the answer **inside the retry loop**, and that
    placement is the point. A 2b model can answer with an unusable *non-empty*
    string -- production got 62 tokens of word salad ("Pro Ad, Access, At S An
    Ac...") for a JSON rewrite -- and parsing outside the loop turned that into a
    failure with no second attempt, while an empty answer got one. The retry is
    worth the same here: different sampling is the only thing that can change a
    degenerate decode.

    `LiveNoteTooLong` is deliberately **not** retried and is re-raised first: it
    is a *good* answer that is merely too long, and the caller handles it with
    the compaction pass. Retrying it would ask the same model to ignore its own
    instruction and produce the same length again.
    """
    loop_task = asyncio.current_task()

    def _cancel():
        if loop_task is not None:
            loop_task.cancel()

    if not LIVE_GATE.register_llm_cancel(job["userId"], _cancel):
        raise NoteJobDeferred("recording started before the model call")
    last_error = None
    try:
        # One retry, and only for the failures that are the DECODER rather than
        # the prompt:
        #
        # - Ollama's repetition abort ("token repeat limit reached"), where a
        #   greedy decode of a long Markdown rewrite re-emits the same run until
        #   the guard fires;
        # - an empty answer (`content:"" done_reason:"stop" eval_count:1`): the
        #   model loaded, read the prompt and emitted a single token. At
        #   temperature 0 that is a fixed point;
        # - an answer `parse` rejects (no JSON object, no note text).
        #
        # Anything else (timeout, missing model, HTTP error) propagates on the
        # first attempt, because being asked twice does not improve it.
        for attempt, sampling in enumerate((LIVE_SAMPLING, LIVE_SAMPLING_RETRY)):
            try:
                answer = await get_llm_response(
                    prompt,
                    system=system,
                    model=LIVE_MODEL,
                    num_predict=max(1, int(num_predict)),
                    options=sampling,
                )
            except RuntimeError as exc:
                if attempt or not is_repeat_abort(exc):
                    raise
                logger.warning(
                    f"live notes: {LIVE_MODEL} repeated itself and Ollama "
                    f"aborted the note answer (attempt 1); retrying with a "
                    f"stronger repeat penalty -- {exc}"
                )
                continue
            if not answer or not answer.strip():
                last_error = LiveNoteError("the model returned an empty answer")
                logger.warning(
                    f"live notes: {LIVE_MODEL} answered with nothing (attempt "
                    f"{attempt + 1}, prompt_chars={len(prompt)})"
                )
                if attempt:
                    break
                continue
            if parse is None:
                return answer
            try:
                return parse(answer)
            except LiveNoteTooLong:
                raise
            except LiveNoteError as exc:
                last_error = exc
                if attempt:
                    break
                logger.warning(
                    f"live notes: {LIVE_MODEL} answered but the answer was not "
                    f"usable ({exc}) on attempt 1; retrying with temperature "
                    f"{LIVE_SAMPLING_RETRY['temperature']}"
                )
        raise LiveNoteError(
            f"{LIVE_MODEL} did not produce a usable answer twice. The last "
            f"reason: {last_error or 'no answer'}. The note was not changed. "
            "Retry the update; the timing lines above say whether the model was "
            "slow (a prompt rate in the tens of tok/s is CPU inference: "
            "`docker exec ollama ollama ps` must say 100% GPU) or answering "
            "rubbish, which is what a model too small for this prompt looks "
            "like -- MEM_LIVE_MODEL selects a different one."
        )
    finally:
        LIVE_GATE.clear_llm_cancel(job["userId"], _cancel)


async def _compact_note(job: dict, note: dict, allowed_ids) -> dict:
    """One compaction pass for a note that would exceed the hard limit."""
    _set_job(job, phase="compacting")
    prompt = build_compaction_prompt(note)
    budget = live_output_budget(len(prompt))
    if not budget["ok"]:
        raise LiveNoteTooLong(
            "the note is over its limit and even its compaction prompt does "
            f"not fit: {budget['reason']}"
        )
    # Parsed inside the call so an unusable answer is retried with different
    # sampling. `max_chars` stays at the hard limit: a compaction that comes
    # back too long is a real answer this pass cannot use, and it is raised
    # rather than retried into the same length again.
    return await _call_live_llm(
        job, prompt, COMPACT_SYSTEM_PROMPT, budget["budget"],
        parse=lambda text: parse_note_json(
            text,
            allowed_ids,
            existing_ids=note.get("referencedFactIds") or [],
            max_chars=LIVE_NOTE_MAX_CHARS,
        ),
    )


async def _commit_note_window(job: dict, note: dict, note_id,
                              segment_ids: list) -> dict:
    """Write one window's note, references and consumption marker.

    The generation check is the first statement, not an earlier guard: between
    the last LLM answer and this call the user may have pressed Start, and a
    commit begun after that would write a note while the microphone is open.
    Once the check passes the caller shields this function; audio starting
    mid-write is allowed to wait rather than tear a cross-store write apart.
    """
    if not _still_current(job):
        raise NoteJobDeferred("recording started before the note commit")
    if LIVE_GATE.is_recording(job["userId"]):
        raise NoteJobDeferred("recording is active")

    # Function-local because fact_manager imports this module's deletion hook.
    from fact_manager import db_add_memory, db_update_memory

    user_id = job["userId"]
    transcription_id = job["transcriptionId"]
    transcription = db_get_transcription(transcription_id, user_id) or {}
    consumed_ids = list(dict.fromkeys(
        list(job.get("consumedIds") or []) + [str(sid) for sid in segment_ids]
    ))
    metadata = {
        "source": "live_notes",
        "transcriptionId": transcription_id,
        "segmentIds": consumed_ids,
        "live": True,
    }

    writable_id = note_id
    created = False
    if writable_id:
        ok = await db_update_memory(
            writable_id, note["title"], note["text"], None, user_id,
            metadata=metadata,
        )
        if not ok:
            # The note was deleted between windows. The next write recreates
            # it rather than failing the rest of the transcript.
            logger.info("live notes: note %s vanished during job; recreating",
                        writable_id)
            writable_id = None
    if not writable_id:
        writable_id = await db_add_memory(
            note["text"], NOTE_CATEGORY, user_id,
            metadata=metadata,
            name=note["title"],
            client_id=transcription.get("clientId"),
            context_id=transcription.get("contextId"),
        )
        created = True
    db_set_note_transcription(writable_id, transcription_id, user_id)
    db_set_note_references(
        writable_id, note.get("referencedFactIds") or [], user_id,
    )
    marked = db_mark_segments_consumed(
        transcription_id, segment_ids, writable_id, user_id,
    )
    # The note is an ordinary fact, and every other write of one is announced
    # so an open Memories or Diary tab can refetch it. This commit happens in a
    # background job rather than in a GUI handler, so nothing else would.
    try:
        await publish_db_event(user_id, "memory_changed", {
            "action": "add" if created else "update",
            "id": writable_id,
            "source": "live_notes",
        })
    except Exception as exc:  # noqa: BLE001 -- the note is already written
        logger.warning("live notes: could not announce note %s: %s",
                       writable_id, exc)
    return {"noteId": writable_id, "marked": int(marked or 0)}


async def _refresh_note(job: dict) -> None:
    """Run the sequential windows, committing each as it completes."""
    user_id = job["userId"]
    transcription_id = job["transcriptionId"]
    transcription = db_get_transcription(transcription_id, user_id)
    if not transcription:
        raise ValueError("the transcription was deleted before the job ran")

    existing = db_get_note_for_transcription(transcription_id, user_id)
    rebuilding = job["mode"] == "all"
    note_id = existing["id"] if existing else None

    if rebuilding and note_id:
        # Rebuild-in-place releases the old consumption markers before the
        # first window. If the job fails halfway, the unprocessed segments are
        # eligible again instead of pointing at note text that no longer
        # contains them.
        release_note_segments(note_id, user_id)
        referenced = []
        working = {"name": (existing or {}).get("name") or "", "text": ""}
    else:
        referenced = db_get_note_references(note_id, user_id) if note_id else []
        working = ({"name": existing.get("name") or "",
                    "text": existing.get("text") or ""} if existing else None)

    segments = db_list_segments(
        transcription_id, user_id, only_unconsumed=not rebuilding,
    )
    windows = segment_windows(segments)
    if not windows:
        raise ValueError("there is no speech to summarise")

    for window in windows:
        if not _still_current(job):
            raise NoteJobDeferred("recording started between note windows")
        _set_job(job, phase="summarising", recording=False)

        # After the first commit the graph is the authority for references;
        # the in-memory list only carries ids and has no titles. Before that,
        # a rebuild starts deliberately with no references while an incremental
        # job starts with the ones the note already has.
        if note_id and job.get("consumedIds"):
            referenced = db_get_note_references(note_id, user_id)
        candidates = db_note_fact_candidates(
            transcription_id, user_id,
            segments=window,
            referenced_ids=[item["id"] for item in referenced],
        )
        prompt = build_live_prompt(
            working, window, referenced, candidates,
            client=transcription.get("clientName") or "",
            context=transcription.get("contextName") or "",
        )
        budget = live_output_budget(len(prompt))
        if not budget["ok"]:
            raise LiveNoteTooLong(
                f"{len(window)} segment(s) do not fit the live note window: "
                f"{budget['reason']}"
            )
        allowed_ids = {item["id"] for item in candidates}
        allowed_ids.update(item["id"] for item in referenced)

        def parse_answer(text, _allowed=allowed_ids, _refs=referenced):
            # Parse without the limit first so an over-long answer can be
            # compacted. Parsing with the limit would raise and throw away the
            # text the compaction pass is supposed to receive.
            return parse_note_json(
                text, _allowed,
                existing_ids=[item["id"] for item in _refs],
                max_chars=max(LIVE_NOTE_MAX_CHARS, len(text) + 1),
            )

        result = await _call_live_llm(
            job, prompt, LIVE_SYSTEM_PROMPT, budget["budget"],
            parse=parse_answer,
        )
        if len(result["text"]) > LIVE_NOTE_MAX_CHARS:
            result = await _compact_note(job, result, allowed_ids)
            if len(result["text"]) > LIVE_NOTE_MAX_CHARS:
                raise LiveNoteTooLong(
                    "the compacted note is still over the "
                    f"{LIVE_NOTE_MAX_CHARS}-char limit"
                )

        _set_job(job, phase="committing")
        segment_ids = [str(seg.get("id")) for seg in window if seg.get("id")]
        committed = await asyncio.shield(
            _commit_note_window(job, result, note_id, segment_ids)
        )
        note_id = committed["noteId"]
        job["noteId"] = note_id
        job["consumedIds"] = list(dict.fromkeys(
            list(job.get("consumedIds") or []) + segment_ids
        ))
        job["consumedSegments"] = int(job.get("consumedSegments") or 0) \
            + len(segment_ids)
        if (result["title"], result["text"]) != (
                (working or {}).get("name") or "",
                (working or {}).get("text") or ""):
            job["changed"] = True
        working = {"name": result["title"], "text": result["text"]}
        referenced = [
            {"id": cid, "name": ""} for cid in result["referencedFactIds"]
        ]
    _set_job(job, phase="done")


async def _run_note_job(job: dict) -> None:
    transcription_id = job["transcriptionId"]
    user_id = job["userId"]
    try:
        async with _user_lock(user_id):
            if not _still_current(job):
                raise NoteJobDeferred("recording started while the job was queued")
            # Refresh the generation after waiting for the user lock: another
            # job may have run, and audio may have started while this one waited.
            job["generation"] = LIVE_GATE.current_generation(user_id)
            _set_job(job, status="running", phase="summarising",
                     recording=False)
            job["startedAt"] = job.get("startedAt") or _now()
            _safe_transcription_status(
                transcription_id, user_id, "processing",
            )
            await _refresh_note(job)
            # The last commit may have finished just as the microphone was
            # opened. The note is safely written, but the transcription status
            # belongs to the recorder now: overwriting `recording` with
            # `paused` here would make the toolbar look stopped while audio
            # was still arriving.
            if not LIVE_GATE.is_recording(user_id):
                final = "paused" if job.get("baseStatus") in (
                    "paused", "deferred", "error", "processing",
                ) else "idle"
                _safe_transcription_status(transcription_id, user_id, final)
            _set_job(job, status="done", phase="done")
    except asyncio.CancelledError:
        # The LLM phase was cancelled by `begin_recording`. Nothing in this
        # window has been consumed, and every earlier window's commit stands.
        job["recording"] = LIVE_GATE.is_recording(user_id)
        _set_job(job, status="deferred", phase="deferred", recording=job["recording"])
        if not job["recording"]:
            _safe_transcription_status(transcription_id, user_id, "deferred")
        logger.info("live notes: job deferred by recording for %s",
                    transcription_id)
    except NoteJobDeferred as exc:
        job["recording"] = LIVE_GATE.is_recording(user_id)
        _set_job(job, status="deferred", phase="deferred", error=str(exc),
                 recording=job["recording"])
        if not job["recording"]:
            _safe_transcription_status(transcription_id, user_id, "deferred")
    except RecordingActiveError as exc:
        _set_job(job, status="deferred", phase="deferred", error=str(exc),
                 recording=True)
    except Exception as exc:  # noqa: BLE001 -- the job reports, never crashes the loop
        logger.error("live notes: note job failed for %s: %s",
                     transcription_id, exc)
        _set_job(job, status="error", phase="error", error=str(exc))
        if not LIVE_GATE.is_recording(user_id):
            _safe_transcription_status(transcription_id, user_id, "error")
    finally:
        job["task"] = None
        with _JOB_LOCK:
            if _NOTE_JOBS.get(transcription_id) is job:
                job["updatedAt"] = _now()
