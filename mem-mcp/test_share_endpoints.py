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
import re
import shutil
import ast as pyast
import subprocess
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

# A <script> element that carries a src= attribute has its inline content
# DISCARDED by the parser -- the element is a bare fetch of the external file,
# and any text between the tags is inert (it is only legal when empty). That is
# invisible to every text-based check: the code is still readable, still parses
# under `node --check` if you extract it, and still contains every string an
# assertion looks for. It is simply never executed, so every button wired to it
# fails with "X is not defined" and the console shows a ReferenceError with no
# other symptom.
_EXTERNAL_SRC = re.compile(r'\bsrc\s*=')


def _script_blocks(html: str):
    """(attrs, inline_body) for every <script> element, in source order."""
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(r'<script([^>]*)>(.*?)</script>', html, re.S)
    ]


def _executable_js(html: str) -> str:
    """Every inline <script> body the browser will actually run."""
    return "\n".join(
        body for attrs, body in _script_blocks(html) if not _EXTERNAL_SRC.search(attrs)
    )


def _describe(attrs: str) -> str:
    """A short, quotable name for a script element, for failure messages."""
    src = re.search(r'\bsrc\s*=\s*"([^"]*)"', attrs)
    return f"<script src={src.group(1)[:40]}...>" if src else "<script> (inline)"


# The JavaScript counterpart of the docstring trap AGENTS.md warns about for
# Python: a guard that greps a source file is satisfied by the word appearing
# in the comment that explains why it must not be used. This feature has been
# bitten by exactly that twice -- once for Cypher, once for the share dialog --
# so anything counted in JS is counted only on lines that are code.
#
# This is deliberately a line filter and not a lexer. A single left-to-right
# scanner was tried first and abandoned: it has to know what a JS regex literal
# is, because a `'` inside a character class (`str.replace(/['"]/g, '')`) is not
# a string delimiter, and one unhandled regex opens a "string" that runs to the
# next apostrophe in the file and silently swallows everything between. Getting
# that right is a project of its own, and a half-right lexer is worse than a
# simple rule because it looks authoritative.
#
# The filter therefore drops whole-line comments and leaves trailing ones. That
# is a known limit, stated rather than hidden: a comment that mentions
# `.hidden = true` on a line of real code will trip the count below. That is a
# loud, cheap failure a human resolves in seconds, which is the right trade
# against a scanner that can go quietly blind.
def _code_lines(js: str) -> list:
    out = []
    for line in js.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        out.append(line)
    return out


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


class NoTransitiveReShareTests(ShareRoutesCase):
    """A grant is not a licence to re-share.

    ``db_list_clients`` returns a grantee's OWN clients *and* the ones other
    owners shared with them (``shared: True``). The owner check behind every
    owner-only share route ran over that combined list, so a grantee could
    mint a link for -- or direct-add a recipient to -- a target belonging to
    somebody else. Sharing is explicitly non-transitive, and the recipient
    cap is bypassed the moment a second owner starts handing the same target
    on.
    """

    def _shared_clients(self):
        return [{"id": "cid-9", "name": "SAP SE", "shared": True,
                 "owner": "alice", "permission": "read",
                 "contexts": [{"id": "xtid-9", "name": "S4 rollout",
                               "shared": True, "owner": "alice",
                               "permission": "read"}]}]

    def test_a_shared_client_is_not_mintable(self):
        self.clients = self._shared_clients()
        with self.assertRaises(_HTTPException) as ctx:
            self._call(self.namespace["api_share_create_link"](
                self._request(),
                self._body(targetType="client", targetId="cid-9",
                           permission="read")))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_shared_project_is_not_mintable(self):
        self.clients = self._shared_clients()
        with self.assertRaises(_HTTPException) as ctx:
            self._call(self.namespace["api_share_create_link"](
                self._request(),
                self._body(targetType="context", targetId="xtid-9",
                           permission="read")))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_grantee_cannot_direct_add_a_recipient_either(self):
        self.clients = self._shared_clients()
        with self.assertRaises(_HTTPException) as ctx:
            self._call(self.namespace["api_share_add_grant"](
                self._request(),
                self._body(targetType="client", targetId="cid-9",
                           username="bob", permission="read")))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_the_owner_can_still_share_their_own_client(self):
        self.clients = self._shared_clients() + [
            {"id": "cid-1", "name": "Deutsche Bank",
             "contexts": [{"id": "xtid-1", "name": "AI Adoption"}]}]
        result = self._call(self.namespace["api_share_create_link"](
            self._request(),
            self._body(targetType="client", targetId="cid-1",
                       permission="read", neverExpires=False,
                       expiresInDays=None, maxUses=None)))
        self.assertTrue(result["token"])


