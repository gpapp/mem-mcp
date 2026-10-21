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
    "record_visible_related",
    "record_permission",
    "record_permission_related",
    "record_read_only",
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


def record_visible_related(owner_user_id: str, related_ids, owners: dict) -> bool:
    """Whether a record is visible because a ``RELEVANT_TO`` target is granted.

    ``RELEVANT_TO`` is the secondary-association edge the scope classifier
    writes *before* it gives up on a primary client — so for a null-primary
    record it is the only scope evidence there is. Reading only the record's own
    ``FOR_CLIENT`` / ``IN_CONTEXT`` edges (which is all ``record_visible`` does)
    made exactly those records invisible: an entry that discusses a shared
    project without being filed under it was unreachable, and nothing in the
    logs said why.

    The edge type is the same for a Client and a Context target, so ``related_ids``
    is one flat set and is checked against both buckets. A record whose primary
    scope IS granted does not need this — the caller asks here only when the
    primary check failed, and the two are deliberately separate so that "visible
    through a related project" and "visible because it is filed there" stay
    distinguishable (see ``primary_scope_hidden``).
    """
    if not owners or not related_ids:
        return False
    entry = owners.get(owner_user_id)
    if not entry:
        return False
    for target_id in related_ids:
        if target_id in entry["clients"] or target_id in entry["contexts"]:
            return True
    return False


def primary_scope_hidden(owner_user_id: str, client_id, context_id,
                         related_ids, owners: dict) -> bool:
    """True when a record is visible ONLY through a related target.

    The privacy half of ``record_visible_related``. A record that is reachable
    only because it mentions a shared project is still filed under the owner's
    OWN client and project — names the caller has no grant for. Rendering them
    would leak the owner's unshared scope on every such record, which is the
    whole thing a context grant is meant to withhold. So the read paths answer
    this and blank the display names; the ids stay, because scope ranking and
    the ``readOnly`` decision both need them.

    A record the caller cannot see at all is NOT "hidden" — the caller never
    renders it, and a flag on an invisible record is noise.
    """
    if not owners:
        return False
    entry = owners.get(owner_user_id)
    if not entry:
        return False
    if client_id and client_id in entry["clients"]:
        return False
    if context_id and context_id in entry["contexts"]:
        return False
    return record_visible_related(owner_user_id, related_ids, owners)


def record_permission(owner_user_id: str, client_id, context_id, owners: dict):
    """'write' | 'read' | None — the caller's permission on ONE record.

    ``owners`` is the map from ``accessible_owner_scope``. None means the
    record is not visible through any grant, which is the same answer
    ``record_visible`` gives; the two are deliberately built from the same
    structure so a caller that already filtered can ask what it filtered for
    without a second trip to SQLite.

    A record carrying both a granted client and a granted context takes the
    weaker of the two. Downgrading is the safe direction: a read grant on the
    client and a write grant on the project means the caller may edit records
    filed under that project, and this record is filed under that client too.
    """
    entry = owners.get(owner_user_id) if owners else None
    if not entry:
        return None
    found = []
    if client_id and client_id in entry["clients"]:
        found.append(entry["clients"][client_id])
    if context_id and context_id in entry["contexts"]:
        found.append(entry["contexts"][context_id])
    if not found:
        return None
    return "write" if "write" in found else "read"


def record_permission_related(owner_user_id: str, related_ids, owners: dict):
    """The permission a record earns from its ``RELEVANT_TO`` targets alone.

    Same rule as ``record_permission``: a write grant on any one of the targets
    wins. That is deliberately the *permissive* direction here, because a
    ``RELEVANT_TO`` edge is an association the owner chose to draw — a record
    cross-referenced to a project the caller may edit is a record about that
    project, and the write grant is the stronger statement of the two.

    This is what makes a related-only record read-only in the UI rather than
    editable: without it ``record_permission`` finds no granted primary and
    returns ``None``, ``record_read_only`` reads that as "own record", and the
    grantee is offered Edit on a record the server will refuse with a 403.
    """
    entry = owners.get(owner_user_id) if owners else None
    if not entry or not related_ids:
        return None
    found = []
    for target_id in related_ids:
        if target_id in entry["clients"]:
            found.append(entry["clients"][target_id])
        elif target_id in entry["contexts"]:
            found.append(entry["contexts"][target_id])
    if not found:
        return None
    return "write" if "write" in found else "read"


def record_read_only(user_id: str, owner_user_id, client_id, context_id,
                     owners: dict, related_ids=None) -> bool:
    """True when the caller may SEE this record but must not CHANGE it.

    The UI half of the sharing feature. The read paths widen to include other
    owners' records, and those records were being rendered by exactly the same
    code as the caller's own — so a grantee holding a read-only grant was
    offered Edit, Delete and link controls that answer 403. Offering an action
    that cannot succeed is worse than hiding it: the person cannot tell a
    permissions problem from a broken button, and a read-only share is the
    first thing anyone tries.

    Own records are never read-only. ``None`` owner means the record is the
    caller's own (the read paths emit ``None`` rather than the caller's id for
    their own rows, so the JSON does not grow on an unshared vault).

    ``related_ids`` are the record's ``RELEVANT_TO`` target ids. They matter
    only for a record whose primary scope is NOT granted: such a record is
    reachable at all only through them, so its permission comes from them too.
    """
    if not owner_user_id or owner_user_id == user_id:
        return False
    if record_permission(owner_user_id, client_id, context_id, owners) is not None:
        return record_permission(owner_user_id, client_id, context_id, owners) == "read"
    return record_permission_related(owner_user_id, related_ids, owners) == "read"


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
