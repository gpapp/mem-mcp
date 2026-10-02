"""
test_sessions.py – the persistent session store and the MCP pre-shared keys.

Every test here *calls* sessions.py. That is possible only because it imports
nothing but the standard library, which is the constraint that makes it worth
testing behaviourally: every security decision in the store — how a key is
hashed, who may revoke it, when a session dies, what is allowed to be written
to disk — is invisible to the shape of the code and only shows up when it is
run.

The suite points MEM_SESSION_DIR at a fresh temporary directory per test, so
nothing here touches a real vault, and the schema cache in sessions.py is keyed
by path precisely so that switching directories mid-run is not a stale-schema
bug waiting to happen.

Run:  python3 -m unittest -v test_sessions.py
"""

import asyncio
import ast
import builtins
import os
import shutil
import sqlite3
import tempfile
import unittest

import sessions

HERE = os.path.dirname(os.path.abspath(__file__))


class Python311AnnotationTests(unittest.TestCase):
    """Annotations must not reference a name that is not bound yet.

    The container runs `python:3.11-slim` (mem-mcp/Dockerfile). This suite runs
    on whatever interpreter the developer has, and on 3.14 that is 3.14 — where
    PEP 649 makes annotations *lazy*, so a forward reference to a class defined
    further down the file imports perfectly. On 3.11 annotations are evaluated
    when the `def` runs, the class body raises `NameError`, and the app does not
    start at all.

    That failure is invisible from here by construction, so it cannot be caught
    by importing the module — the interpreter that would expose it is not the
    one running the tests. Stripping `from __future__ import annotations` does not
    help either: PEP 649 is the 3.14 *default*, not something the future import
    turns on. So this walks the AST and checks the property directly: every name
    used in an annotation is already bound at that point in module order.

    `from __future__ import annotations` makes this pass by making annotations
    strings. That is guaranteed by the language spec on every version ≥ 3.7, so
    it is not a 3.11 guess — but it is also why the check is worth keeping: it
    catches the day someone removes that line, which would look fine here.
    """

    # Scanned as text, never imported: gui.py and common.py cannot be imported
    # on a box without fastapi/httpx, which is precisely why the defect needs a
    # static check rather than a test that imports its subject.
    MODULES = ("sessions.py", "gui.py", "common.py", "server.py")

    @staticmethod
    def _module_bindings(tree):
        """Names bound at module level, with the line each becomes available.

        Only whole-module bindings are recorded, which is what PEP 649-era
        ordering actually turns on: a name used inside a class body must be
        bound at module scope by the time that class body runs. Functions and
        classes are recorded by name because a `def` binds its name the moment
        its body finishes, which is close enough — a self-referential type is
        the one case this admits, and quoting it is correct anyway.
        """
        bindings = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bindings.append((node.lineno, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bindings.append((node.lineno, node.name))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            bindings.append((sub.lineno, sub.id))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bindings.append((node.lineno, node.target.id))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bindings.append((node.lineno, (alias.asname or alias.name).split(".")[0]))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                for sub in ast.walk(node.target):
                    if isinstance(sub, ast.Name):
                        bindings.append((sub.lineno, sub.id))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for sub in ast.walk(item.optional_vars):
                            if isinstance(sub, ast.Name):
                                bindings.append((sub.lineno, sub.id))
        return sorted(bindings)

    @staticmethod
    def _annotation_names(node):
        """Every bare Name appearing anywhere inside an annotation."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                yield sub

    @staticmethod
    def _annotations(tree):
        """(lineno, annotation) for every annotation in the module, nested included."""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                collected = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                for extra in (args.vararg, args.kwarg):
                    if extra is not None and extra.annotation is not None:
                        yield extra.annotation
                for arg in collected:
                    if arg.annotation is not None:
                        yield arg.annotation
                if node.returns is not None:
                    yield node.returns
            elif isinstance(node, ast.AnnAssign):
                yield node.annotation

    def test_no_annotation_uses_a_name_bound_later_in_the_file(self):
        """Safe if annotations are deferred, or if there is nothing to defer.

        The two are alternatives, not requirements together: a forward
        reference is only fatal when the annotation is actually evaluated, which
        is what `from __future__ import annotations` prevents. Asserting on the
        forward reference alone would fail on the fixed file forever; asserting
        on the future import alone would miss a *second* module that introduces
        one without the guard. What must never happen is the combination.
        """
        problems = []
        for name in self.MODULES:
            path = os.path.join(HERE, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            tree = ast.parse(source, name)
            if "from __future__ import annotations" in source:
                continue          # annotations are strings; nothing is evaluated
            bindings = self._module_bindings(tree)
            for annotation in self._annotations(tree):
                for name_node in self._annotation_names(annotation):
                    if name_node.id in dir(builtins):
                        continue
                    available = [b for b in bindings if b[0] <= name_node.lineno]
                    if not any(b[1] == name_node.id for b in available):
                        problems.append(
                            f"{name}:{name_node.lineno} annotates with '{name_node.id}', "
                            f"which is not bound until after this line, and the "
                            f"module does not defer annotations")
        self.assertFalse(
            problems,
            "forward reference in an eagerly-evaluated annotation; the 3.11 "
            "container evaluates annotations at def time and will refuse to "
            "start:\n  " + "\n  ".join(problems))

    def test_sessions_defers_annotations_so_the_container_can_import_it(self):
        """The half of the disjunction above that sessions.py relies on.

        It is a real forward reference — `VaultSessionMiddleware` is defined
        above `VaultSession` and annotates a parameter as `vault: VaultSession`
        — so the future import is what makes this file importable at all on the
        3.11 container. Pinning it directly means removing that one line is
        caught here by name, not merely implied by the check above.
        """
        source = open(os.path.join(HERE, "sessions.py"), encoding="utf-8").read()
        self.assertIn("from __future__ import annotations", source,
                      "sessions.py must defer annotations: the container is 3.11 "
                      "and the class order below requires it")

    def test_the_forward_reference_this_file_relies_on_still_exists(self):
        """Guards the fix from the other side.

        If someone reorders the two classes so `VaultSession` comes first, the
        future import becomes unnecessary — and then a test that only asserted
        the import is present would keep passing while describing nothing. This
        pins the reason the import is there, so removing either the class order
        or the import is a deliberate, visible choice.
        """
        with open(os.path.join(HERE, "sessions.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), "sessions.py")
        order = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        self.assertLess(
            order.index("VaultSessionMiddleware"), order.index("VaultSession"),
            "the forward reference is gone -- if VaultSession now precedes the "
            "middleware, the future import can go too, and this test should be "
            "deleted rather than left explaining a hazard that no longer exists")




class StoreCase(unittest.TestCase):
    """A private database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vault-sessions-")
        self._saved_dir = os.environ.get("MEM_SESSION_DIR")
        os.environ["MEM_SESSION_DIR"] = self.tmp

    def tearDown(self):
        if self._saved_dir is None:
            os.environ.pop("MEM_SESSION_DIR", None)
        else:
            os.environ["MEM_SESSION_DIR"] = self._saved_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def db_bytes(self) -> bytes:
        """Every byte this store has written, including the WAL."""
        blob = b""
        for name in os.listdir(self.tmp):
            if name.startswith("sessions.db"):
                with open(os.path.join(self.tmp, name), "rb") as handle:
                    blob += handle.read()
        return blob


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

class SessionLifecycleTests(StoreCase):

    def test_a_new_session_round_trips_its_payload(self):
        record = sessions.create_session("alice", data={"user": "alice"})
        loaded = sessions.load_session(record["session_id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["username"], "alice")
        self.assertEqual(loaded["data"]["user"], "alice")

    def test_an_unknown_or_empty_id_is_not_a_session(self):
        self.assertIsNone(sessions.load_session("never-issued"))
        self.assertIsNone(sessions.load_session(""))
        self.assertIsNone(sessions.load_session(None))

    def test_expiry_is_absolute_and_does_not_slide(self):
        created = sessions.create_session("alice", now=1000.0)
        # Use it repeatedly across what would be a sliding window's whole life.
        for moment in (2000.0, 3000.0, 4000.0):
            self.assertIsNotNone(sessions.load_session(created["session_id"], now=moment))
        past_deadline = 1000.0 + sessions.SESSION_MAX_AGE + 1
        self.assertIsNone(sessions.load_session(created["session_id"], now=past_deadline),
                         "A replayed cookie must not be able to keep its own session alive")

    def test_expiry_is_at_the_boundary_and_not_one_second_earlier(self):
        created = sessions.create_session("alice", now=1000.0, max_age=60)
        deadline = created["expires_at"]
        self.assertIsNotNone(sessions.load_session(created["session_id"], now=deadline - 1))
        self.assertIsNone(sessions.load_session(created["session_id"], now=deadline))

    def test_an_expired_row_is_deleted_rather_than_left_for_the_clock(self):
        created = sessions.create_session("alice", now=1000.0, max_age=60)
        sessions.load_session(created["session_id"], now=2000.0)
        rows = sqlite3.connect(sessions.db_path()).execute(
            "SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_use_updates_last_seen_without_moving_the_deadline(self):
        created = sessions.create_session("alice", now=1000.0, max_age=600)
        used = sessions.load_session(created["session_id"], now=1100.0)
        self.assertEqual(used["last_seen_at"], 1100.0)
        self.assertEqual(used["expires_at"], created["expires_at"])
        untouched = sessions.load_session(created["session_id"], now=1200.0, touch=False)
        self.assertEqual(untouched["last_seen_at"], 1100.0)

    def test_logout_deletes_only_the_named_session(self):
        keep = sessions.create_session("alice")
        drop = sessions.create_session("alice")
        self.assertTrue(sessions.delete_session(drop["session_id"]))
        self.assertFalse(sessions.delete_session(drop["session_id"]))
        self.assertIsNotNone(sessions.load_session(keep["session_id"]))

    def test_logout_everywhere_ends_only_that_users_sessions(self):
        alice = [sessions.create_session("alice") for _ in range(2)]
        bob = sessions.create_session("bob")
        self.assertEqual(sessions.delete_sessions_for_user("alice"), 2)
        for record in alice:
            self.assertIsNone(sessions.load_session(record["session_id"]))
        self.assertIsNotNone(sessions.load_session(bob["session_id"]))

    def test_purge_removes_expired_sessions_and_keeps_live_ones(self):
        stale = sessions.create_session("alice", now=1000.0, max_age=10)
        live = sessions.create_session("alice", now=5000.0)
        removed = sessions.purge_expired_sessions(now=6000.0)
        self.assertEqual(removed, 1)
        self.assertIsNone(sessions.load_session(stale["session_id"], now=6000.0))
        self.assertIsNotNone(sessions.load_session(live["session_id"], now=6000.0))

    def test_a_malformed_payload_is_treated_as_no_session(self):
        created = sessions.create_session("alice")
        conn = sqlite3.connect(sessions.db_path())
        conn.execute("UPDATE sessions SET data = ? WHERE session_id = ?",
                     ("not json at all", created["session_id"]))
        conn.commit()
        conn.close()
        self.assertIsNone(sessions.load_session(created["session_id"]),
                          "A row that cannot be parsed must not look like a live session")

    def test_a_non_positive_max_age_is_refused_rather_than_silently_infinite(self):
        with self.assertRaises(ValueError):
            sessions.create_session("alice", max_age=0)
        with self.assertRaises(ValueError):
            sessions.create_session("alice", max_age=-1)

    def test_session_ids_are_not_derived_from_the_username(self):
        first = sessions.create_session("alice")["session_id"]
        second = sessions.create_session("alice")["session_id"]
        self.assertNotEqual(first, second)
        self.assertNotIn("alice", first)
        self.assertGreaterEqual(len(first), 32)


# ---------------------------------------------------------------------------
# What is allowed to be persisted
# ---------------------------------------------------------------------------

class SessionPayloadGuardTests(StoreCase):
    """The cookie used to carry the plaintext password. It must not come back."""

    def test_storing_a_password_raises_at_the_point_of_writing(self):
        session = sessions.VaultSession("some-id")
        for key in ("pass", "password", "passwd"):
            with self.assertRaises(ValueError, msg=f"{key!r} was accepted"):
                session[key] = "hunter2"
        self.assertEqual(len(session), 0)

    def test_the_guard_reaches_the_store_not_just_the_dict(self):
        # The dict guard alone would leave a direct create_session(data=...)
        # writing a password to disk, which is the same leak one layer down.
        with self.assertRaises(ValueError):
            sessions.create_session("alice", data={"user": "alice", "pass": "hunter2"})
        with self.assertRaises(ValueError):
            sessions.update_session_data("any-id", {"user": "alice", "password": "hunter2"})

    def test_a_legitimate_extra_key_is_still_allowed(self):
        # Otherwise the fix is "delete the password line" and the next person
        # concludes sessions cannot hold anything but a username.
        session = sessions.VaultSession("some-id")
        session["csrf"] = "opaque-token"
        session.update({"locale": "en"})
        self.assertEqual(session["csrf"], "opaque-token")
        self.assertEqual(session["locale"], "en")

    def test_the_repr_carries_no_payload(self):
        session = sessions.VaultSession("some-id")
        session["user"] = "alice"
        self.assertNotIn("alice", repr(session))


# ---------------------------------------------------------------------------
# Pre-shared keys
# ---------------------------------------------------------------------------

class PskTests(StoreCase):

    def test_a_key_resolves_to_the_user_it_was_minted_for(self):
        created = sessions.create_psk("alice", label="laptop")
        resolved = sessions.resolve_psk(created["key"])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["user_id"], "alice")

    def test_the_plaintext_key_is_not_recoverable_and_leaves_nothing_on_disk(self):
        created = sessions.create_psk("alice", label="laptop")
        key = created["key"]
        self.assertNotIn(key, self.db_bytes().decode("utf-8", "replace"),
                         "The full key must never be written to the database file")
        # The high-entropy remainder, which is everything that matters.
        tail = key[len(sessions.PSK_PREFIX) + 6:]
        self.assertGreaterEqual(len(tail), 30)
        self.assertNotIn(tail, self.db_bytes().decode("utf-8", "replace"))
        # Only the display prefix is stored, and it is short by design.
        self.assertEqual(created["prefix"], key[:len(sessions.PSK_PREFIX) + 6])
        self.assertIn(created["prefix"], self.db_bytes().decode("utf-8", "replace"))

    def test_the_display_prefix_is_too_short_to_be_a_second_credential(self):
        created = sessions.create_psk("alice")
        self.assertLessEqual(len(created["prefix"]), 12)
        self.assertIsNone(sessions.resolve_psk(created["prefix"]),
                          "A stored prefix must not itself authenticate")

    def test_a_key_carries_a_recognisable_scheme(self):
        created = sessions.create_psk("alice")
        self.assertTrue(created["key"].startswith(sessions.PSK_PREFIX))

    def test_an_unknown_or_malformed_key_resolves_to_nothing(self):
        sessions.create_psk("alice")
        self.assertIsNone(sessions.resolve_psk(sessions.PSK_PREFIX + "not-a-real-key"))
        self.assertIsNone(sessions.resolve_psk("hunter2"))
        self.assertIsNone(sessions.resolve_psk(""))
        self.assertIsNone(sessions.resolve_psk(None))

    def test_a_credential_meant_for_another_mechanism_is_refused_before_the_database(self):
        # A session id is a bearer credential too. Without the prefix check it
        # would be hashed and looked up, which is a cross-mechanism path from
        # "stolen cookie" to "full MCP access" waiting to be closed by accident.
        created = sessions.create_session("alice")
        self.assertIsNone(sessions.resolve_psk(created["session_id"]))

    def test_revocation_takes_effect_immediately(self):
        created = sessions.create_psk("alice", label="old laptop")
        self.assertTrue(sessions.revoke_psk(created["id"], "alice"))
        self.assertIsNone(sessions.resolve_psk(created["key"]))

    def test_revoking_twice_reports_that_nothing_happened(self):
        created = sessions.create_psk("alice")
        self.assertTrue(sessions.revoke_psk(created["id"], "alice"))
        self.assertFalse(sessions.revoke_psk(created["id"], "alice"))

    def test_a_revoked_key_stays_revoked_after_its_expiry_is_reached(self):
        created = sessions.create_psk("alice", expires_in_days=1)
        sessions.revoke_psk(created["id"], "alice")
        long_after = created["created_at"] + 10 * 24 * 3600
        self.assertIsNone(sessions.resolve_psk(created["key"], now=long_after))

    def test_replaying_a_dead_key_does_not_warm_its_row(self):
        created = sessions.create_psk("alice")
        sessions.revoke_psk(created["id"], "alice")
        sessions.resolve_psk(created["key"])
        listed = sessions.list_psks("alice")
        self.assertIsNone(listed[0]["last_used_at"])

    def test_use_records_a_timestamp(self):
        created = sessions.create_psk("alice")
        self.assertIsNone(sessions.list_psks("alice")[0]["last_used_at"])
        resolved = sessions.resolve_psk(created["key"], now=1234.0)
        self.assertEqual(resolved["last_used_at"], 1234.0)
        self.assertEqual(sessions.list_psks("alice")[0]["last_used_at"], 1234.0)

    def test_an_expired_key_stops_working(self):
        created = sessions.create_psk("alice", expires_in_days=7)
        just_before = created["expires_at"] - 1
        self.assertIsNotNone(sessions.resolve_psk(created["key"], now=just_before))
        self.assertIsNone(sessions.resolve_psk(created["key"], now=created["expires_at"]))

    def test_a_key_with_no_expiry_never_expires(self):
        created = sessions.create_psk("alice")
        self.assertIsNone(created["expires_at"])
        self.assertIsNotNone(sessions.resolve_psk(created["key"], now=created["created_at"] + 10**9))

    def test_an_out_of_range_expiry_is_refused(self):
        for days in (0, -5, 366):
            with self.assertRaises(ValueError, msg=f"{days} days was accepted"):
                sessions.create_psk("alice", expires_in_days=days)


class PskOwnershipTests(StoreCase):
    """One user's key must never be visible to, or revocable by, another."""

    def setUp(self):
        super().setUp()
        self.alices = sessions.create_psk("alice", label="work")
        self.bobs = sessions.create_psk("bob", label="personal")

    def test_a_list_shows_only_your_own_keys(self):
        listed = sessions.list_psks("alice")
        self.assertEqual([row["id"] for row in listed], [self.alices["id"]])
        self.assertTrue(all(row["user_id"] == "alice" for row in listed))

    def test_another_user_cannot_revoke_your_key(self):
        self.assertFalse(sessions.revoke_psk(self.alices["id"], "bob"))
        self.assertIsNotNone(sessions.resolve_psk(self.alices["key"]),
                             "A refused revoke must leave the key working")
        self.assertEqual(sessions.list_psks("alice")[0]["status"], "active")

    def test_revoking_another_users_key_reveals_nothing_about_its_existence(self):
        # Same answer for "no such id" and "not yours", so the endpoint cannot
        # be used to enumerate which key ids are real.
        self.assertFalse(sessions.revoke_psk("does-not-exist", "bob"))
        self.assertFalse(sessions.revoke_psk(self.alices["id"], "bob"))

    def test_a_key_resolves_to_its_own_minted_user_not_to_the_caller(self):
        self.assertEqual(sessions.resolve_psk(self.bobs["key"])["user_id"], "bob")


class PskListingTests(StoreCase):

    def test_status_reflects_revocation_and_expiry(self):
        active = sessions.create_psk("alice", label="a")
        revoked = sessions.create_psk("alice", label="b")
        expiring = sessions.create_psk("alice", label="c", expires_in_days=1)
        sessions.revoke_psk(revoked["id"], "alice")
        by_id = {row["id"]: row for row in sessions.list_psks("alice")}
        self.assertEqual(by_id[active["id"]]["status"], "active")
        self.assertEqual(by_id[revoked["id"]]["status"], "revoked")
        self.assertEqual(by_id[expiring["id"]]["status"], "active")
        future = expiring["expires_at"] + 1
        by_id = {row["id"]: row for row in sessions.list_psks("alice", now=future)}
        self.assertEqual(by_id[expiring["id"]]["status"], "expired")

    def test_a_revoked_key_stays_visible_in_the_list(self):
        # A key that vanishes from the list is indistinguishable from one that
        # was never revoked, and the owner has to be able to see it is gone.
        created = sessions.create_psk("alice")
        sessions.revoke_psk(created["id"], "alice")
        self.assertEqual(len(sessions.list_psks("alice")), 1)

    def test_a_label_is_collapsed_and_bounded(self):
        created = sessions.create_psk("alice", label="  laptop\n\tand   phone  ")
        self.assertEqual(created["label"], "laptop and phone")
        long_label = sessions.create_psk("alice", label="x" * 500)
        self.assertEqual(len(long_label["label"]), sessions.MAX_LABEL_CHARS)
        self.assertEqual(sessions.create_psk("alice", label=None)["label"], "")

    def test_two_keys_never_collide(self):
        keys = {sessions.create_psk("alice")["key"] for _ in range(50)}
        self.assertEqual(len(keys), 50)

    def test_a_user_with_no_keys_gets_an_empty_list(self):
        self.assertEqual(sessions.list_psks("nobody"), [])
        self.assertEqual(sessions.list_psks(""), [])


# ---------------------------------------------------------------------------
# The middleware
# ---------------------------------------------------------------------------

class AsgiCase(StoreCase):
    """Drives the middleware with real ASGI messages, so these are not shape checks.

    Nothing here needs starlette: the middleware reads and writes `scope`
    directly, which is the whole reason it is written as raw ASGI rather than
    BaseHTTPMiddleware (see its docstring) — and also why it can be tested on a
    box that has no web framework installed at all.
    """

    def setUp(self):
        super().setUp()
        self.app = None
        self.middleware = sessions.VaultSessionMiddleware(self._wrap, max_age=600)

    async def _wrap(self, scope, receive, send):
        if self.app is not None:
            await self.app(scope, receive, send)

    async def _drive(self, handler, headers=(), cookie=None):
        self.app = handler
        if cookie:
            headers = list(headers) + [(b"cookie", cookie.encode())]
        scope = {"type": "http", "method": "GET", "path": "/api/x",
                 "headers": list(headers), "query_string": b""}
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await self.middleware(scope, receive, send)
        return messages

    @staticmethod
    def set_cookie(messages):
        for message in messages:
            if message["type"] != "http.response.start":
                continue
            for key, value in message.get("headers", []):
                if key == b"set-cookie":
                    return value.decode("latin-1")
        return None

    @staticmethod
    def session_id_of(set_cookie):
        if not set_cookie:
            return None
        return set_cookie.split(";")[0].split("=", 1)[1]

    @staticmethod
    def login(body):
        """A handler that logs in, the way gui.py's api_login does."""
        async def handler(scope, receive, send):
            scope["session"].clear()
            scope["session"]["user"] = body
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        return handler

    def login_then(self, username="alice"):
        set_cookie = self.set_cookie(asyncio.run(self._drive(self.login(username))))
        return set_cookie, self.session_id_of(set_cookie)


class SessionMiddlewareTests(AsgiCase):

    def test_an_anonymous_request_gets_no_cookie(self):
        messages = asyncio.run(self._drive(lambda s, r, send: send(
            {"type": "http.response.start", "status": 200, "headers": []})))
        self.assertIsNone(self.set_cookie(messages),
                          "An anonymous request must not be issued a session")

    def test_logging_in_issues_a_cookie_and_stores_the_user(self):
        set_cookie, session_id = self.login_then("alice")
        self.assertIsNotNone(set_cookie)
        self.assertEqual(sessions.load_session(session_id)["username"], "alice")

    def test_the_cookie_carries_only_the_id(self):
        set_cookie, session_id = self.login_then("alice")
        self.assertEqual(self.session_id_of(set_cookie), session_id)
        self.assertNotIn("alice", set_cookie)

    def test_the_cookie_is_httponly_and_samesite(self):
        set_cookie, _ = self.login_then()
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("Secure", set_cookie)  # opt-in via MEM_SESSION_SECURE

    def test_secure_can_be_turned_on(self):
        middleware = sessions.VaultSessionMiddleware(self._wrap, secure=True)
        self.assertIn("Secure", middleware._cookie("abc"))

    def test_secure_follows_the_env_var_when_not_passed(self):
        os.environ["MEM_SESSION_SECURE"] = "1"
        try:
            self.assertTrue(sessions.VaultSessionMiddleware(self._wrap).secure)
        finally:
            del os.environ["MEM_SESSION_SECURE"]

    def test_the_session_is_restored_on_the_next_request(self):
        set_cookie, _ = self.login_then("alice")
        seen = {}

        async def handler(scope, receive, send):
            seen["user"] = scope["session"].get("user")
            await send({"type": "http.response.start", "status": 200, "headers": []})

        asyncio.run(self._drive(handler, cookie=f"{sessions.SESSION_COOKIE}="
                                     + self.session_id_of(set_cookie)))
        self.assertEqual(seen["user"], "alice")

    def test_an_untouched_request_rewrites_no_cookie(self):
        set_cookie, _ = self.login_then("alice")
        messages = asyncio.run(self._drive(
            lambda s, r, send: send({"type": "http.response.start", "status": 200, "headers": []}),
            cookie=f"{sessions.SESSION_COOKIE}=" + self.session_id_of(set_cookie)))
        self.assertIsNone(self.set_cookie(messages))

    def test_logging_out_drops_the_row_and_expires_the_cookie(self):
        set_cookie, session_id = self.login_then("alice")

        async def handler(scope, receive, send):
            scope["session"].clear()
            await send({"type": "http.response.start", "status": 200, "headers": []})

        messages = asyncio.run(self._drive(
            handler, cookie=f"{sessions.SESSION_COOKIE}=" + session_id))
        self.assertIn("Max-Age=0", self.set_cookie(messages))
        self.assertIsNone(sessions.load_session(session_id))

    def test_logging_in_over_an_existing_session_issues_a_different_id(self):
        # Session fixation: an id planted before login must not still be the
        # logged-in id afterwards.
        planted = sessions.create_session("victim")
        messages = asyncio.run(self._drive(
            self.login("alice"),
            cookie=f"{sessions.SESSION_COOKIE}={planted['session_id']}"))
        new_id = self.session_id_of(self.set_cookie(messages))
        self.assertNotEqual(new_id, planted["session_id"])
        self.assertIsNone(sessions.load_session(planted["session_id"]))
        self.assertEqual(sessions.load_session(new_id)["username"], "alice")

    def test_a_forged_cookie_is_simply_no_session(self):
        seen = {}

        async def handler(scope, receive, send):
            seen["user"] = scope["session"].get("user")
            await send({"type": "http.response.start", "status": 200, "headers": []})

        asyncio.run(self._drive(
            handler, cookie=f"{sessions.SESSION_COOKIE}={'x' * 64}"))
        self.assertIsNone(seen["user"])

    def test_deleting_every_key_counts_as_logging_out(self):
        set_cookie, session_id = self.login_then("alice")

        async def handler(scope, receive, send):
            del scope["session"]["user"]
            await send({"type": "http.response.start", "status": 200, "headers": []})

        asyncio.run(self._drive(
            handler, cookie=f"{sessions.SESSION_COOKIE}=" + session_id))
        self.assertIsNone(sessions.load_session(session_id),
                          "An empty session row must not outlive the request")

    def test_a_session_with_no_identity_is_not_persisted(self):
        async def handler(scope, receive, send):
            scope["session"]["csrf"] = "opaque"
            await send({"type": "http.response.start", "status": 200, "headers": []})

        messages = asyncio.run(self._drive(handler))
        self.assertIn("Max-Age=0", self.set_cookie(messages))

    def test_writing_a_password_through_the_middleware_is_refused(self):
        async def handler(scope, receive, send):
            scope["session"]["user"] = "alice"
            scope["session"]["pass"] = "hunter2"
            await send({"type": "http.response.start", "status": 200, "headers": []})

        with self.assertRaises(ValueError):
            asyncio.run(self._drive(handler))

    def test_a_failed_write_does_not_take_the_request_down(self):
        # A disk that is full or read-only must not turn every authenticated
        # request into a 500; the response still goes out, just without a
        # session to carry.
        async def handler(scope, receive, send):
            scope["session"]["user"] = "alice"
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        original = sessions.create_session
        sessions.create_session = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            messages = asyncio.run(self._drive(handler))
        finally:
            sessions.create_session = original
        statuses = [m.get("status") for m in messages if m["type"] == "http.response.start"]
        self.assertEqual(statuses, [200])

    def test_the_body_is_passed_through_untouched(self):
        # SSE lives behind this middleware. Anything that buffered or replaced
        # the body would take /api/events and /api/status/stream with it.
        async def handler(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            for chunk in (b"data: 1\n\n", b"data: 2\n\n"):
                await send({"type": "http.response.body", "body": chunk, "more_body": True})

        messages = asyncio.run(self._drive(handler))
        bodies = b"".join(m.get("body", b"") for m in messages
                          if m["type"] == "http.response.body")
        self.assertEqual(bodies, b"data: 1\n\ndata: 2\n\n")


class IdentityHeaderTests(AsgiCase):
    """X-Vault-User is how mcp_auth_guard says who you are. It must not be askable for."""

    def test_an_inbound_identity_header_is_stripped_from_every_request(self):
        seen = {}

        async def handler(scope, receive, send):
            seen["headers"] = scope["headers"]
            await send({"type": "http.response.start", "status": 200, "headers": []})

        asyncio.run(self._drive(handler, headers=[
            (b"x-vault-user", b"victim"),
            (b"authorization", b"Bearer mvk_something"),
        ]))
        keys = [key for key, _ in seen["headers"]]
        self.assertNotIn(sessions.VAULT_USER_HEADER, keys)
        self.assertIn(b"authorization", keys, "Only the identity header may be removed")

    def test_stripping_is_case_insensitive(self):
        # HTTP header names are case-insensitive, so a lowercase-only filter is
        # not a filter.
        seen = {}

        async def handler(scope, receive, send):
            seen["headers"] = scope["headers"]
            await send({"type": "http.response.start", "status": 200, "headers": []})

        asyncio.run(self._drive(handler, headers=[(b"X-Vault-User", b"victim")]))
        self.assertNotIn(sessions.VAULT_USER_HEADER, [k for k, _ in seen["headers"]])


if __name__ == "__main__":
    unittest.main()