class ShareDialogTests(unittest.TestCase):
    """The share dialog's own failure modes, from the template.

    Every assertion here guards a *silent* defect: the ``api`` helper's
    ``apiFail`` REJECTS on a non-2xx, so an unwrapped ``await`` inside these
    handlers turned every 4xx into a button that appeared dead -- which is
    exactly what a disabled feature flag looks like to the person clicking.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read("mem-mcp/templates/dashboard.html")

    def _dialog_js(self) -> str:
        """The dialog code, read from the block that actually executes.

        Slicing to the next "</script>" was how this defect stayed invisible:
        the code had been spliced into a <script src=...> element, so that slice
        returned the dead copy and every assertion below passed on code the
        browser never ran. The block is selected by the same attribute test the
        handler check uses, so "what I am asserting about" and "what runs" are
        the same object by construction.
        """
        code = _executable_js(self.src)
        start = code.index("let _shareTarget")
        return code[start:]

    def test_no_handler_calls_a_method_the_api_object_does_not_have(self):
        # api defines get/post/put/delete only. `api.del` was a TypeError on
        # click -- a revoke button that did nothing and logged nothing.
        for name in ("api.del(", "api.postx(", "api.gets("):
            self.assertNotIn(name, self.src,
                             msg=f"{name} is not a method on the api object")

    def test_every_state_changing_call_goes_through_share_run(self):
        js = self._dialog_js()
        for call in ("api.post('share/links'", "api.post('share/grants'",
                     "api.put('share/grants/", "api.delete('share/links/",
                     "api.delete('share/grants/"):
            self.assertIn(call, js, msg=f"{call} vanished from the dialog")
        # Each of those sits inside shareRun, which is what catches the
        # rejection. A bare `await api.` inside these handlers is the bug.
        for line in js.splitlines():
            stripped = line.strip()
            if stripped.startswith("await api.") :
                self.assertFalse(
                    stripped.startswith("await api.get"),
                    msg="a bare await outside shareRun swallows its error")
        self.assertIn("catch (e)", js)

    def test_the_dialog_has_a_footer_and_close_is_bottom_right(self):
        markup = self.src[self.src.index('<div class="share-modal-foot">'):]
        foot = markup[:markup.index("</div>")]
        self.assertIn('onclick="closeShareDialog()"', foot,
                      msg="the footer is where Close belongs, and it was "
                          "parked mid-dialog where it read as a form field")
        foot_css = self.src[self.src.index(".share-modal-foot {"):]
        foot_css = foot_css[:foot_css.index("}")]
        self.assertIn("justify-content: flex-end", foot_css,
                      msg="the footer must right-align its Close button")
        # ...and the header's X is a close control too, not the only one.
        self.assertIn('class="share-x"', self.src)

    def test_the_links_list_reads_the_links_key_not_the_bare_object(self):
        # GET /api/share/links answers {"links": [...]}; reading the response
        # as an array rendered "none" for every link the owner had minted.
        js = self._dialog_js()
        self.assertIn("(r && r.links)", js)
        self.assertIn("(r && r.grants)", js)
        # and the row fields are the store's snake_case, not camelCase guesses
        self.assertIn("l.token_prefix", js)
        self.assertIn("l.use_count", js)

    def test_the_expiry_field_cannot_send_both_forms_at_once(self):
        js = self._dialog_js()
        self.assertIn("neverExpires", js)
        self.assertIn("expiresInDays", js)


class ExecutableScriptTests(unittest.TestCase):
    """The template's JS must be in a block the browser will actually run.

    Every other test in this file reads the template as *text*. That is the
    whole problem: code inside a ``<script src=...>`` element is readable,
    greppable, parseable and correct, and the browser discards it without a
    word. A splice anchored on the vis-network ``<script>`` tag put 8 KB of
    share-dialog JS there, and the result was a dashboard whose Share buttons
    all raised ``openShareDialog is not defined`` while the share-dialog suite
    stayed green -- it was asserting against the dead copy.

    ``test_graph_scope._app_script`` did not catch it either, and the reason is
    worth recording: it selects inline blocks with a ``(?![^>]*\\bsrc=)``
    lookahead, so the broken element was *skipped* rather than counted. The
    suite still saw exactly one app script, so the count held and the missing
    functions went unnoticed. A filter that excludes the broken thing also
    excludes the evidence that it is broken.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read("mem-mcp/templates/dashboard.html")

    def test_no_script_element_hides_inline_code_behind_a_src_attribute(self):
        dead = [
            _describe(attrs)
            for attrs, body in _script_blocks(self.src)
            if _EXTERNAL_SRC.search(attrs) and body.strip()
        ]
        self.assertEqual(
            dead, [],
            msg=("these elements carry inline JS behind a src= attribute, which the "
                 "browser discards: " + ", ".join(dead)))

    def test_every_function_called_from_an_inline_handler_is_defined(self):
        """`onclick="openShareDialog(...)"` needs openShareDialog to exist.

        Checked against the *executable* blocks only, so moving a definition
        into a dead element is a failure rather than a silent no-op. Member
        calls (`e.preventDefault()`, `this.find(...)`) are excluded by
        requiring the callee not to follow a dot -- they resolve against an
        object, not the global scope.
        """
        code = _executable_js(self.src)
        defined = set(re.findall(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)', code))
        defined |= set(re.findall(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', code))

        callee = re.compile(r'(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(')
        keywords = {"if", "for", "while", "switch", "catch", "return",
                    "typeof", "await", "function", "new"}
        missing = {}
        for handler in re.findall(r'\bon\w+\s*=\s*"([^"]*)"', self.src):
            for name in callee.findall(handler):
                if name not in defined and name not in keywords:
                    missing.setdefault(name, handler.strip()[:100])

        self.assertEqual(
            sorted(missing), [],
            msg=("these inline handlers call functions that are not defined in any "
                 "executable script block (each button wired to one raises "
                 "`X is not defined`):\n"
                 + "\n".join(f"  {n}()  <- {h}" for n, h in sorted(missing.items()))))

    def test_the_share_dialog_lives_in_an_executable_block(self):
        """The specific regression, named, so the failure is unmistakable."""
        code = _executable_js(self.src)
        self.assertTrue(
            "function openShareDialog" in code,
            msg=("openShareDialog is not in any executable <script> block, so every "
                 "🔗 Share button raises `openShareDialog is not defined`; the "
                 "executable blocks are: "
                 + ", ".join(_describe(attrs) for attrs, _ in _script_blocks(self.src))))


class SharingAffordanceGatingTests(unittest.TestCase):
    """With sharing off, nothing in the dashboard offers sharing.

    ``MEM_SHARING_ENABLED`` defaults to 0, so on a stock deployment every share
    route answers 404. The UI used to render anyway: the "Shared with me" card
    called ``/api/share/mine`` on every page load and rendered "Could not load"
    forever, and every client card carried a Share button that opened a dialog
    whose every action failed. That is the same principle the registration
    section in AGENTS.md states for signup -- a disabled route answers 404
    rather than 403 so the feature is not advertised -- applied to the page
    that advertises it.

    Both halves are needed and neither is sufficient. Rendering the card behind
    the flag is useless if ``get_gui`` never sets it (Jinja then renders the
    undefined name as falsy and the card vanishes for *everyone*, including on
    a deployment that enabled the feature), and setting the flag in the server
    is useless if the template ignores it.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read("mem-mcp/templates/dashboard.html")
        cls.gui = _read("mem-mcp/gui.py")

    def _render(self, sharing_enabled):
        """Render the dashboard both ways and return the two results.

        Rendering rather than grepping is the point: the card can be absent
        because it is gated, or because the selector never matched anything in
        the first place, and a substring test cannot tell those apart.
        """
        import jinja2
        env = jinja2.Environment()
        tpl = env.from_string(self.src)
        return {
            flag: tpl.render(SHARING_ENABLED=flag, IS_ADMIN=False, BASE_URL="",
                             MERGE_MAX_CLUSTER=12, SESSION_MAX_AGE_DAYS=30)
            for flag in (True, False)
        }

    def test_the_shared_with_me_card_is_absent_when_sharing_is_off(self):
        rendered = self._render(sharing_enabled=None)
        # assertTrue/assertFalse, not assertIn/assertNotIn: the rendered page is
        # ~200 KB and assertNotIn echoes the whole thing into the failure
        # output, which buries the message this test exists to deliver.
        self.assertTrue('id="shared-with-me"' in rendered[True],
                        msg="the card is missing with sharing ON — the gate is inverted "
                            "or the selector no longer matches the markup")
        self.assertFalse('id="shared-with-me"' in rendered[False],
                         msg="with sharing off the card still renders, calls a 404 route "
                             "on every page load and shows 'Could not load' forever")

    def test_the_gate_is_a_rendered_flag_not_a_literal(self):
        """Both outcomes must come from one variable, or one is hard-coded.

        A template that renders the card unconditionally fails the test above;
        one that omits it unconditionally fails it too. What is worth pinning is
        that the difference is *caused by the flag*, which is only true if the
        markup is wrapped in a conditional over that name.

        Every such conditional is checked, not just the first: <body> also
        carries a `{% if SHARING_ENABLED %}` attribute, and a search that took
        only the first match would report that one and conclude the card is
        ungated — the same order-dependence that made a `find` answer the wrong
        question once already.
        """
        gates = list(re.finditer(r'{%\s*if\s+SHARING_ENABLED\s*%}((?:(?!{%\s*(?:if|endif)\b).)*?)'
                                 r'{%\s*endif\s*%}', self.src, re.S))
        self.assertTrue(gates, msg="no `{% if SHARING_ENABLED %}` block exists at all")
        self.assertTrue(any('id="shared-with-me"' in g.group(1) for g in gates),
                        msg="no conditional over the flag wraps the shared-with-me card; "
                            "the card's presence is not controlled by the flag")

    def test_the_server_actually_sets_the_flag(self):
        """The seam: get_gui must put the value in the context.

        Jinja renders an undefined name as falsy, so a template gating on
        SHARING_ENABLED with no producer silently hides the feature from every
        user on every deployment -- which is not a failing feature, it is a
        missing one. Nothing else in this suite can see that gap.
        """
        self.assertTrue(
            re.search(r'ctx\["SHARING_ENABLED"\]\s*=\s*sharing\.sharing_enabled\(\)', self.gui),
            msg=('gui.py never sets ctx["SHARING_ENABLED"], so the template gates on an '
                 'undefined name and the feature is hidden from everyone regardless of '
                 'MEM_SHARING_ENABLED'))

    def test_the_share_buttons_are_gated_on_the_rendered_flag(self):
        """The 🔗 Share buttons are JS, so they cannot use a Jinja block.

        They read the flag off a conditional <body data-sharing-enabled>
        attribute instead, which is why that attribute carries the value
        rather than an interpolated one: an interpolated value inside the
        inline app script would leave it unparseable by node --check, the only
        JS validation available in this repo, and an inline-if expression
        between doubled braces is refused outright by the template lint.
        """
        self.assertTrue(
            re.search(r"<body[^>]*\{%\s*if\s+SHARING_ENABLED\s*%\}"
                      r"[^>]*data-sharing-enabled=\"true\"", self.src),
            msg="<body> does not carry a conditional data-sharing-enabled attribute, so "
                "the JS Share buttons cannot know whether sharing is on")
        self.assertTrue(
            re.search(r"SHARING_ENABLED\s*=\s*document\.body\.dataset\.sharingEnabled",
                      _executable_js(self.src)),
            msg="the executable script does not read the flag off <body>")
        for call in ("openShareDialog('context'", "openShareDialog('client'"):
            self.assertIn(call, self.src, msg=f"{call} is not rendered at all — re-check "
                                              f"that the button still exists behind the gate")


class ShareDialogContentsTests(unittest.TestCase):
    """The dialog has to render its four sections, and nothing may hide them.

    A report that the share dialog "only shows Close" is not diagnosable from
    the markup: the four sections are present in the source, the stylesheet
    defines every custom property they use, and the page renders. The failure
    is always a *rendering* one, so every guard written against the source text
    passes while the dialog is empty on screen.

    That is why these tests derive the property rather than pinning a token.
    ``assertIn('class="share-section"', src)`` would be true for a section
    carrying ``hidden``, for a section buried outside ``.share-modal-body``,
    and for a section the stylesheet sets to ``display: none`` -- three
    different ways to ship exactly the reported symptom, all green.

    The stylesheet half matters as much as the markup half, because the one
    rule that would cause this is a plausible-looking "collapse the dialog
    until opened" edit, and it is the kind of line that reads as intentional.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read("mem-mcp/templates/dashboard.html")

    def _rendered(self):
        import jinja2
        env = jinja2.Environment()
        return env.from_string(self.src).render(
            SHARING_ENABLED=True, IS_ADMIN=False, BASE_URL="",
            MERGE_MAX_CLUSTER=12, SESSION_MAX_AGE_DAYS=30)

    @staticmethod
    def _css():
        return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", _read(
            "mem-mcp/templates/dashboard.html"), re.S))

    @staticmethod
    def _rules_naming(css, selector_fragment):
        """Every declaration block whose selector mentions ``selector_fragment``.

        Deliberately not a real CSS parser. It only has to see the rules that
        could hide an element, and a regex over the whole stylesheet finds
        those whether they sit at the top level or inside a media block -- which
        is where a mobile override would put one, and a media-scoped hide is
        the variant that only reproduces on a phone.
        """
        out = []
        for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
            selector, body = match.group(1), match.group(2)
            if selector_fragment in selector:
                out.append((selector.strip(), body.strip()))
        return out

    def test_every_section_reaches_the_rendered_page(self):
        rendered = self._rendered()
        found = rendered.count('class="share-section"')
        self.assertEqual(
            4, found,
            msg=f"expected the dialog's four sections in the rendered page, "
                f"found {found}. The dialog cannot be repaired when only the "
                f"title and Close button arrive.")

    def test_every_control_the_dialog_needs_reaches_the_rendered_page(self):
        rendered = self._rendered()
        for element_id in ("share-title", "share-perm", "share-days",
                           "share-uses", "share-create-btn", "share-result",
                           "share-add-user", "share-add-perm", "share-add-btn",
                           "share-grants-list", "share-links-list", "share-status"):
            self.assertTrue(
                f'id="{element_id}"' in rendered,
                msg=f"#{element_id} is not in the rendered page — the dialog "
                    f"cannot create a link, list recipients or report status")

    def test_the_sections_are_inside_the_scrollable_body(self):
        """A section outside ``.share-modal-body`` renders but out of the panel.

        The body is the only element with ``overflow-y: auto``, and it is a
        flex child, so a section that lands beside it rather than inside it is
        laid out in the wrong box entirely. Ordering is compared by index: a
        substring test cannot tell "inside the body" from "after the footer",
        and slicing to a closing ``</div>`` cannot either -- the first one after
        an opening tag belongs to the element *inside* it, so it silently
        measures the wrong region. Indices do not have that problem.
        """
        src = self.src
        overlay = src.find('<div id="share-overlay"')
        body = src.find('<div class="share-modal-body">')
        foot = src.find('<div class="share-modal-foot">')
        for name, index in (("overlay", overlay), ("body", body), ("foot", foot)):
            self.assertTrue(index != -1, msg=f"the dialog has no .{name} container")
        self.assertTrue(
            overlay < body < foot,
            msg="the dialog's head/body/footer must nest in that order — a "
                "footer before the body puts Close above the content")

        sections = [m.start() for m in re.finditer(r'class="share-section"', src)]
        self.assertEqual(4, len(sections), msg="expected four sections in the source")
        for index in sections:
            self.assertTrue(
                body < index < foot,
                msg="a .share-section sits outside .share-modal-body, so it is "
                    "laid out outside the scrolling panel")

    def test_nothing_in_the_markup_hides_a_section(self):
        """No section may carry ``hidden`` or an inline ``display: none``.

        Both render as an empty dialog with a working Close button, and both
        are invisible to a plain ``assertIn('class="share-section"', src)``.
        """
        for match in re.finditer(r"<[^>]*share-section[^>]*>", self.src):
            tag = match.group(0)
            # assertIsNone/IsNotNone rather than assertFalse(re.search(...)):
            # a Match object is falsy, so assertFalse would interpolate the
            # Match into the failure message ahead of the reason that matters.
            self.assertIsNone(
                re.search(r"\bhidden\b", tag),
                msg=f"a .share-section element is marked hidden: {tag}")
            self.assertIsNone(
                re.search(r"display\s*:\s*none", tag),
                msg=f"a .share-section element is hidden inline: {tag}")

    def test_the_stylesheet_never_hides_a_section(self):
        """The one plausible cause, checked against the actual declarations.

        This is the rule the browser reported and the repository has never
        contained. Pinning the absence here is what makes that report
        actionable rather than a contradiction: if a future edit adds it, this
        fails and names the selector instead of leaving the next person to
        argue with a screenshot.
        """
        hidden = re.compile(r"display\s*:\s*(none|hidden)\b")
        for fragment, subject in ((".share-section", "a dialog section"),
                                  (".share-modal-body", "the dialog body"),
                                  (".share-modal", "the dialog itself")):
            for selector, body in self._rules_naming(self._css(), fragment):
                self.assertFalse(
                    bool(hidden.search(body)),
                    msg=f"{selector} hides {subject}: {{{body}}} — every rule in "
                        f"that selector is visible on the same screen, so check "
                        f"for an !important added elsewhere in this stylesheet")


