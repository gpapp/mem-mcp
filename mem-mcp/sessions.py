"""
sessions.py – persistent login sessions and MCP pre-shared keys.

Standard library only, deliberately. Nothing in here may import `common`,
`gui` or any DB driver: that is what lets test_sessions.py exercise the real
shipping functions on a machine with no Neo4j, no Qdrant and no Ollama. Every
security-relevant decision this module makes — how a key is hashed, who may
revoke it, when a session stops being valid — is therefore testable directly
rather than by reading its source.

Two things live in one file because they share a file handle and a lifetime:
a login session and the pre-shared key that outlasts it.

Persistence is the point. `SessionMiddleware` kept the session *in the signed
cookie*, so it survived a container rebuild only for as long as
MEM_SESSION_SECRET did, and there was no way to revoke one server-side. The
cookie now carries an opaque id and this module owns the rest, in a SQLite
file under MEM_SESSION_DIR, which the compose file bind-mounts from
./mem-mcp-data. A rebuild of the image is then a non-event for logged-in
users, and rotating MEM_SESSION_SECRET no longer logs everyone out.

**Nothing reversible is stored.** The password that was previously written to
the session cookie is gone (see gui.py api_login), and a PSK is kept only as a
SHA-256 digest plus the first few characters needed to recognise a key in a
list. The plaintext exists exactly once, in the return value of create_psk(),
and the caller hands it to the browser on that one response.
"""

# Annotations are strings, not evaluated objects. This is not a style choice:
# `VaultSessionMiddleware` is defined above `VaultSession` and annotates a
# parameter as `vault: VaultSession`. On Python 3.14 (PEP 649) that annotation is
# never evaluated, so the forward reference is invisible — and the module
# imports cleanly. The container runs 3.11, where annotations *are* evaluated at
# `def` time, so the class body raised `NameError: name 'VaultSession' is not
# defined` and the app would not start at all. A guard test cannot catch this by
# importing the module here, because the interpreter that hides the bug is the
# one the tests run on; `Python311AnnotationTests` walks the AST for it instead.
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSION_COOKIE = "mem_session"

# Absolute, not sliding: a leaked cookie that keeps being replayed would never
# expire under a sliding window, and a session that cannot be ended by not
# using it is a session whose only remedy is rotating every PSK.
SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds

# A visible scheme prefix, so a key found in a log, a shell history or a
# screenshot is identifiable as a vault credential rather than a random string.
PSK_PREFIX = "mvk_"

# Enough to tell two keys apart in a list ("mvk_a1b2c3…") and nothing more.
PSK_DISPLAY_CHARS = len(PSK_PREFIX) + 6

MAX_LABEL_CHARS = 60
MAX_EXPIRY_DAYS = 365

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS psks (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    key_hash     TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked_at   REAL,
    expires_at   REAL
);

