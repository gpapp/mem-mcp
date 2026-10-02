"""
test_registration.py – self-service signup: accounts, passwords, and the gates.

This is a separate suite from test_sessions.py and test_auth_guard.py on
purpose, because registration is a *third* credential surface and it is the
only one that is unauthenticated by design: `/api/auth/*` is the only prefix
`auth_guard` lets through with no credential, so a signup endpoint lives there by
necessity. Splitting it out makes it obvious in the test list that the code with
the weakest authentication has its own tests, rather than being one more class
buried in a suite about the two gates that are properly protected.

Half the suite *calls* sessions.py (importing it directly — it is stdlib-only for
exactly this reason) and half *lifts* functions out of gui.py with
`ast.get_source_segment`, because gui.py needs fastapi and this box has none.

The properties worth having tests for are all invisible in the shape of the code:

  * a wrong password and a *corrupt stored hash* must both be False, and the
    second must not raise — the caller is a login path;
  * registering twice must not silently reset an existing password;
  * an address that already names a Google-linked or htpasswd-owned vault must
    not be registrable, or signup becomes account takeover;
  * the throttle must count attempts *before* the flag check, or turning
    registration off removes the rate limit from a still-mounted route;
  * `_verify_account` must return the canonical key, because the vault key for a
    registered account is the lowercased email and a session holding what was
    typed signs you into a vault that does not exist.

Run:  python3 -m unittest -v test_registration.py
"""

import ast
import asyncio
import base64
import logging
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sessions  # noqa: E402  (stdlib-only, so importable with nothing installed)

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(relative: str) -> str:
    with open(os.path.join(os.path.dirname(HERE), relative), "r", encoding="utf-8") as handle:
        return handle.read()


def _lift(relative: str, node_name: str, namespace: dict):
    """exec one function/class out of a module that cannot be imported here.

    The namespace is exec'd into directly, not a copy of it: the lifted function's
    globals ARE that dict, so anything it calls must be present in the same one.
    Copying it produces a function whose globals are somewhere its callers cannot
    see, which fails with a NameError that looks like a bug in the module.
    """
    tree = ast.parse(_read(relative))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == node_name:
            segment = ast.get_source_segment(_read(relative), node)
            exec(compile(segment, relative, "exec"), namespace)
            return namespace[node_name]
    raise AssertionError(f"{node_name} not found in {relative}")


def _function_source(relative: str, node_name: str) -> str:
    tree = ast.parse(_read(relative))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == node_name:
            return ast.get_source_segment(_read(relative), node)
    raise AssertionError(f"{node_name} not found in {relative}")