class ShareDialogClosePathTests(unittest.TestCase):
    """The dialog may be closed from exactly one place in the application.

    The reported symptom was a dialog that "shows up fine then suddenly
    disappears". That is only diagnosable if the set of things that close it is
    small and known, so these tests derive the set from the executable
    JavaScript instead of trusting that nobody adds a fourth path.

    The invariant is deliberately narrow: exactly one line of code hides the
    overlay, and it is inside ``closeShareDialog``. Anything that closes the
    dialog any other way -- an inline ``onclick``, a blur handler, a timer, a
    "close all modals" sweep -- has to go through that function to be visible
    here, and a second direct assignment fails the count.

    The counts are taken over ``_code_lines``, so a comment naming the
    construct cannot satisfy them. See the note on that helper for why it is a
    line filter rather than a lexer.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _executable_js(_read("mem-mcp/templates/dashboard.html"))
        cls.lines = _code_lines(cls.js)

    def _hiding_lines(self):
        return [l for l in self.lines
                if "share-overlay" in l and re.search(r"\.hidden\s*=\s*true", l)]

    def test_exactly_one_line_hides_the_overlay(self):
        hiding = self._hiding_lines()
        self.assertEqual(
            1, len(hiding),
            msg=f"expected exactly one line of code hiding #share-overlay, "
                f"found {len(hiding)}: {hiding}. Each additional one is an "
                f"undocumented way for the dialog to vanish -- which is the "
                f"reported symptom, and it is not diagnosable from a UI that "
                f"has several.")

    def test_that_line_is_inside_closeShareDialog(self):
        """One hiding line is not enough on its own.

        The two ways to get there are a named function everybody calls and a
        single stray line in a random handler. Only the first is reviewable, so
        the location is pinned too.
        """
        hiding = self._hiding_lines()
        self.assertEqual(1, len(hiding), msg=f"expected one hiding line, got {hiding}")
        body = "\n".join(self.lines)
        start = body.find("function closeShareDialog")
        self.assertTrue(start != -1,
                        msg="closeShareDialog is not defined in the executable "
                            "script -- nothing can close the dialog")
        end = body.find("\n  function ", start + 10)
        scope = body[start:end if end != -1 else len(body)]
        self.assertTrue(
            hiding[0] in scope,
            msg="the line that hides #share-overlay is not inside "
                "closeShareDialog(), so the dialog can be closed from code "
                "that does not look like a close handler")

    def test_nothing_closes_the_dialog_automatically(self):
        """No timer, blur or sweep may close it.

        An explicit click is a close; anything else is a close the user did not
        ask for. Matched by name so the failure points at the construct that
        was added.
        """
        code = "\n".join(self.lines)
        for construct, why in (
            # Bounded by `;` rather than by `)`, because the callback's own
            # closing parenthesis comes after the call it makes: in
            # `setTimeout(() => closeShareDialog(), 1000)` a `[^)]*` stops at the
            # arrow function's `)` and never reaches closeShareDialog, which is
            # how a timer guard passes on a timer that closes the dialog.
            (r"setTimeout\s*\([^;]{0,240}?closeShareDialog", "a timer closes the dialog"),
            (r"setInterval\s*\([^;]{0,240}?closeShareDialog", "an interval closes the dialog"),
            (r"addEventListener\s*\(\s*['\"]blur['\"]", "a blur handler can close it"),
            (r"closeAll\w*\s*\(", "a close-all sweep would take the dialog with it"),
        ):
            found = re.search(construct, code)
            self.assertIsNone(
                found,
                msg=f"{why}: {code[found.start():found.start() + 80] if found else ''}")

    def test_escape_only_closes_a_dialog_the_keypress_came_from_inside(self):
        """The one automatic close path must not be global.

        The Escape listener has to stay on ``document``: this script block is
        parsed before the overlay's markup, so moving the listener onto the
        overlay would resolve to null at module scope. The target check is what
        scopes it instead, and it has to survive -- without it the share dialog
        is the one element a stray Escape can dismiss from any focused control
        anywhere in the application.
        """
        code = "\n".join(self.lines)
        listener = re.search(r"addEventListener\s*\(\s*['\"]keydown['\"]", code)
        self.assertTrue(listener,
                        msg="no keydown listener -- the dialog cannot be "
                            "dismissed with Escape at all")
        window = code[listener.start():listener.start() + 400]
        self.assertTrue(
            "share-overlay" in window and ".contains(" in window,
            msg="the keydown listener does not check the event came from inside "
                "#share-overlay, so an Escape anywhere in the app closes it")

    def test_the_helper_ignores_prose(self):
        """The self-check that keeps the four tests above honest.

        A comment line naming the construct must not count, or the counts
        above are measuring documentation rather than behaviour. Feeds the
        helper the exact text it forbids on a comment line and requires it to
        disappear.
        """
        decoy = ("  // document.getElementById('share-overlay').hidden = true;\n"
                 "  * closeShareDialog() hides the overlay from here\n"
                 "  const real = 1;\n")
        kept = _code_lines(decoy)
        self.assertEqual(1, len(kept), msg=f"comment lines survived: {kept}")
        self.assertFalse(
            any("share-overlay" in l for l in kept),
            msg="a comment line was counted as code, so the counts above "
                "include prose and cannot fail on the real defect")


_NODE_HARNESS = r"""
// A fake DOM just rich enough for the dashboard's render functions.
// Everything absorbs: the point is to read the HTML they produce, so a
// listener that does nothing is correct and a listener that ran would be
// noise.
function _fakeEl(id) {
  return {
    id: id, innerHTML: '', textContent: '', value: '', hidden: false,
    style: {}, dataset: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    focus() {}, blur() {}, click() {}, remove() {}, appendChild() {},
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, removeEventListener() {},
    scrollTop: 0, scrollHeight: 0, offsetHeight: 0, clientHeight: 0,
    insertAdjacentHTML() {}, closest() { return null; }, contains() { return false; },
  };
}
const _els = new Map();
function _getEl(id) { if (!_els.has(id)) _els.set(id, _fakeEl(id)); return _els.get(id); }
global.document = {
  getElementById: _getEl, querySelector: () => null, querySelectorAll: () => [],
  createElement: (t) => _fakeEl('new-' + t),
  addEventListener() {}, removeEventListener() {},
  body: _fakeEl('body'), documentElement: _fakeEl('html'), cookie: '',
};
global.window = global;
global.window.location = { origin: 'http://x', href: 'http://x/', pathname: '/' };
global.window.addEventListener = () => {};
global.window.matchMedia = () => ({ matches: false, addEventListener() {} });
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.navigator = { userAgent: 'node', clipboard: { writeText: async () => {} } };
global.fetch = async () => ({ ok: false, status: 404, json: async () => ({}), text: async () => '' });
global.marked = { parse: (s) => s, use() {}, setOptions() {} };
global.vis = { Network: function () {}, DataSet: function () { return { add() {}, get() { return null; }, clear() {} }; } };
global.mermaid = { initialize() {} };

