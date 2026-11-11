"""The :Decision register -- what was decided, by whom, and what replaced it.

`## Decisions` in a diary entry is prose, and prose is where a decision goes to
be forgotten: six months later the only person who can say why an architecture
was chosen is the one who has left. This module turns those headings into
`:Decision` nodes the same way `action_manager` turns `## Actions` into
`:ActionItem`, and it makes one question answerable that a bullet list is not:
*is this still what we are doing, and if not, what replaced it?*

Two rules carried over from the action register, both learned the hard way:

**Re-extraction reconciles, it does not append.** A decision is matched on its
normalised text *within one source*, so re-saving the minutes updates the row
rather than creating a second copy of the same decision.

**Supersession is explicit, never inferred.** A decision that quietly stops
being followed is worse than one that was never logged, because the log then
says something false. The only two ways a row becomes `superseded` are a
sentence a human wrote (`(superseded by: ...)`) or an explicit call from the
UI / MCP tool. Nothing guesses from the calendar, from a newer decision on the
same topic, or from the text drifting.
"""

import uuid
from datetime import datetime, timezone

from common import get_neo4j, logger
from matching_utils import (
    DECISION_STATUSES,
    normalise_decision_text,
    parse_decisions,
)

# `superseded` and `reversed` are terminal: something outside the source said
# this decision stopped being the answer, and a later re-save of the minutes
# must not quietly undo that by listing the bullet again. `withdrawn` is not
# terminal -- it means this particular record stopped mentioning the decision,
# which says nothing about whether it is still in force, so a record that names
# it again revives it.
_TERMINAL_STATUSES = ("superseded", "reversed")

DEFAULT_STATUS = "active"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean_status(value, default=DEFAULT_STATUS):
    status = (value or default).strip().lower()
    if status not in DECISION_STATUSES:
        return default
    return status


def _apply_scope(session, decision_id, user_id, client_id, context_id):
    """Replace the decision's scope edges rather than adding to them.

    Scope is an edge, so a decision that moves from one project to another must
    not end up linked to both -- `MERGE` alone would keep the old one.
    """
    for rel in ("FOR_CLIENT", "IN_CONTEXT"):
        session.run(
            f"""
            MATCH (d:Decision {{id: $id, userId: $userId}})-[r:{rel}]->()
            DELETE r
            """,
            id=decision_id,
            userId=user_id,
        )
    if client_id:
        session.run(
            """
            MATCH (d:Decision {id: $id, userId: $userId})
            MATCH (c:Client {id: $clientId, userId: $userId})
            MERGE (d)-[:FOR_CLIENT]->(c)
            """,
            id=decision_id,
            userId=user_id,
            clientId=client_id,
        )
    if context_id:
        session.run(
            """
            MATCH (d:Decision {id: $id, userId: $userId})
            MATCH (ctx:Context {id: $contextId, userId: $userId})
            MERGE (d)-[:IN_CONTEXT]->(ctx)
            """,
            id=decision_id,
            userId=user_id,
            contextId=context_id,
        )


def _link_source(session, decision_id, user_id, source_id, source_label):
    """Link the decision back to the record that stated it.

    Without this the list is a set of assertions with no provenance, and "who
    decided this?" cannot be answered past the decider's name.
    """
    if not source_id:
        return
    label = "DiaryEntry" if (source_label or "DiaryEntry") == "DiaryEntry" else "Fact"
    session.run(
        f"""
        MATCH (d:Decision {{id: $id, userId: $userId}})
        MATCH (s:{label} {{id: $sourceId}})
        MERGE (d)-[:DECIDED_IN]->(s)
        """,
        id=decision_id,
        userId=user_id,
        sourceId=source_id,
    )


