import os
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("MEM_AUDIT_DB", os.path.join(_TMP, "audit.db"))
os.environ.setdefault("MEM_SHARING_ENABLED", "1")

import audit
import sharing


class AuditLinkTests(unittest.TestCase):
    def setUp(self):
        # fresh db per test
        p = os.path.join(tempfile.mkdtemp(), "audit.db")
        os.environ["MEM_AUDIT_DB"] = p
        audit.init_db()

    def test_mint_returns_token_once_and_hashed(self):
        r = audit.mint_share_link("owner", "client", "cid1", "read", 30)
        self.assertIn("token", r)
        self.assertIn("prefix", r)
        self.assertEqual(len(r["prefix"]), 10)
        # token not stored verbatim
        conn = audit._connect()
        row = conn.execute("SELECT token_hash FROM share_links").fetchone()
        conn.close()
        import hashlib
        self.assertEqual(row["token_hash"], hashlib.sha256(r["token"].encode()).hexdigest())

    def test_resolve_valid_token(self):
        r = audit.mint_share_link("owner", "client", "cid1", "read", 30)
        res = audit.resolve_share_link(r["token"])
        self.assertIsNotNone(res)
        self.assertEqual(res["owner_user_id"], "owner")
        self.assertEqual(res["target_id"], "cid1")

    def test_revoked_link_unresolvable(self):
        r = audit.mint_share_link("owner", "client", "cid1", "read", 30)
        audit.revoke_share_link(r["id"], "owner")
        self.assertIsNone(audit.resolve_share_link(r["token"]))

    def test_expired_link_unresolvable(self):
        r = audit.mint_share_link("owner", "client", "cid1", "read", 1)
        # force expiry by resolving with future now
        future = time.time() + 10 * 86400
        self.assertIsNone(audit.resolve_share_link(r["token"], now=future))

    def test_max_uses_enforced(self):
        r = audit.mint_share_link("owner", "client", "cid1", "read", 30, max_uses=1)
        first = audit.accept_share_link(r["token"], "grantee")
        self.assertIsNotNone(first)
        # second accept should fail (max uses)
        second = audit.accept_share_link(r["token"], "grantee2")
        self.assertIsNone(second)

    def test_enumeration_safe_revoke(self):
        # revoke unknown / not-owned / already-revoked all return False
        self.assertFalse(audit.revoke_share_link("nope", "owner"))
        r = audit.mint_share_link("owner", "client", "cid1", "read", 30)
        audit.revoke_share_link(r["id"], "owner")
        # already revoked
        self.assertFalse(audit.revoke_share_link(r["id"], "owner"))
        # not yours
        r2 = audit.mint_share_link("owner2", "client", "cid2", "read", 30)
        self.assertFalse(audit.revoke_share_link(r2["id"], "owner"))


