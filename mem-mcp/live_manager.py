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

import uuid
from datetime import datetime, timezone
from typing import Optional

from common import get_neo4j, logger
from client_manager import _resolve_client_by_id, _resolve_context_by_id
from live_notes import LIVE_FACT_LIMIT, LivePriorityGate

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
                            limit: Optional[int] = None) -> list:
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
    cap = int(limit if limit is not None else LIVE_FACT_LIMIT)
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
        if len(candidates) >= cap:
            break
    return candidates