CREATE INDEX IF NOT EXISTS psks_user_id_index ON psks (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_index ON sessions (expires_at);

-- The OAuth client this app registered with an external provider, entered
-- through the Setup page rather than the environment, so an operator can change
-- it without a redeploy. Google is the only provider implemented; the column
-- exists so adding one is a row rather than a second table.
CREATE TABLE IF NOT EXISTS oauth_clients (
    provider      TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    client_secret TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

-- Which external identity may open which vault. `user_id` is the same plain
-- username string every other store in this app uses — there is no user table —
-- so linking a Google account is the act of pointing a subject at a name.
--
-- The primary key is (provider, subject) and NOT (provider, subject, user_id):
-- one subject resolves to exactly one vault, forever. That is what makes an
-- unlinked account a refusal rather than a new empty vault, and it is why
-- link_google_identity refuses to move an existing link instead of upserting.
CREATE TABLE IF NOT EXISTS google_identities (
    provider     TEXT NOT NULL DEFAULT 'google',
    subject      TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    email        TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    last_used_at REAL,
    PRIMARY KEY (provider, subject)
);

CREATE INDEX IF NOT EXISTS google_identities_user_index ON google_identities (user_id);

-- Accounts created by self-service registration, and their passwords.
--
-- This is a second credential store alongside the operator's htpasswd file, and
-- it exists because that file is mounted read-only: `/app/htpasswd:ro` in
-- docker-compose.yml means the app could verify a password it is not allowed to
-- write a new one beside. Rather than make the operator's file writable -- which
-- hands the app the ability to rewrite a file a human also edits by hand -- a
-- registration writes here and login checks here first.
--
-- `user_id` is the vault key every other store in this app already scopes by,
-- so it is the PRIMARY KEY and a registered account needs no mapping layer. For
-- a registered account it is the lowercased email address; the pre-existing
-- htpasswd accounts keep their usernames and have no row here at all.
--
-- There is deliberately no `email` column: `user_id` *is* the email for a
-- registered account, and a second copy of the same string is a second thing to
-- keep in agreement.
CREATE TABLE IF NOT EXISTS credentials (
    user_id       TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    disabled_at   REAL
);
"""

_INIT_LOCK = threading.Lock()
# Keyed by path, not a bare bool: the suite points MEM_SESSION_DIR at a temp
# directory per test, and a boolean cache would leave the schema behind in the
# first file and skip it in the second.
_initialised_path = None


def db_path() -> str:
    """Absolute path of the session database, creating its directory.

    Defaults next to LOG_DIR's parent so the Docker layout needs no extra
    configuration: LOG_DIR=/app/logs gives /app/sessions, which compose
    bind-mounts from ./mem-mcp-data/sessions.
    """
    directory = (os.getenv("MEM_SESSION_DIR") or "").strip()
    if not directory:
        log_dir = (os.getenv("LOG_DIR") or "").strip()
        if log_dir:
            directory = os.path.join(os.path.dirname(os.path.abspath(log_dir)), "sessions")
        else:
            directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "sessions.db")


def _connect() -> sqlite3.Connection:
    # A connection per call rather than a shared one: this is called from the
    # event loop, from a worker thread in the maintenance queue and from the
    # test suite, and sqlite3 objects are not safe to share across those.
    conn = sqlite3.connect(db_path(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    """Create the schema. Idempotent, and called before every other function.

    Every statement is CREATE ... IF NOT EXISTS, so a database written by an
    older build gains the tables added since without a migration step: the only
    thing a new column needs is a default, and only new tables have been added.
    """
    global _initialised_path
    path = db_path()
    with _INIT_LOCK:
        if _initialised_path == path:
            return
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            # WAL so a login writing a session does not block an MCP request
            # reading one. The other two stores in this app are not reachable
            # from here, so this file is the only contention that exists.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialised_path = path


def _now(now=None) -> float:
    return time.time() if now is None else float(now)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    """A fresh, unguessable session id.

    This value is the entire credential in the cookie, so it is drawn from
    secrets rather than derived from the username, a timestamp or a hash of
    anything an attacker could read.
    """
    return secrets.token_urlsafe(32)


def create_session(username: str, max_age: int = SESSION_MAX_AGE, now=None, data: dict = None) -> dict:
    """Persist a new login session and return it, including its id.

    `data` is the session payload as JSON. It is stored rather than recomputed
    so this store stays generic — but see FORBIDDEN_SESSION_KEYS: nothing that
    could identify a human holding this cookie is allowed in it.

    `max_age` is clamped to a positive value so a misconfigured knob produces
    a short session rather than one that never expires or one that never
    starts.
    """
    init_db()
    username = (username or "").strip()
    if not username:
        raise ValueError("A session needs a username")
    age = SESSION_MAX_AGE if max_age is None else int(max_age)
    if age <= 0:
        raise ValueError("max_age must be positive")
    payload = dict(data) if data else {}
    payload.setdefault("user", username)
    _reject_credentials(payload)
    moment = _now(now)
    session_id = new_session_id()
    expires_at = moment + age
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, username, data, created_at, expires_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, username, json.dumps(payload), moment, expires_at, moment),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "session_id": session_id,
        "username": username,
        "data": payload,
        "created_at": moment,
        "expires_at": expires_at,
        "last_seen_at": moment,
    }


def load_session(session_id: str, now=None, touch: bool = True) -> dict | None:
    """Return the live session for `session_id`, or None.

    None covers all three unauthenticated cases — unknown id, empty id, and
    expired — because they must be indistinguishable to the caller: a caller
    that can tell "no such session" from "expired session" learns whether an id
    was ever issued. An expired row is deleted on the way past, so a cookie that
    outlives its session is not re-checked against a clock forever.

    `touch` updates last_seen_at for display and pruning. It deliberately does
    NOT move expires_at: see SESSION_MAX_AGE.
    """
    if not session_id or not isinstance(session_id, str):
        return None
    init_db()
    moment = _now(now)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= moment:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        if touch:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
                (moment, session_id),
            )
            conn.commit()
    finally:
        conn.close()
    record = dict(row)
    try:
        payload = json.loads(record.get("data") or "{}")
    except (ValueError, TypeError):
        # A row that cannot be parsed is a row this code cannot trust to say
        # who the caller is. Treat it as absent and drop it, rather than
        # falling back to an empty payload that would look like a live session.
        delete_session(session_id)
        return None
    if not isinstance(payload, dict):
        delete_session(session_id)
        return None
    record["data"] = payload
    if touch:
        record["last_seen_at"] = moment
    return record


def update_session_data(session_id: str, data: dict) -> bool:
    """Replace a session's payload. False if there is no such live row."""
    if not session_id or not isinstance(session_id, str):
        return False
    init_db()
    _reject_credentials(data)
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE sessions SET data = ? WHERE session_id = ?",
            (json.dumps(dict(data)), session_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_session(session_id: str) -> bool:
    """Drop one session (logout). True if a row went away."""
    if not session_id or not isinstance(session_id, str):
        return False
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_sessions_for_user(username: str) -> int:
    """End every session for one user (logout everywhere)."""
    username = (username or "").strip()
    if not username:
        return 0
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def active_sessions_for_user(username: str) -> list:
    """Live sessions for one user, newest first — shown on the Access Keys panel."""
    username = (username or "").strip()
    if not username:
        return []
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT session_id, username, created_at, expires_at, last_seen_at"
            " FROM sessions WHERE username = ? AND expires_at > ?"
            " ORDER BY last_seen_at DESC",
            (username, _now()),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def purge_expired_sessions(now=None) -> int:
    """Delete every session past its expiry. Called on boot and hourly."""
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(now),))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pre-shared keys
# ---------------------------------------------------------------------------

def hash_psk(key: str) -> str:
    """The digest a PSK is stored and looked up by.

    Plain SHA-256, deliberately, and not bcrypt/argon2: a PSK is 32 bytes from
    secrets.token_urlsafe, so there is no low-entropy guess to slow down —
    offline cracking 256 bits is hopeless by any method — while a KDF would add
    ~100ms to *every MCP request*, on the request path of an assistant that
    calls this vault many times a minute. The cost function buys nothing here
    and costs a lot.

    A per-key salt is also deliberately absent: it cannot be applied at lookup
    time without narrowing the query (the salt would have to be found by the
    display prefix, which is 6 characters of the key itself).
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_psk() -> tuple:
    """Return (plaintext, digest, prefix).

    The plaintext leaves this function once and is never recoverable again.
    Callers must show it to the user immediately; that constraint is the
    reason there is no "get the key again" button anywhere in the UI.
    """
    plaintext = PSK_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_psk(plaintext), plaintext[:PSK_DISPLAY_CHARS]


def normalise_label(label) -> str:
    """A PSK label is free text shown in a list; bound it, do not trust it.

    Collapsing whitespace keeps a pasted multi-line label from becoming a
    layout break in the panel. HTML escaping is the renderer's job (escHtml on
    the client); this only bounds length.
    """
    text = " ".join(str(label or "").split())
    return text[:MAX_LABEL_CHARS]


def _expiry_from_days(expires_in_days):
    if expires_in_days is None:
        return None
    days = int(expires_in_days)
    if days <= 0 or days > MAX_EXPIRY_DAYS:
        raise ValueError(f"expiresInDays must be between 1 and {MAX_EXPIRY_DAYS}, or omitted")
    return days * 24 * 60 * 60


def create_psk(user_id: str, label: str = "", expires_in_days=None, now=None) -> dict:
    """Mint a PSK bound to `user_id`.

    The returned dict is the only place the plaintext exists, in its "key" key.
    Every field else is what a later list() call can show.
    """
    init_db()
    user_id = (user_id or "").strip()
    if not user_id:
        raise ValueError("A pre-shared key needs a user")
    lifetime = _expiry_from_days(expires_in_days)
    moment = _now(now)
    plaintext, digest, prefix = generate_psk()
    psk_id = uuid.uuid4().hex
    expires_at = None if lifetime is None else moment + lifetime
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO psks (id, user_id, label, key_hash, prefix, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (psk_id, user_id, normalise_label(label), digest, prefix, moment, expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": psk_id,
        "user_id": user_id,
        "label": normalise_label(label),
        "prefix": prefix,
        "created_at": moment,
        "last_used_at": None,
        "revoked_at": None,
        "expires_at": expires_at,
        "key": plaintext,
    }


def resolve_psk(key: str, now=None) -> dict | None:
    """Resolve a presented PSK to its owning user, or None.

    None is returned for a revoked key and an expired one, exactly as for one
    that was never issued, and last_used_at is bumped only on the success path
    — an attacker replaying a dead key must not be able to keep its row warm.

    A presented value that does not carry PSK_PREFIX is rejected before the
    database is touched at all. That is not an optimisation: it stops an
    htpasswd password or a session id from ever reaching a sha256 lookup, so a
    credential meant for one mechanism can never be accepted by another.
    """
    if not key or not isinstance(key, str):
        return None
    key = key.strip()
    if not key.lower().startswith(PSK_PREFIX):
        return None
    init_db()
    moment = _now(now)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM psks WHERE key_hash = ?", (hash_psk(key),)
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] is not None and row["expires_at"] <= moment:
            return None
        conn.execute("UPDATE psks SET last_used_at = ? WHERE id = ?", (moment, row["id"]))
        conn.commit()
    finally:
        conn.close()
    record = dict(row)
    record["last_used_at"] = moment
    return record


def list_psks(user_id: str, now=None) -> list:
    """Every PSK belonging to one user, newest first, with a status label.

    Only the requesting user's own rows are ever read. The list includes
    revoked and expired keys on purpose — a revoked key that silently vanishes
    from the list is indistinguishable from one that was never revoked, and the
    owner needs to see that it is gone.
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return []
    init_db()
    moment = _now(now)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, user_id, label, prefix, created_at, last_used_at, revoked_at, expires_at"
            " FROM psks WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row, status=psk_status(dict(row), moment)) for row in rows]


