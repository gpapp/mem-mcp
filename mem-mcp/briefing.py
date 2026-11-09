"""Compose a one-page engagement brief out of what the vault already holds.

This module is **stdlib-only and pure**: it takes plain dicts and returns plain
strings, so every rule below is callable from a suite with no database, no
model and no web framework. The gathering lives in ``brief_manager.py`` and the
phrasing pass is one call to ``get_llm_response``; neither is testable here, and
neither is where the interesting decisions are.

Four decisions shape the output, and each is a rule rather than a preference:

**The brief is a read, and it is allowed to be incomplete.** A section with
nothing in it renders as an explicit line saying so. Omitting the section is
worse than saying "no open commitments": the reader cannot tell a quiet
engagement from a broken one, which is the same failure as an empty search that
looks like an empty vault.

**Open commitments come first, and overdue ones first within that.** The
question a consultant opens a brief to answer is "what am I behind on", so the
section that answers it cannot be below a summary of last quarter.

**Every claim is attributed to a record.** Facts carry their category and name,
meetings carry their date and title. A brief whose claims cannot be traced back
is one the reader has to verify, which costs more than reading the vault.

**Silence is stated, not implied.** The gaps section names what is missing — no
meetings recorded, no commitments captured, a project with no facts — because
"the vault knows nothing about this" is a finding, and an absent heading is not.
"""

# Rendered when a section has nothing in it. A named absence is a fact about
# the engagement; a missing heading is indistinguishable from a rendering bug.
EMPTY = "_Nothing recorded._"

# Titles the model is told to keep. Not a schema — the render pass is free to
# reorganise — but a brief whose headings move around every time it is
# regenerated is one the reader has to re-read rather than skim.
SECTION_WHERE = "Where this stands"
SECTION_ACTIONS = "Open commitments"
SECTION_MEETINGS = "Recent meetings"
SECTION_KNOWLEDGE = "What we know"
SECTION_GAPS = "Gaps"

CATEGORY_ORDER = ("Project", "Client", "People", "Technology", "Tool")

# A fact's body is quoted to give the brief something to compress. An
# unbounded body turns the prompt into the vault, and a brief is a summary --
# so each fact contributes an opening clause, and the count of facts is capped
# by the caller.
FACT_PREVIEW_CHARS = 240


def _one_line(value: object) -> str:
    """Collapse any text to a single line, for a bullet that must not wrap."""
    return " ".join(str(value or "").split())


def _preview(text: object, limit: int = FACT_PREVIEW_CHARS) -> str:
    """An opening clause of a fact body, with the cut marked."""
    flat = _one_line(text)
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def _action_line(item: dict) -> str:
    """One commitment, with the three things a reader acts on.

    Owner and due date are the whole point: "send the architecture doc" without
    either is a note, not a commitment. Both are omitted when absent rather
    than filled with a placeholder, because a placeholder reads as data.
    """
    text = _one_line(item.get("text")) or "(no text)"
    parts = [f"**{text}**"]
    owner = _one_line(item.get("owner"))
    if owner:
        parts.append(f"owner: {owner}")
    due = _one_line(item.get("dueAt"))
    if item.get("overdue"):
        parts.append(f"**OVERDUE** (due {due})" if due else "**OVERDUE**")
    elif due:
        parts.append(f"due {due}")
    if item.get("contextName") and not item.get("clientName"):
        parts.append(f"project: {_one_line(item['contextName'])}")
    return "- " + " — ".join(parts)


def _meeting_line(entry: dict) -> str:
    """One meeting, dated. The date is the point of the section."""
    date = _one_line(entry.get("date")) or _one_line(entry.get("timestamp"))[:10]
    name = _one_line(entry.get("name")) or "Unnamed entry"
    return f"- **{date or 'undated'}** — {name}"


def _fact_line(fact: dict) -> str:
    name = _one_line(fact.get("name")) or "(untitled)"
    body = _preview(fact.get("text"))
    return f"- **{name}** — {body}" if body else f"- **{name}**"


def group_facts_by_category(facts: list) -> dict:
    """Facts bucketed by category, each bucket name-sorted.

    Ordering is fixed by ``CATEGORY_ORDER`` and any unknown category is
    appended alphabetically after it, so a vault that grows a category still
    renders in the same shape rather than reshuffling every existing section.
    """
    buckets: dict = {}
    for fact in facts or ():
        category = _one_line(fact.get("category")) or "Uncategorised"
        buckets.setdefault(category, []).append(fact)
    for rows in buckets.values():
        rows.sort(key=lambda f: (_one_line(f.get("name")).lower(),
                                 _one_line(f.get("id"))))
    ordered = {name: buckets[name] for name in CATEGORY_ORDER if name in buckets}
    for name in sorted(buckets):
        if name not in ordered:
            ordered[name] = buckets[name]
    return ordered


def summarise(scope: dict, actions: list, facts: list, entries: list) -> dict:
    """Assemble the sections the brief renders from.

    A dict rather than Markdown so the caller can decide what to do with it --
    render it as the degraded form, or hand it to the model as material. The
    two paths then read the same data, which is what stops the fallback from
    describing a different engagement than the summary did.
    """
    name = _one_line(scope.get("name")) or "this engagement"
    projects = [p for p in (scope.get("contexts") or []) if isinstance(p, dict)]
    project_names = [_one_line(p.get("name")) for p in projects if p.get("name")]
    active = scope.get("effectiveActive", scope.get("active", True))

    where = [f"**{name}**{' — inactive' if active is False else ''}"]
    if project_names:
        where.append("Projects: " + ", ".join(project_names))
    else:
        where.append("Projects: _none recorded_")

    overdue = [a for a in (actions or ()) if a.get("overdue")]
    return {
        "name": name,
        "where": where,
        "actions": list(actions or ()),
        "overdue_count": len(overdue),
        "meetings": list(entries or ()),
        "facts": group_facts_by_category(facts),
        "project_count": len(project_names),
        "fact_count": len(facts or ()),
        "meeting_count": len(entries or ()),
    }


