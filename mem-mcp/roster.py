"""The stakeholder roster: who is on this engagement, and when we last spoke.

Pure layout and parsing, standard library only, **zero app imports** -- for the
same reason `briefing.py` has none. The rules worth testing are the ones that
decide what the roster says, and `roster_manager.py` reaches `common` and a
graph driver, so nothing inside it can be called by a suite on a box with no
database.

Three rules shape what follows:

- **A role is read, never inferred.** If the fact body does not state one, the
  roster says nothing about it. A consultant reading "CTO" next to a name will
  repeat it in the room, so a guess here is worse than a blank.
- **Staleness is stated, not implied.** A person with no date is not "fresh"
  and not "stale"; they are unmeasured, and the roster says so.
- **Nobody is dropped for being quiet.** A silent stakeholder is precisely the
  one worth seeing, so an old date sorts to the top rather than out of the list.

People are `:Fact` rows in category `People`, and the people extractor already
writes `(:DiaryEntry)-[:MENTIONS]->(:Fact)`. There is deliberately no `:Person`
node: the data is already there, and a parallel type would need a migration and
would immediately disagree with the facts it mirrors.
"""

from datetime import date
from typing import Optional

ROLE_LABELS = ("role", "title", "job title", "position", "function")
STALE_DAYS_DEFAULT = 60
ROLE_MAX_CHARS = 80
EMPTY = "_No stakeholders recorded._"

_LABEL_MARKERS = ("**%s:**", "**%s**:", "%s:", "- %s:")


def _one_line(value: object) -> str:
    """Collapse every run of whitespace, so a wrapped sentence reads as one."""
    return " ".join(str(value or "").split())


def _clean_role(value: object) -> Optional[str]:
    """Trim a candidate role to something a person would recognise."""
    text = _one_line(value).strip(" *_-\u2014\u2013:;,")
    if not text:
        return None
    # "Bob works as a data engineer" leaves "a data engineer" behind. The
    # article belongs to the sentence, not to the title, and a roster that
    # prints "a data engineer" beside every name reads like a machine wrote it.
    lowered = text.lower()
    for article in ("the ", "an ", "a "):
        if lowered.startswith(article) and len(text) > len(article):
            text = text[len(article):]
            break
    # A label swallowed the rest of the paragraph; keep the opening clause.
    for stop in (". ", "; ", " and ", ", "):
        if stop in text and len(text) > ROLE_MAX_CHARS:
            text = text.split(stop, 1)[0]
    text = text.strip(" *_-\u2014\u2013:;,")
    if not text or len(text) > ROLE_MAX_CHARS:
        return None
    return text


def parse_role(text: object, name: object = None) -> Optional[str]:
    """Read a stated role out of a People fact body, or return None.

    Three shapes are recognised, in this order, because each is a case where
    the person who wrote the fact said the role explicitly:

    1. a labelled line -- ``Role: CTO``, ``**Title:** Head of Data``;
    2. a sentence that predicates the role of the name -- ``Tim is the CTO at
       Deutsche Bank``;
    3. a name followed by a dash -- ``Tim Smith -- CTO, Deutsche Bank``.

    Nothing else. A body that merely mentions a job word ("the CTO asked…") is
    not a statement about this person's role, and reading it as one is how a
    roster starts inventing org charts.
    """
    body = _one_line(text)
    if not body:
        return None

    lowered = body.lower()
    for label in ROLE_LABELS:
        for marker in _LABEL_MARKERS:
            needle = (marker % label).lower()
            index = lowered.find(needle)
            if index < 0:
                continue
            rest = body[index + len(needle):]
            role = _clean_role(rest.split("|")[0])
            if role:
                return role

    person = _one_line(name)
    if person:
        # "Tim Smith is the CTO at ..." / "Tim Smith is CTO."
        for pronoun in (" is the ", " is a ", " is an ", " is ", " works as ", " serves as "):
            needle = f"{person.lower()}{pronoun}"
            index = lowered.find(needle)
            if index < 0:
                continue
            rest = body[index + len(needle):]
            if " at " in rest.lower():
                rest = rest[:rest.lower().index(" at ")]
            role = _clean_role(rest)
            if role:
                return role

        # "Tim Smith -- CTO, Deutsche Bank" / "Tim Smith - CTO"
        for dash in (" \u2014 ", " \u2013 ", " - "):
            needle = f"{person.lower()}{dash}"
            index = lowered.find(needle)
            if index < 0:
                continue
            rest = body[index + len(needle):]
            cut = rest.split(",")[0]
            role = _clean_role(cut)
            if role:
                return role

    return None


def _parse_day(value: object) -> Optional[date]:
    """Read the date half of a stored timestamp, or None when it is unreadable.

    Deliberately tolerant of a full datetime, an ISO date and a bare
    ``YYYY-MM-DD`` -- the graph hands back whichever the writer used, and being
    strict here would silently report a measured person as unmeasured.
    """
    text = _one_line(value)
    if len(text) < 10:
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except (ValueError, TypeError):
        return None