class StoreCase(unittest.TestCase):
    """A private database per test, and no operator htpasswd in sight."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vault-register-")
        self._saved_dir = os.environ.get("MEM_SESSION_DIR")
        self._saved_htpasswd = os.environ.get("HTPASSWD_PATH")
        os.environ["MEM_SESSION_DIR"] = self.tmp
        # user_id_taken shells out to ask htpasswd whether a name exists.
        # Pointing that at the operator's real file would make these tests
        # depend on what happens to be in it.
        os.environ["HTPASSWD_PATH"] = os.path.join(self.tmp, "absent-htpasswd")

    def tearDown(self):
        for name, value in (("MEM_SESSION_DIR", self._saved_dir),
                            ("HTPASSWD_PATH", self._saved_htpasswd)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def db_bytes(self) -> bytes:
        blob = b""
        for name in os.listdir(self.tmp):
            if name.startswith("sessions.db"):
                with open(os.path.join(self.tmp, name), "rb") as handle:
                    blob += handle.read()
        return blob


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

class PasswordHashingTests(unittest.TestCase):
    GOOD = "correct horse battery staple"

    def test_the_right_password_verifies(self):
        encoded = sessions.hash_password(self.GOOD)
        self.assertTrue(sessions.verify_password(self.GOOD, encoded))

    def test_a_wrong_password_does_not(self):
        encoded = sessions.hash_password(self.GOOD)
        self.assertFalse(sessions.verify_password("Correct horse battery staple", encoded))
        self.assertFalse(sessions.verify_password("", encoded))
        self.assertFalse(sessions.verify_password(self.GOOD + " ", encoded))

    def test_the_same_password_hashes_differently_every_time(self):
        # A per-hash salt. Without it two accounts that chose the same password
        # are visibly identical in the database file and in any backup of it,
        # which is the leak scrypt exists to make harder.
        first = sessions.hash_password(self.GOOD)
        second = sessions.hash_password(self.GOOD)
        self.assertNotEqual(first, second)
        self.assertTrue(sessions.verify_password(self.GOOD, first))
        self.assertTrue(sessions.verify_password(self.GOOD, second))

    def test_the_plaintext_is_not_in_the_hash(self):
        encoded = sessions.hash_password(self.GOOD)
        self.assertNotIn(self.GOOD, encoded)

    def test_the_cost_parameters_travel_inside_the_string(self):
        # So they can be raised later without invalidating anyone's password: a
        # verifier reads the cost from what it was handed, not from what it was
        # compiled with.
        parts = sessions.hash_password(self.GOOD).split("$")
        self.assertEqual(parts[0], "scrypt")
        # 7 fields: algorithm, n, r, p, dklen, salt, digest. dklen is in there
        # because a verifier that derived it from the stored digest would compute
        # a digest of that same length and compare equal.
        self.assertEqual(len(parts), 7)
        self.assertEqual([int(part) for part in parts[1:5]],
                         [sessions.SCRYPT_N, sessions.SCRYPT_R, sessions.SCRYPT_P,
                          sessions.SCRYPT_DKLEN])

    def test_a_hash_written_with_cheaper_parameters_still_verifies(self):
        weak = sessions.hash_password(self.GOOD, n=2 ** 10, dklen=16)
        self.assertTrue(sessions.verify_password(self.GOOD, weak))
        # And a stored digest that disagrees with the length it claims is refused,
        # rather than being taken at its word.
        parts = weak.split("$")
        parts[6] = parts[6][:8]
        self.assertFalse(sessions.verify_password(self.GOOD, "$".join(parts)))

    def test_a_corrupt_hash_is_false_and_not_an_exception(self):
        # The caller is a login path. A row restored from a damaged savepoint,
        # hand-edited, or written by a future format must read as "this account
        # cannot log in" rather than 500ing every request that touches it.
        for broken in ["", "not-a-hash", "scrypt$16384$8$1", "bcrypt$1$2$3",
                       "scrypt$x$8$1$AAAA$AAAA", "scrypt$16384$8$1$32$!!!!$AAAA",
                       "scrypt$0$0$0$AAAA$AAAA", None]:
            with self.subTest(broken=broken):
                self.assertFalse(sessions.verify_password(self.GOOD, broken))

    def test_a_truncated_digest_does_not_verify(self):
        parts = sessions.hash_password(self.GOOD).split("$")
        parts[5] = parts[5][:8]
        self.assertFalse(sessions.verify_password(self.GOOD, "$".join(parts)))

    def test_passwords_outside_the_bounds_are_refused(self):
        with self.assertRaises(ValueError):
            sessions.hash_password("x" * (sessions.MIN_PASSWORD_CHARS - 1))
        with self.assertRaises(ValueError):
            sessions.hash_password("x" * (sessions.MAX_PASSWORD_CHARS + 1))

    def test_the_minimum_length_is_actually_accepted(self):
        # A boundary, not a strict inequality: the documented minimum has to be
        # usable or the policy is a lie the user discovers by being rejected.
        exact = "x" * sessions.MIN_PASSWORD_CHARS
        self.assertTrue(sessions.verify_password(exact, sessions.hash_password(exact)))


# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------

class EmailValidationTests(unittest.TestCase):
    """The shape check is loose on purpose; these pin exactly how loose."""

    def test_it_normalises(self):
        # user_id is the PRIMARY KEY of the credentials table and the userId on
        # every record in the other two stores, so Alice@example.com and
        # alice@example.com being different accounts is the case-collision bug,
        # not a feature.
        self.assertEqual(sessions.validate_email("  Alice@Example.COM "),
                         "alice@example.com")

    def test_unusable_shapes_are_refused_with_a_reason(self):
        cases = {
            "": "required",
            "   ": "required",
            "nosign": "does not look like",
            "a@@b.com": "does not look like",
            "@example.com": "does not look like",
            "alice@": "does not look like",
            "a b@example.com": "cannot contain spaces",
            "alice@exa mple.com": "cannot contain spaces",
            "alice@localhost": "no domain",
        }
        for value, fragment in cases.items():
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    sessions.validate_email(value)
                self.assertIn(fragment, str(caught.exception))

    def test_oversized_is_refused(self):
        with self.assertRaises(ValueError):
            sessions.validate_email("x" * 300 + "@example.com")
        with self.assertRaises(ValueError):
            sessions.validate_email("x" * 70 + "@example.com")

    def test_a_consecutive_dot_in_the_domain_is_accepted(self):
        # Deliberate. RFC 5322 pedantry rejects valid addresses far more often
        # than it catches typos, and the only authority on whether an address
        # receives mail is a message to it, which this app does not send. Pinned
        # so that tightening this later is a conscious change.
        self.assertEqual(sessions.validate_email("a@b..com"), "a@b..com")

    def test_normalise_account_id_does_not_check_the_shape(self):
        # Two functions because they answer different questions: what a vault key
        # is (always the lowercased address) versus whether the address is usable
        # as one. create_credentials validates, so the loose one is never the only
        # gate in front of a write.
        self.assertEqual(sessions.normalise_account_id(" Bob@Example.com "),
                         "bob@example.com")
        with self.assertRaises(ValueError):
            sessions.normalise_account_id("   ")


# ---------------------------------------------------------------------------
# The credential store
# ---------------------------------------------------------------------------

class AccountStoreTests(StoreCase):
    GOOD = "correct horse battery staple"

    def test_an_account_is_created_and_can_log_in(self):
        record = sessions.create_credentials("Alice@Example.com", self.GOOD)
        self.assertEqual(record["user_id"], "alice@example.com")
        self.assertTrue(sessions.verify_account_password("alice@example.com", self.GOOD))
        self.assertFalse(sessions.verify_account_password("alice@example.com", "wrong password"))

    def test_registering_twice_refuses_and_does_not_reset_the_password(self):
        # The failure mode a "create" endpoint must not have: an upsert here
        # would silently repoint an existing account at a password the second
        # caller just chose.
        sessions.create_credentials("alice@example.com", self.GOOD)
        with self.assertRaises(ValueError) as caught:
            sessions.create_credentials("alice@example.com", "a different password")
        self.assertIn("already exists", str(caught.exception))
        self.assertTrue(sessions.verify_account_password("alice@example.com", self.GOOD))
        self.assertFalse(sessions.verify_account_password("alice@example.com",
                                                           "a different password"))

    def test_a_short_password_creates_nothing(self):
        with self.assertRaises(ValueError):
            sessions.create_credentials("alice@example.com", "short")
        self.assertIsNone(sessions.get_credentials("alice@example.com"))
        self.assertFalse(sessions.user_id_taken("alice@example.com"))

    def test_user_id_taken_spans_the_google_identity_table(self):
        # A Google account's vault is named by its email, so the same address can
        # already be spoken for by an identity link. Registering a password over
        # it would hand a second, independent way into someone else's vault.
        sessions.link_google_identity("sub-1", "alice@example.com", email="alice@example.com")
        self.assertTrue(sessions.user_id_taken("alice@example.com"))
        with self.assertRaises(ValueError):
            sessions.create_credentials("alice@example.com", self.GOOD)

    def test_a_disabled_account_cannot_log_in_but_keeps_its_key(self):
        # Disabling rather than deleting, so the address cannot be registered
        # again and come back pointing at a vault whose records are still there.
        sessions.create_credentials("alice@example.com", self.GOOD)
        self.assertTrue(sessions.disable_credentials("alice@example.com"))
        self.assertFalse(sessions.verify_account_password("alice@example.com", self.GOOD))
        self.assertTrue(sessions.user_id_taken("alice@example.com"))
        with self.assertRaises(ValueError):
            sessions.create_credentials("alice@example.com", self.GOOD)

    def test_disabling_twice_is_false_the_second_time(self):
        sessions.create_credentials("alice@example.com", self.GOOD)
        self.assertTrue(sessions.disable_credentials("alice@example.com"))
        self.assertFalse(sessions.disable_credentials("alice@example.com"))

    def test_a_password_can_be_rotated(self):
        sessions.create_credentials("alice@example.com", self.GOOD)
        self.assertTrue(sessions.set_password("alice@example.com", "a brand new password"))
        self.assertFalse(sessions.verify_account_password("alice@example.com", self.GOOD))
        self.assertTrue(sessions.verify_account_password("alice@example.com",
                                                         "a brand new password"))

    def test_rotating_an_unknown_or_disabled_account_is_false(self):
        self.assertFalse(sessions.set_password("nobody@example.com", "a brand new password"))
        sessions.create_credentials("alice@example.com", self.GOOD)
        sessions.disable_credentials("alice@example.com")
        self.assertFalse(sessions.set_password("alice@example.com", "a brand new password"))

    def test_the_listing_never_carries_the_hash(self):
        sessions.create_credentials("alice@example.com", self.GOOD)
        listing = sessions.list_credentials()
        self.assertEqual([row["user_id"] for row in listing], ["alice@example.com"])
        self.assertNotIn("password_hash", listing[0])
        self.assertNotIn(self.GOOD, repr(listing))

    def test_the_plaintext_is_never_on_disk(self):
        sessions.create_credentials("alice@example.com", self.GOOD)
        self.assertNotIn(self.GOOD.encode(), self.db_bytes())

    def test_the_hash_is_stored_verbatim_in_its_row(self):
        # The counterpart to the test above: the redaction is in
        # list_credentials, not in how the row is written, and the suite should
        # say which of the two it is relying on.
        sessions.create_credentials("alice@example.com", self.GOOD)
        record = sessions.get_credentials("alice@example.com")
        self.assertTrue(record["password_hash"].startswith("scrypt$"))
        self.assertNotIn(self.GOOD, record["password_hash"])

    def test_an_unknown_account_has_no_row(self):
        self.assertIsNone(sessions.get_credentials("nobody@example.com"))
        self.assertIsNone(sessions.get_credentials(""))
        self.assertFalse(sessions.verify_account_password("nobody@example.com", self.GOOD))


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------

class RegistrationFlagTests(StoreCase):
    def setUp(self):
        super().setUp()
        self._saved_flag = sessions.REGISTRATION_ENABLED
        self._saved_client = sessions.get_oauth_client(sessions.GOOGLE_PROVIDER)

    def tearDown(self):
        sessions.REGISTRATION_ENABLED = self._saved_flag
        if self._saved_client is None:
            sessions.delete_oauth_client(sessions.GOOGLE_PROVIDER)
        else:
            sessions.save_oauth_client(sessions.GOOGLE_PROVIDER,
                                       self._saved_client["client_id"],
                                       self._saved_client.get("client_secret") or "")
        super().tearDown()

    def test_the_flag_is_off_by_default(self):
        # Read once at import from an unset variable. This is the reason the
        # endpoints are not a thing an operator inherits by upgrading.
        self.assertFalse(sessions.REGISTRATION_ENABLED)

    def test_nothing_is_enabled_while_the_flag_is_off(self):
        sessions.REGISTRATION_ENABLED = False
        sessions.save_oauth_client(sessions.GOOGLE_PROVIDER,
                                   "123-abc.apps.googleusercontent.com")
        self.assertFalse(sessions.registration_enabled("email"))
        self.assertFalse(sessions.registration_enabled(sessions.GOOGLE_PROVIDER))

    def test_email_needs_only_the_flag(self):
        sessions.REGISTRATION_ENABLED = True
        self.assertTrue(sessions.registration_enabled("email"))

    def test_google_needs_a_client_id_as_well(self):
        # "Enabled but not configured" is a 404-shaped state that reads as a
        # broken feature, so it is not offered: a token cannot be checked for
        # audience without a client id.
        sessions.REGISTRATION_ENABLED = True
        sessions.delete_oauth_client(sessions.GOOGLE_PROVIDER)
        self.assertFalse(sessions.registration_enabled(sessions.GOOGLE_PROVIDER))
        sessions.save_oauth_client(sessions.GOOGLE_PROVIDER,
                                   "123-abc.apps.googleusercontent.com")
        self.assertTrue(sessions.registration_enabled(sessions.GOOGLE_PROVIDER))

    def test_a_blank_client_id_counts_as_unconfigured(self):
        # Verifying against an empty audience is the exact mistake the audience
        # check exists to stop, reached from the other direction.
        sessions.REGISTRATION_ENABLED = True
        conn = sessions._connect()
        try:
            conn.execute(
                "INSERT INTO oauth_clients (provider, client_id, client_secret,"
                " created_at, updated_at) VALUES (?, ?, '', 1.0, 1.0)",
                (sessions.GOOGLE_PROVIDER, "   "),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(sessions.registration_enabled(sessions.GOOGLE_PROVIDER))

    def test_an_unknown_method_is_refused_rather_than_assumed_on(self):
        sessions.REGISTRATION_ENABLED = True
        with self.assertRaises(ValueError):
            sessions.registration_enabled("github")


# ---------------------------------------------------------------------------
# The throttle
# ---------------------------------------------------------------------------

class SignupThrottleTests(unittest.TestCase):
    def setUp(self):
        sessions._ATTEMPTS.clear()

    def tearDown(self):
        sessions._ATTEMPTS.clear()

    def test_the_limit_is_reached_and_then_refused(self):
        allowed = [sessions.allow_registration_attempt("1.2.3.4", now=float(n))
                   for n in range(sessions.REGISTRATION_ATTEMPT_LIMIT + 3)]
        limit = sessions.REGISTRATION_ATTEMPT_LIMIT
        self.assertEqual(allowed[:limit], [True] * limit)
        self.assertEqual(allowed[limit:], [False] * 3)

    def test_the_window_slides_rather_than_resetting(self):
        # A client cannot get twice the quota by straddling the boundary.
        window = sessions.REGISTRATION_ATTEMPT_WINDOW
        for n in range(sessions.REGISTRATION_ATTEMPT_LIMIT):
            self.assertTrue(sessions.allow_registration_attempt("1.2.3.4", now=float(n)))
        self.assertFalse(sessions.allow_registration_attempt("1.2.3.4", now=window - 1))
        # The oldest attempt is now outside the window, so room frees up.
        self.assertTrue(sessions.allow_registration_attempt("1.2.3.4", now=window + 1))

    def test_one_client_exhausting_its_quota_does_not_affect_another(self):
        limit = sessions.REGISTRATION_ATTEMPT_LIMIT
        for n in range(limit + 1):
            sessions.allow_registration_attempt("1.2.3.4", now=float(n))
        self.assertFalse(sessions.allow_registration_attempt("1.2.3.4", now=1.0))
        self.assertTrue(sessions.allow_registration_attempt("5.6.7.8", now=1.0))

    def test_idle_clients_are_pruned(self):
        # Keyed by attacker-chosen strings, so the dict must not grow with
        # history or this is a slow memory leak with a remote trigger.
        window = sessions.REGISTRATION_ATTEMPT_WINDOW
        sessions.allow_registration_attempt("1.1.1.1", now=0.0)
        sessions.allow_registration_attempt("2.2.2.2", now=0.0)
        self.assertEqual(len(sessions._ATTEMPTS), 2)
        sessions.allow_registration_attempt("3.3.3.3", now=window * 2)
        self.assertNotIn("1.1.1.1", sessions._ATTEMPTS)
        self.assertNotIn("2.2.2.2", sessions._ATTEMPTS)
        self.assertIn("3.3.3.3", sessions._ATTEMPTS)

    def test_a_missing_client_key_is_still_bucketed(self):
        # Not a free pass: an unidentifiable caller shares one quota rather than
        # getting an unlimited one.
        for _ in range(sessions.REGISTRATION_ATTEMPT_LIMIT + 1):
            last = sessions.allow_registration_attempt("", now=1.0)
        self.assertFalse(last)
        self.assertFalse(sessions.allow_registration_attempt(None, now=1.0))


# ---------------------------------------------------------------------------
# gui.py: the guard, the routes, and the call sites
# ---------------------------------------------------------------------------

class _Request:
    def __init__(self, headers=None, client_host="10.0.0.1"):
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.client = types.SimpleNamespace(host=client_host)
        self.session = {}


class _HTTPError(Exception):
    """Stand-in for fastapi's HTTPException, carrying what the tests assert on."""

    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class GuardCase(StoreCase):
    """Lifts the registration code out of gui.py and drives it.

    gui.py cannot be imported on this box (no fastapi), so the functions are
    lifted with ast.get_source_segment and exec'd against stubs. Only the
    *collaborators* are stubbed — the credential hashing, the throttling and the
    vault-key normalisation are the real ones, because those are the decisions.
    """

    GOOD = "correct horse battery staple"

    def run_route(self, route, request, body):
        """Call a lifted async route. Not a detail: an unawaited coroutine runs
        nothing, so assertRaises sees no exception and every guard in these
        classes would report green against routes that were never entered."""
        return asyncio.run(route(request, body))

    def setUp(self):
        super().setUp()
        # The throttle is module-global and the default _Request client host is
        # the same for every test in a class, so without this the tenth test in a
        # class silently starts getting 429s from the ninth one's quota.
        sessions._ATTEMPTS.clear()
        self.verified = []       # (user_id, password) pairs the stub saw
        self.registered = []     # accounts the route asked to create
        self.enabled = True
        self.limit_calls = 0
        self.google_client = {"provider": "google", "client_id": "123-abc.apps.googleusercontent.com"}
        self.verified_subjects = set()
        self.identity = {"subject": "sub-1", "email": "Alice@Example.com",
                         "name": "Alice", "audience": "123-abc"}
        self.identity_error = None

        namespace = {
            "os": os, "json": json, "base64": base64, "subprocess": subprocess,
            "HTTPException": _HTTPError,
            "vault_sessions": sessions,
            "GOOGLE_PROVIDER": sessions.GOOGLE_PROVIDER,
            "MAX_PASSWORD_CHARS": sessions.MAX_PASSWORD_CHARS,
            "MIN_PASSWORD_CHARS": sessions.MIN_PASSWORD_CHARS,
            "validate_email": sessions.validate_email,
            "create_credentials": self._create_credentials,
            "user_id_taken": sessions.user_id_taken,
            "verify_account_password": sessions.verify_account_password,
            "registration_enabled": self._registration_enabled,
            "allow_registration_attempt": self._allow_attempt,
            "get_oauth_client": lambda provider: self.google_client,
            "resolve_google_identity": self._resolve_google_identity,
            "_verify_htpasswd": self._verify_htpasswd,
            "google_auth": types.SimpleNamespace(
                google_identity=self._google_identity,
                GoogleTokenError=type("GoogleTokenError", (Exception,), {}),
            ),
            # The module, not a Logger. The lifted code calls
            # logging.getLogger("memory-vault") at each log site, so handing it
            # an instance fails with 'Logger' object has no attribute
            # 'getLogger' -- which reads like a defect in the module under test.
            "logging": logging,
        }
        self.namespace = namespace
        self.verify_account = _lift("mem-mcp/gui.py", "_verify_account", namespace)
        self.require_registration = _lift("mem-mcp/gui.py", "_require_registration", namespace)
        self.signup_client = _lift("mem-mcp/gui.py", "_signup_client", namespace)
        self.registration_config = _lift("mem-mcp/gui.py", "registration_config", namespace)
        self.register = _lift("mem-mcp/gui.py", "api_register", namespace)
        self.register_google = _lift("mem-mcp/gui.py", "api_register_google", namespace)

    # -- stubs ---------------------------------------------------------------
    def _create_credentials(self, account, password):
        self.registered.append((account, password))
        return sessions.create_credentials(account, password)

    def _registration_enabled(self, method="email"):
        if not self.enabled:
            return False
        if str(method).lower() == "email":
            return True
        return bool(self.google_client and self.google_client.get("client_id"))

    def _allow_attempt(self, client):
        self.limit_calls += 1
        return sessions.allow_registration_attempt(client)

    def _resolve_google_identity(self, subject, provider=sessions.GOOGLE_PROVIDER, touch=True):
        if subject in self.verified_subjects:
            return {"subject": subject, "user_id": "linked@example.com"}
        return None

    def _verify_htpasswd(self, username, password):
        self.verified.append((username, password))
        return False

    def _google_identity(self, token, client_id):
        if self.identity_error:
            raise self.identity_error
        return dict(self.identity)

    # -- helpers -------------------------------------------------------------
    def body(self, **kwargs):
        return types.SimpleNamespace(**kwargs)