def _resolve_supersede_links(session, user_id, pending):
    """Turn a written `(superseded by: X)` into a link, when X is unambiguous.

    `pending` is a list of `(decision_id, superseded_by_text)` pairs. The text
    is what a human wrote, so resolving it is reading a statement rather than
    inferring one -- but it only binds when exactly one decision in the vault
    has that normalised text. Two candidates is an ambiguous reference, and a
    confident wrong link is worse than no link, because the whole point of the
    field is that it can be trusted.
    """
    if not pending:
        return 0

    wanted = {}
    for decision_id, text in pending:
        key = normalise_decision_text(text)
        if key:
            wanted.setdefault(key, []).append(decision_id)
    if not wanted:
        return 0

    result = session.run(
        """
        MATCH (d:Decision {userId: $userId})
        RETURN d.id AS id, coalesce(d.key, '') AS key
        """,
        userId=user_id,
    )
    by_key = {}
    for record in result:
        by_key.setdefault(record["key"], []).append(record["id"])

    linked = 0
    for key, ids in wanted.items():
        candidates = [c for c in by_key.get(key, []) if c not in ids]
        if len(candidates) != 1:
            logger.warning(
                "decisions: superseded-by reference %r matched %d decisions, leaving it unlinked",
                key[:60],
                len(candidates),
            )
            continue
        target = candidates[0]
        for decision_id in ids:
            session.run(
                """
                MATCH (d:Decision {id: $id, userId: $userId})
                MATCH (t:Decision {id: $targetId, userId: $userId})
                SET d.supersededBy = $targetId
                SET d.status = 'superseded'
                SET d.updatedAt = datetime()
                """,
                id=decision_id,
                userId=user_id,
                targetId=target,
            )
            linked += 1
    return linked


async def db_sync_decisions(
    source_id,
    source_label,
    user_id,
    items,
    client_id=None,
    context_id=None,
    authoritative=True,
):
    """Reconcile one record's decisions against the register.

    Returns `{ok, created, updated, withdrawn, linked}`. `ok: False` with zero
    counts when the graph is unavailable: a diary save must not fail because
    the register could not be updated.

    `authoritative=False` means the record says nothing about decisions at all
    -- a transcript with no `## Decisions` heading, a note written before this
    existed -- and then nothing is withdrawn. Silence is not a withdrawal, and
    treating it as one would empty the log the first time somebody re-saved a
    file with a different heading.
    """
    zero = {"ok": True, "created": 0, "updated": 0, "withdrawn": 0, "linked": 0}
    driver = get_neo4j()
    if not driver:
        logger.warning(
            "decisions: graph unavailable, skipping sync for %s", source_id
        )
        return {"ok": False, "created": 0, "updated": 0, "withdrawn": 0, "linked": 0}

    created = updated = withdrawn = 0
    pending = []

    with driver.session() as session:
        result = session.run(
            """
            MATCH (d:Decision {userId: $userId, sourceId: $sourceId})
            RETURN d.id AS id, coalesce(d.key, '') AS key,
                   coalesce(d.status, 'active') AS status
            """,
            userId=user_id,
            sourceId=source_id,
        )
        by_key = {}
        for record in result:
            by_key[record["key"]] = {
                "id": record["id"],
                "status": record["status"],
            }

        seen = set()
        for item in items or []:
            key = item.get("key") or normalise_decision_text(item.get("text", ""))
            if not key or key in seen:
                continue
            seen.add(key)

            status = _clean_status(item.get("status"))
            previous = by_key.get(key)
            if previous:
                # A human's verdict outlives an extraction.
                if previous["status"] in _TERMINAL_STATUSES:
                    status = previous["status"]
                decision_id = previous["id"]
                session.run(
                    """
                    MATCH (d:Decision {id: $id, userId: $userId})
                    SET d.text = $text,
                        d.decider = $decider,
                        d.decidedAt = $decidedAt,
                        d.status = $status,
                        d.updatedAt = datetime()
                    """,
                    id=decision_id,
                    userId=user_id,
                    text=item.get("text", ""),
                    decider=item.get("decider") or "",
                    decidedAt=item.get("decidedAt") or "",
                    status=status,
                )
                updated += 1
            else:
                decision_id = str(uuid.uuid4())
                session.run(
                    """
                    CREATE (d:Decision {
                        id: $id, userId: $userId, key: $key, text: $text,
                        decider: $decider, decidedAt: $decidedAt, status: $status,
                        sourceId: $sourceId, sourceLabel: $sourceLabel,
                        createdAt: datetime(), updatedAt: datetime()})
                    """,
                    id=decision_id,
                    userId=user_id,
                    key=key,
                    text=item.get("text", ""),
                    decider=item.get("decider") or "",
                    decidedAt=item.get("decidedAt") or "",
                    status=status,
                    sourceId=source_id,
                    sourceLabel=source_label,
                )
                by_key[key] = {"id": decision_id, "status": status}
                created += 1

            _apply_scope(session, decision_id, user_id, client_id, context_id)
            _link_source(session, decision_id, user_id, source_id, source_label)

            if item.get("supersededByText"):
                pending.append((decision_id, item["supersededByText"]))

        if authoritative:
            for key, previous in by_key.items():
                if key in seen:
                    continue
                if previous["status"] in _TERMINAL_STATUSES:
                    continue
                session.run(
                    """
                    MATCH (d:Decision {id: $id, userId: $userId})
                    SET d.status = 'withdrawn', d.updatedAt = datetime()
                    """,
                    id=previous["id"],
                    userId=user_id,
                )
                withdrawn += 1

        linked = _resolve_supersede_links(session, user_id, pending)

    if created or updated or withdrawn or linked:
        logger.info(
            "decisions: %s created=%d updated=%d withdrawn=%d linked=%d",
            source_id,
            created,
            updated,
            withdrawn,
            linked,
        )
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "withdrawn": withdrawn,
        "linked": linked,
    }


