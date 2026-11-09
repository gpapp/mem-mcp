"""Assemble a per-engagement briefing out of what the vault already holds.

A brief is a **read**. Nothing here writes, extracts or classifies: it gathers
the records that already belong to one client or project, arranges them with
``briefing.summarise``, and then asks the model to write the page a consultant
would want to read in the lift on the way up.

Two properties are worth more than the wording, and both are the reason this is
a separate module from ``briefing``:

- **The model gets the rendered material, not the raw rows.** The fallback and
  the polished version then describe the same engagement, and a model failure
  costs a nicer layout rather than the answer.
- **A model failure degrades, it never raises.** Every other LLM surface in
  this app already answers with its unpolished form when Ollama is down; a
  briefing that 503s on the way into a meeting is worse than a plain list.
"""

import json
import os
from datetime import datetime, timezone

import briefing
from common import LLM_QUERY_MODEL, LLM_TIMEOUT, get_llm_response, logger

# The brief is the same kind of writing as a merge draft, so it defaults to the
# judgement model rather than the extraction one. Empty means "inherit".
BRIEF_MODEL = os.getenv("MEM_BRIEF_MODEL") or LLM_QUERY_MODEL

# The brief is a prompt in front of a person about to walk into a room, so it
# gets its own, shorter budget. The background passes can wait 300 s; someone
# standing in a corridor cannot. It is still long enough for a cold model load
# on the CPU path, which is the slow case that actually happens.
BRIEF_TIMEOUT = float(os.getenv("MEM_BRIEF_TIMEOUT") or "120.0")

# How many recent meetings a brief shows. The section exists to orient the
# reader, not to be the archive; the diary tab is the archive.
BRIEF_MEETING_LIMIT = int(os.getenv("MEM_BRIEF_MEETINGS") or "8")

# A brief is rebuilt whenever the vault fingerprint moves, so the cache is
# bounded by how many engagements a person opens rather than by time. Small on
# purpose: this holds whole Markdown documents.
_CACHE_MAX = 64
_CACHE: dict = {}


def _cache_key(user_id: str, kind: str, scope_id: str) -> tuple:
    return (user_id, kind, scope_id)


async def _fingerprint() -> str:
    """A comparable string for the current vault, or "" when unreadable.

    Reuses ``backup.vault_fingerprint`` rather than inventing a second notion of
    "has anything changed". It already answers exactly this question -- newest
    ``updatedAt`` per label plus that label's count plus the relationship total
    -- and it was built for the scheduled backup for the same reason: content
    hashing a whole graph per call is too expensive to do often.

    An unreadable fingerprint is the empty string and never matches, so a store
    that cannot be reached rebuilds the brief instead of serving a stale one.
    """
    try:
        from backup import vault_fingerprint

        return json.dumps(await vault_fingerprint(), sort_keys=True, default=str)
    except Exception as exc:  # noqa: BLE001 - any failure means "no fingerprint"
        logger.warning("brief: could not fingerprint the vault (%s) -- rebuilding", exc)
        return ""


def clear_cache() -> None:
    """Drop every cached brief.

    Called when a record changes, so a brief never outlives the meeting it was
    written from. The fingerprint would catch it on the next request anyway,
    but an explicit clear costs nothing and does not depend on every write
    stamping ``updatedAt``.
    """
    _CACHE.clear()


def _find_client(clients: list, client_id: str):
    for client in clients or ():
        if client.get("id") == client_id:
            return client
    return None


def _find_context(client: dict, context_id: str):
    for context in (client or {}).get("contexts") or ():
        if context.get("id") == context_id:
            return context
    return None


def _describe_actions(items: list, today: str = None) -> list:
    from action_manager import describe_action_item

    described = []
    for item in items or ():
        try:
            described.append(describe_action_item(item, today=today))
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
            logger.warning("brief: could not describe action item %s (%s)",
                           (item or {}).get("id"), exc)
    return described


def _trim_entries(entries: list, limit: int) -> list:
    """Newest first, de-duplicated by id, capped.

    The panel rows already arrive date-descending, but a ``RELEVANT_TO`` merge
    can put the same entry in twice under different edges, and a briefing that
    lists one meeting twice reads as a data problem to the person holding it.
    """
    seen = set()
    out = []
    for entry in entries or ():
        record_id = (entry or {}).get("id")
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


async def db_build_brief(client_id: str, user_id: str, context_id: str = None,
                         use_llm: bool = True) -> dict:
    """Build the brief for one client, or one project within it.

    Returns ``{"markdown", "degraded", "cached", "scope", "name"}``. Raises
    ``ValueError`` when the scope is not one this vault can read -- the route
    turns that into a 404, because "no such client" and "you may not read it"
    are the same answer to someone who does not own it.
    """
    import client_manager

    kind = "context" if context_id else "client"
    scope_id = context_id or client_id

    clients = client_manager.db_list_clients(user_id)
    client = _find_client(clients, client_id)
    if client is None:
        raise ValueError(f"no client with id {client_id!r}")

    context = None
    if context_id:
        context = _find_context(client, context_id)
        if context is None:
            raise ValueError(f"no project with id {context_id!r} under this client")

    fingerprint = await _fingerprint()
    key = _cache_key(user_id, kind, scope_id)
    cached = _CACHE.get(key)
    if cached and fingerprint and cached[0] == fingerprint:
        return {"markdown": cached[1], "degraded": cached[2], "cached": True,
                "scope": cached[3], "name": cached[3].get("name")}

    if context_id:
        items = client_manager.db_context_items(context_id, user_id) or {}
    else:
        items = client_manager.db_client_items(client_id, user_id) or {}

    from action_manager import db_list_action_items

    actions = _describe_actions(
        db_list_action_items(user_id, client_id=client_id,
                             context_id=context_id, status="open"),
        today=datetime.now(timezone.utc).date().isoformat(),
    )

    entries = _trim_entries(items.get("diary") or [], BRIEF_MEETING_LIMIT)
    facts = items.get("facts") or []

    if context_id:
        # The project is the scope, so it belongs in "Where this stands" --
        # passing the client's other projects would describe an engagement the
        # reader did not ask about.
        scope = {"name": client.get("name"), "contexts": [context],
                 "effectiveActive": context.get("effectiveActive",
                                                context.get("active", True))}
    else:
        scope = client

    summary = briefing.summarise(scope, actions, facts, entries)
    gaps_list = briefing.gaps(summary)
    fallback = briefing.render(summary, gaps_list)

    degraded = False
    markdown = fallback
    if use_llm:
        try:
            reply = await get_llm_response(
                briefing.build_prompt(summary, gaps_list),
                system=briefing.SYSTEM_PROMPT,
                model=BRIEF_MODEL,
                timeout=BRIEF_TIMEOUT,
            )
            markdown = briefing.polish(reply, fallback)
            # `polish` hands back the fallback when the answer was empty or too
            # short to be a brief, and that is worth reporting as degraded: the
            # reader is looking at a list, not a briefing, and should know why.
            degraded = markdown.strip() == fallback.strip()
        except Exception as exc:  # noqa: BLE001 - the brief is a read, never a 503
            logger.warning("brief: model pass failed (%s) -- serving the plain brief", exc)
            degraded = True

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (fingerprint, markdown, degraded, scope)

    return {"markdown": markdown, "degraded": degraded, "cached": False,
            "scope": scope, "name": summary.get("name")}
