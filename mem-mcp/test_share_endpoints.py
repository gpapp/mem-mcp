"""
test_share_endpoints.py – the share routes in gui.py, against real stores.

gui.py cannot be imported on this box (no fastapi), so the handlers are
lifted with ``ast.get_source_segment`` and exec'd against stubs — the same
harness test_registration.py uses. Only the *collaborators* are stubbed:
the token store, the grants, the audit log and the recipient cap run as the
real ``audit`` module (per-test temp database), because those are the
decisions this phase makes.

What is pinned here, and why no other file can see it:

- **The disabled flag answers 404 on every share route** — an operator who
  turns MEM_SHARING_ENABLED off must not advertise the feature, for the
  same reason registration routes do.
- **Ownership is checked before minting** — a handler that minted first and
  reconciled later would hand out links to targets the caller does not own.
- **Refusals are enumeration-safe** — one 404 detail for unknown, foreign
  and already-revoked ids.
- **Accept's except order is load-bearing**: ``ShareLimitError`` is a
  ``ValueError`` subclass, so a ``except ValueError`` first swallows every
  full-roster refusal into a 400 (the merge-draft lesson, applied again).
- **/share/{token} resolves nothing for an anonymous visitor** — the token
  existence stays hidden until somebody signed in accepts it.
- **The sign-in gate appears on every /api/share handler** — the /api prefix
  is what auth_guard covers; a handler relying on anything else is a hole
  the suite must catch before the flag goes live.
"""

import asyncio
import json
import os
import shutil
import ast as pyast
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import audit           # noqa: E402  (stdlib-only)
import sharing         # noqa: E402  (stdlib-only)
import sessions        # noqa: E402  (stdlib-only)


def _read(relative: str) -> str:
    with open(os.path.join(ROOT, relative), "r", encoding="utf-8") as handle:
        return handle.read()


_GUI_SOURCE = _read("mem-mcp/gui.py")


def _function_source(node_name: str, source=_GUI_SOURCE) -> str:
    tree = pyast.parse(source)
    for node in tree.body:
        if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef)) \
                and node.name == node_name:
            return pyast.get_source_segment(source, node) or ""
    raise AssertionError(f"gui.py has no module-level {node_name}")


def _lift(node_name: str, namespace: dict):
    """Exec one function out of gui.py into `namespace`.

    The namespace must be the same dict the caller drives the function with —
    exec into a copy leaves the lifted function's collaborators somewhere
    its caller cannot reach, which made a whole class of these tests pass
    while testing nothing.
    """
    exec(compile(_function_source(node_name), "mem-mcp/gui.py", "exec"), namespace)
    return namespace[node_name]


