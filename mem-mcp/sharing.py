"""
sharing.py – permission policy for project/context sharing.

The bridge between ``audit.py`` (the stores) and the endpoints/tools
(gui.py, mcp_tools.py). Asks three questions the request handlers need:

- can this user READ this owned target?    → ``can_read``
- can this user WRITE to this owned target? → ``can_write``
- is sharing turned on at all?              → ``sharing_enabled``

Standard-library only, so it is testable on a machine with no Neo4j, no
Qdrant and no web framework; the database side of every answer lives in
``audit.py`` and the pure rules in ``audit.grant_permits``.

The one rule this layer exists to state plainly: **ownership needs no grant**.
An owner touching their own client is not "access granted by someone", and it
must never pay a lookup for it — ``has_access`` checks identity first. Every
call below answers for someone *else's* target only.
"""

from __future__ import annotations

import os

import audit

__all__ = [
    "sharing_enabled",
    "can_read",
    "can_write",
    "effective_permission",
    "accessible_grants",
    "shared_owners",
    "accessible_scope",
    "record_visible",
    "accessible_owner_scope",
    "readable_owner_ids",
]

_TRUTHY = {"1", "true", "yes", "on"}


def sharing_enabled() -> bool:
    """Whether the share surface is open at all.

    Off by default, and it should stay off until an operator turns it on —
    the same deliberate default as registration. Read with the safe ``or``
    form so present-but-empty behaves like absent.
    """
    return (os.getenv("MEM_SHARING_ENABLED") or "").strip().lower() in _TRUTHY


def can_read(user_id: str, owner_user_id: str, target_type: str,
             target_id: str, parent_target_id=None, now=None) -> bool:
    """Read access to a target owned by ``owner_user_id``.

    ``parent_target_id`` is the client id a shared *context* sits under —
    without it a client-level grant would not cover that client's projects.
    Pass it for context targets, anything for client targets.
    """
    return audit.has_access(user_id, owner_user_id, target_type, target_id,
                            level="read", parent_target_id=parent_target_id,
                            now=now)


def can_write(user_id: str, owner_user_id: str, target_type: str,
              target_id: str, parent_target_id=None, now=None) -> bool:
    """Write access. A read-only grant never implies write; a write grant does."""
    return audit.has_access(user_id, owner_user_id, target_type, target_id,
                            level="write", parent_target_id=parent_target_id,
                            now=now)


def effective_permission(user_id: str, owner_user_id: str, target_type: str,
                         target_id: str, parent_target_id=None,
                         now=None) -> str | None:
    """'write' | 'read' | None — what ``user_id`` may do on the target.

    None means no access at all, which includes "not shared with this user"
    and "the grant was revoked" alike: the caller answers both the same way,
    with a 404/403 that names nothing.
    """
    if user_id and audit.is_owner(user_id, owner_user_id):
        return "write"
    if can_write(user_id, owner_user_id, target_type, target_id,
                 parent_target_id=parent_target_id, now=now):
        return "write"
    if can_read(user_id, owner_user_id, target_type, target_id,
                parent_target_id=parent_target_id, now=now):
        return "read"
    return None


def accessible_grants(user_id: str, now=None) -> list:
    """Every ACTIVE (owner, target, permission) grant for ``user_id``.

    Read paths widen their user scope to this set: the grantee's effective
    readable records are their own vault PLUS records under these owners'
    targets. Returned as the raw grant dicts — including ``via_link_id`` —
    because the dashboard 'shared with me' view wants the same metadata the
    permission check wants.
    """
    return audit.list_grants_for_user(user_id, now=now)


def shared_owners(user_id: str, now=None) -> list:
    """Distinct owners sharing with ``user_id``, never their other data.

    The confirmation of the plan's open decision #2: this is the ONLY list a
    grantee learns, and it names owners, not clients. Sibling clients,
    contexts and unscoped facts of an owner are visible to a grantee only
    when the owner shares those targets.
    """
    owners = []
    seen = set()
    for grant in accessible_grants(user_id, now=now):
        owner = grant.get("owner_user_id")
        if owner and owner not in seen:
            seen.add(owner)
            owners.append(owner)
    return sorted(owners)


# ---------------------------------------------------------------------------
# Read-path widening helpers
#
# Phase 3's contract (tasks.md §6): a grantee's effective read scope is their
# own vault PLUS records under targets explicitly granted to them. The read
# paths in client_manager / fact_manager / diary_manager compute the widened
# id sets ONCE per request from these pure functions and filter with them —
# no Cypher knows grants exist, and a write grant is irrelevant here.
# ---------------------------------------------------------------------------

def accessible_scope(grants: list) -> dict:
    """Reduce active grant rows to ``{owner: {"clients": {id: perm}, "contexts": {id: perm}}}``.

    Pure over the raw ``list_grants_for_user`` rows. A record's row can arrive
    twice for one target (an old revocation re-granted, or two grants that
    resolved to the same target through different links), so the per-target
    merge takes write over read — downgrading a target someone may edit to a
    target nobody may see would let a later read row erase the earlier one in
    a dict collision the caller cannot see.
    """
    owners: dict = {}
    for grant in grants:
        owner = str(grant.get("owner_user_id") or "")
        target_type = str(grant.get("target_type") or "")
        target_id = str(grant.get("target_id") or "")
        if not owner or not target_id or target_type not in ("client", "context"):
            continue
        perm = "write" if str(grant.get("permission") or "") == "write" else "read"
        entry = owners.setdefault(owner, {"clients": {}, "contexts": {}})
        bucket = entry["clients"] if target_type == "client" else entry["contexts"]
        if bucket.get(target_id) != "write":
            bucket[target_id] = perm
    return owners


def record_visible(owner_user_id: str, client_id, context_id, owners: dict) -> bool:
    """Whether a record owned by ``owner_user_id`` is visible through ``owners``.

    A record is visible when ANY of its scope ids is granted: a client grant
    covers the records filed under that client (under any of its projects,
    since those are the client's own projects), and a context grant covers the
    records filed under that one project. A record with an UNgranted client
    under a granted context — i.e. the record names no context at all — is NOT
    visible: a grant is for a target subtree, and sharing a project does not
    share the owner's whole vault.
    """
    if not owners:
        return False
    entry = owners.get(owner_user_id)
    if not entry:
        return False
    if client_id and client_id in entry["clients"]:
        return True
    if context_id and context_id in entry["contexts"]:
        return True
    return False


def accessible_owner_scope(user_id: str, now=None) -> dict | None:
    """The widened scope map for read paths, or ``None`` for the fast path.

    ``None`` means "sharing is off, or this user holds no grants" — every read
    path must then behave byte-identically to its pre-sharing form, with no
    extra queries and no extra keys in its rows. A dict means the caller's own
    id is NOT included and must be handled separately (ownership never needs
    an entry here; see the module docstring).
    """
    if not sharing_enabled():
        return None
    grants = accessible_grants(user_id, now=now)
    if not grants:
        return None
    return {"owners": accessible_scope(grants)}


def readable_owner_ids(user_id: str, now=None) -> list | None:
    """The widened owner-id match set for store queries, or ``None``.

    ``None`` keeps the pre-sharing query shape (a single-owner filter). A list
    always starts with the caller's own id — their records must be reachable
    even when the flag is on — followed by every owner whose records the caller
    may see. Callers filter the added owners' records further against the
    ``owners`` map from ``accessible_owner_scope``, because a *read* grant on a
    client grants that client, not its owner's unrelated records.
    """
    scope_map = accessible_owner_scope(user_id, now=now)
    if not scope_map:
        return None
    return [user_id] + sorted(scope_map["owners"])
