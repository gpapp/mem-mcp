"""timeline_manager.py -- the reading half of the engagement timeline.

One function composes four reads that already exist rather than inventing a
fifth way to ask "what is linked to this client":

* **facts and diary entries** come from ``client_manager._scope_items``, which
  is the only place the scope question is answered through the ``FOR_CLIENT`` /
  ``IN_CONTEXT`` **edges** and through the sharing filter. Re-deriving it here
  would get both wrong: no ``Fact`` or ``DiaryEntry`` node carries a
  ``clientId`` property — that key is a Qdrant payload key — so a property read
  answers ``None`` for every node and the whole timeline comes back empty.
* **decisions** come from ``decision_manager.db_list_decisions``, which already
  filters by a scope id and already includes superseded rows.
* **closed commitments** are the one read that has no existing caller, so the
  query lives here. It selects rows with a terminal status and orders them by
  when they were closed rather than by their due date.

The merging, month grouping and ordering are in ``timeline.py``, which has no
database in it and is therefore testable on this box.

**A shared engagement shows its meetings and facts, and says so about the
rest.** Facts and diary entries belong to the scope node's owner and
``_scope_items`` already filters them through the sharing rules. Decisions and
commitments are read under the *caller's* ``userId`` — a grantee may read the
owner's records but does not own their decision log — so for a shared panel the
two streams are skipped and a ``note`` explains it. Rendering an owner's
decisions in a grantee's panel would be a leak, and rendering an empty list
without the note would read as "this engagement has no decisions", which is
false.
"""

from common import get_neo4j, logger
from client_manager import _scope_items
from decision_manager import db_list_decisions
from timeline import build_timeline

DEFAULT_LIMIT = 200
MAX_PARTICIPANTS = 12

_CLOSED_ACTIONS_QUERY = """
MATCH (a:ActionItem {userId: $userId})
WHERE coalesce(a.status, 'open') IN ['done', 'dropped']
OPTIONAL MATCH (a)-[:FOR_CLIENT]->(cl:Client)
OPTIONAL MATCH (a)-[:IN_CONTEXT]->(ctx:Context)
WITH a, collect(DISTINCT cl)[0] AS cl, collect(DISTINCT ctx)[0] AS ctx
WHERE ($clientId IS NULL OR cl.id = $clientId)
  AND ($contextId IS NULL OR ctx.id = $contextId)
RETURN a.id AS id, a.text AS text, a.owner AS owner, a.status AS status,
       toString(coalesce(a.updatedAt, a.createdAt)) AS updatedAt,
       a.sourceId AS sourceId
ORDER BY updatedAt DESC
LIMIT $limit
"""

_PARTICIPANTS_QUERY = """
MATCH (d:DiaryEntry)-[:MENTIONS]->(p:Fact)
WHERE d.id IN $ids
RETURN d.id AS id, p.name AS name, coalesce(p.text, '') AS text
"""


def _clean_limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return value if value > 0 else DEFAULT_LIMIT


def _participants(session, diary_ids) -> dict:
    """Names of the people each entry mentions, capped per entry.

    The cap is per entry and not per query: a meeting about twelve people is a
    meeting, and truncating the whole result would silently drop the attendees
    of whichever entries happened to sort last.
    """
    found = {}
    if not diary_ids:
        return found
    for row in session.run(_PARTICIPANTS_QUERY, ids=list(diary_ids)):
        name = row["name"] or (row["text"] or "").strip()
        if not name:
            continue
        bucket = found.setdefault(row["id"], [])
        if name not in bucket and len(bucket) < MAX_PARTICIPANTS:
            bucket.append(name)
    return found


def _closed_actions(session, user_id, client_id, context_id, limit) -> list:
    return [
        {"id": r["id"], "text": r["text"], "owner": r["owner"],
         "status": r["status"], "updatedAt": r["updatedAt"],
         "sourceId": r["sourceId"]}
        for r in session.run(
            _CLOSED_ACTIONS_QUERY,
            userId=user_id, clientId=client_id, contextId=context_id, limit=limit,
        )
    ]


def _recent_facts(facts, limit) -> list:
    """The newest facts, not the alphabetically first ones.

    ``_scope_items`` sorts facts by name because its panel is a list; a
    timeline needs the recent end of the same set, and taking the head of the
    name-sorted list would show whichever facts begin with "A".
    """
    dated = [f for f in facts if str(f.get("timestamp") or "")[:4].isdigit()]
    rest = [f for f in facts if f not in dated]
    dated.sort(key=lambda f: str(f.get("timestamp")), reverse=True)
    ordered = dated + rest
    return ordered[:limit] if limit and limit > 0 else ordered


def db_engagement_timeline(user_id: str, client_id: str = "", context_id: str = "",
                           limit: int = DEFAULT_LIMIT) -> dict:
    """One client's or project's story, newest first, grouped by month.

    Exactly one scope is required. "Everything, sorted by date" is a different
    question — it is the global diary list — and answering it under a client
    panel's route would make a mis-sent id look like an empty engagement.
    """
    client_id = (client_id or "").strip()
    context_id = (context_id or "").strip()
    if bool(client_id) == bool(context_id):
        raise ValueError(
            "A timeline is one client or one project — pass client_id or context_id, not both."
        )

    limit = _clean_limit(limit)
    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j not connected.")

    if client_id:
        scope = _scope_items(user_id, "", {"label": ":Client", "rel": ":FOR_CLIENT",
                                           "scopeId": client_id})
    else:
        scope = _scope_items(user_id, "", {"label": ":Context", "rel": ":IN_CONTEXT",
                                          "scopeId": context_id})
    if scope is None:
        raise ValueError("No such client or project in this vault.")

    diary = scope.get("diary") or []
    facts = scope.get("facts") or []
    shared = any(row.get("readOnly") for row in diary + facts)

    with neo4j_driver.session() as session:
        participants = _participants(session, [row["id"] for row in diary])
        if shared:
            decisions, actions = [], []
        else:
            actions = _closed_actions(session, user_id, client_id or None,
                                      context_id or None, limit)
            decisions = db_list_decisions(user_id, client_id=client_id or None,
                                          context_id=context_id or None,
                                          limit=limit, include_inactive=True)

    for row in diary:
        row["participants"] = participants.get(row["id"], [])

    payload = build_timeline(
        diary=diary,
        decisions=decisions,
        actions=actions,
        facts=_recent_facts(facts, limit),
        limit=limit,
    )
    payload["clientId"] = client_id or None
    payload["contextId"] = context_id or None
    payload["shared"] = shared
    if shared:
        payload["note"] = (
            "This engagement was shared with you. Its meetings and records are "
            "included; its decision log and commitments belong to the owner."
        )
    logger.debug(
        "timeline: client=%s context=%s events=%s months=%s shared=%s",
        client_id or "-", context_id or "-", len(payload["events"]),
        len(payload["months"]), shared,
    )
    return payload