def db_list_decisions(
    user_id,
    client_id=None,
    context_id=None,
    status=None,
    limit=200,
    include_inactive=True,
):
    """A client's decision log, newest first.

    `client_id` / `context_id` **filter**, like the action register and unlike
    search scope: the panel asks a closed question, so a decision from another
    engagement is a wrong answer rather than a lower-ranked one.

    Superseded decisions are returned by default. A log that hides them shows
    the current answer with no history, which is the state this feature exists
    to escape -- the caller that wants only what is in force passes
    `include_inactive=False` or an explicit `status`.
    """
    driver = get_neo4j()
    if not driver:
        logger.warning("decisions: graph unavailable, list is empty")
        return []

    clauses = []
    params = {"userId": user_id, "limit": int(limit)}

    if client_id:
        clauses.append("(d)-[:FOR_CLIENT]->(:Client {id: $clientId, userId: $userId})")
        params["clientId"] = client_id
    if context_id:
        clauses.append("(d)-[:IN_CONTEXT]->(:Context {id: $contextId, userId: $userId})")
        params["contextId"] = context_id
    if status:
        clauses.append("coalesce(d.status, 'active') = $status")
        params["status"] = _clean_status(status)
    elif not include_inactive:
        clauses.append("NOT coalesce(d.status, 'active') IN $inactive")
        params["inactive"] = list(DECISION_STATUSES)
        params["inactive"].remove("active")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    try:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (d:Decision {{userId: $userId}})
                {where}
                OPTIONAL MATCH (d)-[:FOR_CLIENT]->(c:Client)
                WITH d, c
                OPTIONAL MATCH (d)-[:IN_CONTEXT]->(ctx:Context)
                WITH d, c, ctx
                OPTIONAL MATCH (d)-[:DECIDED_IN]->(s)
                WITH d, c, ctx, s
                OPTIONAL MATCH (old:Decision {{userId: $userId, supersededBy: d.id}})
                WITH d, c, ctx, s, collect(DISTINCT old.text) AS replaces
                RETURN d.id AS id,
                       d.text AS text,
                       coalesce(d.decider, '') AS decider,
                       coalesce(d.decidedAt, '') AS decidedAt,
                       coalesce(d.status, 'active') AS status,
                       coalesce(d.supersededBy, '') AS supersededBy,
                       replaces AS replaces,
                       coalesce(d.sourceId, '') AS sourceId,
                       coalesce(s.name, '') AS sourceName,
                       coalesce(c.id, '') AS clientId,
                       coalesce(c.name, '') AS clientName,
                       coalesce(ctx.id, '') AS contextId,
                       coalesce(ctx.name, '') AS contextName
                ORDER BY coalesce(d.decidedAt, '') DESC
                LIMIT $limit
                """,
                **params,
            )
            return [dict(record) for record in result]
    except Exception as exc:  # a read must not 500 the panel
        logger.warning("decisions: list failed: %s", exc)
        return []


def db_create_decision(
    user_id,
    text,
    decider=None,
    decided_at=None,
    client_id=None,
    context_id=None,
    status=DEFAULT_STATUS,
):
    """Record a decision that did not come out of a diary entry.

    `sourceId` stays null and `sourceLabel` says `manual`, so the log can tell
    an extracted decision from a typed one -- the extracted one can be rewritten
    by the next save of its source, the typed one never can.
    """
    driver = get_neo4j()
    if not driver:
        return {"ok": False, "reason": "graph unavailable"}

    body = (text or "").strip()
    if not body:
        return {"ok": False, "reason": "a decision needs text"}

    decision_id = str(uuid.uuid4())
    key = normalise_decision_text(body)
    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (d:Decision {
                    id: $id, userId: $userId, key: $key, text: $text,
                    decider: $decider, decidedAt: $decidedAt, status: $status,
                    sourceId: '', sourceLabel: 'manual',
                    createdAt: datetime(), updatedAt: datetime()})
                """,
                id=decision_id,
                userId=user_id,
                key=key,
                text=body,
                decider=decider or "",
                decidedAt=(decided_at or _now())[:10],
                status=_clean_status(status),
            )
            _apply_scope(session, decision_id, user_id, client_id, context_id)
    except Exception as exc:
        logger.warning("decisions: create failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

    return {"ok": True, "item": {"id": decision_id, "text": body,
                                "decider": decider or "",
                                "decidedAt": (decided_at or _now())[:10],
                                "status": _clean_status(status)}}


def db_update_decision(item_id, user_id, status=None, text=None, decider=None):
    """Change a decision in place.

    Ownership is in the `MATCH`, not a comparison after a read, so an id from
    another vault answers exactly like an unknown id.
    """
    driver = get_neo4j()
    if not driver:
        return {"ok": False, "reason": "graph unavailable"}

    sets = []
    params = {"id": item_id, "userId": user_id}

    if status is not None:
        sets.append("d.status = $status")
        params["status"] = _clean_status(status)
    if decider is not None:
        sets.append("d.decider = $decider")
        params["decider"] = decider
    if text is not None:
        body = (text or "").strip()
        if not body:
            return {"ok": False, "reason": "a decision needs text"}
        sets.append("d.text = $text")
        sets.append("d.key = $key")
        params["text"] = body
        params["key"] = normalise_decision_text(body)

    if not sets:
        return {"ok": False, "reason": "nothing to update"}

    sets.append("d.updatedAt = datetime()")

    try:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (d:Decision {{id: $id, userId: $userId}})
                SET {", ".join(sets)}
                RETURN d.id AS id
                """,
                **params,
            )
            if not result.single():
                return {"ok": False, "reason": "no such decision"}
    except Exception as exc:
        logger.warning("decisions: update failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

    item = {"id": item_id}
    if status is not None:
        item["status"] = _clean_status(status)
    if text is not None:
        item["text"] = (text or "").strip()
    if decider is not None:
        item["decider"] = decider
    return {"ok": True, "item": item}


def db_supersede_decision(old_id, new_id, user_id):
    """Say that one decision replaced another, explicitly.

    This is the only path that creates the link without a human writing the
    sentence, and it is still a human's click. Both rows are validated as
    owned by the caller in the same statement, so a stale id from another
    vault cannot be used to attach a link.
    """
    driver = get_neo4j()
    if not driver:
        return {"ok": False, "reason": "graph unavailable"}
    if not old_id or not new_id:
        return {"ok": False, "reason": "both decisions are required"}
    if old_id == new_id:
        return {"ok": False, "reason": "a decision cannot supersede itself"}

    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (old:Decision {id: $oldId, userId: $userId})
                MATCH (new:Decision {id: $newId, userId: $userId})
                SET old.supersededBy = $newId
                SET old.status = 'superseded'
                SET old.updatedAt = datetime()
                SET new.supersedes = $oldId
                SET new.updatedAt = datetime()
                RETURN old.id AS oldId, new.id AS newId
                """,
                oldId=old_id,
                newId=new_id,
                userId=user_id,
            )
            row = result.single()
            if not row:
                return {"ok": False, "reason": "no such decision"}
    except Exception as exc:
        logger.warning("decisions: supersede failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

    return {"ok": True, "id": old_id, "supersededBy": new_id}


async def sync_decisions_from_text(
    source_id,
    source_label,
    user_id,
    content,
    today=None,
    decided_at=None,
    client_id=None,
    context_id=None,
):
    """Parse a record's `## Decisions` and reconcile the register.

    `decided_at` is the record's own date, and it is the default for every
    decision found in it -- a decision recorded in March was made in March, not
    on the day somebody happened to re-save the file. The wall clock is never
    used for this.
    """
    items = parse_decisions(content or "", today=today, decided_at=decided_at)
    return await db_sync_decisions(
        source_id,
        source_label,
        user_id,
        items,
        client_id=client_id,
        context_id=context_id,
        authoritative=bool(items),
    )


def describe_decision(item):
    """Add the derived fields the UI needs, at read time.

    `replacedByText` is not stored: it is a join, and a join stored as a
    property is wrong the moment the other row changes.
    """
    out = dict(item or {})
    out["status"] = _clean_status(out.get("status"))
    out["superseded"] = out["status"] in _TERMINAL_STATUSES
    out["replaces"] = list(out.get("replaces") or [])
    return out