class _HTTPException(Exception):
    """Stand-in for fastapi's HTTPException in lifted handlers."""

    def __init__(self, status_code: int, detail: str = ""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _HTMLResponse:
    def __init__(self, content, status_code=200):
        self.status_code = status_code
        self.text = content


class ShareRoutesCase(unittest.TestCase):
    """Real audit store + stub GUI plumbing, one lift per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="share-endpoint-")
        self._old = {name: os.environ.get(name)
                     for name in ("MEM_AUDIT_DB", "MEM_SESSION_DIR",
                                  "MEM_SHARING_ENABLED",
                                  "MEM_SHARE_MAX_RECIPIENTS",
                                  "MEM_SHARE_LINK_TTL_DAYS")}
        os.environ["MEM_AUDIT_DB"] = os.path.join(self.tmp, "audit.db")
        os.environ["MEM_SESSION_DIR"] = os.path.join(self.tmp, "sessions")
        os.environ["MEM_SHARING_ENABLED"] = "1"
        audit._initialised_path = None
        self.current_user = "alice"
        self.clients = [
            {"id": "cid-1", "name": "Deutsche Bank",
             "contexts": [{"id": "xtid-1", "name": "AI Adoption"}]},
            {"id": "cid-2", "name": "SAP SE", "contexts": []},
        ]
        self.base_url = "https://vault.example/mem-mcp"
        self.namespace = self._fresh_namespace()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for name, value in self._old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        audit._initialised_path = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fresh_namespace(self) -> dict:
        ns = {
            "HTTPException": _HTTPException,
            "HTMLResponse": _HTMLResponse,
            "logging": __import__("logging"),
            "audit": audit,
            "sharing": sharing,
            "sessions": sessions,
            "vault_sessions": sessions,
            "os": os,
            "mem": types.SimpleNamespace(
                db_list_clients=lambda user_id: self.clients,
                BASE_URL=self.base_url,
            ),
            "_base_url": lambda: self.base_url,
            # Insurance for an eager-annotation interpreter (3.11 in the
            # container): the lifted defs name these models.
            "ShareLinkCreateRequest": types.SimpleNamespace,
            "ShareGrantCreateRequest": types.SimpleNamespace,
            "ShareGrantUpdateRequest": types.SimpleNamespace,
        }
        ns["_render"] = self._render
        ns["_user"] = lambda request: self.current_user
        ns["_require_user"] = self._require_user
        # The production helper logs and returns HTTPException(409, str(e));
        # the stub keeps the status mapping without the logging plumbing.
        ns["_conflict"] = lambda e: _HTTPException(409, str(e))
        for name in ("_require_sharing_enabled", "_share_target_owner",
                     "_permission_of", "api_share_create_link",
                     "api_share_list_links", "api_share_revoke_link",
                     "api_share_accept", "api_share_add_grant",
                     "api_share_update_grant", "api_share_revoke_grant",
                     "api_share_list_grants", "api_share_mine",
                     "share_landing", "api_list_audit"):
            _lift(name, ns)
        return ns

    def _render(self, name: str, **ctx):
        return json.dumps(ctx, default=str)

    def _require_user(self, request):
        if not self.current_user or self.current_user == "anonymous":
            raise _HTTPException(401, "Unauthorized")
        return self.current_user

    def _request(self):
        return types.SimpleNamespace(
            session={"user": self.current_user},
            state=types.SimpleNamespace(),
            headers={},
        )

    def _body(self, **kwargs):
        return types.SimpleNamespace(**kwargs)

    def _call(self, coro):
        """Run a lifted async handler.

        Not a detail: an unawaited coroutine enters no function at all, so
        the whole case would report green against routes that never ran.
        """
        return asyncio.run(coro)

    def _run_enabled(self, enabled: bool):
        os.environ["MEM_SHARING_ENABLED"] = "1" if enabled else "0"


class _MintMixin:
    """One mint helper shared by the link tests."""

    def _mint(self, **kwargs):
        # kwargs override the defaults: a test that asks for targetId=… must
        # not crash with "multiple values" — it means exactly what it says.
        data = {"targetType": "client", "targetId": "cid-1",
                "permission": "read", "expiresInDays": None,
                "neverExpires": False, "maxUses": None}
        data.update(kwargs)
        body = self._body(**data)
        return self._call(self.namespace["api_share_create_link"](
            self._request(), body))


class DisabledFlagTests(ShareRoutesCase):
    """A disabled feature must look indistinguishable from one not mounted."""

    def _expect_404(self, case):
        try:
            self._call(case)
        except _HTTPException as exc:
            self.assertEqual(exc.status_code, 404)
        else:
            self.fail("a disabled share route must answer 404, like registration")

    def test_every_share_route_answers_404_when_the_flag_is_off(self):
        ns = self.namespace
        self._run_enabled(False)
        req = self._request()
        cases = [
            ("create link", lambda: ns["api_share_create_link"](
                req, self._body(targetType="client", targetId="cid-1",
                                permission="read", expiresInDays=None,
                                neverExpires=False, maxUses=None))),
            ("list links", lambda: ns["api_share_list_links"](req)),
            ("revoke link", lambda: ns["api_share_revoke_link"]("l1", req)),
            ("accept", lambda: ns["api_share_accept"]("tok", req)),
            ("add grant", lambda: ns["api_share_add_grant"](
                req, self._body(targetType="client", targetId="cid-1",
                                username="bob", permission="read"))),
            ("update grant", lambda: ns["api_share_update_grant"](
                "g1", req, self._body(permission="write"))),
            ("revoke grant", lambda: ns["api_share_revoke_grant"]("g1", req)),
            ("list grants", lambda: ns["api_share_list_grants"](req)),
            ("mine", lambda: ns["api_share_mine"](req)),
            ("landing page", lambda: ns["share_landing"](req, "tok")),
        ]
        for label, case in cases:
            with self.subTest(route=label):
                self._expect_404(case())


class CreateLinkTests(_MintMixin, ShareRoutesCase):
    """Minting a token: the owner's own target, and no more than that."""

    def test_the_response_carries_the_token_once_and_a_url(self):
        captured = time.time()
        response = self._mint()
        self.assertIn("token", response)
        # The model in audit.py follows the PSK one: the raw token has no
        # prefix; the display prefix is a separate shortened form.
        self.assertTrue(response["prefix"].startswith("shs_"))
        self.assertNotIn("shs_", response["token"])
        self.assertEqual(response["permission"], "read")
        self.assertIn("/share/", response["acceptUrl"])
        self.assertGreater(response["expiresAt"], captured + 29 * 86400.0,
                           msg="an absent TTL takes the configured default")

    def test_an_absent_ttl_uses_the_knob_and_never_is_explicit(self):
        os.environ["MEM_SHARE_LINK_TTL_DAYS"] = "3"
        captured = time.time()
        response = self._mint()
        self.assertGreater(response["expiresAt"], captured + 2 * 86400.0,
                           "absent takes the knob, not a hard default")
        never = self._mint(neverExpires=True)
        self.assertIsNone(never["expiresAt"])
        with self.assertRaises(_HTTPException) as caught:
            self._mint(neverExpires=True, expiresInDays=5)
        self.assertEqual(caught.exception.status_code, 400)

    def test_ownership_is_checked_before_anything_is_minted(self):
        with self.assertRaises(_HTTPException) as caught:
            self._mint(targetId="somebody-elses-cid")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(list(audit.query_audit_log(actor_user_id="alice")), [],
                         msg="a refusal mints nothing and audits nothing")

    def test_a_foreign_context_is_the_same_404_as_a_missing_one(self):
        # 'not-mine' belongs to nobody in this test's registry: the caller's
        # db_list_clients is the whole world the handler may see.
        with self.assertRaises(_HTTPException) as foreign:
            self._mint(targetType="context", targetId="not-mine")
        with self.assertRaises(_HTTPException) as missing:
            self._mint(targetType="context", targetId="does-not-exist")
        self.assertEqual(foreign.exception.status_code, 404)
        self.assertEqual(foreign.exception.detail, missing.exception.detail,
                         msg="a caller cannot probe the existence of "
                             "somebody else's context by name")

    def test_bad_target_type_and_empty_target_are_400s(self):
        with self.assertRaises(_HTTPException) as caught:
            self._mint(targetType="vault")
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(_HTTPException) as caught:
            self._mint(targetType="client", targetId="")
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_minted_link_accepts_and_grants_read(self):
        response = self._mint()
        self.current_user = "bob"
        try:
            accepted = self._call(self.namespace["api_share_accept"](
                response["token"], self._request()))
        finally:
            self.current_user = "alice"
        self.assertEqual(accepted["targetId"], "cid-1")
        self.assertEqual(accepted["permission"], "read")
        self.assertEqual(accepted["owner"], "alice")
        self.assertFalse(accepted["alreadyHadAccess"])
        self.assertTrue(audit.has_access("bob", "alice", "client", "cid-1",
                                         level="read"))

    def test_second_accept_reports_already_had_access(self):
        response = self._mint()
        self.current_user = "bob"
        try:
            self._call(self.namespace["api_share_accept"](response["token"],
                                                          self._request()))
            again = self._call(self.namespace["api_share_accept"](
                response["token"], self._request()))
        finally:
            self.current_user = "alice"
        self.assertTrue(again["alreadyHadAccess"])


class AcceptRefusalTests(_MintMixin, ShareRoutesCase):
    """Every refusal is one answer, and a full roster is not a bad request."""

    def _accept(self, token):
        return self._call(self.namespace["api_share_accept"](token,
                                                             self._request()))

    def _age_link(self, token, expires_at=1.0):
        # Fast-forward: real links expire in the future, and the store's own
        # clock reads wall time. A direct UPDATE is honest about what "past"
        # means and leaves no test helper behind in the module.
        conn = audit._connect()
        try:
            conn.execute("UPDATE share_links SET expires_at = ?"
                         " WHERE token_hash = ?",
                         (expires_at, audit._hash_token(token)))
            conn.commit()
        finally:
            conn.close()

    def test_unknown_and_expired_and_revoked_are_one_404(self):
        expired = self._mint()
        self._age_link(expired["token"])          # expires_at -> the past
        revoked = self._mint()
        self._call(self.namespace["api_share_revoke_link"](revoked["id"],
                                                           self._request()))
        details = set()
        for label, token in (("unknown", "totally-unknown"),
                             ("empty", ""),
                             ("expired", expired["token"]),
                             ("revoked", revoked["token"])):
            with self.subTest(case=label):
                with self.assertRaises(_HTTPException) as caught:
                    self._accept(token)
                self.assertEqual(caught.exception.status_code, 404)
                details.add(caught.exception.detail)
        self.assertEqual(len(details), 1,
                         msg="one generic detail: a differentiated refusal "
                             "is an existence oracle for spent tokens")

    def test_a_full_roster_answers_409_not_400(self):
        os.environ["MEM_SHARE_MAX_RECIPIENTS"] = "1"
        link = self._mint()
        assert audit.grant_access("alice", "carol", "client", "cid-1", "read")
        self.current_user = "dave"
        try:
            with self.assertRaises(_HTTPException) as caught:
                self._accept(link["token"])
        finally:
            self.current_user = "alice"
        self.assertEqual(caught.exception.status_code, 409,
                         msg="the remedy is 'ask the owner to revoke "
                             "someone', not 'fix your request'")


class RevokeLinkTests(_MintMixin, ShareRoutesCase):
    """Revocation is one scoped UPDATE answering one False."""

    def _revoke(self, link_id):
        return self._call(self.namespace["api_share_revoke_link"](
            link_id, self._request()))

    def test_one_404_for_unknown_foreign_and_already_revoked(self):
        link = self._mint()
        self.assertTrue(self._revoke(link["id"])["ok"])
        details = set()
        for link_id in (link["id"], "never-existed", "someone-elses"):
            with self.subTest(link_id=link_id[:8]):
                with self.assertRaises(_HTTPException) as caught:
                    self._revoke(link_id)
                self.assertEqual(caught.exception.status_code, 404)
                details.add(caught.exception.detail)
        self.assertEqual(details, {"No such share link"})


class DirectGrantTests(ShareRoutesCase):
    """Direct grants by account name — the link's alternative route."""

    def setUp(self):
        super().setUp()
        # An account that exists in the credentials store: the real
        # user_id_taken answers the handler, not a stub pretending everyone
        # is registered.
        sessions.create_credentials("bob", "calm otter 42 vault")

    def _add(self, **kwargs):
        kwargs.setdefault("username", "bob")
        body = self._body(targetType="client", targetId="cid-1",
                          permission="read", **kwargs)
        return self._call(self.namespace["api_share_add_grant"](self._request(),
                                                                body))

    def test_add_grant_makes_access_visible_without_a_link(self):
        response = self._add()
        self.assertEqual(response["grant"]["grantee_user_id"], "bob")
        self.assertTrue(audit.has_access("bob", "alice", "client", "cid-1",
                                         level="read"))

    def test_unknown_account_is_a_400(self):
        with self.assertRaises(_HTTPException) as caught:
            self._add(username="not-anywhere")
        self.assertEqual(caught.exception.status_code, 400)

    def test_owner_target_updates_change_the_real_permission(self):
        seeded = audit.grant_access("alice", "bob", "client", "cid-1", "read")
        self._call(self.namespace["api_share_update_grant"](
            seeded["id"], self._request(), self._body(permission="write")))
        self.assertTrue(audit.has_access("bob", "alice", "client", "cid-1",
                                         level="write"))

    def test_a_revoked_grant_cannot_be_resurrected_by_update(self):
        seeded = audit.grant_access("alice", "bob", "client", "cid-1", "read")
        audit.revoke_grant(seeded["id"], "alice")
        with self.assertRaises(_HTTPException) as caught:
            self._call(self.namespace["api_share_update_grant"](
                seeded["id"], self._request(), self._body(permission="write")))
        self.assertEqual(caught.exception.status_code, 404)
        self.assertFalse(audit.has_access("bob", "alice", "client", "cid-1"))

    def test_revoke_grant_answers_one_404_for_every_dead_case(self):
        seeded = audit.grant_access("alice", "bob", "client", "cid-1", "read")
        self._call(self.namespace["api_share_revoke_grant"](seeded["id"],
                                                            self._request()))
        details = set()
        for grant_id in (seeded["id"], "nope", "other-owners"):
            with self.subTest(grant_id=grant_id[:8]):
                with self.assertRaises(_HTTPException) as caught:
                    self._call(self.namespace["api_share_revoke_grant"](
                        grant_id, self._request()))
                self.assertEqual(caught.exception.status_code, 404)
                details.add(caught.exception.detail)
        self.assertEqual(details, {"No such share grant"})


class AuditFeedTests(ShareRoutesCase):
    """GET /api/audit — the owner reads their targets, the admin reads all."""

    def setUp(self):
        super().setUp()
        self.admin = False
        import types as _t
        self.namespace["mem"].is_admin_user = lambda user: self.admin

    def _seed_activity(self):
        audit.grant_access("alice", "bob", "client", "cid-1", "read", now=10.0)
        audit.log_access("bob", "alice", "client", "cid-1", action="read",
                         permission="read", shared=True, now=20.0)
        audit.log_access("bob", "dave", "client", "d-target", action="read",
                         shared=True, now=30.0)

    def test_the_owner_sees_only_rows_about_their_own_targets(self):
        self._seed_activity()
        rows = self._call(self.namespace["api_list_audit"](self._request()))["audit"]
        self.assertEqual(
            {r["owner_user_id"] for r in rows}, {"alice"},
            msg="the feed is the same privacy promise as the feature: no "
                "foreign target shows its other readers")

    def test_actor_target_and_since_filters_pass_through(self):
        self._seed_activity()
        feed = self.namespace["api_list_audit"]
        self.assertEqual(
            len(self._call(feed(self._request(), actor="bob"))["audit"]), 1)
        self.assertEqual(
            len(self._call(feed(self._request(), since=25.0))["audit"]), 0,
            msg="nothing newer than now — since cuts the feed from the front")
        rows = self._call(feed(self._request(
        ), targetType="client", targetId="cid-1"))["audit"]
        self.assertEqual(len(rows), 1)

    def test_admin_reads_everything(self):
        self._seed_activity()
        self.admin = True
        rows = self._call(self.namespace["api_list_audit"](
            self._request()))["audit"]
        self.assertEqual(
            {r["owner_user_id"] for r in rows}, {"alice", "dave"})

    def test_a_foreign_target_query_is_not_probeable(self):
        with self.assertRaises(_HTTPException) as caught:
            self._call(self.namespace["api_list_audit"](
                self._request(), targetType="client",
                targetId="somebody-elses"))
        self.assertEqual(caught.exception.status_code, 404)


class SharedWithMeTests(ShareRoutesCase):
    """GET /api/share/mine and the owner's recipients view."""

    def test_mine_lists_only_the_grants_and_owner_names(self):
        audit.grant_access("alice", "bob", "client", "cid-1", "read")
        audit.grant_access("dave", "bob", "context", "x9", "write")
        self.current_user = "bob"
        response = self._call(self.namespace["api_share_mine"](self._request()))
        self.assertEqual(response["owners"], ["alice", "dave"])
        self.assertEqual({g["owner_user_id"] for g in response["grants"]},
                         {"alice", "dave"})

    def test_the_owner_view_shows_only_their_own_grants(self):
        audit.grant_access("alice", "bob", "client", "cid-1", "read")
        # Alice is also a GRANTEE of dave's elsewhere; the owner view must
        # not confuse the two directions.
        audit.grant_access("dave", "alice", "client", "d1", "read")
        response = self._call(self.namespace["api_share_list_grants"](
            self._request()))
        self.assertEqual(
            {g["owner_user_id"] for g in response["grants"]}, {"alice"})


class ShareLandingTests(ShareRoutesCase):
    """GET /share/{token} — anonymous visitors learn nothing but how to sign in."""

    def _landing(self):
        return self._call(self.namespace["share_landing"](self._request(),
                                                          "shs_possible-token"))

    def test_anonymous_page_never_names_the_token_nor_builds_a_url(self):
        self.current_user = "anonymous"
        ctx = json.loads(self._landing().text)
        self.assertIsNone(ctx["ACCEPT_URL"],
                          "no accept URL is built for somebody with no session")
        self.assertNotIn("shs_possible-token", self._landing().text)
        self.assertIsNone(ctx.get("TOKEN"),
                          msg="the token is not handed to the renderer at all — "
                              "only the ACCEPT_URL for a signed-in visitor "
                              "needs it")

    def test_signed_in_page_builds_the_accept_url(self):
        ctx = json.loads(self._landing().text)
        self.assertTrue(ctx["SIGNED_IN"])
        self.assertEqual(ctx["ACCEPT_URL"],
                         "https://vault.example/mem-mcp/api/share/accept/"
                         "shs_possible-token")


class CallSitePinTests(unittest.TestCase):
    """Source-level pins for what a behavioural stub cannot see."""

    HANDLERS = ("api_share_create_link", "api_share_list_links",
                "api_share_revoke_link", "api_share_accept",
                "api_share_add_grant", "api_share_update_grant",
                "api_share_revoke_grant", "api_share_list_grants",
                "api_share_mine", "share_landing")

    def test_every_share_handler_calls_require_user(self):
        for name in self.HANDLERS:
            if name == "share_landing":
                continue  # deliberate: the one public page is anonymous by design
            with self.subTest(handler=name):
                self.assertIn("_require_user(request)", _function_source(name),
                              msg="the /api prefix is the only gate; a handler "
                                  "not calling it depends on nothing")

    def test_the_flag_check_precedes_every_handler_body(self):
        for name in self.HANDLERS:
            src = _function_source(name)
            self.assertIn("_require_sharing_enabled()", src)
            self.assertNotIn("audit.mint_share_link",
                             src[:src.index("_require_sharing_enabled()")],
                             msg="the flag check must not come after a mint")

    def test_the_accept_route_orders_less_errors_before_value_error(self):
        src = _function_source("api_share_accept")
        self.assertLess(src.index("except audit.ShareLimitError"),
                        src.index("except ValueError"),
                        msg="ShareLimitError is a ValueError subclass — an "
                            "inverted order turns every full-roster refusal "
                            "into a bad-request answer")

    def test_the_mint_handler_checks_ownership_before_minting(self):
        src = _function_source("api_share_create_link")
        first_mint = src.index("audit.mint_share_link")
        owner_check = src.index("_share_target_owner(user_id")
        self.assertLess(owner_check, first_mint,
                        msg="ownership must be a precondition, never a "
                            "side effect verified after the token exists")

    def test_the_landing_route_sits_outside_the_api_prefix(self):
        self.assertIn('@web_app.get("/share/{token}"', _GUI_SOURCE)
        self.assertIn('@web_app.post("/api/share/accept/{token}"',
                      _GUI_SOURCE)
        for path in ("/api/share/links", "/api/share/grants", "/api/share/mine"):
            self.assertIn(f'"{path}', _GUI_SOURCE)

    def test_the_landing_handler_resolves_nothing_itself(self):
        src = _function_source("share_landing")
        self.assertNotIn("resolve_share_link", src,
                         msg="an anonymous GET must not consult the token "
                             "store at all")
        self.assertNotIn("accept_share_link", src)

    def test_the_token_is_never_quoted_in_any_log_detail(self):
        src = _function_source("api_share_create_link")
        log_call = src[src.index("log_access"):src.index("logging.getLogger")]
        self.assertNotIn('link["token"]', log_call,
                         msg="the plaintext lives in the response and "
                             "nowhere else")


if __name__ == "__main__":
    unittest.main()
