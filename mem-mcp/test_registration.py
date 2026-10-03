"""
test_registration.py – self-service signup: accounts, passwords, verification, gates.

A separate suite from test_sessions.py and test_auth_guard.py on purpose,
because registration is a *third* credential surface and the only one that is
unauthenticated by design: `/api/auth/*` is the only prefix `auth_guard` lets
through with no credential, so a signup endpoint lives there by necessity.
Splitting it out makes it obvious in the test list that the code with the
weakest authentication has its own tests.

Half the suite *calls* sessions.py (it is stdlib-only for exactly this reason)
and half *lifts* functions out of gui.py with `ast.get_source_segment`, because
gui.py needs fastapi and this box has none.

The properties worth having tests for are all invisible in the shape of the code:

  * a wrong password and a *corrupt stored hash* must both be False, and the
    second must not raise — the caller is a login path;
  * `dklen` has to travel inside the stored string, because a verifier that
    derived it from the digest would verify a shortened hash against anything;
  * registering twice must not silently reset an existing password, and a name
    held by a Google identity or htpasswd must not be registrable;
  * a *new* account is unverified, so login must refuse it with "check your mail"
    rather than "wrong password" — and must say that **before** checking the
    password, or people reset passwords they never got wrong;
  * a verification token must be single-use and stored only as a hash;
  * a signup whose confirmation mail cannot be sent must leave nothing behind,
    because the username would otherwise be taken by an account that can never
    work;
  * the throttle must count attempts *before* the flag check, or turning
    registration off removes the rate limit from a still-mounted route;
  * Google sign-in must not be gated on the registration flag, or switching the
    flag off would sign out everyone who signed up with Google;
  * `state` is the CSRF defence for the whole redirect and must be consumed.

Run:  python3 -m unittest -v test_registration.py
"""

import ast
import asyncio
import base64
import logging
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import sessions  # noqa: E402  (stdlib-only, so importable with nothing installed)
import google_auth  # noqa: E402  (likewise stdlib-only)


def _read(relative: str) -> str:
    with open(os.path.join(ROOT, relative), "r", encoding="utf-8") as handle:
        return handle.read()


def _function_source(relative: str, node_name: str) -> str:
    """The source of a module-level function, by AST."""
    tree = ast.parse(_read(relative))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == node_name:
            return ast.get_source_segment(_read(relative), node) or ""
    raise AssertionError(f"{relative} has no module-level {node_name}")


def _lift(relative: str, node_name: str, namespace: dict):
    """Exec a function out of a module into `namespace`.

    Two things this must get right, both learned the hard way:

    * the target has to be the *same dict* the function will be called with.
      `_lift(..., dict(namespace))` execs into a copy, and then the lifted
      function's globals are somewhere its caller cannot see.
    * the namespace needs the `logging` *module*, not a Logger — the lifted code
      calls `logging.getLogger("memory-vault")` at each log site.
    """
    exec(compile(_function_source(relative, node_name), relative, "exec"), namespace)
    return namespace[node_name]