class VerifyAccountTests(GuardCase):
    def test_a_registered_account_resolves_to_its_lowercased_key(self):
        sessions.create_credentials("alice@example.com", self.GOOD)
        self.assertEqual(self.verify_account("Alice@Example.com", self.GOOD),
                         "alice@example.com")

    def test_an_htpasswd_user_keeps_the_name_exactly_as_typed(self):
        # htpasswd usernames are case-sensitive: `Freddie` and `freddie` are two
        # different users there and must stay so, which is why the registered
        # lookup is tried twice and the htpasswd fallback only once, with the raw
        # string.
        def accepts_freddie(username, password):
            self.verified.append((username, password))
            return username == "Freddie" and password == "htpasswd-secret"

        self.namespace["_verify_htpasswd"] = accepts_freddie
        lifted = _lift("mem-mcp/gui.py", "_verify_account", self.namespace)
        self.assertEqual(lifted("Freddie", "htpasswd-secret"), "Freddie")
        self.assertIsNone(lifted("freddie", "htpasswd-secret"))

    def test_the_credential_store_is_checked_before_htpasswd(self):
        # An address could name an account in both stores. The row this app owns
        # wins, because it is the one whose password can be rotated by the app --
        # the reverse would make a password change made in the UI a silent no-op.
        sessions.create_credentials("alice@example.com", self.GOOD)
        self.assertEqual(self.verify_account("alice@example.com", self.GOOD),
                         "alice@example.com")
        self.assertEqual(self.verified, [], msg=(
            "htpasswd must not be consulted once the credential store has "
            "answered, or a rotated password would be shadowed by the file"))

    def test_an_unregistered_name_falls_through_to_htpasswd(self):
        self.assertEqual(self.verify_account("freddie", "htpasswd-secret"), None)
        self.assertEqual(self.verified, [("freddie", "htpasswd-secret")])

    def test_nothing_is_checked_without_a_password(self):
        # Not even htpasswd: an empty password is a credential, and asking the
        # file about one costs a subprocess on an unauthenticated route.
        self.assertIsNone(self.verify_account("alice@example.com", ""))
        self.assertIsNone(self.verify_account("", self.GOOD))
        self.assertEqual(self.verified, [])

    def test_a_broken_credential_store_does_not_lock_the_operator_out(self):
        # A corrupt table must not become a login outage: the htpasswd fallback is
        # what the operator uses to get in and fix it.
        def explodes(user_id, password):
            raise RuntimeError("database is locked")

        def accepts(username, password):
            self.verified.append((username, password))
            return True

        self.namespace["verify_account_password"] = explodes
        self.namespace["_verify_htpasswd"] = accepts
        lifted = _lift("mem-mcp/gui.py", "_verify_account", self.namespace)
        self.assertEqual(lifted("freddie", "secret"), "freddie")


