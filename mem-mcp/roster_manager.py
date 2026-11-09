"""Who is on this engagement, and when we last spoke to them.

Read-only. Every number here is derived from edges the rest of the app already
writes -- `(:DiaryEntry)-[:MENTIONS]->(:Fact)` from the people extractor, and
`FOR_CLIENT` / `IN_CONTEXT` from the scope classifier -- so adding the roster
costs no migration and cannot disagree with what the diary says.

The one trap is that a `MENTIONS` edge does not mean "person": link management
lets a user attach any fact to an entry, so an entry discussing a migration has
a `MENTIONS` edge to a `Technology` fact. Every query here therefore constrains
the target to category `People` rather than trusting the edge type to carry the
meaning.
"""

import os

from common import get_neo4j, logger
from roster import (STALE_DAYS_DEFAULT, build_roster, render_roster, stale_rows)

ROSTER_STALE_DAYS = int(os.getenv("MEM_ROSTER_STALE_DAYS") or STALE_DAYS_DEFAULT)
ROSTER_LIMIT = int(os.getenv("MEM_ROSTER_LIMIT") or "200")

_PEOPLE_FILTER = "toLower(coalesce(p.category, '')) = 'people'"


def _scope_clause(kind: str) -> str:
    """The scope edge a person or their meeting can be attached by.

    A client roster is built from both halves on purpose. A People fact is
    often scoped to the client (``FOR_CLIENT``), but the classifier leaves many
    unscoped -- they are only ever *mentioned* in an entry that is scoped. A
    roster that reads one half lists half the room.
    """
    if kind == "context":
        return ":Context"
    return ":Client"


def _query(kind: str) -> str:
    """One query, two scope halves, and a mention count that ignores scope.

    ``mentionCount`` and ``lastSeen`` are deliberately **not** scoped. "When did
    I last speak to this person" is a question about the person, not about one
    engagement, and scoping it would report somebody as gone quiet because the
    recent conversations were filed under a different project.

    ``LIMIT`` is applied after the scope test, so the cap trims the roster
    rather than trimming the candidate set before the interesting rows are
    found.
    """
    label = _scope_clause(kind)
    rel = "IN_CONTEXT" if kind == "context" else "FOR_CLIENT"
    return f"""
MATCH (p:Fact {{userId: $userId}})
WHERE {_PEOPLE_FILTER}
OPTIONAL MATCH (p)-[:{rel}]->(direct{label} {{id: $scopeId, userId: $userId}})
OPTIONAL MATCH (d:DiaryEntry {{userId: $userId}})-[:MENTIONS]->(p)
OPTIONAL MATCH (d)-[:{rel}]->(via{label} {{id: $scopeId, userId: $userId}})
WITH p,
     count(DISTINCT direct) AS directScope,
     count(DISTINCT via) AS viaScope,
     count(DISTINCT d) AS mentionCount,
     max(d.timestamp) AS lastSeen
WHERE directScope > 0 OR viaScope > 0
RETURN p.id AS id, p.name AS name, p.text AS text,
       mentionCount, lastSeen
ORDER BY toLower(p.name)
LIMIT $limit
"""


def _rows(result) -> list:
    try:
        return [dict(row) for row in result.data()]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("roster: could not read %r: %s", result, exc)
        return []


def db_stakeholder_roster(user_id: str, client_id: str = None,
                          context_id: str = None, today: str = None,
                          stale_days: int = None) -> dict:
    """Build the roster for one client or project.

    Returns ``{ok, rows, stale, staleDays, markdown}``. ``ok: False`` with an
    empty roster means the graph could not be read -- which the panel has to be
    able to tell apart from "nobody recorded", because one of those is a
    transient fault and the other is a hole in the notes.

    Raises ``ValueError`` when neither scope id is given: a roster of *everyone*
    is not a stakeholder roster, it is the People category with a different
    heading, and rendering it in a client panel would be a lie about scope.
    """
    if not client_id and not context_id:
        raise ValueError("a roster needs a client or a context")

    driver = get_neo4j()
    if driver is None:
        logger.warning("roster: graph unavailable -- no roster for %s",
                       client_id or context_id)
        return {"ok": False, "rows": [], "stale": [], "staleDays": 0,
                "markdown": "_The vault is not reachable right now._"}

    kind = "context" if (context_id and not client_id) else "client"
    scope_id = context_id if kind == "context" else client_id
    window = ROSTER_STALE_DAYS if stale_days is None else int(stale_days)

    try:
        with driver.session() as session:
            result = session.run(_query(kind), userId=user_id,
                                 scopeId=scope_id, limit=ROSTER_LIMIT)
            people = _rows(result)
    except Exception as exc:
        logger.warning("roster: query failed for %s: %s", scope_id, exc)
        return {"ok": False, "rows": [], "stale": [], "staleDays": window,
                "markdown": "_The roster could not be read._"}

    rows = build_roster(people, [], today=today, stale_days=window)
    return {
        "ok": True,
        "rows": rows,
        "stale": stale_rows(rows),
        "staleDays": window,
        "markdown": render_roster(rows, stale_days=window),
    }


def attach_open_actions(roster: dict, actions: list) -> dict:
    """Fold open-commitment counts into a built roster.

    Kept separate from the query because the action register and the graph are
    two reads with two failure modes: a roster that renders without the counts
    is still useful, and a roster that fails because the action query failed
    would be the wrong trade.
    """
    if not roster.get("ok"):
        return roster
    rows = build_roster(
        [{"id": row["id"], "name": row["name"], "lastSeen": row["lastSeen"],
          "mentionCount": row["mentionCount"],
          "text": row.get("text")} for row in roster.get("rows") or ()],
        actions or [],
        stale_days=roster.get("staleDays") or ROSTER_STALE_DAYS,
    )
    # `parse_role` already ran; carry the result rather than re-deriving it from
    # a dict that no longer holds the body.
    for row, original in zip(rows, roster.get("rows") or ()):
        row["role"] = original.get("role")
    return {
        **roster,
        "rows": rows,
        "stale": stale_rows(rows),
        "markdown": render_roster(rows, stale_days=roster.get("staleDays")
                                  or ROSTER_STALE_DAYS),
    }
