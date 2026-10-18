"""
test_sharing.py – audit.py (share links, grants, audit log) and sharing.py.

``audit.py`` is standard library only by design, so this suite IMPORTS and
CALLS the shipping module directly — no ``ast.get_source_segment`` lifts are
needed here, the same rule ``test_sessions.py`` follows. Every assertion is
behavioural on purpose: the interesting properties of this layer are what a
call does (grant accepted, use counted, failure swallowed) not what its
source looks like.

The environment knobs are pointed at a per-test temporary directory, which is
also how the schema-initialisation cache is exercised: it is keyed by path,
so every test drives a real init, and a boolean cache would be a bug this
suite exists to notice.

Two properties get their own tests because nothing else would catch them:

- **A dead token is one generic None.** Unknown, revoked, expired and
  exhausted must be indistinguishable, or the accept endpoint is an
  existence oracle for previously-valid tokens.
- **An audit write failure must not raise, and must not be silent.**
  ``log_access`` swallows and logs at WARNING; re-injecting a raise (or
  deleting the logging) must fail the swallowed-failure test.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import audit
import sharing

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYS_PYTHON = sys.executable or "python3"


class SharingStoreTestCase(unittest.TestCase):
    """Base: give every test its own audit.db, and put it back afterwards."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="audit-test-")
        self._old = {name: os.environ.get(name)
                     for name in ("MEM_AUDIT_DB", "MEM_SESSION_DIR",
                                  "MEM_SHARE_LINK_TTL_DAYS",
                                  "MEM_SHARE_MAX_RECIPIENTS",
                                  "MEM_AUDIT_KEEP_DAYS")}
        os.environ["MEM_AUDIT_DB"] = os.path.join(self._tmp, "audit.db")
        os.environ["MEM_SESSION_DIR"] = os.path.join(self._tmp, "sessions")
        audit._initialised_path = None

    def tearDown(self):
        for name, value in self._old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        audit._initialised_path = None
        shutil.rmtree(self._tmp, ignore_errors=True)


class SchemaTests(SharingStoreTestCase):
    """Init behaviour: default path, the three tables, idempotency."""

    def test_the_default_path_lives_next_to_the_session_store(self):
        os.environ.pop("MEM_AUDIT_DB", None)
        audit._initialised_path = None
        # With MEM_SESSION_DIR set by setUp, sessions.db_path() puts the
        # session store there; the audit log must sit in the same directory.
        expected = os.path.join(os.environ["MEM_SESSION_DIR"], "audit.db")
        self.assertEqual(audit.db_path(), expected)
        self.assertTrue(os.path.isdir(os.path.dirname(expected)),
                        msg="the default location's directory was not created")

    def test_init_creates_the_three_tables(self):
        audit.init_db()
        import sqlite3
        conn = sqlite3.connect(audit.db_path())
        try:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        for table in ("share_links", "share_grants", "audit_log"):
            self.assertIn(table, names, msg=f"{table} missing from the schema")
        audit.init_db()  # idempotent: a second call must not raise


