"""deliverable_manager.py -- the :Deliverable register, the Cypher half.

The rules are in ``deliverables.py``; this module only stores and reads. Three
of its decisions are not obvious:

* **Silence does not un-ship an artifact.** Actions close something when the
  source stops listing it, and decisions withdraw it. A deliverable is a real
  thing that was produced: a re-saved entry that no longer names it does not
  mean it never existed, and deleting the row would destroy the only record
  that it was delivered. So extraction adds and updates, and never removes.
* **A human's status outlives a re-extraction.** If somebody marked the deck
  `sent`, a later save of the same minutes must not reset it to `draft`. The
  parser's default is the default of a *new* row, not of an existing one.
* **Scope follows the source, and is replaced rather than added.** A record whose
  client changed must not be linked to two.
"""

import uuid
from datetime import datetime, timezone

from common import get_neo4j, logger
from deliverables import (
    DEFAULT_KIND,
    DEFAULT_STATUS,
    DELIVERABLE_KINDS,
    infer_kind,
    normalise_deliverable_name,
    normalise_status,
    parse_deliverables,
)

DEFAULT_LIMIT = 200

_CLEAR_CLIENT = ("MATCH (d:Deliverable {id: $id, userId: $userId}) "
                 "OPTIONAL MATCH (d)-[r:FOR_CLIENT]->(:Client) DELETE r")
_CLEAR_CONTEXT = ("MATCH (d:Deliverable {id: $id, userId: $userId}) "
                  "OPTIONAL MATCH (d)-[r:IN_CONTEXT]->(:Context) DELETE r")
_SET_CLIENT = ("MATCH (d:Deliverable {id: $id, userId: $userId}) "
               "MATCH (c:Client {id: $scopeId, userId: $userId}) "
               "MERGE (d)-[:FOR_CLIENT]->(c)")
_SET_CONTEXT = ("MATCH (d:Deliverable {id: $id, userId: $userId}) "
                "MATCH (c:Context {id: $scopeId, userId: $userId}) "
                "MERGE (d)-[:IN_CONTEXT]->(c)")

_LINK_DIARY = ("MATCH (d:Deliverable {id: $id, userId: $userId}) "
               "MATCH (s:DiaryEntry {id: $sourceId, userId: $userId}) "
               "MERGE (d)-[:DELIVERED_IN]->(s)")