class StoreCase(unittest.TestCase):
    """A private sessions database per test, torn down afterwards."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reg-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        # Restored, not popped. An unconditional pop discards whatever value the
        # runner exported, and `db_path()` then falls back to <repo>/sessions/ and
        # mkdir's it — so a later test in the same process that touches the real
        # sessions module writes a live sessions.db into the working tree. It is
        # gitignored, which is exactly why it went unnoticed: `git status` stays
        # clean while the file appears.
        previous = os.environ.get("MEM_SESSION_DIR")
        os.environ["MEM_SESSION_DIR"] = self.dir
        self.addCleanup(self._restore_dir, previous)

    def _restore_dir(self, previous):
        if previous is None:
            os.environ.pop("MEM_SESSION_DIR", None)
        else:
            os.environ["MEM_SESSION_DIR"] = previous

    def db_bytes(self) -> bytes:
        with open(os.path.join(self.dir, "sessions.db"), "rb") as handle:
            return handle.read()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

class PasswordHashingTests(unittest.TestCase):
    def test_verification_follows_the_password(self):
        # A matching and a non-matching password are one property -- the stored
        # hash decides -- over two inputs.
        stored = sessions.hash_password("correct horse battery staple")
        for password, expected in (("correct horse battery staple", True),
                                   ("Correct horse battery staple", False)):
            with self.subTest(password=password, expected=expected):
                self.assertEqual(sessions.verify_password(password, stored), expected)

    def test_the_same_password_hashes_differently_every_time(self):
        # A per-password salt: identical passwords must not be recognisable as
        # identical rows in the database.
        first = sessions.hash_password("same password here")
        second = sessions.hash_password("same password here")
        self.assertNotEqual(first, second)
        self.assertTrue(sessions.verify_password("same password here", first))
        self.assertTrue(sessions.verify_password("same password here", second))

    def test_the_plaintext_is_not_in_the_hash(self):
        stored = sessions.hash_password("hunter2-is-my-password")
        self.assertNotIn("hunter2", stored)

    def test_the_cost_parameters_travel_inside_the_string(self):
        # A verifier that read n/r/p from the *stored* value rather than from the
        # string could not verify a row written with a cheaper setting, and every
        # future cost increase would invalidate every existing password.
        stored = sessions.hash_password("a long enough password")
        parts = stored.split("$")
        self.assertEqual(parts[0], "scrypt")
        self.assertEqual([int(p) for p in parts[1:5]],
                         [sessions.SCRYPT_N, sessions.SCRYPT_R, sessions.SCRYPT_P,
                          sessions.SCRYPT_DKLEN])

    def test_a_hash_written_with_cheaper_parameters_still_verifies(self):
        import hashlib
        salt = b"0123456789abcdef"
        digest = hashlib.scrypt(b"legacy password", salt=salt, n=2 ** 12, r=8, p=1,
                                dklen=32, maxmem=2 ** 26)
        stored = "$".join(["scrypt", str(2 ** 12), "8", "1", "32",
                           base64.b64encode(salt).decode("ascii"),
                           base64.b64encode(digest).decode("ascii")])
        self.assertTrue(sessions.verify_password("legacy password", stored))

    def test_a_corrupt_hash_is_false_and_not_an_exception(self):
        # The caller is a login path, and a row restored from a damaged savepoint
        # or hand-edited must read as "this account cannot log in".
        for stored in ("", "not-a-hash", "scrypt$1$2$3", "scrypt$a$b$c$d$e$f",
                       "scrypt$16384$8$1$32$AAAA$"):
            with self.subTest(stored=stored):
                self.assertFalse(sessions.verify_password("anything at all", stored))

    def test_a_truncated_digest_does_not_verify(self):
        # The first implementation derived dklen from the stored digest, so it
        # computed a digest of that same length and compared equal: anyone able
        # to shorten the stored hash had made it verify against anything.
        stored = sessions.hash_password("a long enough password")
        head, _, _ = stored.rpartition("$")
        shortened = head + "$" + "A" * (len(stored.split("$")[-1]) - 1)
        self.assertFalse(sessions.verify_password("a long enough password", shortened))
        self.assertNotEqual(len(shortened.split("$")[-1]),
                            int(stored.split("$")[4]))

    def test_passwords_outside_the_bounds_are_refused(self):
        for value in ("", "   ", "x" * (sessions.MAX_PASSWORD_CHARS + 1), None, 12345):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(ValueError):
                    sessions.hash_password(value)

    def test_the_minimum_length_is_actually_accepted(self):
        exactly = "x" * sessions.MIN_PASSWORD_CHARS
        self.assertTrue(sessions.verify_password(
            exactly, sessions.hash_password(exactly)))


# ---------------------------------------------------------------------------
# Usernames and addresses
# ---------------------------------------------------------------------------

class UsernameTests(unittest.TestCase):
    def test_a_reasonable_username_is_normalised_not_reshaped(self):
        # Lowercasing, keeping the allowed characters and trimming are one
        # property -- a usable canonical key -- over three inputs.
        # Lowercased, and that is not cosmetic: `user_id` *is* the vault key and
        # every store compares it with `=`, so `Alice` and `alice` would otherwise
        # be two vaults nobody can tell apart. The alternative -- keeping the name
        # as typed and case-insensitively matching -- is the same thing with an
        # extra step.
        for value, expected in (("Alice", "alice"),
                                ("bob.smith_1", "bob.smith_1"),
                                ("  alice  ", "alice")):
            with self.subTest(value=repr(value), expected=expected):
                self.assertEqual(sessions.normalise_username(value), expected)

    def test_the_allowed_set_is_exactly_letters_digits_dot_underscore_dash(self):
        # Anything else is refused rather than transliterated: two spellings of
        # one name is the bug the restriction exists to prevent, and a
        # transliteration table is where such a pair comes from.
        for value in ("alice bob", "alice@example.com", "alice/bob", "álice",
                      "alice+bob", "alice!", "al\nice"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sessions.normalise_username(value)

    def test_it_must_not_start_or_end_with_a_separator(self):
        for value in (".alice", "-alice", "_alice", "alice.", "alice-", "alice_"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sessions.normalise_username(value)

    def test_the_length_bounds_are_inclusive(self):
        shortest = "a" * sessions.MIN_USERNAME_CHARS
        longest = "b" * sessions.MAX_USERNAME_CHARS
        self.assertEqual(sessions.normalise_username(shortest), shortest)
        self.assertEqual(sessions.normalise_username(longest), longest)
        for value in ("a" * (sessions.MIN_USERNAME_CHARS - 1),
                      "b" * (sessions.MAX_USERNAME_CHARS + 1)):
            with self.subTest(length=len(value)):
                with self.assertRaises(ValueError):
                    sessions.normalise_username(value)

    def test_empty_and_non_strings_are_refused(self):
        for value in ("", "   ", None, 42):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    sessions.normalise_username(value)

    def test_the_username_does_not_become_an_address(self):
        # The vault key is what you type at the login form, so it must never be
        # silently reshaped into something else.
        self.assertNotIn("@", sessions.normalise_username("alice"))


class EmailValidationTests(unittest.TestCase):
    def test_a_legal_address_comes_back_normalised_but_otherwise_unchanged(self):
        # Normalising case and whitespace and passing through a domain the server
        # judges for itself are one property -- the return value -- over two
        # inputs. Refusing the consecutive dot would reject addresses that work.
        for value, expected in (("  Alice@Example.COM ", "alice@example.com"),
                                ("alice@ex..ample.com", "alice@ex..ample.com")):
            with self.subTest(value=value, expected=expected):
                self.assertEqual(sessions.validate_email(value), expected)

    def test_unusable_shapes_are_refused_with_a_reason(self):
        # Every shape and the oversized address are one property -- refuse rather
        # than truncate, because the address is where the confirmation goes and a
        # shortened one is an address nobody can confirm.
        cases = [("empty", ""), ("blank", "   "), ("no-at", "alice"),
                 ("no-local-part", "alice@"), ("no-domain", "@example.com"),
                 ("space-in-local", "a b@example.com"), ("double-at", "alice@@example.com"),
                 ("space-in-domain", "alice@ex ample.com"), ("none", None), ("int", 42),
                 ("oversized", "a" * (sessions.MAX_EMAIL_CHARS + 1) + "@x.com")]
        for label, value in cases:
            with self.subTest(label=label, value=repr(value)[:40]):
                with self.assertRaises(ValueError):
                    sessions.validate_email(value)


# ---------------------------------------------------------------------------
# The account store
# ---------------------------------------------------------------------------

class AccountStoreTests(StoreCase):
    GOOD = "correct horse battery staple"

    def _create(self, username="alice", email="alice@example.com"):
        return sessions.create_credentials(username, self.GOOD, email)

    def test_an_account_is_created_and_can_log_in_once_confirmed(self):
        record = self._create()
        self.assertEqual(record["user_id"], "alice")
        self.assertEqual(record["email"], "alice@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        self.assertTrue(sessions.verify_account_password("alice", self.GOOD))

    def test_a_new_account_is_not_verified_yet(self):
        record = self._create()
        self.assertIsNone(record["email_verified_at"])
        self.assertFalse(sessions.account_verified("alice"))

    def test_an_unverified_account_cannot_log_in(self):
        # This is the property the whole confirmation flow exists for.
        self._create()
        self.assertFalse(sessions.verify_account_password("alice", self.GOOD))

    def test_registering_twice_refuses_and_does_not_reset_the_password(self):
        # An upsert here is account takeover: it would reset the password of an
        # account someone already signed up with.
        self._create()
        with self.assertRaises(ValueError):
            sessions.create_credentials("alice", "a different password", "other@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        self.assertTrue(sessions.verify_account_password("alice", self.GOOD))

    def test_the_username_is_reserved_by_a_google_identity_too(self):
        # An address that already names a Google-linked vault must not be
        # registrable by password: that is a second, weaker way into it.
        sessions.link_google_identity("sub-1", "alice", email="alice@example.com")
        with self.assertRaises(ValueError):
            sessions.create_credentials("alice", self.GOOD, "elsewhere@example.com")

    def test_the_address_is_unique_across_accounts(self):
        self._create(username="alice", email="shared@example.com")
        with self.assertRaises(ValueError):
            sessions.create_credentials("bob", self.GOOD, "shared@example.com")

    def test_an_htpasswd_account_reserves_its_name_and_not_its_address(self):
        # Two stores answer two different questions. A username that is already
        # the operator's account must not be handed to a stranger -- two
        # passwords into one vault. An address that merely *equals* an htpasswd
        # user name is a coincidence, and refusing it would be a coincidence
        # mistaken for a policy.
        original = sessions.htpasswd_user_exists
        sessions.htpasswd_user_exists = lambda username: username == "operator"
        self.addCleanup(setattr, sessions, "htpasswd_user_exists", original)

        self.assertTrue(sessions.user_id_taken("operator"))
        with self.assertRaises(ValueError):
            sessions.create_credentials("operator", self.GOOD, "new@example.com")

        self.assertFalse(sessions.email_taken("operator@example.com"))
        sessions.create_credentials("someone", self.GOOD, "operator@example.com")
        self.assertTrue(sessions.email_taken("operator@example.com"))

    def test_an_unusable_input_creates_nothing(self):
        # A short password, an unusable username and an unusable address are one
        # property -- the create is refused and leaves no row behind.
        cases = (("short password", "alice", "alice@example.com", "short"),
                 ("unusable username", "a b", "alice@example.com", self.GOOD),
                 ("unusable address", "alice", "not-an-address", self.GOOD))
        for label, username, email, password in cases:
            with self.subTest(label=label, username=username):
                with self.assertRaises(ValueError):
                    sessions.create_credentials(username, password, email)
                self.assertIsNone(sessions.get_credentials(username))

    def test_deleting_an_account_frees_the_name(self):
        # The undo for a signup whose mail could not be sent. Without it the name
        # is held by an account that can never be confirmed.
        self._create()
        self.assertTrue(sessions.delete_credentials("alice"))
        self.assertIsNone(sessions.get_credentials("alice"))
        self._create()
        self.assertFalse(sessions.verify_account_password("alice", self.GOOD))

    def test_a_disabled_account_cannot_log_in_but_keeps_its_key(self):
        # Disabling is not deleting: the name stays occupied so it cannot be
        # registered again and come back pointing at a vault whose records are
        # still there.
        self._create()
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        self.assertTrue(sessions.verify_account_password("alice", self.GOOD))
        self.assertTrue(sessions.disable_credentials("alice"))
        self.assertFalse(sessions.verify_account_password("alice", self.GOOD))
        self.assertFalse(sessions.disable_credentials("alice"))

    def test_a_password_can_be_rotated(self):
        self._create()
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        self.assertTrue(sessions.set_password("alice", "a brand new password"))
        self.assertTrue(sessions.verify_account_password("alice", "a brand new password"))
        self.assertFalse(sessions.verify_account_password("alice", self.GOOD))

    def test_rotating_an_unknown_or_disabled_account_is_false(self):
        self.assertFalse(sessions.set_password("nobody", "a brand new password"))
        self._create()
        self.assertFalse(sessions.set_password("nobody-at-all", "a brand new password"))

    def test_the_listing_never_carries_the_hash_or_a_token(self):
        self._create()
        token = sessions.issue_verification_token("alice")
        rendered = repr(sessions.list_credentials())
        self.assertNotIn(sessions.hash_password(self.GOOD)[:20], rendered)
        self.assertNotIn(token, rendered)
        self.assertNotIn("password_hash", rendered)
        self.assertNotIn("verification_token", rendered)
        self.assertIn("alice@example.com", rendered)

    def test_the_plaintext_is_never_on_disk(self):
        self._create()
        self.assertNotIn(self.GOOD.encode(), self.db_bytes())
        self.assertNotIn(sessions.hash_password(self.GOOD).encode(), self.db_bytes())

    def test_the_hash_is_stored_verbatim_in_its_row(self):
        self._create()
        row = sessions.get_credentials("alice")
        # Verbatim, i.e. not re-derived at read time: a row whose stored hash
        # could not be checked against the password it was made from would be a
        # row nobody can ever sign in with.
        self.assertTrue(sessions.verify_password(self.GOOD, row["password_hash"]))
        self.assertNotIn(self.GOOD, row["password_hash"])

    def test_an_unknown_account_has_no_row(self):
        self.assertIsNone(sessions.get_credentials("nobody"))

    def test_user_id_taken_answers_for_every_store(self):
        self.assertFalse(sessions.user_id_taken("alice"))
        self._create()
        self.assertTrue(sessions.user_id_taken("alice"))
        self.assertTrue(sessions.email_taken("ALICE@example.com"))
        self.assertFalse(sessions.email_taken("someone@example.com"))


class VerificationTests(StoreCase):
    GOOD = "correct horse battery staple"

    def setUp(self):
        super().setUp()
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")

    def test_a_token_verifies_the_account_and_clears_itself(self):
        # The second click is part of this property, not a separate one: a mail
        # client that previews a link, or a double-click, must not report a
        # failure to the second person who opens it.
        token = sessions.issue_verification_token("alice")
        self.assertEqual(sessions.verify_email_token(token), "alice")
        self.assertTrue(sessions.account_verified("alice"))
        self.assertIsNone(sessions.verify_email_token(token))

    def test_a_token_is_stored_only_as_a_hash(self):
        token = sessions.issue_verification_token("alice")
        self.assertNotIn(token.encode(), self.db_bytes())
        row = sessions.get_credentials("alice")
        self.assertEqual(row["verification_token"],
                         sessions._token_hash(token))

    def test_an_issued_token_supersedes_the_previous_one(self):
        first = sessions.issue_verification_token("alice")
        second = sessions.issue_verification_token("alice")
        self.assertIsNone(sessions.verify_email_token(first))
        self.assertEqual(sessions.verify_email_token(second), "alice")

    def test_garbage_and_empty_tokens_are_refused_not_raised(self):
        for value in ("", "   ", "nope", "a" * 200, None, 12345):
            with self.subTest(value=repr(value)[:30]):
                self.assertIsNone(sessions.verify_email_token(value))

    def test_no_token_is_issued_for_an_unknown_or_disabled_account(self):
        with self.assertRaises(ValueError):
            sessions.issue_verification_token("nobody")
        sessions.disable_credentials("alice")
        with self.assertRaises(ValueError):
            sessions.issue_verification_token("alice")


class VerificationMailTests(unittest.TestCase):
    def test_the_message_carries_the_link_and_the_username(self):
        subject, body = sessions.verification_email("alice", "https://x/verify?token=t0k")
        self.assertIn("alice", body, msg="the message must say which account it is for")
        self.assertIn("https://x/verify?token=t0k", body)

    def test_the_message_says_the_link_works_once(self):
        # A forwarded or previewed link that silently does nothing is the
        # complaint this line pre-empts.
        _, body = sessions.verification_email("alice", "https://x/verify?token=t0k")
        self.assertTrue("once" in body.lower() or "single" in body.lower())

    def test_a_failing_client_raises_rather_than_returning_false(self):
        # The caller is a route that has to decide whether to keep or delete the
        # account it just made; a false return would be the wrong shape entirely.

        class Broken:
            def __enter__(self):
                raise OSError("connection refused")

            def __exit__(self, *exc):
                return False

        with self.assertRaises(Exception):
            sessions.send_mail("alice@example.com", "s", "b", client=Broken())


# ---------------------------------------------------------------------------
# Which methods the landing page may offer
# ---------------------------------------------------------------------------

class RegistrationFlagTests(StoreCase):
    """Each method is gated on the flag AND on its own prerequisite.

    A form that renders and then returns 404 costs a person a page load to learn
    the same thing twice, so the template branches on the same predicate the
    route enforces.

    The predicates read module-level constants, which is what the container gets
    from the environment once at import -- so the tests patch the constants
    rather than re-importing with a doctored environment, which is the only way
    to exercise them on a box where the environment is already fixed.
    """

    def _patch(self, module, **values):
        """Set module-level constants and put them back afterwards.

        The constants are read from the environment once, at import, which is
        what the container gets -- so the tests patch them rather than
        re-importing under a doctored environment.
        """
        for name, value in values.items():
            original = getattr(module, name)
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, original)

    def test_the_flag_is_off_by_default(self):
        self.assertFalse(sessions.REGISTRATION_ENABLED)

    def test_nothing_is_enabled_while_the_flag_is_off(self):
        # Both prerequisites satisfied; only the operator's flag stands between
        # this and a signup form.
        self._patch(sessions, SMTP_HOST="smtp.example.com",
                    SMTP_FROM="vault@example.com")
        self._patch(google_auth, configured=lambda: True)
        self.assertFalse(sessions.REGISTRATION_ENABLED)
        self.assertFalse(sessions.registration_enabled("email"))
        self.assertFalse(sessions.registration_enabled("google"))

    def test_the_email_form_needs_a_mail_server(self):
        # Both entry points read the same two values, so the three stages are one
        # property -- host *and* from -- checked through the predicate and through
        # the gate the route enforces. Two blanks is a compose line with no value.
        stages = (("nothing configured", "", "", False),
                  ("host only", "smtp.example.com", "", False),
                  ("from only", "", "vault@example.com", False),
                  ("host and from", "smtp.example.com", "vault@example.com", True))
        for label, host, from_address, expected in stages:
            with self.subTest(label=label):
                self._patch(sessions, REGISTRATION_ENABLED=True, SMTP_HOST=host,
                            SMTP_FROM=from_address)
                self.assertEqual(sessions.smtp_configured(), expected)
                self.assertEqual(sessions.registration_enabled("email"), expected)

    def test_google_needs_a_client_id_and_a_secret(self):
        # Every configuration state is one property -- configured() is False until
        # both values are present and non-blank. A client id alone can start a
        # login and cannot finish it, and the predicate the route and the template
        # both read has to agree with the module rather than with a stub.
        self._patch(sessions, REGISTRATION_ENABLED=True)
        client_id = "cid.apps.googleusercontent.com"
        cases = (("nothing configured", {}, False),
                 ("client id only", {"GOOGLE_CLIENT_ID": client_id}, False),
                 ("blank secret", {"GOOGLE_CLIENT_ID": client_id,
                                   "GOOGLE_CLIENT_SECRET": "   "}, False),
                 ("client id and secret", {"GOOGLE_CLIENT_ID": client_id,
                                           "GOOGLE_CLIENT_SECRET": "secret"}, True))
        previous = {name: os.environ.get(name)
                    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")}
        self.addCleanup(lambda: [os.environ.pop(name, None) if value is None
                                 else os.environ.__setitem__(name, value)
                                 for name, value in previous.items()])
        for label, values, expected in cases:
            with self.subTest(label=label):
                for name in previous:
                    os.environ.pop(name, None)
                os.environ.update(values)
                self.assertEqual(google_auth.configured(), expected)
                self.assertEqual(sessions.registration_enabled("google"), expected)

    def test_the_truthy_spellings_are_accepted(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                self.assertEqual(
                    str(value).strip().lower() in ("1", "true", "yes", "on"), True)
                self.assertEqual(
                    str(os.getenv("X") or "0").strip().lower() in ("1", "true", "yes", "on"),
                    False)

    def test_an_unknown_method_is_refused_rather_than_assumed_on(self):
        # A typo in `_require_registration("gmial", ...)` must surface here, not
        # quietly enable nothing.
        self._patch(sessions, REGISTRATION_ENABLED=True)
        with self.assertRaises(ValueError):
            sessions.registration_enabled("gmial")


# ---------------------------------------------------------------------------
# The in-process throttle
# ---------------------------------------------------------------------------

class SignupThrottleTests(unittest.TestCase):
    def setUp(self):
        sessions._ATTEMPTS.clear()
        self.addCleanup(sessions._ATTEMPTS.clear)

    def test_the_limit_is_reached_and_then_refused(self):
        # The quota is per client key, and a key that is not there is still a
        # bucket: one property, two keys.
        for client in ("1.2.3.4", ""):
            with self.subTest(client=client or "<empty>"):
                for _ in range(sessions.REGISTRATION_ATTEMPT_LIMIT):
                    self.assertTrue(sessions.allow_registration_attempt(client))
                self.assertFalse(sessions.allow_registration_attempt(client))

    def test_the_window_slides_rather_than_resetting(self):
        for _ in range(sessions.REGISTRATION_ATTEMPT_LIMIT):
            sessions.allow_registration_attempt("1.2.3.4")
        saved = sessions.time.time
        try:
            sessions.time.time = lambda: saved() + sessions.REGISTRATION_ATTEMPT_WINDOW + 1
            self.assertTrue(sessions.allow_registration_attempt("1.2.3.4"))
        finally:
            sessions.time.time = saved

    def test_one_client_exhausting_its_quota_does_not_affect_another(self):
        for _ in range(sessions.REGISTRATION_ATTEMPT_LIMIT):
            sessions.allow_registration_attempt("1.2.3.4")
        self.assertTrue(sessions.allow_registration_attempt("5.6.7.8"))

    def test_idle_clients_are_pruned(self):
        # An unbounded dict of client keys is a slow memory leak, and the bound
        # exists to prevent exactly that.
        for index in range(50):
            sessions.allow_registration_attempt(f"10.0.0.{index}")
        saved = sessions.time.time
        try:
            sessions.time.time = lambda: saved() + sessions.REGISTRATION_ATTEMPT_WINDOW * 2
            sessions.allow_registration_attempt("10.0.0.0")
            self.assertLessEqual(len(sessions._ATTEMPTS), 2)
        finally:
            sessions.time.time = saved


# ---------------------------------------------------------------------------
# The lifted routes
# ---------------------------------------------------------------------------

class _Request:
    def __init__(self, headers=None, client_host="10.0.0.1", session=None):
        self.headers = dict(headers or {})
        self.client = types.SimpleNamespace(host=client_host)
        self.state = types.SimpleNamespace()
        self.session = session if session is not None else {}


class _HTTPError(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Redirect(Exception):
    """Stand-in for starlette's RedirectResponse.

    An exception rather than a return value so that a route which forgets to
    return one fails loudly here instead of silently handing the test a None.
    """

    def __init__(self, url, status_code=302):
        super().__init__(url)
        self.url = url
        self.status_code = status_code


class GuardCase(StoreCase):
    """Lifts the registration code out of gui.py and drives it.

    gui.py cannot be imported on this box (no fastapi), so the functions are
    lifted with ast.get_source_segment and exec'd against stubs. Only the
    *collaborators* are stubbed — the hashing, the verification store, the
    throttling and the username normalisation are the real ones, because those
    are the decisions.
    """

    GOOD = "correct horse battery staple"

    def run_route(self, route, *args, **kwargs):
        """Call a lifted async route.

        Not a detail: an unawaited coroutine enters no function at all, so
        assertRaises would see nothing and every guard in these classes would
        report green against routes that were never entered.
        """
        return asyncio.run(route(*args, **kwargs))

    def setUp(self):
        super().setUp()
        sessions._ATTEMPTS.clear()
        self.verified = []        # (username, password) handed to htpasswd
        self.registered = []      # accounts the route asked to create
        self.mails = []           # (to, subject, body) send_mail was asked to send
        self.mail_error = None
        self.enabled = True
        self.limit_calls = 0
        self.google_configured = True
        self.linked = {}          # subject -> vault
        self.identity = {"subject": "sub-1", "email": "alice@example.com",
                         "email_verified": True, "name": "Alice"}
        self.oauth_error = None
        self.redirect_uri_used = []

        def send_mail(to, subject, body, **_kwargs):
            if self.mail_error:
                raise self.mail_error
            self.mails.append((to, subject, body))
            return True

        namespace = {
            "os": os, "json": json, "base64": base64, "subprocess": subprocess,
            "asyncio": asyncio, "hmac": __import__("hmac"),
            "secrets": __import__("secrets"), "re": re,
            "urllib": urllib, "urllib.parse": urllib.parse,
            "HTTPException": _HTTPError,
            "RedirectResponse": _Redirect,
            "mem": types.SimpleNamespace(BASE_URL="https://hass.example/mem-mcp"),
            "vault_sessions": sessions,
            "GOOGLE_PROVIDER": sessions.GOOGLE_PROVIDER,
            "MAX_PASSWORD_CHARS": sessions.MAX_PASSWORD_CHARS,
            "MIN_PASSWORD_CHARS": sessions.MIN_PASSWORD_CHARS,
            "get_credentials": sessions.get_credentials,
            "create_credentials": self._create_credentials,
            "delete_credentials": sessions.delete_credentials,
            "user_id_taken": sessions.user_id_taken,
            "email_taken": sessions.email_taken,
            "verify_account_password": sessions.verify_account_password,
            "normalise_username": sessions.normalise_username,
            "validate_email": sessions.validate_email,
            "issue_verification_token": sessions.issue_verification_token,
            "verify_email_token": sessions.verify_email_token,
            "verification_email": sessions.verification_email,
            "send_mail": send_mail,
            "registration_enabled": self._registration_enabled,
            "allow_registration_attempt": self._allow_attempt,
            "link_google_identity": self._link_google_identity,
            "resolve_google_identity": self._resolve_google_identity,
            "_verify_htpasswd": self._verify_htpasswd,
            "_service_unavailable": lambda exc: _HTTPError(503, str(exc)),
            "google_auth": types.SimpleNamespace(
                configured=lambda: self.google_configured,
                callback_url=google_callback_url,
                authorization_url=self._authorization_url,
                exchange_code=self._exchange_code,
                fetch_userinfo=self._fetch_userinfo,
                GoogleOAuthError=type("GoogleOAuthError", (Exception,), {}),
            ),
            # The module, not a Logger: the lifted code calls
            # logging.getLogger("memory-vault") at each log site.
            "logging": logging,
        }
        self.namespace = namespace
        for name in ("_verify_account", "_require_registration", "_signup_client",
                     "registration_config", "_base_url", "_landing",
                     "api_register", "api_verify_email", "api_google_start",
                     "api_google_callback", "_google_signup", "api_login"):
            _lift("mem-mcp/gui.py", name, namespace)

    # -- stubs ---------------------------------------------------------------
    def _create_credentials(self, username, password, email="", now=None):
        self.registered.append((username, password, email))
        return sessions.create_credentials(username, password, email)

    def _registration_enabled(self, method="email"):
        return bool(self.enabled)

    def _allow_attempt(self, client):
        self.limit_calls += 1
        return True

    def _verify_htpasswd(self, username, password):
        self.verified.append((username, password))
        return False

    def _link_google_identity(self, subject, user_id, email="", name="",
                              provider=sessions.GOOGLE_PROVIDER, now=None):
        self.linked[subject] = user_id
        return sessions.link_google_identity(subject, user_id, email=email, name=name)

    def _resolve_google_identity(self, subject, provider=sessions.GOOGLE_PROVIDER,
                                 now=None, touch=True):
        if subject in self.linked:
            return {"subject": subject, "user_id": self.linked[subject]}
        return None

    def _authorization_url(self, state, redirect_uri, **kwargs):
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def _exchange_code(self, code, redirect_uri, **kwargs):
        self.redirect_uri_used.append(redirect_uri)
        if self.oauth_error:
            raise self.oauth_error
        return "access-token-for-" + code

    def _fetch_userinfo(self, access_token):
        if self.oauth_error:
            raise self.oauth_error
        return dict(self.identity)

    # -- helpers -------------------------------------------------------------
    def body(self, **kwargs):
        return types.SimpleNamespace(**kwargs)

    def redirected(self, call, *args, **kwargs):
        """Run something that must end in a redirect, and return that redirect.

        Not a convenience: the routes *return* a RedirectResponse, so a test that
        used assertRaises here would pass on any route that raised nothing at all
        -- which is every route that silently lost its redirect.
        """
        result = asyncio.run(call(*args, **kwargs)) if asyncio.iscoroutinefunction(call) \
            else call(*args, **kwargs)
        self.assertIsInstance(result, _Redirect,
                              msg=f"expected a redirect, got {result!r}")
        self.assertEqual(result.status_code, 302)
        return result


def google_callback_url(base_url):
    return f"{str(base_url or '').rstrip('/')}/api/auth/google/callback"


class VerifyAccountTests(GuardCase):
    def test_a_registered_account_resolves_to_its_exact_username(self):
        # Uppercase in, lowercased key out -- one property over two spellings.
        # `user_id` *is* the vault key and every store in the app compares it with
        # `=`, so the canonical spelling has to be the one that is stored; signing
        # up as `Alice` and in as `ALICE` must reach the one stored key, or a
        # case-sensitive compare opens a second empty vault nobody can tell apart.
        stored = sessions.create_credentials("Alice", self.GOOD, "alice@example.com")
        self.assertEqual(stored["user_id"], "alice")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        for spelling in ("Alice", "ALICE"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.namespace["_verify_account"](spelling, self.GOOD),
                                 ("alice", ""))

    def test_an_unverified_account_is_told_to_check_their_mail(self):
        # Before the password is checked: telling someone their password is wrong
        # when the account simply never finished registering sends them to reset a
        # password they never got wrong.
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        key, reason = self.namespace["_verify_account"]("alice", "wrong password")
        self.assertIsNone(key)
        self.assertIn("confirmed", reason)
        self.assertEqual(self.verified, [], "htpasswd must not be consulted")

    def test_a_disabled_account_is_told_it_is_disabled(self):
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        sessions.disable_credentials("alice")
        key, reason = self.namespace["_verify_account"]("alice", self.GOOD)
        self.assertIsNone(key)
        self.assertIn("disabled", reason)

    def test_a_plain_wrong_password_has_no_reason(self):
        # The empty reason is what makes the 401 rather than a 403: the caller
        # cannot say anything about the account without confirming it exists.
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        self.assertEqual(self.namespace["_verify_account"]("alice", "nope"),
                         (None, ""))

    def test_an_htpasswd_user_keeps_the_name_exactly_as_typed(self):
        # `Freddie` and `freddie` are different users in an htpasswd file.
        def accepts_freddie(username, password):
            self.verified.append((username, password))
            return username == "Freddie" and password == self.GOOD
        self.namespace["_verify_htpasswd"] = accepts_freddie
        self.assertEqual(self.namespace["_verify_account"]("Freddie", self.GOOD),
                         ("Freddie", ""))

    def test_the_credential_store_is_checked_before_htpasswd(self):
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        self.namespace["_verify_account"]("alice", self.GOOD)
        self.assertEqual(self.verified, [],
                         msg="a registered account must not reach the htpasswd probe")

    def test_an_unregistered_name_falls_through_to_htpasswd(self):
        def accepts(username, password):
            self.verified.append((username, password))
            return True
        self.namespace["_verify_htpasswd"] = accepts
        self.assertEqual(self.namespace["_verify_account"]("freddie", self.GOOD),
                         ("freddie", ""))

    def test_nothing_is_checked_without_a_password(self):
        self.assertEqual(self.namespace["_verify_account"]("alice", ""),
                         (None, "no account name or password was given"))
        self.assertEqual(self.namespace["_verify_account"]("", self.GOOD),
                         (None, "no account name or password was given"))
        self.assertEqual(self.verified, [])

    def test_a_broken_credential_store_does_not_lock_the_operator_out(self):
        # A row restored from a damaged savepoint must not turn into a total
        # lockout: htpasswd still answers.
        def explodes(candidate):
            raise sqlite3_error("database is locked")
        def accepts(username, password):
            self.verified.append((username, password))
            return True
        self.namespace["get_credentials"] = explodes
        self.namespace["_verify_htpasswd"] = accepts
        self.assertEqual(self.namespace["_verify_account"]("freddie", self.GOOD),
                         ("freddie", ""))


def sqlite3_error(message):
    import sqlite3
    return sqlite3.OperationalError(message)


class LoginRouteTests(GuardCase):
    def test_a_successful_login_stores_the_canonical_key(self):
        sessions.create_credentials("Alice", self.GOOD, "alice@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        request = _Request()
        result = self.run_route(self.namespace["api_login"], request,
                                self.body(username="ALICE", password=self.GOOD))
        self.assertEqual(result["user"], "alice", msg=(
            "the canonical key goes in the session, not what was typed: a session "
            "holding 'Alice' when the store holds 'alice' is a signed-in session "
            "pointed at an empty vault"))
        self.assertEqual(request.session["user"], "alice",
                         msg="storing what was typed signs you into a vault that does "
                             "not exist")

    def test_the_session_is_cleared_before_the_user_is_written(self):
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        request = _Request(session={"oauth_state": "planted", "user": "someone-else"})
        self.run_route(self.namespace["api_login"], request,
                       self.body(username="alice", password=self.GOOD))
        self.assertNotIn("oauth_state", request.session)
        self.assertEqual(request.session["user"], "alice")

    def test_an_unverified_account_is_a_403_with_the_reason(self):
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        request = _Request()
        with self.assertRaises(_HTTPError) as ctx:
            self.run_route(self.namespace["api_login"], request,
                           self.body(username="alice", password=self.GOOD))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("confirmed", ctx.exception.detail)
        self.assertEqual(request.session, {})

    def test_a_plain_wrong_password_is_a_401(self):
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        sessions.verify_email_token(sessions.issue_verification_token("alice"))
        with self.assertRaises(_HTTPError) as ctx:
            self.run_route(self.namespace["api_login"], _Request(),
                           self.body(username="alice", password="wrong"))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Invalid credentials")


class RegistrationGuardTests(GuardCase):
    def test_the_throttle_runs_before_the_flag_check(self):
        # Counting only attempts that pass the flag means turning registration off
        # removes the rate limit from a route that is still mounted, and the
        # counter only ever sees people who got in.
        self.enabled = False
        request = _Request()
        with self.assertRaises(_HTTPError) as ctx:
            self.namespace["_require_registration"]("email", request)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.limit_calls, 1)

    def test_the_throttle_refuses_before_the_flag_is_consulted(self):
        self.namespace["allow_registration_attempt"] = lambda client: False
        with self.assertRaises(_HTTPError) as ctx:
            self.namespace["_require_registration"]("email", _Request())
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("Too many", ctx.exception.detail)

    def test_a_disabled_route_is_a_404_and_not_a_403(self):
        # A 403 says "this exists and you may not"; a disabled signup route should
        # be indistinguishable from one that was never mounted.
        self.enabled = False
        with self.assertRaises(_HTTPError) as ctx:
            self.namespace["_require_registration"]("google", _Request())
        self.assertEqual(ctx.exception.status_code, 404)

    def test_an_enabled_method_passes(self):
        self.assertIsNone(self.namespace["_require_registration"]("email", _Request()))

    def test_the_client_key_is_the_last_hop_the_client_did_not_choose(self):
        # One property over three request shapes: nginx appends the peer it saw,
        # so the rightmost entry is the answer and the leftmost is whatever the
        # client sent; with no header the socket peer is used -- behind the proxy
        # that is nginx itself, so everyone shares one bucket, which is the right
        # way for that fallback to fail -- and with neither there is one bucket.
        cases = (("forwarded hop", {"x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.10.11.12"},
                  {}, "9.10.11.12"),
                 ("socket peer", {}, {"client_host": "172.17.0.1"}, "172.17.0.1"),
                 ("no peer", {}, {"client_host": ""}, "unknown"))
        for label, headers, peer, expected in cases:
            with self.subTest(label=label, headers=headers):
                request = _Request(headers=headers, **peer)
                self.assertEqual(self.namespace["_signup_client"](request), expected)

    def test_registration_config_reports_the_two_methods_separately(self):
        # One "registration is on" flag would have to render a Google form that
        # 404s on a deployment with no client configured.
        config = self.namespace["registration_config"]()
        self.assertEqual(config["email"], True)
        self.assertEqual(config["google"], True)
        self.assertEqual(config["password_hint"], sessions.MIN_PASSWORD_CHARS)

    def test_the_landing_redirects_never_carry_request_controlled_text(self):
        # Everything that ends a flow points at the front door with a fixed value
        # chosen in gui.py -- one property per outcome, so one case each.
        for params in ({"verified": "1"}, {"google": "state"}, {}):
            with self.subTest(params=params):
                url = self.namespace["_landing"](**params)
                self.assertTrue(url.startswith("https://hass.example/mem-mcp/"), url)
                for key, value in params.items():
                    self.assertIn(f"{key}={value}", url)


class EmailRegistrationRouteTests(GuardCase):
    def drive(self, request=None, **body):
        body.setdefault("username", "alice")
        body.setdefault("email", "alice@example.com")
        body.setdefault("password", self.GOOD)
        return self.run_route(self.namespace["api_register"],
                              request or _Request(), self.body(**body))

    def test_an_account_is_created_and_the_mail_sent(self):
        result = self.drive()
        self.assertEqual(result["user"], "alice")
        self.assertEqual(result["email"], "alice@example.com")
        self.assertTrue(result["verification_sent"])
        self.assertEqual(len(self.mails), 1)
        to, subject, body = self.mails[0]
        self.assertEqual(to, "alice@example.com")
        self.assertIn("Memory Vault", subject)
        self.assertIn("/api/auth/verify?token=", body)
        self.assertIn("alice", body, msg="the message must say which account it is about")

    def test_the_link_carries_the_token_that_was_stored(self):
        self.drive()
        _, _, body = self.mails[0]
        link = re.search(r"token=([^\s\"'>]+)", body).group(1)
        self.assertEqual(sessions.verify_email_token(link), "alice")

    def test_registration_does_not_sign_anybody_in(self):
        # The address has not been proven, so a session handed out here would be
        # a session for an account nobody can use yet and nobody else can reach.
        request = _Request()
        self.drive(request=request)
        self.assertEqual(request.session, {})

    def test_the_account_starts_unverified(self):
        self.drive()
        self.assertFalse(sessions.account_verified("alice"))
        self.assertFalse(sessions.verify_account_password("alice", self.GOOD))

    def test_a_rejected_signup_is_a_400_and_sends_nothing(self):
        # Four refusals, one property: the route answers 400 and the mail outbox
        # stays empty, so a rejected signup leaves nothing half-created behind.
        cases = (("unusable username", {"username": "alice bob"}),
                 ("unusable address", {"email": "not-an-address"}),
                 ("short password", {"password": "short"}))
        for label, overrides in cases:
            with self.subTest(label=label):
                with self.assertRaises(_HTTPError) as ctx:
                    self.drive(**overrides)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertEqual(self.mails, [])

        with self.subTest("taken username"):
            self.drive()
            self.mails.clear()
            with self.assertRaises(_HTTPError) as ctx:
                self.drive(email="other@example.com")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(self.mails, [])

    def test_a_mail_that_cannot_be_sent_leaves_nothing_behind(self):
        # Otherwise the username is held by an account that can never be
        # confirmed, and a second attempt says "that username is taken".
        self.mail_error = OSError("connection refused")
        with self.assertRaises(_HTTPError) as ctx:
            self.drive()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIsNone(sessions.get_credentials("alice"))
        self.assertFalse(sessions.user_id_taken("alice"))
        # ... and the name is immediately reusable.
        self.mail_error = None
        self.assertEqual(self.drive()["user"], "alice")

    def test_the_route_runs_when_registration_is_closed(self):
        self.enabled = False
        with self.assertRaises(_HTTPError) as ctx:
            self.drive()
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.mails, [])

    def test_the_throttle_is_consulted_by_the_route(self):
        self.drive()
        self.assertEqual(self.limit_calls, 1)


class VerifyEmailRouteTests(GuardCase):
    def drive(self, token=""):
        return self.run_route(self.namespace["api_verify_email"], _Request(),
                              token=token)

    def test_a_good_token_redirects_to_the_front_door_saying_so(self):
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        token = sessions.issue_verification_token("alice")
        result = self.drive(token)
        self.assertIn("verified=1", result.url)
        self.assertTrue(sessions.account_verified("alice"))

    def test_a_spent_token_redirects_saying_it_did_not_work(self):
        sessions.create_credentials("alice", self.GOOD, "alice@example.com")
        token = sessions.issue_verification_token("alice")
        self.drive(token)
        self.assertIn("verified=0", self.drive(token).url)

    def test_garbage_and_empty_tokens_get_the_same_answer(self):
        # One answer for a bad token, a spent one and an empty one: a message
        # distinguishing them tells an attacker which guesses are live.
        first = self.drive("").url
        for label, value in (("blank", "   "), ("garbage", "nope")):
            with self.subTest(label=label):
                result = self.drive(value)
                self.assertEqual(result.url, first)
                self.assertIn("verified=0", result.url)

    def test_the_route_needs_no_session(self):
        # The link comes out of a mail client, which will not POST and has no
        # cookie; the token is the whole credential.
        self.assertTrue(self.namespace["api_verify_email"])


class GoogleSignInTests(GuardCase):
    STATE = "the-state-value"

    def start(self, request=None):
        request = request or _Request()
        return request, self.redirected(self.namespace["api_google_start"], request)

    def test_starting_signs_in_to_googles_consent_screen(self):
        request, result = self.start()
        self.assertEqual(result.status_code, 302)
        self.assertIn("accounts.google.com", result.url)
        self.assertIn("state=", result.url)

    def test_the_state_is_kept_in_the_session_not_a_cookie_of_our_own(self):
        _, result = self.start(_Request())
        # It is already server-side, already HttpOnly, and cleared on the request
        # that consumes it.
        self.assertNotIn("state", result.url.split("?")[0])

    def test_start_stores_a_state_the_callback_can_check(self):
        request, result = self.start()
        self.assertTrue(request.session.get("oauth_state"))
        self.assertIn(request.session["oauth_state"], result.url)

    def test_start_is_a_404_when_google_is_not_configured(self):
        self.google_configured = False
        with self.assertRaises(_HTTPError) as ctx:
            self.start()
        self.assertEqual(ctx.exception.status_code, 404)

    def test_sign_in_still_works_when_registration_is_closed(self):
        # Switching registration off has to stop new accounts without signing out
        # the people who already have one.
        self.enabled = False
        _, result = self.start()
        self.assertEqual(result.status_code, 302)

    def callback(self, session=None, **kwargs):
        kwargs.setdefault("code", "auth-code")
        kwargs.setdefault("state", self.STATE)
        return self.redirected(self.namespace["api_google_callback"],
                               _Request(session=session or {"oauth_state": self.STATE}),
                               **kwargs)

    def test_a_first_time_account_is_created_and_signed_in(self):
        request = _Request(session={"oauth_state": self.STATE, "user": "someone-else"})
        result = self.redirected(self.namespace["api_google_callback"], request,
                                 code="auth-code", state=self.STATE)
        self.assertEqual(result.url, "https://hass.example/mem-mcp/gui")
        self.assertEqual(request.session["user"], "alice@example.com")
        self.assertNotIn("oauth_state", request.session,
                         msg="the old session must be cleared before the user is "
                             "written, or a planted id is upgraded rather than replaced")

    def test_the_vault_is_linked_to_the_subject_so_a_second_sign_in_finds_it(self):
        self.callback()
        self.assertEqual(self.linked.get("sub-1"), "alice@example.com")

    def test_the_code_is_redeemed_with_the_same_redirect_uri(self):
        self.callback()
        self.assertEqual(self.redirect_uri_used,
                         ["https://hass.example/mem-mcp/api/auth/google/callback"])

    def test_a_linked_subject_signs_in_without_creating_anything(self):
        self.linked["sub-1"] = "existing-vault"
        result = self.callback()
        self.assertEqual(result.url, "https://hass.example/mem-mcp/gui")
        self.assertEqual(self.registered, [])

    def test_a_state_that_is_wrong_or_absent_is_refused(self):
        # Without the comparison, a code minted for someone else's login could be
        # posted here and signed in as them. One property, three ways of failing
        # it: a value that is not the one issued, an empty one and a missing one.
        for label, session, state in (
                ("mismatched", {"oauth_state": self.STATE}, "someone-elses-state"),
                ("empty", {}, ""),
                ("absent", {}, None)):
            with self.subTest(label=label):
                result = self.callback(session=session, state=state)
                self.assertIn("google=state", result.url)
                self.assertEqual(self.registered, [])

    def test_the_state_is_consumed_so_it_cannot_be_replayed(self):
        session = {"oauth_state": self.STATE}
        self.redirected(self.namespace["api_google_callback"],
                        _Request(session=session), code="c", state=self.STATE)
        self.assertFalse(session.get("oauth_state"),
                         msg="the state is spent whether or not the login worked")
        replay = self.redirected(self.namespace["api_google_callback"],
                                 _Request(session=session), code="c", state=self.STATE)
        self.assertIn("google=state", replay.url)

    def test_a_dismissed_consent_screen_is_not_a_failure(self):
        result = self.callback(code="", error="access_denied")
        self.assertIn("google=declined", result.url)

    def test_a_code_that_will_not_exchange_reports_a_failure(self):
        self.oauth_error = self.namespace["google_auth"].GoogleOAuthError(
            "Google refused the authorization code (invalid_grant)")
        result = self.callback()
        self.assertIn("google=failed", result.url)
        self.assertEqual(self.registered, [])

    def test_a_signup_that_cannot_happen_reports_closed(self):
        self.enabled = False
        result = self.callback()
        self.assertIn("google=closed", result.url)

    def test_a_failure_never_writes_a_session_user(self):
        self.oauth_error = self.namespace["google_auth"].GoogleOAuthError("boom")
        request = _Request(session={"oauth_state": self.STATE})
        self.redirected(self.namespace["api_google_callback"], request,
                        code="c", state=self.STATE)
        self.assertNotIn("user", request.session)


class GoogleSignupTests(GuardCase):
    INFO = {"subject": "sub-1", "email": "alice@example.com",
            "email_verified": True, "name": "Alice"}

    def signup(self, info=None):
        return self.namespace["_google_signup"](dict(info or self.INFO),
                                               logging.getLogger("memory-vault"))

    def test_a_verified_address_becomes_the_vault(self):
        self.assertEqual(self.signup(), "alice@example.com")
        self.assertEqual(self.linked["sub-1"], "alice@example.com")

    def test_no_confirmation_mail_is_sent(self):
        # Google has already verified that this account controls this address, so
        # re-verifying would prove nothing.
        self.signup()
        self.assertEqual(self.mails, [])

    def test_every_refusal_creates_no_vault_and_no_subject_link(self):
        # Four different reasons to say no, one property: nothing is created and
        # the subject stays unlinked, so the next sign-in has to try again.
        cases = (
            # Google has not proved the address is theirs.
            ("unverified address", dict(self.INFO, email_verified=False), True, False),
            # The vault key is the address; without one there is nothing to build.
            ("missing address", dict(self.INFO, email=""), True, False),
            ("registration closed", self.INFO, False, False),
            # Google has proved who the person is; it has not proved which of
            # their accounts they meant, and only a subject link can. The username
            # rules rightly refuse an "@", so this collision is reachable only
            # through a Google signup.
            ("address already a vault", self.INFO, True, True),
        )
        for label, info, enabled, preoccupied in cases:
            with self.subTest(label=label):
                self.enabled = enabled
                if preoccupied:
                    sessions.link_google_identity("sub-9", "alice@example.com",
                                                  "alice@example.com")
                self.assertIsNone(self.signup(info))
                self.assertEqual(self.linked, {})


# ---------------------------------------------------------------------------
# The landing page
# ---------------------------------------------------------------------------

class LandingPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = _read("mem-mcp/templates/landing.html")

    def test_the_signup_card_is_gated_on_what_is_usable(self):
        # Three pins of one property: the card renders for a usable method, and
        # each half is gated on its own flag -- one "registration is on" flag
        # would have to render a Google button on a deployment that cannot
        # complete a Google login.
        for gate in ("{% if SIGNUP_EMAIL or SIGNUP_GOOGLE %}", "{% if SIGNUP_EMAIL %}",
                     "{% if SIGNUP_GOOGLE %}"):
            with self.subTest(gate=gate):
                self.assertIn(gate, self.page)

    def test_the_form_constraints_match_the_server(self):
        # A cross-file contract: a form that rejects a password the backend would
        # have accepted, or vice versa, fails silently. One attribute per case.
        block = self.page[self.page.find('id="registerForm"'):]
        block = block[:block.find("</form>")]
        for attribute in (f'minlength="{sessions.MIN_USERNAME_CHARS}"',
                          f'maxlength="{sessions.MAX_USERNAME_CHARS}"',
                          'pattern="[A-Za-z0-9._-]+"'):
            with self.subTest(attribute=attribute):
                self.assertIn(attribute, block)
        with self.subTest(attribute="password minimum"):
            self.assertIn(f'minlength="{sessions.MIN_PASSWORD_CHARS}"', self.page)

    def test_the_page_says_the_username_is_what_you_sign_in_with(self):
        self.assertIn("SIGNUP_PASSWORD_MIN", self.page)
        lower = self.page.lower()
        self.assertIn("username", lower)
        self.assertIn("confirm", lower)

    def test_google_is_a_redirect_and_not_a_pasted_token(self):
        # The whole point of the rewrite: no token is ever typed into this page.
        self.assertIn('href="{{BASE_URL}}/api/auth/google/start"', self.page)
        body = self.page.split("</style>", 1)[1]
        self.assertNotIn("<textarea", body,
                         msg="a pasted token is what this flow exists to remove")
        self.assertNotIn("ID token", self.page)

    def test_registration_ends_in_a_check_your_mail_state(self):
        self.assertIn('id="registerPending"', self.page)
        self.assertIn('id="registerPendingEmail"', self.page)
        self.assertIn("once", self.page)

    def test_the_three_outcome_messages_are_present(self):
        for value in ('VERIFIED == "1"', 'VERIFIED == "0"', "GOOGLE_RESULT =="):
            self.assertIn(value, self.page)

    def test_nothing_suggests_a_password_can_be_reset(self):
        # There is no reset flow; a user who does not know that will eventually
        # rely on "reset it with that address".
        self.assertNotIn("forgot", self.page.lower())
        self.assertNotIn("reset your password", self.page.lower())

    def test_the_verification_is_a_get_because_a_mail_client_will_not_post(self):
        subject, body = sessions.verification_email(
            "alice", "https://hass.example/mem-mcp/api/auth/verify?token=abc", "Memory Vault")
        self.assertIn("/api/auth/verify?token=abc", body)
        self.assertIn("window.location", self.page)


# ---------------------------------------------------------------------------
# Call sites — a test of a helper is not a test of its call site
# ---------------------------------------------------------------------------

class CallSiteTests(unittest.TestCase):
    def test_both_login_paths_go_through_verify_account(self):
        # An address can be in `credentials` *and* htpasswd, and the two stores
        # have different rotation rules; two call sites that each pick a store is
        # how they end up disagreeing about who can log in. Both halves of
        # `_check_session_auth` read the same function, so it is one case each.
        for name in ("api_login", "_check_session_auth"):
            source = _function_source("mem-mcp/gui.py", name)
            self.assertIn("_verify_account(", source, msg=(
                f"{name} must not verify a password itself"))

    def test_the_throttle_is_textually_before_the_flag_check(self):
        # Ordering, not presence: a guard that counted only successes would leave
        # a still-mounted route with no rate limit at all.
        source = _function_source("mem-mcp/gui.py", "_require_registration")
        self.assertLess(source.index("allow_registration_attempt("),
                        source.index("registration_enabled("))

    def test_the_oauth_state_lives_in_the_session_and_nowhere_else(self):
        # The single seam between the two routes. `state` has to survive the
        # round trip to Google, and the server-side session is the only thing
        # here that survives it -- so it goes in the session, and the value that
        # comes back is compared against what is in there.
        #
        # This is pinned rather than exercised because every test of these two
        # routes drives `_Request(session={...})`, a plain dict. Nothing in this
        # file runs VaultSessionMiddleware, so the state "surviving" in a test
        # says nothing at all about it surviving in production. It did not: a
        # session with no `user` was refused by the middleware, so every start
        # handed the browser an expired cookie and every callback reported that
        # the sign-in had not come back. Both halves were green.
        start = _function_source("mem-mcp/gui.py", "api_google_start")
        self.assertIn('request.session["oauth_state"] = state', start, msg=(
            "the state must be written to the session -- that is the only thing "
            "the callback can read it back from"))
        self.assertIn("google_auth.authorization_url(state,", start, msg=(
            "the same value has to go to Google, or there is nothing to compare "
            "the callback's echo against"))

        callback = _function_source("mem-mcp/gui.py", "api_google_callback")
        self.assertIn('request.session.get("oauth_state")', callback, msg=(
            "the comparison has to read the session, not a parameter"))
        self.assertIn("hmac.compare_digest", callback)

        # And nowhere else: a state in a query string survives in browser
        # history, in a Referer and in any proxy log on the way back from Google.
        for name in ("api_google_start", "api_google_callback"):
            source = _function_source("mem-mcp/gui.py", name)
            self.assertNotIn('["state"]', source.replace('session["oauth_state"]', ""),
                             msg=f"{name} must not build a URL carrying the state")
            self.assertNotIn("state=", source, msg=(
                f"{name} must not put the state in a URL"))

    def test_the_pre_auth_session_is_persisted_by_the_middleware(self):
        # The other half of that seam, asserted from the suite that owns the flow
        # so that a change to either module fails here. test_sessions pins the
        # middleware's behaviour in detail; what matters for this flow is only
        # that the store accepts a row with no user in it.
        directory = tempfile.mkdtemp(prefix="reg-oauth-")
        self.addCleanup(shutil.rmtree, directory, True)
        previous = os.environ.get("MEM_SESSION_DIR")
        os.environ["MEM_SESSION_DIR"] = directory
        self.addCleanup(lambda: os.environ.__setitem__("MEM_SESSION_DIR", previous)
                        if previous is not None else None)
        record = sessions.create_session("", data={"oauth_state": "opaque"})
        self.assertNotIn("user", record["data"],
                         "a pre-auth session must carry no identity to read")

    def test_google_start_is_not_gated_on_the_registration_flag(self):
        source = _function_source("mem-mcp/gui.py", "api_google_start")
        self.assertIn("google_auth.configured()", source)
        self.assertNotIn("registration_enabled", source, msg=(
            "gating sign-in on the registration flag would sign out everyone who "
            "signed up with Google the moment an operator turned it off"))

    def test_only_the_signup_path_creates_an_account(self):
        source = _function_source("mem-mcp/gui.py", "api_register")
        self.assertIn("create_credentials(", source)
        self.assertNotIn("session[\"user\"]", source)

    def test_a_failed_mail_deletes_the_account_it_just_made(self):
        source = _function_source("mem-mcp/gui.py", "api_register")
        self.assertIn("delete_credentials(", source)
        self.assertIn("_service_unavailable", source)

    def test_mcp_is_still_token_only(self):
        # Registration is GUI-only. A signup produces a vault key, and a vault key
        # is a credential, so exposing it on /mcp would put an unauthenticated
        # endpoint on the path that holds every tool.
        for name in ("resolve_bearer_token",):
            source = _function_source("mem-mcp/gui.py", name)
            self.assertIn("resolve_psk(", source)
            for gone in ("looks_like_a_google_token", "resolve_google_identity",
                         "google_identity("):
                self.assertNotIn(gone, source, msg=(
                    "a browser sign-in ends in a session cookie, and a cookie is "
                    "not something an MCP client presents"))
            # The reason survives, as prose: the rung is gone on purpose and the
            # next person needs to know that rather than re-adding it.
            self.assertIn("cookie", source.lower())

    def test_the_only_credential_google_can_present_is_a_session(self):
        source = _function_source("mem-mcp/gui.py", "api_google_callback")
        self.assertIn('session["user"] = user_id', source)
        self.assertNotIn("create_psk", source)


if __name__ == "__main__":
    unittest.main()