def days_since(value: object, today: object = None) -> Optional[int]:
    """Whole days between a stored timestamp and today, or None if unreadable."""
    seen = _parse_day(value)
    if seen is None:
        return None
    if today is None:
        current = date.today()
    elif isinstance(today, date):
        current = today
    else:
        current = _parse_day(today)
        if current is None:
            current = date.today()
    return (current - seen).days


def build_roster(people: list, actions: list = None, today: object = None,
                 stale_days: int = STALE_DAYS_DEFAULT) -> list:
    """Turn People facts plus action items into roster rows.

    ``people`` rows need ``name`` and ``text``; ``lastSeen`` and ``mentionCount``
    are used when the graph supplied them. ``actions`` is the open-commitment
    list, and the only thing read from it is ``owner`` -- an owner that does not
    match a name is simply not counted, because inferring that two spellings
    are one person is the same mistake the people matcher already refuses to
    make without confidence.

    Rows are sorted so the quietest people come first: no date at all, then the
    oldest. A roster sorted alphabetically hides exactly the person it exists to
    surface.
    """
    open_by_owner: dict = {}
    for item in actions or ():
        owner = _one_line((item or {}).get("owner")).lower()
        if owner:
            open_by_owner[owner] = open_by_owner.get(owner, 0) + 1

    rows = []
    for person in people or ():
        if not isinstance(person, dict):
            continue
        name = _one_line(person.get("name"))
        if not name:
            continue
        gap = days_since(person.get("lastSeen"), today)
        try:
            mentions = int(person.get("mentionCount") or 0)
        except (TypeError, ValueError):
            mentions = 0
        rows.append({
            "id": person.get("id"),
            "name": name,
            "role": parse_role(person.get("text"), name),
            "lastSeen": _one_line(person.get("lastSeen"))[:10] or None,
            "daysSince": gap,
            "mentionCount": mentions,
            "openActions": open_by_owner.get(name.lower(), 0),
            # A window of zero is not "everything is stale", it is "do not
            # flag" -- the operator setting `MEM_ROSTER_STALE_DAYS=0` is
            # switching the warning off, and `gap >= 0` is true of every
            # person who has ever been mentioned. The other reading is
            # arithmetically correct and operationally the opposite of what
            # was asked for.
            "stale": bool(stale_days) and gap is not None and gap >= stale_days,
            "measured": gap is not None,
        })

    rows.sort(key=lambda row: (row["daysSince"] is not None,
                               -(row["daysSince"] or 0),
                               row["name"].lower()))
    return rows


def stale_rows(rows: list) -> list:
    """The subset worth chasing: measured, and quiet for longer than the window."""
    return [row for row in rows if row["stale"]]


def render_roster(rows: list, stale_days: int = STALE_DAYS_DEFAULT) -> str:
    """Markdown table for the client panel.

    A column is only shown when at least one row can fill it -- an all-empty
    "Role" column is noise that makes the table look broken. The gone-quiet
    count is stated in a line under the table rather than left to be counted,
    because "four people have gone quiet" is the sentence that prompts an email
    and a table is not.
    """
    if not rows:
        return EMPTY

    show_role = any(row["role"] for row in rows)
    show_actions = any(row["openActions"] for row in rows)

    header = ["| Stakeholder |"]
    divider = ["|---|"]
    if show_role:
        header.append(" Role |")
        divider.append("---|")
    header.append(" Last spoken |")
    divider.append("---|")
    header.append(" Mentions |")
    divider.append("---|")
    if show_actions:
        header.append(" Open actions |")
        divider.append("---|")

    lines = ["".join(header), "".join(divider)]
    for row in rows:
        cells = ["| ", row["name"], " |"]
        if show_role:
            cells += [" ", row["role"] or "\u2014", " |"]
        if row["daysSince"] is None:
            cells += [" not recorded |"]
        elif row["stale"]:
            cells += [" **", row["lastSeen"], f"** ({row['daysSince']}d) |"]
        else:
            cells += [" ", row["lastSeen"], f" ({row['daysSince']}d) |"]
        cells += [" ", str(row["mentionCount"]), " |"]
        if show_actions:
            cells += [" ", str(row["openActions"]) if row["openActions"] else "\u2014", " |"]
        lines.append("".join(cells))

    quiet = stale_rows(rows)
    if quiet:
        names = ", ".join(row["name"] for row in quiet[:5])
        more = f" and {len(quiet) - 5} more" if len(quiet) > 5 else ""
        lines.append("")
        lines.append(f"{len(quiet)} stakeholder(s) not mentioned in {stale_days} days: "
                     f"{names}{more}.")
    return "\n".join(lines)