def psk_status(record: dict, now=None) -> str:
    """active | revoked | expired — one function so the UI and the resolver agree."""
    if record.get("revoked_at") is not None:
        return "revoked"
    expires_at = record.get("expires_at")
    if expires_at is not None and expires_at <= _now(now):
        return "expired"
    return "active"


def revoke_psk(psk_id: str, user_id: str, now=None) -> bool:
    """Revoke one of `user_id`'s keys. False if it is not theirs, or already dead.

    Ownership is a WHERE clause, not a Python `if`. Reading the row first and
    then comparing the owner leaves a window between the read and the write in
    which the row could change hands, and it is the reason the caller must be
    able to see a row it does not own at all.

    False is returned identically for "no such id", "not yours" and "already
    revoked", so this cannot be used to discover which key ids exist.
    """
    if not psk_id or not user_id:
        return False
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE psks SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (_now(now), str(psk_id), str(user_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# External identities (Google)
# ---------------------------------------------------------------------------

GOOGLE_PROVIDER = "google"

# Google's client ids are of the form "<number>-<slug>.apps.googleusercontent.com",
# around 40-120 characters. A generous bound rejects a pasted paragraph without
# needing the real shape, which is a documentation detail not a security control.
MAX_CLIENT_ID_CHARS = 255
MAX_CLIENT_SECRET_CHARS = 512

# The display fields are bounded here as well as in google_auth, for the reason
# PSK labels are: these store functions are public, so a caller that bypasses
# google_auth entirely (a script, a future provider) still cannot write an
# unbounded string into the database. sessions.py cannot import the constants
# from google_auth because google_auth needs PyJWT and this module is
# stdlib-only precisely so the test suite can import and call it with nothing
# installed — the duplication is the price of that, and it is two integers.
MAX_EMAIL_CHARS = 254   # RFC 5321's practical maximum
MAX_NAME_CHARS = 120


def _normalise_provider(provider) -> str:
    """The provider key. Only one is implemented, so this is also the validator."""
    name = str(provider or "").strip().lower()
    if name != GOOGLE_PROVIDER:
        raise ValueError(f"provider must be {GOOGLE_PROVIDER!r}")
    return name


def save_oauth_client(provider, client_id, client_secret=None, now=None) -> dict:
    """Store (or replace) the OAuth client for a provider. Returns the record.

    `client_secret=None` keeps whatever secret is already stored; `""` clears it.
    That distinction is the whole reason it is not a plain string: the Setup page
    never receives the secret back, so a save that only meant to correct a client
    id cannot send the stored value, and "absent means keep" is what stops it
    wiping the secret instead.

    The secret is stored as typed. That is a deliberate consequence of letting an
    operator configure this from the Setup page rather than the environment: the
    value has to be written somewhere the app owns, and this database is already
    the place sessions and key hashes live, on the operator's bind mount. It is
    not encrypted at rest, and pretending otherwise in a comment would be worse
    than saying it. What keeps it from mattering is that verifying a Google ID
    token needs no secret at all — the signature is checked against Google's
    public keys — so the stored secret is only read if a code-exchange flow is
    added later.
    """
    init_db()
    name = _normalise_provider(provider)
    client_id = str(client_id or "").strip()
    if not client_id:
        raise ValueError("A client id is required")
    # Refused, not truncated. A display field that loses its tail is still
    # readable, but a credential that loses its tail is simply a wrong
    # credential: it would be stored, shown as saved, and fail later at the point
    # of use with a Google-side error that says nothing about a length limit
    # here. The bound is generous relative to what Google issues, so exceeding it
    # means a paste went wrong and should be refused at the point of entry.
    if len(client_id) > MAX_CLIENT_ID_CHARS:
        raise ValueError("That client id is too long to be a Google client id")
    if client_secret is not None and len(str(client_secret).strip()) > MAX_CLIENT_SECRET_CHARS:
        raise ValueError("That client secret is too long to be a Google client secret")
    moment = _now(now)
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM oauth_clients WHERE provider = ?", (name,)
        ).fetchone()
        if client_secret is None:
            # Absent: keep the stored secret, or "" when there was never one.
            secret = (existing["client_secret"] if existing is not None else "") or ""
        else:
            secret = str(client_secret).strip()
        created_at = existing["created_at"] if existing is not None else moment
        conn.execute(
            "INSERT INTO oauth_clients (provider, client_id, client_secret, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(provider) DO UPDATE SET"
            " client_id = excluded.client_id,"
            " client_secret = excluded.client_secret,"
            " updated_at = excluded.updated_at",
            (name, client_id, secret, created_at, moment),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM oauth_clients WHERE provider = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def get_oauth_client(provider=GOOGLE_PROVIDER) -> dict | None:
    """The stored client, secret included, or None. Internal: see the public_* pair.

    Returning the secret here and stripping it one layer out is deliberate, and
    it is only safe because oauth_client_public is the single place that builds a
    client record for a response. A caller that wants to show the configuration
    must go through that.
    """
    init_db()
    name = _normalise_provider(provider)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM oauth_clients WHERE provider = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def oauth_client_public(record) -> dict:
    """A client record safe to send to a browser: the secret becomes a flag.

    The Setup page needs to show whether a secret is configured so an operator
    can tell "never set one" from "set one and forgot it", and it must not be
    able to read the secret back — this app has exactly one place a Google secret
    is ever needed (a future code exchange), and that is not the UI.
    """
    record = dict(record or {})
    secret = record.get("client_secret") or ""
    return {
        "provider": record.get("provider") or GOOGLE_PROVIDER,
        "client_id": record.get("client_id") or "",
        "secret_set": bool(secret),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def delete_oauth_client(provider=GOOGLE_PROVIDER) -> bool:
    """Forget the OAuth client. Identity links are deliberately left alone.

    Removing the client id makes every token unverifiable, because the audience
    check has nothing to compare against — so the links stop granting access at
    that moment without the links themselves being rewritten. Deleting them too
    would mean that restoring a client id did not restore access, which is the
    opposite of what an operator who fat-fingered the field wants.
    """
    init_db()
    name = _normalise_provider(provider)
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM oauth_clients WHERE provider = ?", (name,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def link_google_identity(subject, user_id, email="", name="", provider=GOOGLE_PROVIDER, now=None) -> dict:
    """Point an external subject at a vault, and return the record.

    Raises ValueError if the subject is already linked to a *different* vault. A
    subject is one account at one provider, so a second link is a contradiction
    rather than a second grant — and silently reassigning would let anyone who can
    log in as any vault claim another vault's account by pasting its token,
    which is a privilege escalation dressed up as a convenience.

    Re-linking the same subject to the same vault is an update of the display
    fields, so an operator can correct a mistyped email without a delete.
    """
    init_db()
    subject = str(subject or "").strip()
    user_id = str(user_id or "").strip()
    if not subject:
        raise ValueError("A subject is required")
    if not user_id:
        raise ValueError("A linked identity needs a vault")
    provider = _normalise_provider(provider)
    email = str(email or "").strip()[:MAX_EMAIL_CHARS]
    display_name = " ".join(str(name or "").split())[:MAX_NAME_CHARS]
    moment = _now(now)

    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT user_id FROM google_identities WHERE provider = ? AND subject = ?",
            (provider, subject),
        ).fetchone()
        if existing is not None and existing["user_id"] != user_id:
            raise ValueError(
                "that Google account is already linked to a different vault"
            )
        if existing is None:
            conn.execute(
                "INSERT INTO google_identities"
                " (provider, subject, user_id, email, display_name, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (provider, subject, user_id, email, display_name, moment),
            )
        else:
            conn.execute(
                "UPDATE google_identities SET email = ?, display_name = ?"
                " WHERE provider = ? AND subject = ?",
                (email, display_name, provider, subject),
            )
        conn.commit()
        # Re-read rather than assembling the return from what was written: the
        # UPDATE path does not touch created_at, so a hand-built dict would claim
        # a fresh creation time for a link that has existed for months, and
        # last_used_at could not be filled in at all. One extra SELECT on a
        # write a human performs once is cheaper than a return value that lies.
        row = conn.execute(
            "SELECT * FROM google_identities WHERE provider = ? AND subject = ?",
            (provider, subject),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def unlink_google_identity(subject, user_id, provider=GOOGLE_PROVIDER) -> bool:
    """Unlink, if the link belongs to `user_id`. False otherwise.

    Ownership is a WHERE clause for the reason revoke_psk gives: reading the row
    first and comparing in Python leaves a window in which the link could change
    hands, and it would let the caller see a subject that is not theirs.
    """
    if not subject or not user_id:
        return False
    init_db()
    provider = _normalise_provider(provider)
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM google_identities WHERE provider = ? AND subject = ? AND user_id = ?",
            (provider, str(subject).strip(), str(user_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def list_google_identities(user_id) -> list:
    """Every external identity linked to one vault, oldest first.

    Only the requesting vault's own rows are read. Unlike the PSK list there is
    no revoked/expired status to show: a link is either there or it was deleted,
    and a deleted link leaving a tombstone would only invite the question of
    whether a tombstone still grants anything.
    """
    user_id = str(user_id or "").strip()
    if not user_id:
        return []
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT provider, subject, user_id, email, display_name, created_at, last_used_at"
            " FROM google_identities WHERE user_id = ? ORDER BY created_at, subject",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def resolve_google_identity(subject, provider=GOOGLE_PROVIDER, now=None, touch: bool = True) -> dict | None:
    """The vault a verified subject is linked to, or None.

    last_used_at is bumped only on the success path, exactly as resolve_psk
    does: a subject that is not linked must not be able to keep a row alive by
    being replayed, and there is no way to warm a row that does not exist.

    `touch=False` is for the Setup page's "check this token" button. Looking at
    a token to find out who it belongs to is not authenticating with it, and a
    last_used_at that moved while somebody was reading the Setup page would
    describe something that never happened.

    This is only ever called after google_auth has verified the signature, the
    issuer, the audience and the expiry. It is a lookup, not a check, and its
    name says so deliberately.
    """
    if not subject:
        return None
    init_db()
    provider = _normalise_provider(provider)
    moment = _now(now)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM google_identities WHERE provider = ? AND subject = ?",
            (provider, str(subject).strip()),
        ).fetchone()
        if row is None:
            return None
        if touch:
            conn.execute(
                "UPDATE google_identities SET last_used_at = ? WHERE provider = ? AND subject = ?",
                (moment, provider, row["subject"]),
            )
            conn.commit()
    finally:
        conn.close()
    record = dict(row)
    if touch:
        record["last_used_at"] = moment
    return record


# ---------------------------------------------------------------------------
# Accounts and passwords
#
# Registration writes here; login reads here first and falls back to the
# operator's htpasswd. See the comment on the `credentials` table in _SCHEMA for
# why there are two stores instead of one writable file.
#
# Everything in this section is deliberately stdlib. sessions.py is imported and
# called directly by the test suite on a machine with no database and no web
# framework, which is the only reason these functions are testable at all.
# ---------------------------------------------------------------------------

# Registration is opt-in and off by default. It cannot be a code default of "on"
# for the same reason auth_basic must not come back to the nginx MCP location:
# an endpoint that creates accounts should be something an operator turned on,
# not something a deployment inherits by upgrading.
#
# The `or "0"` form rather than a two-argument getenv, because a variable that is
# present-but-empty must read as absent. `os.getenv(NAME, "1")` returns "" for an
# empty MEM_REGISTRATION_ENABLED, and `"" == "1"` is False here so that case is
# already safe -- but the empty string is also what an operator gets from a
# compose `REGISTRATION_ENABLED=` line with no value, and treating that as
# anything other than "off" is the wrong direction to be wrong in.
REGISTRATION_ENABLED = str(os.getenv("MEM_REGISTRATION_ENABLED") or "0").strip().lower() in ("1", "true", "yes", "on")

# scrypt parameters. N=2**14 with r=8 is ~16 MiB of memory per hash and roughly
# 50-100ms on the small hardware this runs on, which is the intended cost: it is
# paid on login and on signup, never on a request that has no password to check.
# Stored in the encoded string rather than read from the environment, so raising
# N later cannot lock anyone out of their own account.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

# A password is not length-capped by any hash here, but an unbounded one is a
# denial-of-service primitive: scrypt's cost is in the salt and N, but the
# input still has to be hashed, and a 100 MB "password" is 100 MB of work per
# unauthenticated request on an endpoint that has no rate limit upstream.
MIN_PASSWORD_CHARS = 10
MAX_PASSWORD_CHARS = 1024

# A password no real account has, used only to ask htpasswd whether a name
# exists. Binary control characters make a collision with anything a human would
# pick effectively impossible.
_UNMATCHABLE_PASSWORD = "\x00\x01htpasswd-probe\x00"

# Signup attempts per client per window. There is no rate limiting anywhere else
# in this app -- no slowapi, no limiter, no 429 -- and a registration endpoint is
# the one place where "guess a password" is a thing an outsider can attempt at
# will. This is a plain in-process counter, which is honest about what it is: it
# stops a script, not a distributed one. A proxy in front of the app is the real
# answer for that.
REGISTRATION_ATTEMPT_LIMIT = 10
REGISTRATION_ATTEMPT_WINDOW = 600.0

_ATTEMPTS_LOCK = threading.Lock()
_ATTEMPTS: dict = {}


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R,
                  p: int = SCRYPT_P, dklen: int = SCRYPT_DKLEN) -> str:
    """Hash a password into the one string this module stores.

    scrypt over PBKDF2 because PBKDF2's only cost knob is iterations and
    iterations are cheap to raise on a GPU -- a hash you can do 10 billion times
    a second is a hash that has to be checked 10 billion times a second. scrypt's
    cost is in *memory*, which a GPU does not have to spare, so the same work
    stays expensive on the attacker's hardware and not just on the server's.

    The parameters travel inside the string (`scrypt$n$r$p$salt$hash`) so they can
    be raised later without invalidating anyone's existing password: a verifier
    reads the cost from what it was given rather than from what it was compiled
    with.
    """
    text = str(password or "")
    if len(text) < MIN_PASSWORD_CHARS:
        raise ValueError(f"the password must be at least {MIN_PASSWORD_CHARS} characters")
    if len(text) > MAX_PASSWORD_CHARS:
        raise ValueError(f"the password must be at most {MAX_PASSWORD_CHARS} characters")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.scrypt(text.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)
    # dklen travels in the string as well as n/r/p, and that is not redundancy.
    # A verifier that *derived* the length from the stored digest would compute a
    # digest of that same length and compare equal -- so anyone who could shorten
    # the stored hash by one byte would have made it verify against anything.
    return "$".join([
        "scrypt", str(n), str(r), str(p), str(dklen),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, encoded: str) -> bool:
    """True if `password` produced `encoded`. Never raises on a malformed hash.

    A stored hash that will not parse is a *false*, not an exception. The caller
    here is a login path, and a database row with a truncated hash -- restored
    from a damaged savepoint, hand-edited, written by a future version with a
    different format -- must read as "this account cannot log in" rather than as
    a 500 on every request that touches it.
    """
    try:
        parts = str(encoded or "").split("$")
        if len(parts) != 7 or parts[0] != "scrypt":
            return False
        _, raw_n, raw_r, raw_p, raw_dklen, raw_salt, raw_digest = parts
        dklen = int(raw_dklen)
        salt = base64.b64decode(raw_salt, validate=True)
        expected = base64.b64decode(raw_digest, validate=True)
        if len(expected) != dklen:
            # The stored digest disagrees with the length it claims to be, so the
            # row is damaged rather than merely foreign. Refusing here is what
            # stops a truncated hash from verifying against itself.
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"), salt=salt,
            n=int(raw_n), r=int(raw_r), p=int(raw_p), dklen=dklen,
        )
    except Exception:
        return False
    # compare_digest, not ==: a byte-by-byte comparison leaks the length of the
    # matching prefix through timing, and this is the one comparison in the app
    # where an attacker gets to run it repeatedly against a value of their
    # choosing.
    return hmac.compare_digest(digest, expected)


def normalise_account_id(email) -> str:
    """The vault key for a registered account: the email, lowercased and trimmed.

    Lowercasing is not cosmetic. `user_id` is the PRIMARY KEY of this table and
    the `userId` on every Fact, DiaryEntry and Client in the other two stores, so
    `Alice@example.com` and `alice@example.com` would otherwise be two accounts
    with two vaults and two sets of records -- the exact shape of the
    case-collision bugs that surface as "my notes disappeared".

    It also means the email is the vault key, which is a deliberate trade (see
    AGENTS.md "Registration"): a reassigned address would reach the old vault.
    That is why registration is opt-in rather than always available.
    """
    text = str(email or "").strip().lower()
    if not text:
        raise ValueError("an email address is required")
    if len(text) > MAX_EMAIL_CHARS:
        raise ValueError(f"that email address is longer than {MAX_EMAIL_CHARS} characters")
    return text


def validate_email(email) -> str:
    """Return the normalised account id, or raise ValueError with a usable reason.

    The shape check is deliberately loose -- one `@`, something on each side, a
    dot in the domain, no whitespace. A strict RFC 5322 parser rejects valid
    addresses (`a@b` is legal, `user@localhost` is legal in practice) and the
    only authority on whether an address receives mail is a message to it, which
    this app does not send. What is being stopped here is a string that could not
    be an address at all, and a string long enough or shaped oddly enough to be
    something else.
    """
    text = str(email or "").strip().lower()
    if not text:
        raise ValueError("an email address is required")
    if len(text) > MAX_EMAIL_CHARS:
        raise ValueError(f"that email address is longer than {MAX_EMAIL_CHARS} characters")
    if text.count("@") != 1:
        raise ValueError("that does not look like an email address")
    local, _, domain = text.partition("@")
    if not local or not domain:
        raise ValueError("that does not look like an email address")
    if any(character.isspace() for character in text):
        raise ValueError("an email address cannot contain spaces")
    if "." not in domain:
        raise ValueError("that email address has no domain")
    if len(local) > 64 or len(domain) > 253:
        raise ValueError("that email address is too long")
    return text


def user_id_taken(user_id: str) -> bool:
    """True if this vault key already exists in *any* of the three stores.

    A registered account's `user_id` is its email, and an email can already name
    something: an htpasswd user (the operator's own account, or one they added by
    hand) or the vault a Google account is already linked to. Creating a
    credentials row for either would produce two ways into one vault with two
    independent passwords -- or, worse, silently re-point an existing vault at a
    password the person registering just chose, which is an account takeover
    dressed as a signup.

    So the check is across all three stores rather than against this table alone.
    """
    key = str(user_id or "").strip()
    if not key:
        return False
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM credentials WHERE user_id = ?"
            " UNION ALL SELECT 1 FROM google_identities WHERE user_id = ? LIMIT 1",
            (key, key),
        ).fetchone()
    finally:
        conn.close()
    if row is not None:
        return True
    # htpasswd is a file, so this one costs a subprocess -- but only for a
    # candidate name that is not already known to be free, and registration is a
    # rare event behind an opt-in flag.
    return htpasswd_user_exists(key)


def htpasswd_user_exists(username: str) -> bool:
    """True if the operator's htpasswd file has this user.

    `htpasswd -v` with an empty password is the only way to ask the file whether a
    name is present, because htpasswd exposes no list operation. An empty
    password cannot match a real entry unless someone deliberately created an
    account whose password is the empty string, which the login form's own
    `required` attribute prevents in practice; and even then the answer this
    function gives is only "do not offer to register over the top of it".
    """
    path = str(os.getenv("HTPASSWD_PATH") or "").strip()
    if not path or not os.path.exists(path):
        return False
    try:
        result = subprocess.run(
            ["htpasswd", "-vb", path, str(username), _UNMATCHABLE_PASSWORD],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        # Unreadable htpasswd is not a reason to refuse a signup: the check is a
        # courtesy against shadowing an operator's account, and the credential
        # this actually creates is in a store this app owns.
        return False
    return result.returncode == 0


def create_credentials(email, password: str, now=None) -> dict:
    """Register an email/password account and return the stored record.

    Refuses if the vault key is already spoken for, rather than upserting: an
    upsert here would silently reset the password of an existing account, which
    is the failure mode a "create" endpoint must not have.
    """
    key = validate_email(email)
    if user_id_taken(key):
        raise ValueError("that account already exists")
    moment = _now(now)
    encoded = hash_password(password)
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO credentials (user_id, password_hash, created_at, updated_at, disabled_at)"
            " VALUES (?, ?, ?, ?, NULL)",
            (key, encoded, moment, moment),
        )
        conn.commit()
    finally:
        conn.close()
    return {"user_id": key, "created_at": moment, "updated_at": moment, "disabled_at": None}


def get_credentials(user_id) -> dict | None:
    """The credential row for a vault key, or None. Includes the password hash."""
    key = str(user_id or "").strip()
    if not key:
        return None
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT user_id, password_hash, created_at, updated_at, disabled_at"
            " FROM credentials WHERE user_id = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def verify_account_password(user_id, password: str) -> bool:
    """True if `password` is the registered password for this vault key.

    A disabled account is False even with the right password. Disabling is not
    deleting so that the row keeps the vault key occupied: without that, the
    address could be registered again and would come back pointing at a vault
    whose records are still there.
    """
    record = get_credentials(user_id)
    if not record or record.get("disabled_at") is not None:
        return False
    return verify_password(password, record["password_hash"])


def set_password(user_id, password: str, now=None) -> bool:
    """Replace the password for an existing registered account."""
    key = str(user_id or "").strip()
    if not key:
        return False
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE credentials SET password_hash = ?, updated_at = ?"
            " WHERE user_id = ? AND disabled_at IS NULL",
            (hash_password(password), _now(now), key),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def disable_credentials(user_id, now=None) -> bool:
    """Refuse future logins for this account without releasing its vault key."""
    key = str(user_id or "").strip()
    if not key:
        return False
    moment = _now(now)
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE credentials SET disabled_at = ?, updated_at = ?"
            " WHERE user_id = ? AND disabled_at IS NULL",
            (moment, moment, key),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def list_credentials() -> list:
    """Every registered account, newest first. Never includes the hash."""
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, created_at, updated_at, disabled_at"
            " FROM credentials ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def allow_registration_attempt(client: str, now=None) -> bool:
    """Count one signup attempt for `client`; False if it is over the limit.

    In-process and therefore per-worker, which is stated plainly rather than
    dressed up: it raises the cost of a script and does nothing about a botnet.
    A proxy in front of the app is where real rate limiting belongs, and this
    exists so that "there is none at all" is not the answer on a route that
    creates accounts.

    The window slides rather than resetting on the boundary, so a client cannot
    get 2x the quota by straddling it. Entries are pruned on every call, which is
    what keeps this from being a slow memory leak keyed by attacker-chosen
    strings.
    """
    key = str(client or "").strip() or "unknown"
    moment = _now(now)
    cutoff = moment - REGISTRATION_ATTEMPT_WINDOW
    with _ATTEMPTS_LOCK:
        stamps = [value for value in _ATTEMPTS.get(key, ()) if value > cutoff]
        if len(stamps) >= REGISTRATION_ATTEMPT_LIMIT:
            _ATTEMPTS[key] = stamps
            return False
        stamps.append(moment)
        _ATTEMPTS[key] = stamps
        # Prune clients that are over the window and not the one being asked
        # about, so the dict tracks live traffic rather than every address ever
        # seen. Bounded by the number of concurrent clients, not by history.
        for other in [k for k, v in _ATTEMPTS.items() if k != key and not any(t > cutoff for t in v)]:
            _ATTEMPTS.pop(other, None)
    return True


def registration_enabled(method="email") -> bool:
    """Whether this signup *method* may be used right now.

    Both halves are required, and the second is the one that is easy to forget:
    the flag turns the *route* on, but a Google signup cannot verify anything
    without a client id, and one without a client id would accept a token it has
    no way to check the audience of. "Enabled but not configured" is a 404-shaped
    state that reads as a broken feature, so it is not offered.

    This takes a method name ("email" / "google"), not a provider key, because
    the two are not the same axis: the flag is about registration as a whole and
    the client id is about Google specifically. A Google-only deployment and an
    email-only one are both reachable without touching the flag.
    """
    if not REGISTRATION_ENABLED:
        return False
    name = str(method or "").strip().lower()
    if name == "email":
        return True
    if name != GOOGLE_PROVIDER:
        raise ValueError(f"unknown registration method: {method!r}")
    client = get_oauth_client(GOOGLE_PROVIDER)
    # stripped, not just truthy: a client id of "   " is the client id you get
    # from a form that was submitted with an empty box, and verifying a token
    # against it as an audience is the exact mistake the audience check exists
    # to stop -- reached from the other direction.
    return bool(client and str(client.get("client_id") or "").strip())


# ---------------------------------------------------------------------------
# The middleware
# ---------------------------------------------------------------------------

# The verified-identity header. Set by mcp_auth_guard (gui.py) *after* it has
# authenticated the request, and read by extract_user_from_headers (common.py)
# to decide whose vault a tool call is for. It outranks every other source,
# which is precisely why VaultSessionMiddleware deletes it from every inbound
# request: a header that decides who you are must never be one the client can
# simply send. It is stripped in one place, on the way in, rather than in each
# consumer.
VAULT_USER_HEADER = b"x-vault-user"


def _cookie_value(headers, name: str):
    """Read one cookie out of a raw ASGI header list."""
    prefix = f"{name}=".encode("latin-1")
    for key, value in headers:
        if key != b"cookie":
            continue
        for part in value.split(b";"):
            part = part.strip()
            if part.startswith(prefix):
                return part[len(prefix):].decode("latin-1")
    return None


class VaultSessionMiddleware:
    """ASGI middleware putting a persistent session at `request.session`.

    Replaces starlette's SessionMiddleware, which kept the session *inside* the
    signed cookie. That survived a container rebuild, but only for as long as
    MEM_SESSION_SECRET did, could not be revoked server-side, and carried the
    plaintext password. The cookie here carries an opaque id and nothing else,
    and the row it points at lives in a file the operator owns.

    Written as raw ASGI rather than BaseHTTPMiddleware on purpose: /api/events
    and /api/status/stream are SSE, and a BaseHTTPMiddleware in that path
    buffers the stream it is supposed to pass through. Wrapping `send` is what
    lets a Set-Cookie be appended to the response start message without
    touching the body.

    Reads and writes `scope["headers"]` directly — no starlette import — so the
    whole thing stays testable with nothing but the standard library.
    """

    def __init__(self, app, cookie_name: str = SESSION_COOKIE,
                 max_age: int = SESSION_MAX_AGE, secure=None, same_site: str = "Lax"):
        self.app = app
        self.cookie_name = cookie_name
        self.max_age = int(max_age)
        # Default off because the app is reached over plain HTTP inside the
        # container and only HTTPS at the proxy, so it cannot detect the
        # scheme. An operator who is sure the outside world is HTTPS sets
        # MEM_SESSION_SECURE=1; leaving it off is not a default to "fix later"
        # so much as the honest answer to a question the process cannot see.
        if secure is None:
            secure = str(os.getenv("MEM_SESSION_SECURE") or "0").strip().lower() in ("1", "true", "yes", "on")
        self.secure = bool(secure)
        self.same_site = same_site

    # -- cookies ------------------------------------------------------------

    def _cookie(self, session_id: str) -> str:
        parts = [
            f"{self.cookie_name}={session_id}",
            "Path=/",
            "HttpOnly",          # not readable from JS: XSS cannot exfiltrate it
            f"Max-Age={self.max_age}",
            f"SameSite={self.same_site}",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _expired_cookie(self) -> str:
        return "; ".join([
            f"{self.cookie_name}=", "Path=/", "HttpOnly", "Max-Age=0",
            f"SameSite={self.same_site}",
        ] + (["Secure"] if self.secure else []))

    # -- persistence --------------------------------------------------------

    def _commit(self, vault: VaultSession) -> str:
        """Write the session back if it changed. Returns a Set-Cookie value or None.

        The three cases are distinct and each is deliberate:

        cleared + data   a fresh login. The old row is deleted and a *new id*
                         issued, which is what defeats session fixation — an id
                         planted in the browser before login must not become a
                         logged-in id by being carried across the login.
        cleared + no data  logout. Drop the row and expire the cookie.
        dirty + data     an in-place edit of an existing session.
        dirty + no data  every key was deleted off the session; treat that as
                         logout rather than leaving an empty row that some
                         future code might read as "a session exists".
        """
        vault.mark_committed()
        try:
            if vault.cleared:
                if vault.session_id:
                    delete_session(vault.session_id)
                return self._write_session(vault)
            if not vault.dirty:
                return None
            if not vault.data:
                if vault.session_id:
                    delete_session(vault.session_id)
                return self._expired_cookie()
            if vault.existed:
                update_session_data(vault.session_id, vault.data)
                return None
            return self._write_session(vault)
        except Exception as exc:  # a failed write must not 500 the request
            print(f"session: could not persist session for {vault.username!r}: {exc}", flush=True)
            return None

    def _write_session(self, vault: VaultSession) -> str:
        username = (vault.data.get("user") or "").strip()
        if not username:
            # A session with no identity is not a session. Refusing to persist
            # it keeps "is there a row" and "is there a user" the same question.
            if vault.session_id:
                delete_session(vault.session_id)
            return self._expired_cookie()
        record = create_session(username, max_age=self.max_age, data=vault.data)
        vault.set_record(record)
        return self._cookie(record["session_id"])

    # -- ASGI ---------------------------------------------------------------

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # lower(), not ==: HTTP header names are case-insensitive, so a
        # comparison against the lowercase constant is not a filter — a client
        # sending `X-Vault-User` would sail past it. uvicorn lowercases header
        # names in both its h11 and httptools implementations, so this is not
        # currently load-bearing; it is load-bearing for anything that drives
        # this scope by hand (a test, an ASGI client, a different server).
        headers = [
            (key, value) for key, value in scope["headers"]
            if key.lower() != VAULT_USER_HEADER
        ]
        session_id = _cookie_value(headers, self.cookie_name)
        record = load_session(session_id) if session_id else None
        vault = VaultSession(session_id, record)
        vault.existed = record is not None
        scope["headers"] = headers
        scope["session"] = vault

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                set_cookie = self._commit(vault)
                if set_cookie:
                    message.setdefault("headers", []).append(
                        (b"set-cookie", set_cookie.encode("latin-1"))
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)

# The cookie used to carry `{"user": ..., "pass": "<plaintext password>"}`, and
# the password was there for one reason: gui.py's Setup page rebuilt a Basic
# Auth header out of it to show the user. Now that the page shows a PSK
# instead, nothing needs it — and with the session on disk rather than in the
# cookie, storing it would mean a password sitting in a SQLite file inside
# ./mem-mcp-data, which a backup, a `cp -r` or a support bundle would carry away.
#
# These keys raise rather than warn. A comment asking the next person not to
# re-add `session["pass"]` is exactly the kind of guard that survives review
# and still ships; this one fails at the moment it is written. `csrf` is
# deliberately absent from the list — a CSRF token is not a credential and
# belongs here if it is ever needed.
# ---------------------------------------------------------------------------
# The request-scoped session object, and the middleware that persists it
# ---------------------------------------------------------------------------

FORBIDDEN_SESSION_KEYS = frozenset({"pass", "password", "passwd"})


def _reject_credentials(payload: dict) -> None:
    offending = sorted(FORBIDDEN_SESSION_KEYS.intersection(payload or {}))
    if offending:
        raise ValueError(
            "Refusing to store a credential in a session: "
            + ", ".join(offending)
            + ". Sessions are persisted to disk; authenticate with a PSK instead."
        )


class VaultSession:
    """A mutable dict backed by a row in the sessions table.

    It stands in for Starlette's session dict at `request.session`, so the
    existing `request.session.get("user")` and `request.session["user"] = ...`
    call sites are unchanged. What differs is where the data lives and what may
    be in it: this is persisted to a file, it holds no credentials (see
    FORBIDDEN_SESSION_KEYS), and the middleware writes it back only when
    something actually mutated it.

    `clear()` is logout, and it is also how a fresh login starts: it marks the
    session cleared so the middleware deletes the old row and mints a new id,
    which is what stops a pre-login session id from surviving into the
    authenticated one. Otherwise the id an attacker planted in a victim's
    browser before login would become a logged-in session by being carried over.
    """

    __slots__ = ("_data", "_session_id", "_existed", "_dirty", "_cleared", "_committed")

    def __init__(self, session_id: str = None, record: dict = None):
        self._session_id = session_id
        self._data = {}
        self._existed = record is not None
        self._dirty = False
        self._cleared = False
        self._committed = False
        if record:
            self._data = dict(record.get("data") or {})
            self._session_id = record.get("session_id") or session_id

    # -- read ---------------------------------------------------------------

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    # -- write --------------------------------------------------------------

    def __setitem__(self, key, value):
        _reject_credentials({key: value})
        self._data[key] = value
        self._dirty = True

    def __delitem__(self, key):
        del self._data[key]
        self._dirty = True

    def update(self, other):
        _reject_credentials(other)
        self._data.update(dict(other))
        self._dirty = True

    def setdefault(self, key, default=None):
        if key not in self._data:
            self[key] = default
        return self._data[key]

    def clear(self):
        """Log out. The middleware drops the row and issues a new id."""
        self._data.clear()
        self._cleared = True
        self._dirty = True

    # -- what the middleware asks -------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def existed(self) -> bool:
        """Whether a stored row backs this session id at request start.

        Decided on the way in rather than by re-querying at commit time: the
        answer to "is this an update or an insert" is a fact about the request,
        and answering it with a second SELECT would be a fact about the database
        that a concurrent logout could have changed underneath.
        """
        return self._existed

    @existed.setter
    def existed(self, value: bool) -> None:
        self._existed = bool(value)

    @property
    def username(self):
        return self._data.get("user")

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def cleared(self) -> bool:
        return self._cleared

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def data(self) -> dict:
        return self._data

    def mark_committed(self) -> None:
        self._committed = True

    def set_record(self, record: dict) -> None:
        """Adopt a freshly written row (new id, expiry) after a commit."""
        if not record:
            return
        self._session_id = record.get("session_id", self._session_id)

    def __repr__(self):
        # No payload in the repr. An exception traceback embeds this, and a
        # traceback is exactly the sort of thing that ends up in a shared log.
        return f"<VaultSession {'authenticated' if self.username else 'anonymous'}>"