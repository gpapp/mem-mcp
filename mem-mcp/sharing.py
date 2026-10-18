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
