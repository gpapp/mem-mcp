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

import hashlib
import json
import os
import secrets
import sqlite3
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
    """Create the schema. Idempotent, and called before every other function."""
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

        headers = [
            (key, value) for key, value in scope["headers"]
            if key != VAULT_USER_HEADER
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