class LinkLifecycleTests(SharingStoreTestCase):
    """Mint, resolve, accept, revoke: the token is evidence, nothing stored."""

    def test_mint_returns_the_token_exactly_once_and_stores_only_a_hash(self):
        link = audit.mint_share_link("alice", "client", "cid-1", "read",
                                     expires_in_days=7, now=1_000_000.0)
        self.assertIn("token", link, "the plaintext exists in the return value")
        import sqlite3
        conn = sqlite3.connect(audit.db_path())
        try:
            row = conn.execute("SELECT * FROM share_links").fetchone()
        finally:
            conn.close()
        self.assertNotIn(link["token"], str(row),
                         msg="the plaintext must never be stored")
        self.assertEqual(link["prefix"], row[2],
                         "the stored prefix is the display form")

    def test_a_mint_needs_an_owner_and_a_valid_target_and_permission(self):
        for kwargs in ({"owner_user_id": "", "target_type": "client",
                        "target_id": "c1", "permission": "read"},
                       {"owner_user_id": "a", "target_type": "vault",
                        "target_id": "c1", "permission": "read"},
                       {"owner_user_id": "a", "target_type": "client",
                        "target_id": "  ", "permission": "read"},
                       {"owner_user_id": "a", "target_type": "client",
                        "target_id": "c1", "permission": "admin"}):
            with self.assertRaises(ValueError, msg=kwargs):
                audit.mint_share_link(**kwargs)

    def test_default_ttl_comes_from_the_knob(self):
        os.environ["MEM_SHARE_LINK_TTL_DAYS"] = "5"
        link = audit.mint_share_link("alice", "client", "c1", "read",
                                     now=1_000_000.0)
        self.assertEqual(link["expires_at"], 1_000_000.0 + 5 * 86400.0)

    def test_an_explicit_never_expires_is_available_but_must_be_chosen(self):
        link = audit.mint_share_link("alice", "client", "c1", "read",
                                     expires_in_days=None, now=1_000_000.0)
        self.assertIsNone(link["expires_at"])
        with self.assertRaises(ValueError):
            audit.mint_share_link("alice", "client", "c1", "read",
                                  expires_in_days=0)

    def test_resolve_returns_the_link_and_never_consumes_a_use(self):
        link = audit.mint_share_link("alice", "client", "c1", "read",
                                     now=1_000_000.0)
        for _ in range(3):
            resolved = audit.resolve_share_link(link["token"], now=1_000_001.0)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["use_count"], 0,
                             "resolving must not count a use; only accepting does")

    def test_expired_revoked_and_unknown_are_one_generic_none(self):
        link = audit.mint_share_link("alice", "client", "c1", "read",
                                     expires_in_days=1, now=1_000_000.0)
        self.assertIsNone(audit.resolve_share_link(link["token"], now=1_900_000.0))
        never = audit.mint_share_link("bob", "client", "c2", "read", now=1_000_000.0)
        self.assertTrue(audit.revoke_share_link(never["id"], "bob", now=1_100_000.0))
        self.assertIsNone(audit.resolve_share_link(never["token"], now=1_200_000.0))
        self.assertIsNone(audit.resolve_share_link("no-such-token", now=1_200_000.0))
        self.assertIsNone(audit.resolve_share_link("", now=1_200_000.0))

    def test_exhausted_link_stops_resolving_at_max_uses(self):
        link = audit.mint_share_link("alice", "client", "c1", "read",
                                     max_uses=1, now=1_000_000.0)
        self.assertIsNotNone(audit.accept_share_link(link["token"], "bob",
                                                     now=1_000_001.0))
        self.assertIsNone(audit.accept_share_link(link["token"], "carol",
                                                  now=1_000_002.0))

    def test_revoke_answers_false_for_not_found_not_yours_and_already_revoked(self):
        link = audit.mint_share_link("alice", "client", "c1", "read", now=1.0)
        with self.subTest("already revoked"):
            self.assertTrue(audit.revoke_share_link(link["id"], "alice"))
            self.assertFalse(audit.revoke_share_link(link["id"], "alice"))
        with self.subTest("not yours"):
            other = audit.mint_share_link("bob", "client", "c2", "read", now=1.0)
            self.assertFalse(audit.revoke_share_link(other["id"], "alice"))
        with self.subTest("no such id"):
            self.assertFalse(audit.revoke_share_link("nonsense", "alice"))

    def test_list_reports_status_and_keeps_dead_rows_visible(self):
        fresh = audit.mint_share_link("alice", "client", "c1", "read",
                                      expires_in_days=10, now=1.0)
        dead = audit.mint_share_link("alice", "context", "x1", "write", now=1.0)
        audit.revoke_share_link(dead["id"], "alice")
        rows = audit.list_share_links("alice", target_type="client",
                                      target_id="c1", now=2.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "active")
        rows = audit.list_share_links("alice", now=2.0)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"active", "revoked"})