class GrantTests(unittest.TestCase):
    def setUp(self):
        p = os.path.join(tempfile.mkdtemp(), "audit.db")
        os.environ["MEM_AUDIT_DB"] = p
        audit.init_db()

    def test_grant_idempotent(self):
        g1 = audit.grant_access("owner", "grantee", "client", "cid1", "read")
        g2 = audit.grant_access("owner", "grantee", "client", "cid1", "write")
        self.assertEqual(g1["id"], g2["id"])  # same grant row
        # permission upgraded
        grants = audit.list_grants_for_user("grantee")
        self.assertTrue(any(g["permission"] == "write" for g in grants))

    def test_revoke_grant(self):
        g = audit.grant_access("owner", "grantee", "client", "cid1", "read")
        self.assertTrue(audit.revoke_grant(g["id"], "owner"))
        grants = audit.list_grants_for_user("grantee")
        # revoked grants may still list but marked
        active = [x for x in grants if not x.get("revoked_at")]
        self.assertFalse(any(x["id"] == g["id"] for x in active))

    def test_max_recipients_enforced(self):
        # mint many grants up to limit
        limit = audit.max_recipients()
        for i in range(limit):
            audit.grant_access("owner", f"g{i}", "client", "cid1", "read")
        with self.assertRaises(audit.ShareLimitError):
            audit.grant_access("owner", "overflow", "client", "cid1", "read")

    def test_cross_user_no_access(self):
        audit.grant_access("owner", "grantee", "client", "cid1", "read")
        # another user has no access
        self.assertFalse(audit.has_access("other", "owner", "client", "cid1", "read"))
        self.assertTrue(audit.has_access("grantee", "owner", "client", "cid1", "read"))

    def test_owner_always_access(self):
        self.assertTrue(audit.has_access("owner", "owner", "client", "cid1", "read"))
        self.assertTrue(audit.has_access("owner", "owner", "client", "cid1", "write"))

    def test_permission_read_vs_write(self):
        audit.grant_access("owner", "grantee", "client", "cid1", "read")
        self.assertTrue(audit.has_access("grantee", "owner", "client", "cid1", "read"))
        self.assertFalse(audit.has_access("grantee", "owner", "client", "cid1", "write"))


class SharingHelpersTests(unittest.TestCase):
    def setUp(self):
        p = os.path.join(tempfile.mkdtemp(), "audit.db")
        os.environ["MEM_AUDIT_DB"] = p
        audit.init_db()

    def test_sharing_enabled_flag(self):
        os.environ["MEM_SHARING_ENABLED"] = "1"
        self.assertTrue(sharing.sharing_enabled())
        os.environ["MEM_SHARING_ENABLED"] = "0"
        self.assertFalse(sharing.sharing_enabled())

    def test_accessible_grants(self):
        audit.grant_access("owner", "grantee", "client", "cid1", "read")
        grants = sharing.accessible_grants("grantee")
        self.assertTrue(any(g["target_id"] == "cid1" for g in grants))

    def test_can_read_write(self):
        audit.grant_access("owner", "grantee", "client", "cid1", "read")
        self.assertTrue(sharing.can_read("grantee", "owner", "client", "cid1"))
        self.assertFalse(sharing.can_write("grantee", "owner", "client", "cid1"))


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        p = os.path.join(tempfile.mkdtemp(), "audit.db")
        os.environ["MEM_AUDIT_DB"] = p
        audit.init_db()

    def test_log_access_and_query(self):
        audit.log_access("grantee", owner_user_id="owner", target_type="client",
                        target_id="cid1", action="read", permission="read",
                        shared=True)
        rows = audit.query_audit_log(owner_user_id="owner", limit=10)
        self.assertTrue(len(rows) >= 1)
        self.assertEqual(rows[0]["actor_user_id"], "grantee")

    def test_no_token_in_detail(self):
        audit.log_access("g", owner_user_id="o", target_type="client",
                        target_id="c", action="accept", detail={"token": "secret123", "prefix": "abc"})
        rows = audit.query_audit_log(owner_user_id="o", limit=10)
        # detail should not contain full token (sanitized)
        import json
        for r in rows:
            d = r.get("detail")
            if d:
                self.assertNotIn("secret123", str(d))


class AcceptLinkTests(unittest.TestCase):
    def setUp(self):
        p = os.path.join(tempfile.mkdtemp(), "audit.db")
        os.environ["MEM_AUDIT_DB"] = p
        audit.init_db()

    def test_accept_creates_grant(self):
        r = audit.mint_share_link("owner", "client", "cid1", "read", 30)
        res = audit.accept_share_link(r["token"], "grantee")
        self.assertIsNotNone(res)
        self.assertEqual(res["owner_user_id"], "owner")
        self.assertFalse(res.get("already_had_access"))
        # second accept idempotent
        res2 = audit.accept_share_link(r["token"], "grantee")
        self.assertTrue(res2.get("already_had_access"))


if __name__ == "__main__":
    unittest.main()
