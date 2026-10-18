"""
audit.py – share links, share grants and the access audit log.

Standard library only, deliberately (like ``sessions.py``): nothing in here
may import ``common``, ``gui`` or any DB driver, so the whole sharing
permission layer is importable and *callable* on a machine with no Neo4j, no
Qdrant and no web framework.

Three things live in one file because they share a file handle and a lifetime:

- ``share_links`` — a revocable, expiring token an owner mints for ONE target
  (a client or a context). Resolving to a grant consumes a use.
- ``share_grants`` — the (owner, grantee, target, permission) record a token
  mint creates. This table is the **single source of truth** for sharing
  access. (The tasks.md plan originally proposed mirroring grants as Neo4j
  nodes; that mirror was left out on purpose — a dual-write between SQLite and
  Neo4j has a partial-write failure mode where access is decided by whichever
  store the crash let finish, and the read paths must resolve an explicit
  accessible-id set from this store anyway. If graph-traversal performance ever
  demands a node projection, it should be derived and rebuildable, not
  authoritative.)
- ``audit_log`` — append-only rows recording who did what, when, on which
  target, at which permission level. ``log_access`` swallows its own
  failures on purpose: an audit row must never be the reason a request fails
  (the fact that it swallowed is logged at WARNING, which is the level this
  app keeps for consequential failures).

**Nothing reversible is stored for a link.** Like a PSK, the token is kept
only as a SHA-256 digest plus the first ten characters for display. The
plaintext exists exactly once, in the return value of mint_share_link(), and
the caller hands it to the owner on that one response.

Timestamps are epoch seconds (float), not ISO text — the same convention
``sessions.py`` uses and the one every test here needs no clock gymnastics
for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid

logger = logging.getLogger("audit")

DB_NAME = "audit.db"

# Display prefix so a link found in a log or browser history is identifiable
# as a share token rather than a random string. The plaintext token never
# carries this prefix — it is for the stored ``token_prefix`` and any list UI.
LINK_PREFIX = "shs_"

VALID_TARGET_TYPES = ("client", "context")
VALID_PERMISSIONS = ("read", "write")
# A write grant covers read; a read grant never covers write.
PERMISSION_RANK = {"read": 0, "write": 1}

# ``detail`` keys whose value is never written to the audit log, whatever the
# caller passes. The rule is "no secrets/tokens in detail, prefix at most" —
# this filter is the cheap belt behind that, in case a call site forgets.
_REDACTED_DETAIL_KEYS = {
    "token", "share_token", "link_token", "key", "password", "secret",
    "password_hash", "hash", "auth", "authorization",
}
_DETAIL_MAX_CHARS = 4000

_UNSET = object()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    """Read an integer knob with the safe ``or`` form.

    Present-but-empty must behave like absent (that is why this is not
    ``os.getenv(name, str(default))`` — the two-argument form returns "" for
    an empty value, which would then explode on int("")).
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def share_ttl_days() -> int:
    """Default lifetime of a newly minted share link, in days."""
    return _env_int("MEM_SHARE_LINK_TTL_DAYS", 30)


def max_recipients() -> int:
    """Cap on active grants per (owner, target)."""
    return max(1, _env_int("MEM_SHARE_MAX_RECIPIENTS", 50))


def audit_keep_days() -> int:
    """How long audit rows are kept before ``purge_audit_log`` removes them."""
    return max(1, _env_int("MEM_AUDIT_KEEP_DAYS", 90))


# ---------------------------------------------------------------------------
# Schema / connection
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_links (
    id            TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL UNIQUE,
    token_prefix  TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    target_type   TEXT NOT NULL CHECK (target_type IN ('client','context')),
    target_id     TEXT NOT NULL,
    permission    TEXT NOT NULL CHECK (permission IN ('read','write')),
    created_at    REAL NOT NULL,
    expires_at    REAL,
    revoked_at    REAL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    max_uses      INTEGER
);
CREATE INDEX IF NOT EXISTS share_links_owner_index
    ON share_links (owner_user_id, target_id);