class GrantTests(SharingStoreTestCase):
    """Grants are the access records; every way of arriving is bounded."""

    def test_a_grant_needs_two_distant_parties(self):
        with self.assertRaises(ValueError):
            audit.grant_access("alice", "alice", "client", "c1", "read")
        with self.assertRaises(ValueError):
            audit.grant_access("alice", "", "client", "c1", "read")

    def test_grant_then_read_then_revoke(self):
        grant = audit.grant_access("alice", "bob", "client", "c1", "read", now=1.0)
        self.assertTrue(audit.has_access("bob", "alice", "client", "c1",
                                         level="read"))
        self.assertTrue(audit.grant_access("alice", "bob", "client", "c1",
                                           "write", now=2.0)["already_had_access"],
                        "a re-grant of an ACTIVE grant already had access")
        self.assertTrue(audit.revoke_grant(grant["id"], "alice", now=3.0))
        self.assertFalse(audit.has_access("bob", "alice", "client", "c1"))

    def test_accept_creates_a_grant_counts_a_use_and_is_idempotent(self):
        link = audit.mint_share_link("alice", "client", "c1", "read",
                                     expires_in_days=1, now=1.0)
        first = audit.accept_share_link(link["token"], "bob", now=2.0)
        self.assertEqual(first["target_id"], "c1")
        self.assertEqual(first["permission"], "read")
        self.assertEqual(first["owner_user_id"], "alice")
        self.assertFalse(first["already_had_access"])
        second = audit.accept_share_link(link["token"], "bob", now=3.0)
        self.assertTrue(second["already_had_access"])
        rows = audit.list_share_links("alice", target_type="client",
                                      target_id="c1", now=3.0)
        self.assertEqual(rows[0]["use_count"], 2,
                         "each accepted use is counted, re-accepts included")

    def test_accept_of_a_dead_or_malformed_token_is_a_generic_none(self):
        link = audit.mint_share_link("alice", "client", "c1", "read",
                                     expires_in_days=1, now=1.0)
        dead = audit.accept_share_link(link["token"], "bob", now=99_000_000.0)
        self.assertIsNone(dead, msg="expired must not differ from unknown")
        self.assertEqual(audit.accept_share_link("", "bob"), None)
        self.assertEqual(audit.accept_share_link(None, "bob"), None)

    def test_write_grant_covers_read_but_read_never_covers_write(self):
        audit.grant_access("alice", "bob", "client", "c1", "read", now=1.0)
        self.assertTrue(audit.has_access("bob", "alice", "client", "c1",
                                         level="read"))
        self.assertFalse(audit.has_access("bob", "alice", "client", "c1",
                                          level="write"))
        audit.grant_access("alice", "bob", "client", "c1", "write", now=2.0)
        self.assertTrue(audit.has_access("bob", "alice", "client", "c1",
                                         level="write"))
        self.assertTrue(audit.has_access("bob", "alice", "client", "c1",
                                         level="read"))

    def test_a_client_grant_covers_its_contexts_through_the_parent_id(self):
        audit.grant_access("alice", "bob", "client", "cid", "read", now=1.0)
        self.assertTrue(audit.has_access("bob", "alice", "context", "xtid",
                                         parent_target_id="cid"))
        self.assertFalse(
            audit.has_access("bob", "alice", "context", "xtid",
                             parent_target_id="other-client"),
            msg="a client grant covers ITS contexts, not another client's")

    def test_the_owner_needs_no_grant_and_pay_no_lookup(self):
        self.assertTrue(audit.has_access("alice", "alice", "client", "c1"))
        # And an unrelated vault user must not be conflated with the owner.
        self.assertFalse(audit.has_access("ALICE", "alice", "client", "c1"))

    def test_recipient_cap_bounds_every_way_a_grant_can_arrive(self):
        os.environ["MEM_SHARE_MAX_RECIPIENTS"] = "2"
        audit.grant_access("alice", "bob", "client", "c1", "read", now=1.0)
        audit.grant_access("alice", "carol", "client", "c1", "read", now=1.0)
        with self.assertRaises(audit.ShareLimitError):
            audit.grant_access("alice", "dave", "client", "c1", "read", now=1.0)
        # An existing holder may be re-granted (it is not a new recipient),
        # and re-activating a REPLACED permission of the same grantee must not
        # count against the cap.
        grant = audit.grant_access("alice", "bob", "client", "c1", "write",
                                   now=2.0)
        self.assertTrue(grant["already_had_access"])
        with self.assertRaises(audit.ShareLimitError):
            link = audit.mint_share_link("alice", "client", "c1", "read",
                                         expires_in_days=1, now=2.0)
            audit.accept_share_link(link["token"], "new-comer", now=3.0)

    def test_a_grant_upsert_reactivates_a_revoked_row_on_new_consent(self):
        grant = audit.grant_access("alice", "bob", "client", "c1", "read",
                                   now=1.0)
        self.assertTrue(audit.revoke_grant(grant["id"], "alice", now=2.0))
        self.assertFalse(audit.has_access("bob", "alice", "client", "c1"))
        regrant = audit.grant_access("alice", "bob", "client", "c1", "write",
                                     now=3.0)
        self.assertEqual(regrant["permission"], "write")
        self.assertTrue(audit.has_access("bob", "alice", "client", "c1",
                                         level="write"))

    def test_list_grants_for_target_and_user_report_the_right_half(self):
        audit.grant_access("alice", "bob", "client", "c1", "read", now=1.0)
        audit.grant_access("alice", "carol", "context", "x1", "write", now=2.0)
        audit.grant_access("dave", "bob", "client", "z9", "read", now=3.0)
        target_rows = audit.list_grants_for_target("alice", "client", "c1")
        self.assertEqual([r["grantee_user_id"] for r in target_rows], ["bob"])
        mine = audit.list_grants_for_user("bob")
        self.assertEqual({(r["owner_user_id"], r["target_id"])
                          for r in mine}, {("alice", "c1"), ("dave", "z9")})

    def test_revoke_all_for_a_user_kills_only_that_grantee(self):
        audit.grant_access("alice", "bob", "client", "c1", "read", now=1.0)
        audit.grant_access("alice", "carol", "client", "c1", "read", now=1.0)
        audit.grant_access("alice", "bob", "context", "x1", "read", now=1.0)
        killed = audit.revoke_all_grants_for_user("alice", "bob", now=2.0)
        self.assertEqual(killed, 2)
        self.assertTrue(audit.has_access("carol", "alice", "client", "c1"))
        self.assertFalse(audit.has_access("bob", "alice", "client", "c1"))