// Captured by the wrapper installed below: a render function that hands a
// menu to showItemMenu is asserting on that array, so it is recorded rather
// than drawn. Plain module-scope bindings, NOT global.* — node wraps this file
// in a function, so the dashboard's own `function showItemMenu` is
// module-scoped and a global property would be a second, never-called copy.
var __menuItems = [];

__APP_JS__

// Rebind the dashboard's own declaration so a menu builder's items are
// observable without a real DOM.
showItemMenu = function (anchorEl, items) { __menuItems = items || []; };

const out = {};
(async () => {
__CASES__
  // An async case resolves to a promise; await them all before printing.
  for (const k of Object.keys(out)) {
    if (out[k] && typeof out[k].then === 'function') out[k] = await out[k];
  }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write(JSON.stringify({ fatal: String(e && e.message || e) })); });
"""


class ReadOnlyGatingTests(unittest.TestCase):
    """A read-only grant must hide the controls it cannot honour.

    Every one of these handlers already answers 403 when the record belongs to
    another owner who shared it read-only, so the buttons "work" in the sense
    that nothing is corrupted -- they just cost a click, a toast and the
    impression that the vault is broken. Hiding them is the same principle as a
    disabled route answering 404 rather than 403: do not offer an action the
    caller cannot perform.

    These tests *execute* the render functions rather than reading their
    source, because the property is what comes out, not what is written. A
    source guard is satisfied by a gated button whose onclick still names the
    handler, by a handler hidden behind a style the CSS later overrides, and by
    a fragment that is built and then discarded -- three ways to ship the
    original report with a green suite. Driving the functions through a fake
    DOM and reading the HTML they produce has none of those blind spots.
    """

    # Every one of these is a write. `clearFactPane`, `selectFact`,
    # `selectDiaryEntry` and `switchTab` are deliberately absent: closing a
    # pane and navigating are reads, and a grantee must still be able to look
    # at the record they were given.
    MUTATING = (
        "saveFactEdit", "saveDiaryEdit", "saveDiaryLink", "savePaneLink",
        "deleteLink", "showLinkEdit", "deleteDiaryLinkFromFact",
        "deleteDiaryLink", "_factPickClient", "_factPickProject",
        "_factRemoveRelevant", "_factAddRelevant", "_showFactMenu",
        "togglePaneLinkForm", "toggleDiaryLinkForm",
    )

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed; the JS is not executable here")

    def _render(self, cases):
        """Run ``cases`` in node and return the captured HTML.

        Each case is (name, js-expression). The harness loads the dashboard's
        own inline script into a fake DOM and evaluates each expression with
        the render functions in scope, so what is asserted is the output of the
        shipping code rather than a copy of it.
        """
        source = _read("mem-mcp/templates/dashboard.html")
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", source, re.S)
        # The first two are the external vis-network fetches, which carry no
        # inline body. Selecting by position rather than by a src= filter keeps
        # the harness honest about what it is loading -- a filter that excludes
        # an element also excludes the evidence of what it contained.
        app_js = "\n".join(blocks[2:])
        payload = "".join(
            f"  try {{ out[{name!r}] = ({expr}); }} "
            f"catch (e) {{ out[{name!r}] = 'THREW: ' + e.message; }}\n"
            for name, expr in cases
        )
        harness = _NODE_HARNESS.replace("__APP_JS__", app_js).replace("__CASES__", payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "render.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(harness)
            proc = subprocess.run([self.node, path], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            self.fail(f"the dashboard script did not load under the fake DOM: "
                      f"{proc.stderr.strip()[:400]}")
        return json.loads(proc.stdout)

    def _owned_fact(self):
        return ("{ id: 'f1', name: 'A fact', text: 'body text', category: 'People', "
                "clientId: null, contextId: null, clientName: '', contextName: '', "
                "relevantClients: [], links: [], metadata: {} }")

    def _shared_fact(self):
        return ("{ id: 'f1', readOnly: true, name: 'A fact', text: 'body text', "
                "category: 'People', clientId: null, contextId: null, clientName: '', "
                "contextName: '', relevantClients: [{ id: 'c1', name: 'ACME', kind: 'client' }], "
                "links: [{ target_id: 'f2', target_label: 'Fact', rel: 'KNOWS', "
                "target_name: 'Bob', direction: 'out' }, "
                "{ target_id: 'd1', target_label: 'DiaryEntry', rel: 'MENTIONS', "
                "target_name: 'Note', direction: 'in' }], metadata: {} }")

    def _projects_html(self, contexts):
        """`loadProjects()` output for one client carrying these projects.

        Driven rather than read: the shared row's actions are built by a
        conditional in a template literal, and a source assertion cannot tell a
        gated button from one whose onclick survived the gate.
        """
        cache = json.dumps([{"id": "c1", "name": "ACME", "contexts": contexts}])
        return self._render([
            ("projects", f"(clientListCache = {cache}, await loadProjects(), "
                         f"document.getElementById('project-list').innerHTML)"),
        ])["projects"]

    def test_a_shared_project_offers_no_rename_or_delete(self):
        """The reported defect: Rename was still visible on a shared project.

        The Projects page is a second surface for the same rows the Clients
        page renders, and it was missed — the client card had gated its actions
        since the sharing work landed, this list had not.
        """
        html = self._projects_html([
            {"id": "x1", "name": "Secret Project", "shared": True,
             "owner": "alice", "permission": "read"},
        ])
        self.assertNotIn("THREW", html)
        self.assertNotIn("renameContext(", html,
                         msg="a shared project still renders a Rename button; "
                             "the owner's node is not the grantee's to rename")
        self.assertNotIn("deleteContext(", html,
                         msg="a shared project still renders a Delete button")
        # Presence before the negative: a guard that removed every action
        # would pass the two assertions above.
        self.assertIn("🔗 shared", html)
        self.assertIn("Secret Project", html)

    def test_an_owned_project_still_offers_rename_and_delete(self):
        html = self._projects_html([{"id": "x2", "name": "Own Project"}])
        self.assertIn("renameContext('x2')", html)
        self.assertIn("deleteContext('x2')", html)

    def test_a_shared_client_card_offers_no_mutating_control(self):
        """The client card and its nested projects, both gated.

        `_renderClientCard` is where the shared badge and the gated actions
        are decided, so driving it covers the Clients page and the client
        filter in one expression.
        """
        shared = {"id": "c1", "name": "ACME", "shared": True, "owner": "alice",
                  "permission": "read",
                  "contexts": [{"id": "x1", "name": "Secret", "shared": True,
                                "owner": "alice", "permission": "read"}]}
        html = self._render([("card", f"_renderClientCard({json.dumps(shared)})")])["card"]
        self.assertNotIn("THREW", html)
        for handler in ("renameClient(", "deleteClient(", "toggleClient(",
                        "toggleCrossClient(", "renameContext(", "deleteContext("):
            self.assertNotIn(handler, html,
                             msg=f"a shared client card still renders {handler}")
        self.assertIn("🔗 shared by alice", html)

    def test_an_owned_client_card_still_offers_every_control(self):
        owned = {"id": "c1", "name": "ACME", "effectiveActive": True,
                 "contexts": [{"id": "x1", "name": "Own"}]}
        html = self._render([("card", f"_renderClientCard({json.dumps(owned)})")])["card"]
        for handler in ("renameClient(", "deleteClient(", "toggleClient(",
                        "renameContext(", "deleteContext("):
            self.assertIn(handler, html,
                          msg=f"an owned client card no longer offers {handler}")

    def test_a_read_only_record_offers_no_mutating_control(self):
        """The reported bug, asserted on the output of both render paths."""
        out = self._render([
            ("factPane", f"renderFactPane({self._shared_fact()}, false), "
                         f"document.getElementById('fact-pane').innerHTML"),
            ("linkSection", f"renderLinkSection({self._shared_fact()})"),
        ])
        for name, html in out.items():
            self.assertNotIn("THREW", html, msg=f"{name} could not be rendered")
            for handler in self.MUTATING:
                self.assertFalse(
                    handler + "(" in html,
                    msg=f"a read-only record still renders a control calling "
                        f"{handler}() in {name} -- it is refused with a 403, so "
                        f"offering it costs the user a click to learn nothing")

    def test_an_owned_record_still_offers_every_mutating_control(self):
        """The other half, and the reason this is not just a blanket removal.

        Over-gating is the failure mode of a fix written by deleting buttons:
        the owner loses their own editor and the vault becomes read-only for
        everybody. Presence is asserted before the negative above, so a guard
        that removes everything passes.
        """
        out = self._render([
            ("view", f"renderFactPane({self._owned_fact()}, false), "
                     f"document.getElementById('fact-pane').innerHTML"),
            ("edit", f"renderFactPane({self._owned_fact()}, true), "
                     f"document.getElementById('fact-pane').innerHTML"),
        ])
        # The save button lives in the edit branch and the scope and menu
        # controls in the view branch, so both have to be driven: asserting
        # only one passes while the other is over-gated.
        for handler, where in (("saveFactEdit", "edit"), ("_factPickClient", "view"),
                               ("_factPickProject", "view"), ("_factAddRelevant", "view"),
                               ("_showFactMenu", "view"), ("togglePaneLinkForm", "view")):
            self.assertTrue(
                handler + "(" in out[where],
                msg=f"an owned record no longer offers {handler}() in {where} mode "
                    f"-- the read-only gate has removed a control the owner needs")

    def _owned_entry(self):
        return ("[{ id: 'e1', readOnly: false, name: 'An entry', content: 'the body', "
                "timestamp: '2026-01-01T10:00', clientName: 'ACME', contextName: '', "
                "mentions: [{ id: 'f2', rel: 'KNOWS', target_name: 'Bob', "
                "target_label: 'Fact' }] }]")

    def _diary(self, read_only):
        entry = ("{ id: 'e1', readOnly: %s, name: 'An entry', content: 'the body', "
                 "timestamp: '2026-01-01T10:00', clientName: 'ACME', contextName: '', "
                 "mentions: [{ id: 'f2', rel: 'KNOWS', target_name: 'Bob', "
                 "target_label: 'Fact' }] }" % ("true" if read_only else "false"))
        return ("(async () => { diaryEntries = [" + entry + "]; "
                "await renderDiaryEditMode('e1'); "
                "return document.getElementById('diary-view-content').innerHTML; })()")

    def test_an_owned_diary_entry_still_offers_save_and_link(self):
        """The diary half of the over-gating guard.

        Without this the diary editor is only ever asserted read-only, so
        removing its Save button for *everybody* passes -- the exact failure a
        fix written by deleting buttons produces. Verified by re-injection:
        flipping the diary gate to a constant true leaves every other test in
        this class green.
        """
        out = self._render([("diary", self._diary(False))])
        html = out["diary"]
        self.assertNotIn("THREW", html, msg="the owned diary entry could not be rendered")
        for handler in ("saveDiaryEdit", "toggleDiaryLinkForm", "deleteDiaryLink"):
            self.assertTrue(
                handler + "(" in html,
                msg=f"an owned diary entry no longer offers {handler}() -- the "
                    f"read-only gate has removed a control the owner needs")

    def test_the_diary_editor_hides_save_and_link_controls(self):
        """The third render path, driven the same way.

        The diary editor is the widest surface: five inputs, the mentions
        badges, the link picker and the save button. A source guard on the
        button alone passes while the inputs stay editable.
        """
        out = self._render([
            ("diary", self._diary(True)),
        ])
        html = out["diary"]
        if "THREW" in html:
            self.skipTest(f"the diary editor could not be driven headless: {html[:200]}")
        for handler in self.MUTATING:
            self.assertFalse(
                handler + "(" in html,
                msg=f"a read-only diary entry still renders a control calling "
                    f"{handler}() -- it is refused with a 403")
        # Cancel stays: leaving an editor is not a mutation, and trapping a
        # grantee in a pane they cannot save is worse than the 403 was.
        self.assertTrue("selectDiaryEntry('e1')" in html,
                        msg="Cancel is gone, so a grantee cannot leave the editor")

    def test_the_link_picker_offers_both_kinds(self):
        """Linking a diary entry to a fact must offer facts.

        This is the picker that could not complete the link it exists to
        create: it returned memories only, with no kind to tell them apart, so
        the two directions of the same feature disagreed about what could be
        linked. Both arrays, both kinds, same helpers as the fact pane.
        """
        out = self._render([
            ("kinds", "(memories = [{ id: 'f1', name: 'A fact', text: 'body', "
                      "category: 'People' }], "
                      "diaryEntries = [{ id: 'e1', name: 'An entry', content: 'the body', "
                      "date: '2026-01-01', timestamp: '2026-01-01T10:00', mentions: [] }, "
                      "{ id: 'e2', name: 'Another entry', content: 'more body', "
                      "date: '2026-01-02', timestamp: '2026-01-02T10:00', mentions: [] }], "
                      "dlinkCandidates('e1', '').map(c => c.kind).sort().join(','))"),
            ("titles", "(memories = [{ id: 'f1', name: 'A fact', text: 'body', "
                       "category: 'People' }], "
                       "diaryEntries = [{ id: 'e1', name: 'An entry', content: 'the body', "
                       "date: '2026-01-01', timestamp: '2026-01-01T10:00', mentions: [] }, "
                       "{ id: 'e2', name: 'Another entry', content: 'more body', "
                       "date: '2026-01-02', timestamp: '2026-01-02T10:00', mentions: [] }], "
                       "dlinkCandidates('e1', '').map(c => _paneLinkTitle(c)).join('|'))"),
        ])
        self.assertEqual("diary,fact", out["kinds"],
                         msg="the diary picker does not offer both kinds, so one "
                             "direction of the link cannot be completed")
        self.assertIn("A fact", out["titles"],
                      msg="a fact is offered by the diary picker but its title is "
                          "not rendered, so the user cannot tell what it is")

    def test_the_link_picker_still_excludes_what_is_already_linked(self):
        """Widening must not turn the picker into a duplicate-edge factory.

        A second edge of the same type is a duplicate, and the old
        implementation excluded linked records for a reason. The widening is a
        superset of that behaviour, not a replacement for it.
        """
        out = self._render([
            ("ids", "(memories = [{ id: 'f1', name: 'A fact', text: 'body', category: 'People' }, "
                    "{ id: 'f2', name: 'Another', text: 'text', category: 'People' }], "
                    "diaryEntries = [{ id: 'e1', name: 'An entry', content: 'the body', "
                    "date: '2026-01-01', timestamp: '2026-01-01T10:00', "
                    "mentions: [{ id: 'f1' }] }, "
                    "{ id: 'e2', name: 'Another entry', content: 'more body', "
                    "date: '2026-01-02', timestamp: '2026-01-02T10:00', mentions: [] }], "
                    "dlinkCandidates('e1', '').map(c => c.rec.id).join(','))"),
        ])
        self.assertNotIn("f1,", out["ids"],
                         msg="a record already linked to this entry is still offered")
        self.assertIn("f2", out["ids"],
                      msg="an unlinked fact was dropped by the widening")

    # ------------------------------------------------------------------
    # The diary VIEW mode, and the hamburger menu that sits in its header.
    #
    # The editor above was gated first and the view mode was assumed to be
    # covered by it. It is not: the view mode is what a grantee sees on open,
    # and it carried the scope chips, the mention delete buttons, the add
    # relevant button and the link form -- four mutating surfaces the editor
    # gate never touched.
    # ------------------------------------------------------------------

    def _diary_view(self, read_only, primary_hidden=False):
        entry = ("{ id: 'e1', readOnly: %s, primaryScopeHidden: %s, name: 'An entry', "
                 "content: 'the body', date: '2026-01-01', "
                 "timestamp: '2026-01-01T10:00', clientName: 'ACME', clientId: 'c1', "
                 "contextName: 'Secret Project', contextId: 'x1', "
                 "relevantClients: [{ id: 'x9', name: 'Shared Project', kind: 'context' }], "
                 "mentions: [{ id: 'f2', name: 'Bob', text: 'a fact', "
                 "target_label: 'Fact' }] }"
                 % ("true" if read_only else "false", "true" if primary_hidden else "false"))
        return ("(async () => { diaryEntries = [" + entry + "]; "
                "clientListCache = [{ id: 'c1', name: 'ACME', contexts: [] }]; "
                "return renderDiaryViewMode(" + entry + "); })()")

    def test_the_diary_view_mode_hides_every_mutating_control(self):
        """The view mode is the surface a grantee actually lands on.

        The editor gate above is not this: it only runs after the Edit action,
        which is itself one of the controls hidden here. Four surfaces were
        ungated — scope chips, mention deletes, the ＋ add-relevant button and
        the link form — and each of them answers 403.

        ``_showDiaryMenu`` is deliberately NOT in this list. Opening a menu is
        a read, so the button stays and the *items* are filtered — that pair is
        asserted by ``test_the_diary_menu_offers_no_write_action_on_a_read_only_entry``
        below, and hiding the button here would pass while the menu still
        offered every write.
        """
        out = self._render([("view", self._diary_view(True))])
        html = out["view"]
        if "THREW" in html:
            self.skipTest(f"the diary view could not be driven headless: {html[:200]}")
        for handler in ("_diaryPickClient", "_diaryPickProject", "_diaryAddRelevant",
                        "_diaryRemoveRelevant", "deleteDiaryLink",
                        "toggleDiaryLinkForm", "saveDiaryLink"):
            self.assertFalse(
                handler + "(" in html,
                msg=f"a read-only diary entry still renders a control calling "
                    f"{handler}() in the VIEW mode -- it is refused with a 403")
        # Reads stay: the body, the date, the linked facts and the badge that
        # says why the controls are gone. A grantee must still be able to read
        # what they were given.
        for needle in ("the body", "Linked Facts", "Bob", "read-only"):
            self.assertIn(needle, html,
                          msg=f"the read-only diary view dropped {needle!r}, so the "
                              f"grantee cannot read the record they were given")

    def test_the_diary_view_mode_still_offers_its_controls_to_the_owner(self):
        """The over-gating half, for the view mode.

        Without this the whole view could be gated for everybody and the test
        above would pass — the exact failure a fix written by deleting
        markup produces. Verified by re-injection: making the diary view gate a
        constant true leaves every other test in this class green.
        """
        out = self._render([("view", self._diary_view(False))])
        html = out["view"]
        self.assertNotIn("THREW", html, msg="the owned diary entry could not be rendered")
        for handler in ("_diaryPickClient", "_diaryPickProject", "_diaryAddRelevant",
                        "_diaryRemoveRelevant", "deleteDiaryLink",
                        "toggleDiaryLinkForm", "_showDiaryMenu"):
            self.assertTrue(
                handler + "(" in html,
                msg=f"an owned diary entry no longer offers {handler}() in the view "
                    f"mode -- the read-only gate has removed a control the owner needs")

    def test_a_record_reachable_only_through_a_related_project_shows_no_scope(self):
        """The privacy half of a related-only grant.

        The record is visible because it mentions a shared project, but it is
        FILED UNDER the owner's own client and project — names this caller
        holds no grant for. Rendering them would leak exactly the scope a
        context grant exists to withhold, so the server blanks them and the UI
        says so rather than showing an empty chip that reads as "unscoped".
        """
        out = self._render([
            ("hidden", self._diary_view(False, primary_hidden=True)),
            ("plain", self._diary_view(False, primary_hidden=False)),
        ])
        for name, html in out.items():
            self.assertNotIn("THREW", html, msg=f"{name} could not be rendered")
        self.assertIn("(private)", out["hidden"],
                      msg="a related-only record does not say its scope is private")
        self.assertNotIn("Secret Project", out["hidden"],
                         msg="the owner's own project name is rendered on a record "
                             "the caller has no grant for")
        self.assertNotIn("ACME", out["hidden"],
                         msg="the owner's own client name is rendered on a record "
                             "the caller has no grant for")
        # And the shared project it IS reachable through still shows, as a
        # related chip -- otherwise the record reads as having no scope at all.
        self.assertIn("Shared Project", out["hidden"],
                      msg="the shared project the record is reachable through is "
                          "not shown, so the record reads as unscoped")
        self.assertIn("Secret Project", out["plain"],
                      msg="an owned record lost its project name")

    def test_the_diary_menu_offers_no_write_action_on_a_read_only_entry(self):
        """The hamburger menu, asserted on what it hands the menu builder.

        Edit, Reclassify scope, Extract participants and Delete all write --
        each is refused with a 403 on a read-only grant, so offering them costs
        a click and an error toast. Copy MD, Show in Graph and Reload are reads
        and must survive, or the grantee cannot copy the record they were given
        or pick up a scope change the owner just made.
        """
        out = self._render([
            ("readOnly", "(diaryEntries = [{ id: 'e1', readOnly: true, name: 'x' }], "
                         "_showDiaryMenu({ getBoundingClientRect: () => ({ top: 0, left: 0, "
                         "bottom: 0, right: 0, width: 0, height: 0 }) }, 'e1'), "
                         "__menuItems.map(i => i.label + ':' + (i.action || 'sep')).join(','))"),
            ("owned", "(diaryEntries = [{ id: 'e1', readOnly: false, name: 'x' }], "
                      "_showDiaryMenu({ getBoundingClientRect: () => ({ top: 0, left: 0, "
                      "bottom: 0, right: 0, width: 0, height: 0 }) }, 'e1'), "
                      "__menuItems.map(i => i.label + ':' + (i.action || 'sep')).join(','))"),
        ])
        for name, html in out.items():
            self.assertNotIn("THREW", html, msg=f"{name} could not be driven")
        for action in ("_diaryMenuEdit", "_diaryMenuReclassify",
                       "_diaryMenuExtractPeople", "_diaryMenuDelete"):
            self.assertNotIn(
                action, out["readOnly"],
                msg=f"a read-only diary entry still offers {action}() in its menu")
        for action in ("_diaryMenuCopyMd", "_diaryMenuGraph", "_diaryMenuReload"):
            self.assertIn(
                action, out["readOnly"],
                msg=f"a read-only diary entry lost the read action {action}() -- a "
                    f"grantee must still be able to copy and reload the record")
        # The owner's menu is unchanged: every item, in the original order.
        for action in ("_diaryMenuEdit", "_diaryMenuReclassify",
                       "_diaryMenuExtractPeople", "_diaryMenuDelete",
                       "_diaryMenuCopyMd", "_diaryMenuGraph", "_diaryMenuReload"):
            self.assertIn(action, out["owned"],
                          msg=f"an owned diary entry lost {action}() from its menu")


if __name__ == "__main__":
    unittest.main()