class RegistrationGuardTests(GuardCase):
    def test_the_throttle_runs_before_the_flag_check(self):
        # The only version that measures what an attacker is doing — and the only
        # version that does not remove the rate limit from a still-mounted route
        # the moment an operator turns registration off.
        for _ in range(sessions.REGISTRATION_ATTEMPT_LIMIT):
            self.require_registration("email", _Request())
        self.assertEqual(self.limit_calls, sessions.REGISTRATION_ATTEMPT_LIMIT)
        with self.assertRaises(_HTTPError) as caught:
            self.require_registration("email", _Request())
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(self.limit_calls, sessions.REGISTRATION_ATTEMPT_LIMIT + 1)

    def test_the_flag_is_checked_even_when_the_throttle_allows(self):
        self.enabled = False
        with self.assertRaises(_HTTPError) as caught:
            self.require_registration("email", _Request())
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.limit_calls, 1)

    def test_a_disabled_route_is_a_404_and_not_a_403(self):
        # 403 says "this exists and you may not"; a disabled signup route should
        # be indistinguishable from one that was never mounted, so that turning
        # the flag off does not advertise that registration is a thing this app
        # has.
        self.enabled = False
        with self.assertRaises(_HTTPError) as caught:
            self.require_registration(sessions.GOOGLE_PROVIDER, _Request())
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail, "Not found")

    def test_the_client_key_is_the_rightmost_forwarded_hop(self):
        # nginx is configured with $proxy_add_x_forwarded_for, which *appends* the
        # peer it saw. So the rightmost entry is the last hop the client did not
        # choose, and a forged `X-Forwarded-For: 1.2.3.4` arrives as
        # `1.2.3.4, <real peer>`. The leftmost entry is whatever the client sent,
        # and trusting it would hand every attacker an unlimited quota.
        request = _Request({"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 9.10.11.12"})
        self.assertEqual(self.signup_client(request), "9.10.11.12")

    def test_the_socket_peer_is_the_fallback(self):
        # Behind the proxy this is nginx itself, so every signup shares one
        # bucket rather than getting a free pass — the right way for that
        # fallback to fail.
        self.assertEqual(self.signup_client(_Request()), "10.0.0.1")

    def test_registration_config_reports_the_two_methods_separately(self):
        # One "registration is on" flag would have to render a Google form that
        # 404s on a deployment with no client id configured.
        config = self.registration_config()
        self.assertEqual(sorted(config), ["email", "google", "password_hint"])
        self.assertTrue(config["email"])
        self.assertTrue(config["google"])
        self.google_client = None
        self.assertTrue(self.registration_config()["email"])
        self.assertFalse(self.registration_config()["google"])