class AuditLogTests(SharingStoreTestCase):
    """Append-only rows: never a secret, never a failed request, pruned."""

    def test_a_row_is_written_and_reading_back_filters(self):
        audit.log_access("bob", "alice", "client", "c1", action="read",
                         permission="read", shared=True,
                         detail={"q": "meeting notes"}, now=100.0)
        audit.log_access("bob", "alice", "client", "c1", action="update",
                         permission="write", shared=True, now=200.0)
        audit.log_access("carol", "dave", "client", "c9", action="read",
                         now=300.0)
        rows = audit.query_audit_log(owner_user_id="alice")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "update", "newest first")
        self.assertTrue(audit.query_audit_log(actor_user_id="carol"))
        self.assertEqual([r["shared"] for r in
                          audit.query_audit_log(owner_user_id="alice",
                                                target_id="c9")], [])
        self.assertEqual(len(audit.query_audit_log(owner_user_id="alice",
                                                   since=150.0)), 1)
        self.assertEqual(len(audit.query_audit_log(owner_user_id="alice",
                                                   limit=1)), 1)

    def test_detail_never_carries_a_secret(self):
        audit.log_access("bob", "alice", "client", "c1", action="read",
                         detail={"token": "shs_SUPERSECRET",
                                 "password": "hunter2",
                                 "via_link_prefix": "shs_abcd",
                                 "q": "safe"}, now=1.0)
        row = audit.query_audit_log(actor_user_id="bob")[0]
        self.assertNotIn("SUPERSECRET", row["detail"])
        self.assertNotIn("hunter2", row["detail"])
        self.assertIn("via_link_prefix", row["detail"])
        self.assertIn("<redacted>", row["detail"])

    def test_a_failed_write_is_swallowed_not_raised_and_not_silent(self):
        import logging
        original = audit._connect
        records = []

        class Probe(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("audit")
        handler = Probe()
        logger.addHandler(handler)
        try:
            def broken():
                raise RuntimeError("disk full")
            audit._connect = broken
            row_id = audit.log_access("bob", "alice", "client", "c1",
                                      action="read", now=1.0)
            self.assertIsNone(row_id, "a failed audit write returns None")
            self.assertTrue(any("audit log write failed" in m for m in records),
                            msg="the swallow must leave a WARNING, or a dead "
                                "audit feed is indistinguishable from a "
                                "quiet vault")
        finally:
            audit._connect = original
            logger.removeHandler(handler)

    def test_purge_keeps_the_retention_window(self):
        audit.log_access("bob", "alice", "client", "c1", action="read",
                         now=1.0)
        audit.log_access("bob", "alice", "client", "c1", action="read",
                         now=100.0)
        removed = audit.purge_audit_log(older_than_days=1, now=86_500.0)
        self.assertEqual(removed, 1, "the 1s-old row is inside the 1-day window")
        self.assertEqual(len(audit.query_audit_log(owner_user_id="alice")), 1)
        with self.assertRaises(ValueError):
            audit.purge_audit_log(older_than_days=0)


class KnobTests(SharingStoreTestCase):
    """The env contract is behavioural: an empty value behaves like absent."""

    def test_sharing_is_off_by_default_and_reads_truish_values(self):
        self.assertFalse(sharing.sharing_enabled())
        for value in ("1", "true", "YES", "on", " On "):
            os.environ["MEM_SHARING_ENABLED"] = value
            self.assertTrue(sharing.sharing_enabled(), msg=value)
        os.environ["MEM_SHARING_ENABLED"] = "0"
        self.assertFalse(sharing.sharing_enabled())

    def test_knobs_treat_an_empty_value_as_absent(self):
        os.environ["MEM_SHARE_LINK_TTL_DAYS"] = ""
        self.assertEqual(audit.share_ttl_days(), 30)
        os.environ["MEM_SHARE_MAX_RECIPIENTS"] = ""
        self.assertEqual(audit.max_recipients(), 50,
                         "present-but-empty must use the code default")
        os.environ["MEM_AUDIT_KEEP_DAYS"] = ""
        self.assertEqual(audit.audit_keep_days(), 90)
        os.environ["MEM_SHARE_MAX_RECIPIENTS"] = "7"
        self.assertEqual(audit.max_recipients(), 7)
        os.environ["MEM_SHARE_MAX_RECIPIENTS"] = "banana"
        self.assertEqual(audit.max_recipients(), 50, "garbage falls back")


class PurePolicyTests(unittest.TestCase):
    """``grant_permits`` with no store at all — the coverage rules live here."""

    CLIENT_GRANT = {"owner_user_id": "alice", "target_type": "client",
                    "target_id": "cid", "permission": "read"}
    CONTEXT_GRANT = {"owner_user_id": "alice", "target_type": "context",
                     "target_id": "xtid", "permission": "write"}
    OTHER_GRANT = {"owner_user_id": "dave", "target_type": "client",
                   "target_id": "cid", "permission": "write"}

    def test_read_client_grant_permits_read_not_write(self):
        self.assertTrue(audit.grant_permits([self.CLIENT_GRANT], "alice",
                                            "client", "cid"))
        self.assertFalse(audit.grant_permits([self.CLIENT_GRANT], "alice",
                                             "client", "cid", level="write"))

    def test_write_grant_contributes_read_too(self):
        self.assertTrue(audit.grant_permits([self.CONTEXT_GRANT], "alice",
                                            "context", "xtid"))
        self.assertTrue(audit.grant_permits([self.CONTEXT_GRANT], "alice",
                                            "context", "xtid", level="write"))

    def test_a_client_grant_covers_only_its_own_context(self):
        self.assertTrue(audit.grant_permits([self.CLIENT_GRANT], "alice",
                                            "context", "xtid",
                                            parent_target_id="cid"))
        self.assertFalse(audit.grant_permits(
            [self.CLIENT_GRANT], "alice", "context", "xtid",
            parent_target_id="somebody-elses-client"))

    def test_a_context_grant_does_not_cover_the_client(self):
        self.assertFalse(audit.grant_permits([self.CONTEXT_GRANT], "alice",
                                             "client", "cid"))
        self.assertFalse(audit.grant_permits([self.CONTEXT_GRANT], "alice",
                                             "client", "cid",
                                             parent_target_id="xtid"))

    def test_another_owners_same_named_target_grants_nothing(self):
        self.assertFalse(audit.grant_permits([self.OTHER_GRANT], "alice",
                                             "client", "cid"))
        self.assertTrue(audit.grant_permits([self.OTHER_GRANT], "dave",
                                            "client", "cid"))

    def test_an_unknown_level_is_refused_not_assumed(self):
        with self.assertRaises(ValueError):
            audit.grant_permits([self.CLIENT_GRANT], "alice", "client", "cid",
                                level="admin")


class SharingModuleTests(SharingStoreTestCase):
    """sharing.py is policy over the store — driven against real grants."""

    def test_effective_permission_reports_ownership_read_and_none(self):
        self.assertEqual(sharing.effective_permission("alice", "alice",
                                                      "client", "c1"),
                         "write", "ownership is write, always")
        self.assertIsNone(sharing.effective_permission("bob", "alice",
                                                       "client", "c1"))
        audit.grant_access("alice", "bob", "client", "c1", "read", now=1.0)
        self.assertEqual(sharing.effective_permission("bob", "alice",
                                                      "client", "c1"), "read")
        audit.grant_access("alice", "bob", "client", "c1", "write", now=2.0)
        self.assertEqual(sharing.effective_permission("bob", "alice",
                                                      "client", "c1"), "write")

    def test_shared_owners_names_owners_and_nothing_else(self):
        audit.grant_access("alice", "bob", "client", "c1", "read", now=1.0)
        audit.grant_access("alice", "bob", "context", "x1", "read", now=2.0)
        audit.grant_access("dave", "bob", "client", "z9", "read", now=3.0)
        self.assertEqual(sharing.shared_owners("bob"), ["alice", "dave"],
                         "sorted owners, deduplicated — no other data leaks")


class ImportDisciplineTests(unittest.TestCase):
    """The two new modules import in a bare subprocess.

    audit.py and sharing.py must stay standard-library-only (plus each
    other), or the suites can no longer import them on a plain box and every
    behavioural test above quietly degrades to a lift. Importing in a
    subprocess catches a heavyweight import that a source scan could miss,
    the same lesson test_sessions.py records.
    """

    def test_both_modules_import_with_nothing_installed_but_the_stdlib(self):
        code = ("import sys; sys.path.insert(0, r'%s'); import audit, sharing;"
                " print('ok')" % _HERE)
        result = subprocess.run([_SYS_PYTHON, "-c", code],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