def gaps(brief: dict) -> list:
    """What the vault does not know, stated as sentences.

    Three separate absences, because they mean different things and have
    different remedies: no meeting recorded is a capture problem, no
    commitment captured is an extraction problem, and a project with no facts
    is a note-taking problem. Collapsing them into one "not much data" line
    tells the reader to go and look, which is the work the brief exists to save.
    """
    out = []
    if not brief["meeting_count"]:
        out.append("No meetings recorded for this engagement yet.")
    if not brief["actions"]:
        out.append("No open commitments captured — either nothing is owed, "
                   "or the actions in the minutes were not in a recognisable section.")
    elif not brief["overdue_count"]:
        out.append("No commitments are overdue.")
    if not brief["fact_count"]:
        out.append("No facts recorded against this client or project.")
    if not brief["project_count"]:
        out.append("No projects recorded under this client.")
    return out


def render(brief: dict, gaps_list: list = None) -> str:
    """The deterministic Markdown brief — also the fallback when no model answers.

    This is not a degraded second-class path: it is the same information the
    model is given and asked to phrase, rendered without the phrasing. If the
    summary pass fails, the reader loses polish and nothing else, which is why
    the route returns this rather than a 503.
    """
    lines = [f"# Brief — {brief['name']}", ""]

    lines += [f"## {SECTION_WHERE}", ""]
    lines += [f"- {item}" for item in brief["where"]]
    lines += ["", f"- {brief['meeting_count']} meeting(s), "
                  f"{brief['fact_count']} fact(s), "
                  f"{len(brief['actions'])} open commitment(s)", ""]

    lines += [f"## {SECTION_ACTIONS}", ""]
    if brief["actions"]:
        overdue = [a for a in brief["actions"] if a.get("overdue")]
        rest = [a for a in brief["actions"] if not a.get("overdue")]
        if overdue:
            lines += ["### Overdue", ""] + [_action_line(a) for a in overdue] + [""]
        if rest:
            lines += ["### Outstanding", ""] + [_action_line(a) for a in rest] + [""]
    else:
        lines += [EMPTY, ""]

    lines += [f"## {SECTION_MEETINGS}", ""]
    if brief["meetings"]:
        lines += [_meeting_line(e) for e in brief["meetings"]] + [""]
    else:
        lines += [EMPTY, ""]

    lines += [f"## {SECTION_KNOWLEDGE}", ""]
    if brief["facts"]:
        for category, rows in brief["facts"].items():
            lines += [f"### {category}", ""] + [_fact_line(f) for f in rows] + [""]
    else:
        lines += [EMPTY, ""]

    gaps_list = gaps(brief) if gaps_list is None else gaps_list
    if gaps_list:
        lines += [f"## {SECTION_GAPS}", ""] + [f"- {g}" for g in gaps_list] + [""]

    return "\n".join(lines).rstrip() + "\n"


SYSTEM_PROMPT = (
    "You write a one-page briefing for a technology consultant who is about to "
    "walk into a meeting about one engagement.\n"
    "You are given material that has already been gathered from their notes: "
    "the engagement, the open commitments, the recent meetings, and the facts "
    "recorded about the client, its people, its technology and its projects.\n"
    "Write the brief they would want to read in the lift on the way up.\n"
    "Rules:\n"
    "- Use ONLY the material supplied. Never add a person, date, number, "
    "decision or commitment that is not in it. An invented detail in a briefing "
    "is worse than a missing one, because the reader will repeat it to a client.\n"
    "- Keep every owner and every date exactly as given.\n"
    "- Lead with what is overdue or due next, then what changed recently, then "
    "the context needed to discuss it.\n"
    "- Say plainly when something is missing, rather than smoothing over it.\n"
    "- Keep the headings: {headings}.\n"
    "- Four hundred words or fewer. Prose and short bullets, no preamble, no "
    "closing offer to help."
)


def build_prompt(brief: dict, gaps_list: list = None) -> str:
    """The user turn: the deterministic render, which is already the material.

    The render is handed to the model rather than the raw rows because the
    render is what the fallback would have shown. One source for both paths
    means a failure changes the wording and nothing else — if the prompt were
    assembled separately, the two could describe different engagements and only
    one of them would be the one the reader saw yesterday.
    """
    headings = ", ".join([SECTION_WHERE, SECTION_ACTIONS, SECTION_MEETINGS,
                          SECTION_KNOWLEDGE, SECTION_GAPS])
    body = render(brief, gaps_list)
    return (SYSTEM_PROMPT.replace("{headings}", headings)
            + "\n\nMATERIAL (gathered from their notes):\n\n" + body)


def polish(text: object, fallback: str) -> str:
    """Use the model's answer when it is usable, otherwise the render.

    A model that returns nothing, whitespace, or a fragment short enough to be
    a refusal has not produced a brief, and returning it would replace a
    complete document with an empty one. The length floor is deliberately low —
    this catches an empty answer, not a terse one.
    """
    candidate = (text or "").strip()
    if len(candidate) < 80:
        return fallback
    return candidate if candidate.endswith("\n") else candidate + "\n"
