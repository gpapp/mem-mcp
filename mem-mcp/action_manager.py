"""Commitments: create, reconcile and read the ``:ActionItem`` register.

An action item is its own node rather than a `:Fact`, and the reason is a state
machine. A fact is a statement about the world and stays true or stops being
true; a commitment is *owed*, has an owner, may have a deadline, and moves
through open -> done or open -> dropped. Putting that on `:Fact` would give a
node type a property it has no concept of and would make every fact query
either filter it out or carry it.

Two rules shape everything here:

**Re-extraction reconciles, it does not append.** A diary entry is edited and
re-saved constantly, and a save that appended would multiply the register until
it was unreadable. Items are matched on the normalised bullet text within one
source, so an unchanged bullet is updated in place, a bullet that has gone is
closed, and an item a human marked `done` is never reopened by a re-extraction.

**Scope is copied from the record the item was raised in, and follows it.** An
item inherits its source's `FOR_CLIENT` / `IN_CONTEXT` edges, so the client
panel can list what is outstanding for that client without a second lookup, and
a re-scope of the source carries its open items with it.
"""

import uuid
from datetime import datetime, timezone

from common import get_neo4j, logger
from matching_utils import (
    ACTION_STATUSES,
    normalise_action_text,
    parse_action_items,
)

# A document with no action section has not withdrawn the commitments already
# extracted from it. This is the difference between "the meeting said nothing
# about actions" and "every action was deleted", and only one of them is a
# reason to close anything.
_OPEN_STATUSES = ("open",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_status(value) -> str:
    status = str(value or "open").strip().lower()
    return status if status in ACTION_STATUSES else "open"


def _apply_scope(session, item_id: str, user_id: str,
                 client_id, context_id) -> None:
    """Re-point an item's scope edges at the given client / project.

    Delete-then-merge rather than ``SET``: scope is an edge, and an item whose
    client changed must not end up linked to two clients. Deleting only until
    the link is recreated is what the rest of the codebase does for facts, and
    matching it here keeps one reader (`_scope_items`) working unchanged.
    """
    session.run(
        "MATCH (a:ActionItem {id: $itemId, userId: $userId}) "
        "OPTIONAL MATCH (a)-[r:FOR_CLIENT]->(:Client) DELETE r",
        itemId=item_id, userId=user_id,
    )
    session.run(
        "MATCH (a:ActionItem {id: $itemId, userId: $userId}) "
        "OPTIONAL MATCH (a)-[r:IN_CONTEXT]->(:Context) DELETE r",
        itemId=item_id, userId=user_id,
    )
    if client_id:
        session.run(
            "MATCH (a:ActionItem {id: $itemId, userId: $userId}), "
            "(c:Client {id: $clientId, userId: $userId}) "
            "MERGE (a)-[:FOR_CLIENT]->(c)",
            itemId=item_id, userId=user_id, clientId=client_id,
        )
    if context_id:
        session.run(
            "MATCH (a:ActionItem {id: $itemId, userId: $userId}), "
            "(x:Context {id: $contextId, userId: $userId}) "
            "MERGE (a)-[:IN_CONTEXT]->(x)",
            itemId=item_id, userId=user_id, contextId=context_id,
        )


def _link_source(session, item_id: str, user_id: str, source_id, source_label) -> None:
    """``(item)-[:RAISED_IN]->(source)``, so a commitment names its meeting."""
    if not source_id:
        return
    label = "DiaryEntry" if str(source_label or "").lower() in (
        "diary", "diaryentry", "diary_entry") else "Fact"
    session.run(
        f"MATCH (a:ActionItem {{id: $itemId, userId: $userId}}), "
        f"(s:{label} {{id: $sourceId, userId: $userId}}) "
        "MERGE (a)-[:RAISED_IN]->(s)",
        itemId=item_id, userId=user_id, sourceId=source_id,
    )


async def db_sync_action_items(source_id: str, source_label: str, user_id: str,
                               items: list, client_id=None, context_id=None,
                               authoritative: bool = True) -> dict:
    """Reconcile the action items extracted from one record.

    ``authoritative`` is the one decision a caller has to make deliberately. A
    record whose text was parsed for actions is authoritative: what it no longer
    says, it no longer asks for. A record that has no action section at all is
    not, because silence about actions is not a withdrawal -- the items already
    there may have come from a live note, from the LLM fallback, or from a
    human. Closing them there would quietly empty the register every time
    somebody re-saved a meeting minutes file with a different heading.

    Returns ``{ok, created, updated, closed}``; ``ok: False`` with counts of
    zero when the graph is unavailable, because a boot or save that cannot
    reach Neo4j must not turn into a failed diary write.
    """
    driver = get_neo4j()
    if not driver:
        logger.warning("action items: graph unavailable -- %s items not "
                       "reconciled for %s", len(items or ()), source_id)
        return {"ok": False, "created": 0, "updated": 0, "closed": 0}

    parsed = []
    for item in items or ():
        key = item.get("key") or normalise_action_text(item.get("text"))
        if not key:
            continue
        parsed.append((key, item))

    created = updated = closed = 0
    seen = {key for key, _ in parsed}

    with driver.session() as session:
        rows = session.run(
            "MATCH (a:ActionItem {userId: $userId, sourceId: $sourceId}) "
            "RETURN a.id AS id, a.status AS status, coalesce(a.key, '') AS key",
            userId=user_id, sourceId=source_id,
        ).data()
        # Keyed by normalised text, which is the only thing a re-extraction can
        # compare. The id cannot: it is minted on create, and a bullet edited
        # from "send the doc" to "send the doc today" is the same commitment.
        by_key = {row["key"]: row for row in rows if row.get("key")}

        for key, item in parsed:
            previous = by_key.get(key)
            status = _clean_status(item.get("status"))
            if previous:
                # A human's verdict outlives a re-extraction. Reopening a done
                # item is how a register becomes untrustworthy: the work was
                # finished, the bullet is still in the meeting notes, and the
                # item comes back as owed.
                if str(previous.get("status") or "") in ("done", "dropped"):
                    status = previous["status"]
                session.run(
                    "MATCH (a:ActionItem {id: $itemId, userId: $userId}) "
                    "SET a.text = $text, a.owner = $owner, a.dueAt = $dueAt, "
                    "a.status = $status, a.updatedAt = datetime()",
                    itemId=previous["id"], userId=user_id,
                    text=item.get("text") or "", owner=item.get("owner"),
                    dueAt=item.get("dueAt"), status=status,
                )
                if client_id or context_id:
                    _apply_scope(session, previous["id"], user_id, client_id, context_id)
                updated += 1
                continue

            item_id = str(uuid.uuid4())
            session.run(
                "CREATE (a:ActionItem {"
                "id: $itemId, userId: $userId, text: $text, owner: $owner, "
                "dueAt: $dueAt, status: $status, key: $key, "
                "sourceId: $sourceId, sourceLabel: $sourceLabel, "
                "createdAt: datetime(), updatedAt: datetime()})",
                itemId=item_id, userId=user_id, text=item.get("text") or "",
                owner=item.get("owner"), dueAt=item.get("dueAt"), status=status,
                key=key, sourceId=source_id,
                sourceLabel="DiaryEntry" if str(source_label or "").lower()
                in ("diary", "diaryentry", "diary_entry") else "Fact",
            )
            if client_id or context_id:
                _apply_scope(session, item_id, user_id, client_id, context_id)
            _link_source(session, item_id, user_id, source_id, source_label)
            created += 1

        if authoritative:
            for key, row in by_key.items():
                if key in seen:
                    continue
                if str(row.get("status") or "") in ("done", "dropped"):
                    continue
                session.run(
                    "MATCH (a:ActionItem {id: $itemId, userId: $userId}) "
                    "SET a.status = 'dropped', a.updatedAt = datetime()",
                    itemId=row["id"], userId=user_id,
                )
                closed += 1

    if created or updated or closed:
        logger.info(
            "action items: %s created, %s updated, %s closed for %s %s",
            created, updated, closed, source_label, source_id)
    return {"ok": True, "created": created, "updated": updated, "closed": closed}


def db_list_action_items(user_id: str, client_id=None, context_id=None,
                         status=None, due_before=None, limit: int = 200) -> list:
    """Open (or all) commitments, newest deadline first.

    ``client_id`` / ``context_id`` **filter** rather than rank, unlike search
    scope, because the panel is asking a closed question: what is outstanding
    for *this* engagement. An item with no scope has no engagement to belong to
    and is only returned when neither filter is given.
    """
    driver = get_neo4j()
    if not driver:
        return []

    clauses = ["MATCH (a:ActionItem {userId: $userId})"]
    params = {"userId": user_id, "limit": max(1, int(limit or 200))}
    if client_id:
        clauses.append("MATCH (a)-[:FOR_CLIENT]->(:Client {id: $clientId, userId: $userId})")
        params["clientId"] = client_id
    if context_id:
        clauses.append("MATCH (a)-[:IN_CONTEXT]->(:Context {id: $contextId, userId: $userId})")
        params["contextId"] = context_id
    if status:
        clauses.append("WHERE a.status = $status")
        params["status"] = _clean_status(status)
    elif not client_id and not context_id:
        clauses.append("WHERE coalesce(a.status, 'open') = 'open'")
    if due_before:
        clauses.append("AND a.dueAt IS NOT NULL AND a.dueAt <= $dueBefore")
        params["dueBefore"] = str(due_before)[:10]
    clauses.append(
        "OPTIONAL MATCH (a)-[:FOR_CLIENT]->(c:Client) "
        "OPTIONAL MATCH (a)-[:IN_CONTEXT]->(x:Context) "
        "WITH a, c, x ORDER BY a.dueAt IS NULL, a.dueAt "
        "RETURN a.id AS id, a.text AS text, a.owner AS owner, a.dueAt AS dueAt, "
        "coalesce(a.status, 'open') AS status, a.sourceId AS sourceId, "
        "a.sourceLabel AS sourceLabel, c.id AS clientId, c.name AS clientName, "
        "x.id AS contextId, x.name AS contextName LIMIT $limit"
    )
    try:
        with driver.session() as session:
            rows = session.run(" ".join(clauses), **params).data()
    except Exception as exc:            # noqa: BLE001 - a read must not 500 a panel
        logger.warning("action items: could not list for %s: %s", user_id, exc)
        return []
    return [dict(row) for row in rows]


def db_update_action_item(item_id: str, user_id: str, status=None, due_at=None,
                          owner=None, text=None) -> dict:
    """Change one item's state. Returns ``{ok, item}``; never raises.

    Ownership is in the ``MATCH``, not in a Python comparison after the read:
    an id from another vault matches nothing and answers ``ok: False`` the same
    way an unknown id does, so the endpoint cannot be walked for real ids.
    """
    driver = get_neo4j()
    if not driver:
        return {"ok": False, "reason": "graph unavailable"}

    sets, params = ["a.updatedAt = datetime()"], {"itemId": item_id, "userId": user_id}
    if status is not None:
        sets.append("a.status = $status")
        params["status"] = _clean_status(status)
    if due_at is not None:
        sets.append("a.dueAt = $dueAt")
        params["dueAt"] = str(due_at)[:10] or None
    if owner is not None:
        sets.append("a.owner = $owner")
        params["owner"] = str(owner) or None
    if text is not None:
        key = normalise_action_text(text)
        if not key:
            return {"ok": False, "reason": "text cannot be empty"}
        sets.append("a.text = $text")
        sets.append("a.key = $key")
        params["text"] = str(text)
        params["key"] = key

    try:
        with driver.session() as session:
            row = session.run(
                "MATCH (a:ActionItem {id: $itemId, userId: $userId}) "
                f"SET {', '.join(sets)} "
                "RETURN a.id AS id, a.text AS text, a.owner AS owner, "
                "a.dueAt AS dueAt, coalesce(a.status, 'open') AS status",
                **params,
            ).single()
    except Exception as exc:            # noqa: BLE001
        logger.warning("action items: could not update %s: %s", item_id, exc)
        return {"ok": False, "reason": str(exc)}
    if not row:
        return {"ok": False, "reason": "no such action item"}
    return {"ok": True, "item": dict(row)}


def db_create_action_item(user_id: str, text: str, owner=None, due_at=None,
                          client_id=None, context_id=None, status="open") -> dict:
    """Create one item by hand, outside any source record.

    Exists because the register is only as good as what can be put into it: a
    commitment made in a corridor has no diary entry to be extracted from, and
    an agent that can only read commitments cannot record one.
    """
    driver = get_neo4j()
    if not driver:
        return {"ok": False, "reason": "graph unavailable"}
    body = str(text or "").strip()
    key = normalise_action_text(body)
    if not key:
        return {"ok": False, "reason": "text is required"}

    item_id = str(uuid.uuid4())
    try:
        with driver.session() as session:
            session.run(
                "CREATE (a:ActionItem {id: $itemId, userId: $userId, text: $text, "
                "owner: $owner, dueAt: $dueAt, status: $status, key: $key, "
                "sourceId: null, sourceLabel: 'manual', "
                "createdAt: datetime(), updatedAt: datetime()})",
                itemId=item_id, userId=user_id, text=body, owner=owner or None,
                dueAt=(str(due_at)[:10] or None) if due_at else None,
                status=_clean_status(status), key=key,
            )
            if client_id or context_id:
                _apply_scope(session, item_id, user_id, client_id, context_id)
    except Exception as exc:            # noqa: BLE001
        logger.warning("action items: could not create for %s: %s", user_id, exc)
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "item": {"id": item_id, "text": body, "owner": owner,
                                 "dueAt": (str(due_at)[:10] or None) if due_at else None,
                                 "status": _clean_status(status)}}


async def sync_actions_from_text(source_id: str, source_label: str, user_id: str,
                                 content: str, today=None, client_id=None,
                                 context_id=None) -> dict:
    """Parse a record's Markdown and reconcile its register.

    Thin on purpose: the parsing rules live in ``matching_utils`` where they are
    pure and testable, and this function exists so every caller -- diary save,
    diary update, live-note commit -- reaches the same reconciliation instead of
    each one inventing its own.
    """
    items = parse_action_items(content, today=today)
    has_section = bool(items)
    return await db_sync_action_items(
        source_id, source_label, user_id, items,
        client_id=client_id, context_id=context_id,
        authoritative=has_section,
    )


def describe_action_item(item: dict, today=None) -> dict:
    """Add the derived fields the UI needs, and nothing else.

    ``overdue`` is computed here rather than stored: a stored flag is wrong the
    moment the clock passes midnight and nothing runs to change it.
    """
    row = dict(item or {})
    due = row.get("dueAt")
    row["overdue"] = False
    if due and str(row.get("status") or "open") in _OPEN_STATUSES:
        reference = str(today or datetime.now(timezone.utc).date().isoformat())[:10]
        row["overdue"] = str(due)[:10] < reference
    return row