-- One grant per (owner, grantee, target): a UNIQUE key is what makes
-- accept_share_link a single atomic upsert, so two concurrent accepts of one
-- link cannot race into two rows or lose the permission bump.
CREATE TABLE IF NOT EXISTS share_grants (
    id             TEXT PRIMARY KEY,
    owner_user_id  TEXT NOT NULL,
    grantee_user_id TEXT NOT NULL,
    target_type    TEXT NOT NULL CHECK (target_type IN ('client','context')),
    target_id      TEXT NOT NULL,
    permission     TEXT NOT NULL CHECK (permission IN ('read','write')),
    granted_at     REAL NOT NULL,
    revoked_at     REAL,
    via_link_id    TEXT,
    UNIQUE (owner_user_id, grantee_user_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS share_grants_grantee_index
    ON share_grants (grantee_user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY,
    ts            REAL NOT NULL,
    actor_user_id TEXT NOT NULL,
    owner_user_id TEXT,
    target_type   TEXT,
    target_id     TEXT,
    action        TEXT NOT NULL,
    permission    TEXT,
    shared        INTEGER NOT NULL DEFAULT 0,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS audit_log_ts_index ON audit_log (ts);
CREATE INDEX IF NOT EXISTS audit_log_actor_index ON audit_log (actor_user_id, ts);
CREATE INDEX IF NOT EXISTS audit_log_target_index
    ON audit_log (owner_user_id, target_type, target_id, ts);
"""

_INIT_LOCK = threading.Lock()
# Keyed by path, not a bare bool: the suite points MEM_AUDIT_DB at a temp
# directory per test, and a boolean cache would initialise the first file and
# skip the schema in the second.
_initialised_path = None


def db_path() -> str:
    """Absolute path of the audit database, creating its directory.

    ``MEM_AUDIT_DB`` names the file itself when set. The default is literally
    "next to the session store": sessions.py's ``db_path()`` already knows
    where that is (``<dirname LOG_DIR>/sessions`` by default), and re-hosting
    the audit log beside it means one bind mount carries both, and ``grep``
    over ``mem-mcp-data`` finds a share grant next to the session that
    accepted it.
    """
    path = (os.getenv("MEM_AUDIT_DB") or "").strip()
    if not path:
        import sessions
        path = os.path.join(os.path.dirname(sessions.db_path()), DB_NAME)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    # A connection per call rather than a shared one: called from the event
    # loop, from background jobs and from the test suite, and sqlite3 objects
    # are not safe to share across those.
    conn = sqlite3.connect(db_path(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    """Create the schema. Idempotent, called before every other function.

    Every table is CREATE ... IF NOT EXISTS, so a database written by an
    earlier build gains the tables added since without a migration step.
    Sharing ships with no schema to migrate — grants are new rows, never
    rewrites of old ones (P9.1).
    """
    global _initialised_path
    path = db_path()
    with _INIT_LOCK:
        if _initialised_path == path:
            return
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            # WAL so an audit write never blocks a permission check reading
            # the grants table, which is on the hot path of every shared
            # search once Phase 3 lands.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialised_path = path


def _now(now=None) -> float:
    return time.time() if now is None else float(now)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_target(target_type: str, target_id: str) -> None:
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"target_type must be one of {VALID_TARGET_TYPES!r}")
    target_id = (target_id or "").strip()
    if not target_id:
        raise ValueError("a share needs a target")


def _validate_permission(permission: str) -> None:
    if permission not in VALID_PERMISSIONS:
        raise ValueError(f"permission must be one of {VALID_PERMISSIONS!r}")


class ShareLimitError(ValueError):
    """Raised when a target already holds the maximum number of active grants.

    A ``ValueError`` subclass so the API layer's generic ``except ValueError``
    turns it into a 400 naming the reason, exactly like the merge-draft
    refusal does.
    """


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------

def mint_share_link(owner_user_id: str, target_type: str, target_id: str,
                    permission: str, expires_in_days=_UNSET,
                    max_uses=None, now=None) -> dict:
    """Mint a share token for one target, bound to ``owner_user_id``.

    The returned dict's ``"token"`` key is the only place the plaintext
    exists. ``expires_in_days`` has a sentinel default: unset takes the
    ``MEM_SHARE_LINK_TTL_DAYS`` knob, an explicit number is that many days,
    and an explicit ``None`` means the link never expires (the owner's
    deliberate choice, per the plan — "never expires" must be available, but
    it must be chosen, not stumbled into by forgetting the argument).
    """
    init_db()
    owner_user_id = (owner_user_id or "").strip()
    if not owner_user_id:
        raise ValueError("a share link needs an owner")
    _validate_target(target_type, target_id)
    _validate_permission(permission)

    if expires_in_days is _UNSET:
        lifetime = share_ttl_days() * 86400.0
    elif expires_in_days is None:
        lifetime = None
    else:
        days = float(expires_in_days)
        if days <= 0:
            raise ValueError("expires_in_days must be positive, or None for 'never'")
        lifetime = days * 86400.0

    uses = None if max_uses is None else int(max_uses)
    if uses is not None and uses < 1:
        raise ValueError("max_uses must be at least 1, or None for unlimited")

    moment = _now(now)
    plaintext = secrets.token_urlsafe(32)
    link_id = uuid.uuid4().hex
    expires_at = None if lifetime is None else moment + lifetime
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO share_links (id, token_hash, token_prefix, owner_user_id,"
            " target_type, target_id, permission, created_at, expires_at, use_count,"
            " max_uses) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (link_id, _hash_token(plaintext), LINK_PREFIX + plaintext[:6],
             owner_user_id, target_type, target_id, permission, moment,
             expires_at, uses),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": link_id,
        "token": plaintext,
        "prefix": LINK_PREFIX + plaintext[:6],
        "owner_user_id": owner_user_id,
        "target_type": target_type,
        "target_id": target_id,
        "permission": permission,
        "created_at": moment,
        "expires_at": expires_at,
        "use_count": 0,
        "max_uses": uses,
    }


def link_status(record: dict, now=None) -> str:
    """active | revoked | expired | exhausted — one function so list and resolve agree."""
    if record.get("revoked_at") is not None:
        return "revoked"
    expires_at = record.get("expires_at")
    if expires_at is not None and expires_at <= _now(now):
        return "expired"
    max_uses = record.get("max_uses")
    if max_uses is not None and record.get("use_count", 0) >= max_uses:
        return "exhausted"
    return "active"


def _resolve_token(token: str, now=None):
    """Find the row for a presented token; generic None for anything dead.

    Returns ``(row, reason)`` where ``row`` is a sqlite Row or None. ``reason``
    exists for the accept path's own logging only — it is never shown to a
    caller who presented a wrong token, because a differentiated refusal is
    an existence oracle: "expired" confirms the token WAS valid once.
    """
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    if not token:
        return None
    init_db()
    now = _now(now)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM share_links WHERE token_hash = ?", (_hash_token(token),)
        ).fetchone()
        if row is None:
            return None
        if link_status(dict(row), now) != "active":
            return None
        return row
    finally:
        conn.close()


def resolve_share_link(token: str, now=None) -> dict | None:
    """Resolve a presented token to its link row, or None.

    None is returned for an unknown, revoked, expired or exhausted link
    alike — indistinguishably, for the same reason resolve_psk returns None
    for a dead key. This probe does NOT consume a use: only accepting does.
    """
    row = _resolve_token(token, now)
    return dict(row) if row is not None else None


def accept_share_link(token: str, grantee_user_id: str, now=None) -> dict | None:
    """Accept a share token: create the grant, count one use, return the target.

    Generic ``None`` for a token that is not valid *right now* — unknown,
    revoked, expired, exhausted or malformed are one answer, so the accept
    endpoint cannot be probed for the shape of a dead token.

    The use-count bump is a conditional UPDATE — ``WHERE`` the link is still
    active — and runs *after* the grant upsert. Reversing them would count a
    use the grant refused, and a link that has hit ``max_uses`` through
    refused accepts looks exhausted without any recipient to show for it.
    """
    if not grantee_user_id or not str(grantee_user_id).strip():
        return None
    grantee_user_id = str(grantee_user_id).strip()
    row = _resolve_token(token, now)
    if row is None:
        return None
    now = _now(now)
    link = dict(row)

    already = has_access(grantee_user_id, link["owner_user_id"],
                         link["target_type"], link["target_id"], level="read",
                         now=now)
    grant = grant_access(link["owner_user_id"], grantee_user_id,
                         link["target_type"], link["target_id"],
                         link["permission"], via_link_id=link["id"], now=now)
    log_access(grantee_user_id, link["owner_user_id"], link["target_type"],
               link["target_id"], action="share.accept",
               permission=link["permission"], shared=True,
               detail={"grant_id": grant["id"], "via_link_prefix": link["token_prefix"],
                       "already_had_access": already},
               now=now)
    conn = _connect()
    try:
        conn.execute(
            "UPDATE share_links SET use_count = use_count + 1 WHERE id = ?"
            " AND token_hash = ? AND revoked_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " AND (max_uses IS NULL OR use_count < max_uses)",
            (link["id"], link["token_hash"], now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "target_type": link["target_type"],
        "target_id": link["target_id"],
        "permission": link["permission"],
        "owner_user_id": link["owner_user_id"],
        "grant": grant,
        "already_had_access": already,
    }


def revoke_share_link(link_id: str, owner_user_id: str, now=None) -> bool:
    """Revoke one of ``owner_user_id``'s links.

    One False answer for "no such id", "not yours" and "already revoked", so
    this cannot be used to discover which link ids exist — the same answer
    revoke_psk gives. Ownership is a WHERE clause, not a Python ``if``.
    """
    if not link_id or not owner_user_id:
        return False
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE share_links SET revoked_at = ? WHERE id = ?"
            " AND owner_user_id = ? AND revoked_at IS NULL",
            (_now(now), str(link_id), str(owner_user_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def list_share_links(owner_user_id: str, target_type=None, target_id=None,
                     now=None) -> list:
    """Every link minted by ``owner_user_id``, newest first, with a status.

    Revoked and dead links stay in the list on purpose: a link that vanishes
    from the list is indistinguishable from one that was never minted, and
    the owner needs to see that a share they revoked is really gone.
    """
    owner_user_id = (owner_user_id or "").strip()
    if not owner_user_id:
        return []
    init_db()
    now = _now(now)
    query = ("SELECT id, token_prefix, owner_user_id, target_type, target_id,"
             " permission, created_at, expires_at, revoked_at, use_count, max_uses"
             " FROM share_links WHERE owner_user_id = ?")
    params = [owner_user_id]
    if target_type:
        _validate_target(target_type, target_id)
        query += " AND target_type = ? AND target_id = ?"
        params += [target_type, target_id]
    query += " ORDER BY created_at DESC, id DESC"
    conn = _connect()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [dict(row, status=link_status(dict(row), now)) for row in rows]


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

def grant_access(owner_user_id: str, grantee_user_id: str, target_type: str,
                 target_id: str, permission: str, via_link_id=None,
                 now=None) -> dict:
    """Grant ``permission`` on one target, idempotently.

    Re-granting an existing (owner, grantee, target) updates the permission,
    un-revokes the row and moves ``granted_at``. A re-accept is a fresh
    expression of the owner's consent (a live link was minted, or the owner
    called this directly), so a revoked grant reactivated by an explicit new
    action is not an ambush — and the owner can revoke again in one click.

    Raises ShareLimitError when the target is already at its active-grant cap
    and this grantee is not among its holders.
    """
    init_db()
    owner_user_id = (owner_user_id or "").strip()
    grantee_user_id = (grantee_user_id or "").strip()
    if not owner_user_id or not grantee_user_id:
        raise ValueError("a grant needs an owner and a grantee")
    if owner_user_id == grantee_user_id:
        raise ValueError("an owner cannot be granted access to their own vault")
    _validate_target(target_type, target_id)
    _validate_permission(permission)

    now = _now(now)
    grant_id = uuid.uuid4().hex
    conn = _connect()
    try:
        # Cap lives inside the write so every path — link accept and direct
        # add alike — is bounded by the same knob, whichever way it arrives.
        active = conn.execute(
            "SELECT COUNT(*) FROM share_grants WHERE owner_user_id = ?"
            " AND target_type = ? AND target_id = ? AND revoked_at IS NULL",
            (owner_user_id, target_type, target_id),
        ).fetchone()[0]
        existing = conn.execute(
            "SELECT id FROM share_grants WHERE owner_user_id = ?"
            " AND grantee_user_id = ? AND target_type = ? AND target_id = ?",
            (owner_user_id, grantee_user_id, target_type, target_id),
        ).fetchone()
        if existing is None and active >= max_recipients():
            raise ShareLimitError(
                f"this share already has the maximum of {max_recipients()}"
                " recipients")
        # "Already had access" means the row existed AND was active: a
        # re-grant over a revoked row did not expose the target before this
        # call, and the accept endpoint's already_had_access answer must not
        # claim otherwise.
        already = existing is not None and conn.execute(
            "SELECT 1 FROM share_grants WHERE id = ? AND revoked_at IS NULL",
            (existing["id"],),
        ).fetchone() is not None
        conn.execute(
            """
            INSERT INTO share_grants (id, owner_user_id, grantee_user_id,
                target_type, target_id, permission, granted_at, revoked_at,
                via_link_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT (owner_user_id, grantee_user_id, target_type, target_id)
            DO UPDATE SET permission = excluded.permission,
                granted_at = excluded.granted_at,
                revoked_at = NULL,
                via_link_id = excluded.via_link_id
            """,
            (existing["id"] if existing is not None else grant_id,
             owner_user_id, grantee_user_id, target_type, target_id,
             permission, now, via_link_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": existing["id"] if existing is not None else grant_id,
        "owner_user_id": owner_user_id,
        "grantee_user_id": grantee_user_id,
        "target_type": target_type,
        "target_id": target_id,
        "permission": permission,
        "granted_at": now,
        "revoked_at": None,
        "via_link_id": via_link_id,
        "already_had_access": already,
    }


def revoke_grant(grant_id: str, owner_user_id: str, now=None) -> bool:
    """Revoke one grant. One False answer, scoped by owner, enumeration-safe."""
    if not grant_id or not owner_user_id:
        return False
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE share_grants SET revoked_at = ? WHERE id = ?"
            " AND owner_user_id = ? AND revoked_at IS NULL",
            (_now(now), str(grant_id), str(owner_user_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def revoke_all_grants_for_user(owner_user_id: str, grantee_user_id: str,
                               now=None) -> int:
    """Revoke every grant ``owner_user_id`` gave ``grantee_user_id``.

    Used when deleting or disabling a grantee's account: a disabled account
    must not keep reading through shares, the same way delete_credentials
    goes after every access key. Count of grants killed.
    """
    if not grantee_user_id:
        return 0
    init_db()
    now = _now(now)
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE share_grants SET revoked_at = ? WHERE grantee_user_id = ?"
            " AND revoked_at IS NULL" + (
                " AND owner_user_id = ?" if owner_user_id else ""),
            ([now, str(grantee_user_id), str(owner_user_id)]
             if owner_user_id else [now, str(grantee_user_id)]),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def list_grants_for_target(owner_user_id: str, target_type: str,
                           target_id: str, now=None) -> list:
    """Every grant held on one target (active and revoked), for owner management UI."""
    init_db()
    _validate_target(target_type, target_id)
    now = _now(now)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, owner_user_id, grantee_user_id, target_type, target_id,"
            " permission, granted_at, revoked_at, via_link_id"
            " FROM share_grants WHERE owner_user_id = ? AND target_type = ?"
            " AND target_id = ? ORDER BY granted_at DESC, id DESC",
            (str(owner_user_id), target_type, target_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row, status="revoked" if row["revoked_at"] is not None
                 else "active") for row in rows]


def list_grants_for_user(grantee_user_id: str, now=None) -> list:
    """Every ACTIVE grant held by one user — the "shared with me" list.

    Revoked grants are deliberately absent here (unlike the links and
    target-grants lists, where the owner needs to see the dead rows): for the
    person who lost the access, the list is what they can still use, and a
    dead row in it is just clutter over a target they cannot open.
    """
    grantee_user_id = (grantee_user_id or "").strip()
    if not grantee_user_id:
        return []
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, owner_user_id, grantee_user_id, target_type, target_id,"
            " permission, granted_at, via_link_id"
            " FROM share_grants WHERE grantee_user_id = ? AND revoked_at IS NULL"
            " ORDER BY granted_at DESC, id DESC",
            (grantee_user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------

def is_owner(user_id: str, owner_user_id: str) -> bool:
    """True when ``user_id`` IS the vault's owner."""
    return bool(user_id) and bool(owner_user_id) \
        and str(user_id) == str(owner_user_id)


def grant_permits(grants, owner_user_id: str, target_type: str, target_id: str,
                  level: str = "read", parent_target_id=None) -> bool:
    """Whether any of ``grants`` permits ``level`` access to one target.

    Pure — ``grants`` is a plain list of dicts as returned by
    ``list_grants_for_user`` — so the rule is testable with no store at all.

    Two coverage rules, both load-bearing:

    - **A client grant covers its contexts.** Sharing a client means "read my
      bank project", and that must include the projects under it; checking
      the context id alone would strand every record filed under a shared
      client. ``parent_target_id`` is how the caller tells us which client a
      context sits under — this store cannot look it up, because scope
      parents live in Neo4j and this module is stdlib-only.
    - **write implies read.** Rank, not a second membership test: {"read":
      0, "write": 1} makes "write grant permits read" and "read grant does
      not permit write" the same one comparison.
    """
    if level not in PERMISSION_RANK:
        raise ValueError(f"level must be one of {tuple(PERMISSION_RANK)!r}")
    wanted = PERMISSION_RANK[level]
    # A context grant is a direct hit; a client grant covers the contexts
    # under it, which the second pass below handles.
    for grant in grants:
        if str(grant.get("owner_user_id", "")) != str(owner_user_id):
            continue
        if grant.get("permission") not in PERMISSION_RANK:
            continue
        if PERMISSION_RANK[grant["permission"]] < wanted:
            continue
        if grant.get("target_type") != target_type or \
                grant.get("target_id") != target_id:
            continue
        return True
    # Second pass: a client-level grant covers the contexts filed under it.
    for grant in grants:
        if str(grant.get("owner_user_id", "")) != str(owner_user_id):
            continue
        if PERMISSION_RANK[grant.get("permission")] < wanted:
            continue
        if grant.get("target_type") == "client" \
                and grant.get("target_id") == parent_target_id:
            return True
    return False


def has_access(user_id: str, owner_user_id: str, target_type: str,
               target_id: str, level: str = "read", parent_target_id=None,
               now=None) -> bool:
    """Whether ``user_id`` may reach one owned target at ``level``.

    Ownership is checked first — the owner needs no grant and must never pay
    a lookup for their own vault. ``parent_target_id`` is the client id a
    shared context sits under, so a client-level grant covers its projects.
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return False
    if is_owner(user_id, owner_user_id):
        return True
    grants = list_grants_for_user(user_id, now=now)
    return grant_permits(grants, owner_user_id, target_type, target_id,
                         level=level, parent_target_id=parent_target_id)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _sanitize_detail(detail):
    """JSON-encode an audit detail, with secrets stripped.

    Values under secret-shaped keys become a redaction marker; this is the
    belt behind the rule that detail carries at most a token *prefix*, never
    a token. Non-dict details are wrapped as text.
    """
    if not isinstance(detail, dict):
        return str(detail)[:_DETAIL_MAX_CHARS]
    cleaned = {}
    for key, value in detail.items():
        if str(key).lower() in _REDACTED_DETAIL_KEYS:
            cleaned[key] = "<redacted>"
        else:
            cleaned[key] = value
    text = json.dumps(cleaned, default=str)
    if len(text) > _DETAIL_MAX_CHARS:
        text = text[:_DETAIL_MAX_CHARS]
    return text


def log_access(actor_user_id: str, owner_user_id=None, target_type=None,
               target_id=None, action: str = "read", permission=None,
               shared=False, detail=None, now=None) -> str | None:
    """Append one audit row. Returns the row id, or None when the write failed.

    Failures are swallowed on purpose — an audit row must never be the reason
    a request fails — but *not* silently: every failure is logged at WARNING
    with the action, which is the line an operator greps when the audit
    viewer shows nothing. (The defect this replaces was a dead audit feed
    indistinguishable from a quiet vault.)
    """
    actor_user_id = (actor_user_id or "").strip() or "anonymous"
    ts = _now(now)
    row_id = uuid.uuid4().hex
    detail_text = _sanitize_detail(detail)
    try:
        init_db()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO audit_log (id, ts, actor_user_id, owner_user_id,"
                " target_type, target_id, action, permission, shared, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row_id, ts, actor_user_id,
                 str(owner_user_id) if owner_user_id else None,
                 target_type, target_id, str(action), permission,
                 1 if shared else 0, detail_text),
            )
            conn.commit()
        finally:
            conn.close()
        return row_id
    except Exception as exc:  # noqa: BLE001 - audit must never break a request
        logger.warning(
            "audit log write failed (action=%s actor=%s owner=%s"
            " target=%s/%s): %s: %s",
            action, actor_user_id, owner_user_id, target_type, target_id,
            type(exc).__name__, exc,
        )
        return None


def query_audit_log(owner_user_id=None, actor_user_id=None,
                    target_type=None, target_id=None, since=None,
                    limit: int = 200, offset: int = 0) -> list:
    """Read the audit log, newest first, with optional filters.

    ``owner_user_id`` is the primary filter: the settings viewer shows the
    owner the activity on THEIR targets, and the admin sees all. ``limit`` is
    capped in code because an unbounded query over a per-read log is the one
    table that can grow without bound.
    """
    init_db()
    limit = max(1, min(int(limit), 500))
    query = ("SELECT id, ts, actor_user_id, owner_user_id, target_type,"
             " target_id, action, permission, shared, detail FROM audit_log")
    clauses, params = [], []
    if owner_user_id:
        clauses.append("owner_user_id = ?")
        params.append(str(owner_user_id))
    if actor_user_id:
        clauses.append("actor_user_id = ?")
        params.append(str(actor_user_id))
    if target_type:
        clauses.append("target_type = ?")
        params.append(target_type)
    if target_id:
        clauses.append("target_id = ?")
        params.append(str(target_id))
    if since is not None:
        clauses.append("ts >= ?")
        params.append(float(since))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    params += [limit, max(0, int(offset))]
    conn = _connect()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def purge_audit_log(older_than_days=None, now=None) -> int:
    """Delete audit rows older than the retention window. Count removed.

    Defaults to the ``MEM_AUDIT_KEEP_DAYS`` knob (90 days). Returns the count
    so the daily job's log line can say what it did.
    """
    days = audit_keep_days() if older_than_days is None else older_than_days
    if days is None or days <= 0:
        raise ValueError("older_than_days must be a positive number of days")
    cutoff = _now(now) - float(days) * 86400.0
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