class EmailRegistrationRouteTests(GuardCase):
    def drive(self, request=None, **body):
        request = request or _Request()
        kwargs = {"email": "", "password": ""}
        kwargs.update(body)
        result = self.run_route(self.register, request, self.body(**kwargs))
        return request, result

    def test_a_new_account_is_created_and_the_user_is_signed_in(self):
        request, result = self.drive(email="Alice@Example.com", password=self.GOOD)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["user"], "alice@example.com")
        self.assertEqual(request.session["user"], "alice@example.com")
        self.assertTrue(sessions.verify_account_password("alice@example.com", self.GOOD))

    def test_the_session_is_cleared_before_the_user_is_written(self):
        # The middleware reads a cleared session as "delete the old row and mint
        # a new id", which is what defeats session fixation. An id planted in the
        # browser before signup must not be the logged-in id after it.
        request = _Request()
        request.session["stale"] = "planted"
        self.drive.__self__  # keep the linter honest about the unused name
        self.run_route(self.register, request,
                        self.body(email="alice@example.com", password=self.GOOD))
        self.assertNotIn("stale", request.session)

    def test_an_existing_account_is_a_409(self):
        sessions.create_credentials("alice@example.com", self.GOOD)
        with self.assertRaises(_HTTPError) as caught:
            self.drive(email="alice@example.com", password="a different password")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("already exists", caught.exception.detail)
        self.assertTrue(sessions.verify_account_password("alice@example.com", self.GOOD))

    def test_an_address_that_belongs_to_a_google_vault_is_a_409(self):
        sessions.link_google_identity("sub-1", "alice@example.com")
        with self.assertRaises(_HTTPError) as caught:
            self.drive(email="alice@example.com", password=self.GOOD)
        self.assertEqual(caught.exception.status_code, 409)

    def test_a_bad_address_is_a_400_and_creates_nothing(self):
        with self.assertRaises(_HTTPError) as caught:
            self.drive(email="not-an-address", password=self.GOOD)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.registered, [])

    def test_a_short_password_is_a_400(self):
        with self.assertRaises(_HTTPError) as caught:
            self.drive(email="alice@example.com", password="short")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn(str(sessions.MIN_PASSWORD_CHARS), caught.exception.detail)

    def test_the_route_is_closed_when_the_flag_is_off(self):
        self.enabled = False
        with self.assertRaises(_HTTPError) as caught:
            self.drive(email="alice@example.com", password=self.GOOD)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.registered, [])