_LINK_FACT = ("MATCH (d:Deliverable {id: $id, userId: $userId}) "
              "MATCH (s:Fact {id: $sourceId, userId: $userId}) "
              "MERGE (d)-[:DELIVERED_IN]->(s)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_kind(value) -> str:
    kind = (value or "").strip().lower()
    return kind if kind in DELIVERABLE_KINDS else DEFAULT_KIND


def _apply_scope(session, item_id, user_id, client_id, context_id) -> None:
    """Delete-then-MERGE: scope is one edge, so a move must not leave two."""
    session.run(_CLEAR_CLIENT, id=item_id, userId=user_id)
    session.run(_CLEAR_CONTEXT, id=item_id, userId=user_id)
    if client_id:
        session.run(_SET_CLIENT, id=item_id, userId=user_id, scopeId=client_id)
    if context_id:
        session.run(_SET_CONTEXT, id=item_id, userId=user_id, scopeId=context_id)


def _link_source(session, item_id, user_id, source_id, source_label) -> None:
    if not source_id:
        return
    query = _LINK_FACT if str(source_label or "").lower() == "fact" else _LINK_DIARY
    session.run(query, id=item_id, userId=user_id, sourceId=source_id)


async def db_sync_deliverables(source_id, source_label, user_id, items,
                               client_id=None, context_id=None) -> dict:
    """Create the artifacts a source now names, and update the ones it still does.

    There is deliberately no `authoritative` flag. A commitment the minutes stop
    mentioning was withdrawn; an assessment that was delivered was delivered,
    and a register that forgets it on the next save is worse than one that
    never had it.
    """
    result = {"ok": True, "created": 0, "updated": 0}
    driver = get_neo4j()
    if not driver:
        result["ok"] = False
        return result

    items = list(items or [])
    with driver.session() as session:
        existing = {}
        for row in session.run(
                "MATCH (d:Deliverable {userId: $userId, sourceId: $sourceId}) "
                "RETURN d.id AS id, coalesce(d.key, '') AS key, "
                "coalesce(d.status, 'draft') AS status",
                userId=user_id, sourceId=source_id):
            existing[row["key"]] = {"id": row["id"], "status": row["status"]}

        for item in items:
            key = item.get("key") or normalise_deliverable_name(item.get("name"))
            if not key:
                continue
            previous = existing.get(key)
            if previous:
                outcome = session.run(
                    "MATCH (d:Deliverable {id: $id, userId: $userId}) "
                    "SET d.name = $name, d.kind = $kind, d.updatedAt = datetime() "
                    "RETURN d.id AS id",
                    id=previous["id"], userId=user_id,
                    name=item.get("name"), kind=_clean_kind(item.get("kind")),
                ).single()
                if outcome:
                    result["updated"] += 1
                if item.get("deliveredAt"):
                    session.run(
                        "MATCH (d:Deliverable {id: $id, userId: $userId}) "
                        "SET d.deliveredAt = $deliveredAt",
                        id=previous["id"], userId=user_id,
                        deliveredAt=item["deliveredAt"][:10],
                    )
                continue

            item_id = str(uuid.uuid4())
            session.run(
                "CREATE (d:Deliverable {id: $id, userId: $userId, "
                "name: $name, key: $key, kind: $kind, status: $status, "
                "deliveredAt: $deliveredAt, sourceId: $sourceId, "
                "sourceLabel: $sourceLabel, createdAt: datetime(), "
                "updatedAt: datetime()})",
                id=item_id, userId=user_id, name=item.get("name"), key=key,
                kind=_clean_kind(item.get("kind") or infer_kind(item.get("name"))),
                status=normalise_status(item.get("status")),
                deliveredAt=(item.get("deliveredAt") or "")[:10] or None,
                sourceId=source_id or "", sourceLabel=source_label or "",
            )
            _apply_scope(session, item_id, user_id, client_id, context_id)
            _link_source(session, item_id, user_id, source_id, source_label)
            result["created"] += 1

    logger.info("deliverables: source=%s created=%s updated=%s",
                source_id, result["created"], result["updated"])
    return result


def db_list_deliverables(user_id, client_id=None, context_id=None,
                         status=None, kind=None, source_id=None,
                         limit=DEFAULT_LIMIT) -> list:
    """The register for one scope, newest first.

    `client_id` / `context_id` **filter** here, unlike search: the panel asks a
    closed question, so an artifact from another engagement is a wrong answer
    rather than a lower-ranked one.
    """
    driver = get_neo4j()
    if not driver:
        return []
    clauses = []
    params = {"userId": user_id, "limit": limit}
    if client_id:
        clauses.append("MATCH (d)-[:FOR_CLIENT]->(:Client {id: $clientId, userId: $userId})")
        params["clientId"] = client_id
    if context_id:
        clauses.append("MATCH (d)-[:IN_CONTEXT]->(:Context {id: $contextId, userId: $userId})")
        params["contextId"] = context_id
    if source_id:
        clauses.append("MATCH (d)-[:DELIVERED_IN]->(:DiaryEntry {id: $sourceId, userId: $userId})")
        params["sourceId"] = source_id
    where = []
    if status:
        where.append("coalesce(d.status, 'draft') = $status")
        params["status"] = normalise_status(status)
    if kind:
        where.append("coalesce(d.kind, 'other') = $kind")
        params["kind"] = _clean_kind(kind)

    query = "MATCH (d:Deliverable {userId: $userId})\n"
    if clauses:
        query += "\n".join(clauses) + "\n"
    if where:
        query += "WHERE " + " AND ".join(where) + "\n"
    query += (
        "OPTIONAL MATCH (d)-[:FOR_CLIENT]->(cl:Client)\n"
        "WITH d, cl\n"
        "OPTIONAL MATCH (d)-[:IN_CONTEXT]->(ctx:Context)\n"
        "WITH d, cl, ctx\n"
        "RETURN d.id AS id, d.name AS name, coalesce(d.kind, 'other') AS kind, "
        "coalesce(d.status, 'draft') AS status, coalesce(d.deliveredAt, '') AS deliveredAt, "
        "coalesce(d.sourceId, '') AS sourceId, coalesce(d.sourceLabel, '') AS sourceLabel, "
        "cl.id AS clientId, cl.name AS clientName, ctx.id AS contextId, ctx.name AS contextName\n"
        "ORDER BY coalesce(d.deliveredAt, '') DESC, d.name ASC\n"
        "LIMIT $limit"
    )
    try:
        with driver.session() as session:
            return [dict(row) for row in session.run(query, **params)]
    except Exception as exc:  # a read must not 500 a panel
        logger.warning("deliverables: list failed: %s", exc)
        return []


def db_create_deliverable(user_id, name, kind=None, status=None, delivered_at=None,
                          client_id=None, context_id=None, source_id=None) -> dict:
    """A manual entry: an artifact nobody wrote into a meeting."""
    cleaned = (name or "").strip()
    if not cleaned:
        return {"ok": False, "reason": "A deliverable needs a name."}
    driver = get_neo4j()
    if not driver:
        return {"ok": False, "reason": "Neo4j not connected."}

    item_id = str(uuid.uuid4())
    with driver.session() as session:
        session.run(
            "CREATE (d:Deliverable {id: $id, userId: $userId, name: $name, "
            "key: $key, kind: $kind, status: $status, deliveredAt: $deliveredAt, "
            "sourceId: $sourceId, sourceLabel: 'manual', "
            "createdAt: datetime(), updatedAt: datetime()})",
            id=item_id, userId=user_id, name=cleaned,
            key=normalise_deliverable_name(cleaned),
            kind=_clean_kind(kind or infer_kind(cleaned)),
            status=normalise_status(status),
            deliveredAt=(delivered_at or "")[:10] or None,
            sourceId=source_id or "",
        )
        _apply_scope(session, item_id, user_id, client_id, context_id)
        row = session.run(
            "MATCH (d:Deliverable {id: $id, userId: $userId}) "
            "RETURN d.id AS id, d.name AS name, coalesce(d.kind, 'other') AS kind, "
            "coalesce(d.status, 'draft') AS status, "
            "coalesce(d.deliveredAt, '') AS deliveredAt, "
            "coalesce(d.sourceId, '') AS sourceId",
            id=item_id, userId=user_id,
        ).single()
    return {"ok": True, "item": dict(row) if row else {"id": item_id}}


def db_update_deliverable(item_id, user_id, name=None, kind=None, status=None,
                          delivered_at=None) -> dict:
    """Ownership is in the MATCH, so another vault's id answers as unknown."""
    updates = {}
    if name is not None:
        cleaned = (name or "").strip()
        if not cleaned:
            return {"ok": False, "reason": "A deliverable needs a name."}
        updates["name"] = cleaned
        updates["key"] = normalise_deliverable_name(cleaned)
    if kind is not None:
        updates["kind"] = _clean_kind(kind)
    if status is not None:
        updates["status"] = normalise_status(status)
    if delivered_at is not None:
        updates["deliveredAt"] = (delivered_at or "")[:10] or None
    if not updates:
        return {"ok": False, "reason": "Nothing to update."}

    driver = get_neo4j()
    if not driver:
        return {"ok": False, "reason": "Neo4j not connected."}

    assignments = ", ".join("d.%s = $%s" % (k, k) for k in updates)
    params = dict(updates)
    params["id"] = item_id
    params["userId"] = user_id
    with driver.session() as session:
        row = session.run(
            "MATCH (d:Deliverable {id: $id, userId: $userId}) "
            "SET " + assignments + ", d.updatedAt = datetime() "
            "RETURN d.id AS id, d.name AS name, coalesce(d.kind, 'other') AS kind, "
            "coalesce(d.status, 'draft') AS status, "
            "coalesce(d.deliveredAt, '') AS deliveredAt",
            **params
        ).single()
    if not row:
        return {"ok": False, "reason": "No such deliverable in this vault."}
    return {"ok": True, "item": dict(row)}


async def sync_deliverables_from_text(source_id, source_label, user_id, content,
                                      delivered_at=None, client_id=None,
                                      context_id=None) -> dict:
    """Parse `## Deliverables` and reconcile it into the register."""
    items = parse_deliverables(content, delivered_at=delivered_at)
    return await db_sync_deliverables(
        source_id, source_label, user_id, items,
        client_id=client_id, context_id=context_id,
    )


def describe_deliverable(item, today=None) -> dict:
    from deliverables import describe_deliverable as _describe

    return _describe(item, today=today)
