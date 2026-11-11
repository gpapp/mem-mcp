"""deliverables.py -- the :Deliverable register, the half with no database in it.

A deliverable is the one thing a consultant is actually paid for: the
assessment, the roadmap, the deck, the report. Until this existed the only trace
of one in the vault was `original_file` in a diary entry's metadata, which is a
filename and nothing else -- it cannot say what the artifact was, whether it was
sent, or which engagement it belonged to.

**Nothing here writes and nothing here guesses a status.** The kind is inferred
from the name, because "which bucket is this" is a labelling convenience and a
wrong label costs a filter. The status is **read**, from a marker somebody wrote,
because "was this sent to the client" is a fact about the world: a register that
invents it is worse than one that leaves it at `draft`.

The shapes are the ones `:ActionItem` and `:Decision` already use, including the
reconcile rule: extraction matches on the normalised name within one source, so
re-saving minutes updates a row instead of appending a duplicate.
"""

import re
from datetime import date

DELIVERABLE_KINDS = ("assessment", "roadmap", "deck", "report", "other")
DELIVERABLE_STATUSES = ("draft", "in-review", "sent", "approved")

DEFAULT_KIND = "other"
DEFAULT_STATUS = "draft"

#: `approved` is the only terminal status. A deck that was sent can come back
#: for revision, and a register that treats `sent` as final would then describe
#: work as finished that the client has just red-lined.
TERMINAL_STATUSES = ("approved",)

_DELIVERABLE_SECTIONS = {
    "deliverable", "deliverables", "artefact", "artefacts",
    "artifact", "artifacts", "output", "outputs", "what we delivered",
}

_HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BULLET_LINE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(.*\S)[ \t]*$")
_CHECKBOX = re.compile(r"^\[([ xX])\][ \t]+")
_STATUS_MARKER = re.compile(
    r"^\[(draft|in[ -]?review|sent|approved)\][ \t:.\u2014\u2013-]*", re.IGNORECASE)

#: Ordered, first match wins. `assessment` before `report` because "readiness
#: assessment report" is an assessment, and the longer noun is the product.
_KIND_WORDS = (
    ("assessment", ("assessment", "readiness", "audit", "diagnostic",
                    "evaluation", "maturity")),
    ("roadmap", ("roadmap", "road map", "strategy", "target architecture")),
    ("deck", ("deck", "presentation", "slides", "slide pack")),
    ("report", ("report", "write-up", "writeup", "memo", "findings")),
)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def normalise_status(value, default=DEFAULT_STATUS) -> str:
    """`In review`, `in-review` and `IN REVIEW` are one status."""
    raw = _text(value).lower().replace("_", " ").replace("-", " ")
    raw = re.sub(r"[ \t]+", " ", raw).strip()
    if not raw:
        return default
    if raw in ("in review", "inreview"):
        return "in-review"
    return raw if raw in DELIVERABLE_STATUSES else default


def infer_kind(name) -> str:
    """The bucket a human would put this in, from its own name.

    Deliberately a word list and not a model call: the cost of being wrong is a
    filter row in the wrong group, and the cost of a model call on every save is
    one more chat request on a card that is already contended.
    """
    lowered = _text(name).lower()
    if not lowered:
        return DEFAULT_KIND
    for kind, words in _KIND_WORDS:
        for word in words:
            if word in lowered:
                return kind
    return DEFAULT_KIND


def normalise_deliverable_name(text) -> str:
    """The key a re-save reconciles on: no marker, no case, no punctuation.

    The status marker is stripped so that flipping a deliverable from `draft` to
    `sent` updates the row instead of forking it into two artefacts with the
    same name.
    """
    raw = _text(text)
    raw = _CHECKBOX.sub("", raw)
    raw = _STATUS_MARKER.sub("", raw)
    raw = re.sub(r"[ \t]+", " ", raw).strip()
    raw = raw.rstrip(".,;: ").strip()
    return raw.lower()


def _clean_name(text) -> str:
    raw = _text(text)
    raw = _CHECKBOX.sub("", raw)
    raw = _STATUS_MARKER.sub("", raw)
    raw = re.sub(r"[ \t]+", " ", raw).strip()
    return raw.rstrip(".,;: ").strip()


def parse_deliverables(text, delivered_at=None) -> list:
    """Bullets under a `## Deliverables` heading, in document order.

    Only that heading (and its synonyms) is read. Every other section is full of
    things somebody called a deliverable in passing, and a register built from
    those is a register nobody trusts.
    """
    if not text:
        return []
    lines = str(text).splitlines()
    found = []
    seen = set()
    in_section = False
    for line in lines:
        heading = _HEADING_LINE.match(line)
        if heading:
            title = heading.group(2).strip().lower()
            in_section = title in _DELIVERABLE_SECTIONS
            continue
        if not in_section:
            continue
        bullet = _BULLET_LINE.match(line)
        if not bullet:
            continue
        raw = bullet.group(1)
        marker = _STATUS_MARKER.match(raw)
        status = normalise_status(marker.group(1)) if marker else DEFAULT_STATUS
        name = _clean_name(raw)
        if not name:
            continue
        key = normalise_deliverable_name(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append({
            "name": name,
            "key": key,
            "kind": infer_kind(name),
            "status": status,
            "deliveredAt": _text(delivered_at),
        })
    return found


def _days_between(value, today) -> int:
    try:
        then = date.fromisoformat(_text(value)[:10])
        now = date.fromisoformat(_text(today)[:10])
    except (TypeError, ValueError):
        return -1
    return (now - then).days


def describe_deliverable(row, today=None) -> dict:
    """Read-side derivation: nothing here is stored.

    `ageDays` is computed rather than persisted for the same reason an action
    item's `overdue` is: a stored number is wrong the moment midnight passes.
    """
    item = dict(row or {})
    item["kind"] = _text(item.get("kind")) or DEFAULT_KIND
    if item["kind"] not in DELIVERABLE_KINDS:
        item["kind"] = DEFAULT_KIND
    item["status"] = normalise_status(item.get("status"))
    item["statusLabel"] = item["status"].replace("-", " ")
    item["delivered"] = bool(_text(item.get("deliveredAt")))
    item["terminal"] = item["status"] in TERMINAL_STATUSES
    item["ageDays"] = _days_between(item.get("deliveredAt"), today) if today else -1
    return item


def sort_deliverables(rows) -> list:
    """Newest first, undated last, then by name.

    A register sorted by name is the shape a spreadsheet has; a reader asking
    "what did we just ship" wants the other end.
    """
    dated = [dict(r) for r in rows or [] if _text(r.get("deliveredAt"))[:10]]
    undated = [dict(r) for r in rows or [] if not _text(r.get("deliveredAt"))[:10]]
    dated.sort(key=lambda r: _text(r.get("deliveredAt"))[:10], reverse=True)
    undated.sort(key=lambda r: _text(r.get("name")).lower())
    return dated + undated