class GoogleRegistrationRouteTests(GuardCase):
    def real_link(self):
        """What the actual store holds, since the route links for real."""
        return sessions.resolve_google_identity("sub-1", touch=False)

    def drive(self, token="a-google-id-token", **kwargs):
        return self.run_route(self.register_google, _Request(),
                               self.body(token=token, **kwargs))

    def test_a_valid_token_creates_the_account_named_by_its_email(self):
        request = _Request()
        result = self.run_route(self.register_google, request, self.body(token="a-google-id-token"))
        self.assertEqual(result["user"], "alice@example.com")
        self.assertEqual(request.session["user"], "alice@example.com")
        # The link went through the real store, so that is what is asserted --
        # not a stub's call log, which would pass against a route that called
        # nothing at all.
        link = sessions.resolve_google_identity("sub-1", touch=False)
        self.assertIsNotNone(link)
        self.assertEqual(link["user_id"], "alice@example.com")
        self.assertEqual(link["email"], "Alice@Example.com")

    def test_an_already_linked_account_is_a_409_rather_than_a_shortcut(self):
        # /api/auth/google is the route for signing in. Silently logging someone
        # in on the register endpoint would make "register" and "sign in" the
        # same request.
        self.verified_subjects.add("sub-1")
        with self.assertRaises(_HTTPError) as caught:
            self.drive()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Sign in instead", caught.exception.detail)
        self.assertIsNone(self.real_link())

    def test_an_unusable_token_is_a_400_with_the_reason(self):
        self.identity_error = self.namespace["google_auth"].GoogleTokenError(
            "that Google token is not usable (ExpiredSignatureError)")
        with self.assertRaises(_HTTPError) as caught:
            self.drive()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("ExpiredSignatureError", caught.exception.detail)

    def test_a_token_with_no_email_cannot_name_an_account(self):
        # Inventing a key from the subject would produce a vault whose owner
        # cannot type their own login.
        self.identity = {"subject": "sub-1", "email": "", "name": "", "audience": "x"}
        with self.assertRaises(_HTTPError) as caught:
            self.drive()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("no email", caught.exception.detail)
        self.assertIsNone(self.real_link())

    def test_an_address_that_already_has_a_vault_is_refused_not_linked(self):
        # Linking on the strength of a matching email string would give a second
        # Google identity write access to someone else's vault.
        sessions.create_credentials("alice@example.com", self.GOOD)
        with self.assertRaises(_HTTPError) as caught:
            self.drive()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNone(self.real_link())

    def test_the_route_is_closed_when_google_is_not_configured(self):
        self.enabled = False
        with self.assertRaises(_HTTPError) as caught:
            self.drive()
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIsNone(self.real_link())

    def test_a_missing_client_id_is_a_404_even_if_the_flag_is_on(self):
        # Belt to registration_config's braces: the config function hides the
        # form, and this makes the endpoint agree.
        self.google_client = None
        with self.assertRaises(_HTTPError) as caught:
            self.drive()
        self.assertEqual(caught.exception.status_code, 404)


