"""timeline.py -- the pure half of the engagement timeline.

The timeline answers "where did this engagement get to?" by merging four
independent streams into one date-ordered narrative: the meetings, the
decisions, the commitments that closed, and the facts that were added. It
composes reads the vault already has; nothing here writes.

**Why this module has no database in it.** The rules that decide what the
reader sees are all pure: which streams become their own row, which one absorbs
the others, how a month is labelled, and how a record with no date is ordered
against records that have one. Each of those is a silent defect when it is
wrong — a duplicated meeting, a decision shown twice, a month that reads
"2026-11" — and none of them raises. Keeping them importable means a suite can
call them on a machine with no Neo4j driver.

**A decision is not a separate row when its meeting is in the same view.** The
decision log stores where each decision came from, and the meeting row already
renders its own decisions. Emitting both would show the same decision twice,
once attached to its meeting and once floating, which reads as two decisions.
When the meeting is *not* in the result set — a live note, an entry outside the
window, a source that was deleted — the decision is promoted to its own row,
because a decision with no visible origin still has to be readable.
"""

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# The order independent events appear in when they share a timestamp. A meeting
# is the container a reader scans for, so it leads the day it happened on;
# facts trail, because a day with one meeting and six facts is a meeting day.
KIND_ORDER = {"diary": 0, "decision": 1, "action": 2, "fact": 3}

UNDATED = "0000-00-00"


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def day_of(value) -> str:
    """The calendar day a timestamp falls on, as ``YYYY-MM-DD``.

    Neo4j hands back ``toString()`` of a temporal, which carries a time and an
    offset; the vault also stores plain dates on diary entries. Both start with
    the day, so the first ten characters are the day in every shape the app
    writes. Anything shorter than a date is treated as undated rather than
    guessed at — a partial string is not a date.
    """
    text = _text(value)
    return text[:10] if len(text) >= 10 and text[4:5] == "-" else ""


def month_of(value) -> str:
    """The ``YYYY-MM`` bucket a timestamp belongs to, or ``""``."""
    day = day_of(value)
    return day[:7] if day else ""


def month_label(key: str) -> str:
    """``2026-11`` -> ``November 2026``. An unknown key is returned as it is.

    A month the reader cannot name is worse than no heading at all, so an
    unparseable key keeps its raw form instead of becoming "Invalid Date" —
    the reader can at least see what the data actually holds.
    """
    key = _text(key)
    try:
        year, month = key.split("-")
        index = int(month)
    except (ValueError, TypeError):
        return key
    if not 1 <= index <= 12:
        return key
    return f"{MONTH_NAMES[index - 1]} {year}"


def sort_key(event: dict):
    """Newest first, and stable within a day.

    A record with **no** date sorts last rather than first: undated means "we do
    not know when", and putting it at the top of a chronological narrative
    claims it happened recently. ``UNDATED`` is a sentinel so the tuple
    comparison stays total instead of comparing ``None`` to ``str``.
    """
    at = _text(event.get("at"))
    day = day_of(at) or UNDATED
    kind = KIND_ORDER.get(event.get("kind"), 9)
    title = _text(event.get("title")).lower()
    # Reverse the whole tuple, so a later day leads and, inside one day, the
    # kind order reads diary -> decision -> action -> fact.
    return (day, at[:19], -kind, title)


def _decision_row(decision: dict) -> dict:
    return {
        "kind": "decision",
        "id": decision.get("id"),
        "at": _text(decision.get("decidedAt")),
        "title": _text(decision.get("text")),
        "decider": _text(decision.get("decider")),
        "status": _text(decision.get("status")) or "active",
        "supersededBy": decision.get("supersededBy"),
        "sourceId": decision.get("sourceId") or "",
    }


def _action_row(item: dict) -> dict:
    return {
        "kind": "action",
        "id": item.get("id"),
        # An action is closed at the moment somebody marked it, which is
        # ``updatedAt``; its due date may be months earlier and would put the
        # row in the wrong month entirely.
        "at": _text(item.get("closedAt") or item.get("updatedAt")),
        "title": _text(item.get("text")),
        "owner": _text(item.get("owner")),
        "status": _text(item.get("status")),
        "sourceId": item.get("sourceId") or "",
    }