# ---------------------------------------------------------------------------
# The landing page
# ---------------------------------------------------------------------------

class LandingPageTests(unittest.TestCase):
    LANDING = os.path.join(HERE, "templates", "landing.html")

    def setUp(self):
        with open(self.LANDING, "rb") as handle:
            self.raw = handle.read()
        self.source = self.raw.decode("utf-8")

    def body(self):
        """The markup between the sign-in card and the authenticated branch.

        Scoped, because assertIn over a whole template passes on any page that
        happens to contain the word — and the sign-up script mentions the form ids
        even when the forms are not rendered.
        """
        start = self.source.index('<div class="card">\n      <h2>🔐 Sign In</h2>')
        end = self.source.index("{% else %}", start)
        return self.source[start:end]

    def test_the_signup_section_exists(self):
        self.assertIn("Create an account", self.body())

    def test_each_method_is_gated_on_its_own_flag(self):
        body = self.body()
        self.assertIn("{% if SIGNUP_EMAIL %}", body)
        self.assertIn("{% if SIGNUP_GOOGLE %}", body)
        # And the whole section is gated too, so a deployment with registration
        # off renders nothing at all rather than an empty card.
        self.assertIn("{% if SIGNUP_EMAIL or SIGNUP_GOOGLE %}", body)

    def test_the_password_minimum_matches_the_server(self):
        # The cross-file contract. minlength is a client-side hint; sessions.py's
        # MIN_PASSWORD_CHARS is the policy. If they drift the browser either
        # blocks a password the server would accept, or lets someone submit one
        # the server will refuse with a message they were never warned about.
        self.assertIn('minlength="%d"' % sessions.MIN_PASSWORD_CHARS, self.source)
        self.assertIn("{{SIGNUP_PASSWORD_MIN}}", self.source)

    def test_the_google_box_says_id_token_not_access_token(self):
        body = self.body()
        self.assertIn("ID token", body)
        self.assertIn("ya29.", body)
        self.assertIn("no redirect", body.lower())

    def test_it_says_the_address_is_not_verified(self):
        # Anyone can register an address they do not own. A user who does not
        # know that will eventually rely on "you can reset it with that address".
        self.assertIn("not verified", self.body())

    def test_failures_surface_the_servers_reason(self):
        # A bare alert() is what the login form used to do, and it discards the
        # only text that distinguishes "already registered" from "no domain" from
        # "that token carries no email address".
        script = self.source[self.source.index("<script>"):]
        self.assertNotIn("alert(", script)
        self.assertIn("await apiFail(res)", script)

    def test_the_error_line_is_rendered_for_each_form(self):
        body = self.body()
        for error_id in ("loginError", "registerError", "registerGoogleError"):
            self.assertIn('id="%s"' % error_id, body)

    def test_the_forms_post_to_the_three_endpoints(self):
        for endpoint in ("/api/auth/login", "/api/auth/register",
                         "/api/auth/register/google"):
            self.assertIn("'" + endpoint + "'", self.source)

    def test_only_bare_identifiers_appear_between_the_braces(self):
        # Jinja lexes every {{ in the file, so an expression in one — even in a
        # comment — is a TemplateSyntaxError that takes the whole page down.
        import re
        found = re.findall(r"\{\{(.*?)\}\}", self.source, re.S)
        for expression in found:
            self.assertRegex(
                expression.strip(), r"^[A-Za-z_][A-Za-z0-9_]*$",
                msg="{{ %s }} is an expression, not a bare identifier" % expression)