def _fact_row(fact: dict) -> dict:
    title = _text(fact.get("name")) or _text(fact.get("text"))
    if len(title) > 140:
        title = title[:137].rstrip() + "..."
    return {
        "kind": "fact",
        "id": fact.get("id"),
        "at": _text(fact.get("timestamp")),
        "title": title,
        "category": _text(fact.get("category")),
        "sourceId": "",
    }


def build_events(diary=(), decisions=(), actions=(), facts=()) -> list:
    """Normalise the four streams into timeline rows.

    The de-duplication is the whole function. Each decision is attached to its
    source meeting when that meeting is present in ``diary`` and dropped as a
    top-level row; otherwise it is promoted. Attaching happens before sorting
    so a promoted decision lands among the meetings by its own date.
    """
    meetings = []
    by_id = {}
    for entry in diary:
        row = {
            "kind": "diary",
            "id": entry.get("id"),
            "at": _text(entry.get("date")) or _text(entry.get("timestamp")),
            "title": _text(entry.get("name")) or "Untitled diary entry",
            "participants": list(entry.get("participants") or []),
            "decisions": [],
            "category": "",
            "sourceId": entry.get("id") or "",
        }
        meetings.append(row)
        if row["id"]:
            by_id[row["id"]] = row

    promoted = []
    for decision in decisions:
        row = _decision_row(decision)
        host = by_id.get(row["sourceId"]) if row["sourceId"] else None
        if host is not None:
            host["decisions"].append(row)
        else:
            promoted.append(row)

    # A meeting's own decisions read oldest-first: they happened in sequence
    # inside the meeting, and reversing that would show the conclusion before
    # the question it answered.
    for meeting in meetings:
        meeting["decisions"].sort(key=lambda d: (_text(d.get("at")), _text(d.get("title"))))

    rows = meetings + promoted
    rows += [_action_row(item) for item in actions]
    rows += [_fact_row(fact) for fact in facts]
    rows.sort(key=sort_key, reverse=True)
    return rows


def group_by_month(events, limit: int = 0) -> list:
    """Bucket rows into month groups, newest month first.

    The cap is applied **before** grouping, not per month: a reader asking for
    the last fifty events wants the last fifty, and a per-month cap would
    silently truncate a busy month while showing a quiet one in full.

    Undated rows share one final group labelled ``Undated`` rather than being
    dropped. A record nobody dated is still part of the engagement's story, and
    hiding it would make the timeline disagree with the record count.
    """
    rows = list(events)
    if limit and limit > 0:
        rows = rows[:limit]
    groups = {}
    order = []
    for row in rows:
        key = month_of(row.get("at"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    out = []
    for key in sorted(order, reverse=True):
        out.append({
            "month": key,
            "label": month_label(key) if key else "Undated",
            "events": groups[key],
        })
    return out


def summarise(events, months) -> dict:
    """Counts a panel can state without walking the rows itself."""
    counts = {"diary": 0, "decision": 0, "action": 0, "fact": 0, "undated": 0}
    for row in events:
        kind = row.get("kind") or ""
        if kind in counts:
            counts[kind] += 1
        # A decision nested under its meeting is still a decision, and a panel
        # that says "1 decision" when two were taken is the one number a reader
        # would quote. Count the nested rows too.
        for _ in row.get("decisions") or ():
            counts["decision"] += 1
        if not day_of(row.get("at")):
            counts["undated"] += 1
    # The list is newest-first, so the head is the latest date and the tail the
    # earliest. "first" reads as the start of the story, which is the old end.
    first = events[-1].get("at") if events else ""
    last = events[0].get("at") if events else ""
    return {
        "total": len(events),
        "months": len(months),
        "first": _text(first),
        "last": _text(last),
        **counts,
    }


def build_timeline(diary=(), decisions=(), actions=(), facts=(), limit: int = 200) -> dict:
    """The whole read, in one call: rows, month groups and counts."""
    events = build_events(diary=diary, decisions=decisions, actions=actions, facts=facts)
    if limit and limit > 0:
        events = events[:limit]
    months = group_by_month(events)
    return {"events": events, "months": months, "summary": summarise(events, months)}