class CallSiteTests(unittest.TestCase):
    """A test of a helper is not a test of its call site.

    Four properties here are all "looks right and is wrong": both login paths
    reaching the same store, the guard being the only thing that decides a
    signup is allowed, the signup routes living under the one unauthenticated
    prefix, and the throttle actually being reached.
    """

    def test_both_login_paths_go_through_verify_account(self):
        for function in ("api_login", "_check_session_auth"):
            source = _function_source("mem-mcp/gui.py", function)
            self.assertIn("_verify_account(", source,
                          msg="%s must accept both stores, not just htpasswd" % function)
            self.assertNotIn("_verify_htpasswd(", source,
                             msg="%s picking a store itself is how the two paths "
                                 "end up disagreeing about who can log in" % function)

    def test_the_login_response_carries_the_canonical_key(self):
        source = _function_source("mem-mcp/gui.py", "api_login")
        self.assertIn('request.session["user"] = account', source)
        self.assertNotIn('request.session["user"] = body.username', source,
                         msg="storing what was typed signs a registered user "
                             "into a vault whose key is the lowercased address")

    def test_both_signup_routes_are_behind_the_guard(self):
        for function in ("api_register", "api_register_google"):
            source = _function_source("mem-mcp/gui.py", function)
            self.assertIn("_require_registration(", source,
                          msg="%s is unauthenticated by construction, so the flag "
                              "and the throttle are the only thing in front of it"
                                  % function)

    def test_the_throttle_is_inside_the_guard_and_not_after_the_flag_check(self):
        # Inside, so that turning registration off does not remove the rate limit
        # from a route that is still mounted; and before the flag check, or the
        # count only ever sees successful attempts.
        source = _function_source("mem-mcp/gui.py", "_require_registration")
        self.assertLess(source.index("allow_registration_attempt("),
                        source.index("registration_enabled("))

    def test_the_signup_routes_live_under_the_open_prefix(self):
        # /api/auth is the only prefix auth_guard lets through unauthenticated,
        # which is what makes a signup endpoint possible at all — and what makes
        # it the weakest surface in the app.
        source = _read("mem-mcp/gui.py")
        for route in ('@web_app.post("/api/auth/register"',
                      '@web_app.post("/api/auth/register/google"'):
            self.assertIn(route, source)

    def test_no_signup_route_is_mounted_anywhere_else(self):
        source = _read("mem-mcp/gui.py")
        self.assertEqual(source.count('@web_app.post("/api/auth/register"'), 1)
        self.assertEqual(source.count('@web_app.post("/api/auth/register/google"'), 1)

    def test_the_mcp_gate_knows_nothing_about_registration(self):
        # "GUI only" is not a restriction imposed for tidiness — it falls out of
        # the design. McpAuthGuard accepts an access key or a Google token and
        # nothing else, and must not acquire a signup-shaped hole.
        source = _function_source("mem-mcp/gui.py", "McpAuthGuard")
        self.assertNotIn("register", source)
        self.assertNotIn("create_credentials", source)


if __name__ == "__main__":
    unittest.main()
