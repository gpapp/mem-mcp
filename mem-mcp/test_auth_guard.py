"""
test_auth_guard.py – the request path's trust chain.

The store in sessions.py decides *whether a credential is valid*. This suite
decides *which credential wins* when a request carries more than one, which is
where the security properties actually live and where a test that only checks
the store would be testing the easy half.

None of this can be imported: gui.py needs fastapi, and common.py needs httpx,
neither of which exists on this host. So the two functions that make the
decisions are lifted out with `ast.get_source_segment` and exec'd against stubs
— which tests the shipping code rather than a copy of it, and is the same trick
the other suites in this repo use. The stubs are *stricter* than the real thing
where it matters: `resolve_psk` is a plain dict lookup, so a test that passed a
key the real store would have rejected still fails here for the right reason.

Run:  python3 -m unittest -v test_auth_guard.py
"""

import ast
import asyncio
import base64
import os
import types
import unittest
from urllib.parse import urlsplit

import sessions

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _read(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as handle:
        return handle.read()


def _lift(relative_path, node_name, namespace):
    """Exec the real definition of `node_name` out of a module we cannot import.

    A hand-copied version of these functions would keep passing after the
    originals were broken, which is the failure mode this technique exists to
    avoid: the source that runs here *is* the source that ships.
    """
    source = _read(*relative_path.split("/"))
    tree = ast.parse(source)
    for node in tree.body:
        # AsyncFunctionDef as well as FunctionDef: auth_guard is `async def`, and
        # matching only the sync kind makes it look undefined.
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == node_name:
            segment = ast.get_source_segment(source, node)
            exec(compile(segment, relative_path, "exec"), namespace)
            return namespace[node_name]
    raise AssertionError(f"{node_name} is not defined in {relative_path}")


# ---------------------------------------------------------------------------
# extract_user_from_headers — precedence
# ---------------------------------------------------------------------------

class HeaderPrecedenceTests(unittest.TestCase):
    """Whichever source names the user, the verified one must win.

    The failure this guards is not "the wrong username was returned". It is
    that a header nobody verified outranks a credential that was checked, and
    that is invisible until someone sets the header.
    """

    @classmethod
    def setUpClass(cls):
        cls.known = {"mvk_real_key": "alice", "mvk_bobs_key": "bob"}
        namespace = {
            "base64": base64,
            # A module-level import, so stubbing it here actually takes effect;
            # an `import sessions` inside the function would bind the real one.
            "sessions": _StubStore(cls.known),
        }
        # staticmethod, or `self.extract` binds it as a method and every call
        # arrives with an extra `self`.
        cls.extract = staticmethod(_lift("common.py", "extract_user_from_headers", namespace))

    @staticmethod
    def basic(user, password="secret"):
        raw = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {raw}"}

    def test_the_verified_identity_header_wins_over_everything(self):
        headers = self.basic("attacker")
        headers["x-vault-user"] = "alice"
        headers["Authorization"] = "Bearer mvk_bobs_key"
        headers["Remote-User"] = "attacker"
        self.assertEqual(self.extract(headers), "alice")

    def test_a_valid_bearer_key_resolves_to_its_owner(self):
        self.assertEqual(
            self.extract({"Authorization": "Bearer mvk_real_key"}), "alice")

    def test_bearer_is_case_insensitive_on_the_scheme(self):
        # Clients are not careful about scheme case, and a 401 that only
        # happens when a client writes `bearer` is a bug report, not a policy.
        self.assertEqual(
            self.extract({"Authorization": "bearer mvk_real_key"}), "alice")

    def test_an_unknown_bearer_key_yields_no_user_at_all(self):
        self.assertEqual(
            self.extract({"Authorization": "Bearer mvk_guessed"}), "anonymous")

    def test_a_bearer_key_is_not_fallback_checked_as_a_username(self):
        # If Bearer missed and the function fell through to the proxy headers,
        # a bad key would fall back to trusting Remote-User. A key that is
        # wrong must be an answer, not an absence of one.
        headers = {"Authorization": "Bearer mvk_guessed", "Remote-User": "victim"}
        self.assertEqual(self.extract(headers), "anonymous")

    def test_basic_still_works_so_existing_clients_keep_connecting(self):
        self.assertEqual(self.extract(self.basic("alice")), "alice")

    def test_proxy_identity_headers_are_still_the_last_resort(self):
        self.assertEqual(self.extract({"Remote-User": "alice"}), "alice")
        self.assertEqual(self.extract({"X-Forwarded-User": "alice"}), "alice")

    def test_header_names_are_case_insensitive(self):
        self.assertEqual(self.extract({"AUTHORIZATION": "Bearer mvk_real_key"}), "alice")
        self.assertEqual(self.extract({"X-Vault-User": "alice"}), "alice")

    def test_no_evidence_is_anonymous_rather_than_a_guess(self):
        self.assertEqual(self.extract({}), "anonymous")

    def test_a_malformed_authorization_header_does_not_raise(self):
        for value in ("Bearer", "Bearer ", "Basic", "Basic not-base64!!",
                      "Basic " + base64.b64encode(b"no-colon").decode()):
            self.assertEqual(self.extract({"Authorization": value}), "anonymous",
                             msg=f"{value!r} did not fall through cleanly")


class _StubStore:
    """Just enough of sessions.resolve_psk to drive the extractor."""

    def __init__(self, known):
        self.known = known

    def resolve_psk(self, key, **kwargs):
        user = self.known.get(key)
        return None if user is None else {"user_id": user, "id": "k"}


# ---------------------------------------------------------------------------
# McpAuthGuard – what actually answers a request to /mcp
# ---------------------------------------------------------------------------

class GuardCase(unittest.TestCase):
    """Drives the real guard class with real ASGI messages.

    One credential is accepted on /mcp — a Bearer access key — so the owner a
    test resolves also proves which credential was honoured. An assertion on
    the status code alone would pass if the guard picked the wrong credential
    and then 200'd anyway, and the two credentials that are *refused* are the
    reason that matters: a test has to be able to say "a correct password is
    still refused", which is indistinguishable from "refused" without also
    checking that the refusal is for the right reason.
    """

    @classmethod
    def setUpClass(cls):
        cls.known_keys = {"mvk_alice_key": "alice", "mvk_bob_key": "bob"}
        namespace = {
            "base64": base64,
            "logging": _NullLogger(),
            # The real module: it is stdlib-only and importable, and the guard
            # reads VAULT_USER_HEADER off it. resolve_psk is stubbed separately
            # below because the real one needs a database.
            "vault_sessions": sessions,
            "resolve_psk": lambda key, **kw: (
                None if key not in cls.known_keys
                else {"user_id": cls.known_keys[key], "id": "k"}),
        }
        cls.Guard = _lift("gui.py", "McpAuthGuard", namespace)
        # Still needed, to build the header a refusal test has to send. Nothing
        # in the guard verifies it any more, and that is the point.
        cls.basic = staticmethod(lambda user, pw: base64.b64encode(
            f"{user}:{pw}".encode()).decode())

    def call(self, headers=(), session_user=None, path="/mcp"):
        guard = self.Guard(self._echo_app)
        scope = {"type": "http", "path": path, "method": "POST",
                 "headers": [(k.encode() if isinstance(k, str) else k,
                              v.encode() if isinstance(v, str) else v)
                             for k, v in headers],
                 "query_string": b""}
        if session_user is not None:
            scope["session"] = {"user": session_user}
        captured = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            captured.setdefault("messages", []).append(message)

        asyncio.run(guard(scope, receive, send))
        start = next(m for m in captured["messages"]
                     if m["type"] == "http.response.start")
        return start["status"], bool(scope.get("app_reached")), scope

    @staticmethod
    async def _echo_app(scope, receive, send):
        # The guard awaits its app, so the stand-in has to be a coroutine
        # function; a sync one fails as "NoneType can't be awaited", which reads
        # like a defect in the guard rather than in the harness. It also has to
        # answer, because the guard is a pass-through and a pass-through that is
        # tested against an app which says nothing proves nothing.
        scope["app_reached"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


class _NullLogger:
    """Stands in for the logging module, and for one of its loggers."""

    def getLogger(self, *a, **k):
        return self

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class McpAuthGuardTests(GuardCase):

    def test_a_request_with_no_credential_is_refused(self):
        status, reached, _ = self.call()
        self.assertEqual(status, 401)
        self.assertFalse(reached, "The MCP app must never see an unauthenticated request")

    def test_a_valid_access_key_is_accepted(self):
        status, reached, scope = self.call([("Authorization", "Bearer mvk_alice_key")])
        self.assertEqual(status, 200)
        self.assertTrue(reached)
        self.assertEqual(self.vault_user(scope), "alice")

    def test_an_unknown_access_key_is_refused(self):
        status, reached, _ = self.call([("Authorization", "Bearer mvk_guessed_key")])
        self.assertEqual(status, 401)
        self.assertFalse(reached)

    def test_an_empty_or_malformed_bearer_is_refused(self):
        for value in ("Bearer", "Bearer ", "bearer"):
            status, reached, _ = self.call([("Authorization", value)])
            self.assertEqual(status, 401, msg=f"{value!r} was not refused")
            self.assertFalse(reached)

    def test_a_session_cookie_is_refused(self):
        # The dashboard keeps its sessions; /mcp does not use them. A cookie is
        # a bearer credential the browser replays on its own, so accepting one
        # here hands every MCP client something that cannot be scoped to a
        # device — and cannot be revoked without ending the user's own session.
        status, reached, _ = self.call(session_user="alice")
        self.assertEqual(status, 401)
        self.assertFalse(reached, "A session cookie must not authenticate /mcp")

    def test_basic_auth_is_refused_even_with_the_correct_password(self):
        # Not the mechanism that is refused — Basic is per-call and stateless,
        # which is the same property a key has. It is the credential: the
        # account password, so it also unlocks /api/*, it rotates only when a
        # human changes it, and it cannot be revoked for one lost laptop
        # without changing it for everyone. Keeping it here would have made
        # every access key revocable in name only.
        status, reached, _ = self.call(
            [("Authorization", f"Basic {self.basic('bob', 'hunter2')}")])
        self.assertEqual(status, 401)
        self.assertFalse(reached,
                         "A correct account password must not authenticate /mcp")

    def test_basic_auth_with_the_wrong_password_is_refused(self):
        status, reached, _ = self.call(
            [("Authorization", f"Basic {self.basic('bob', 'wrong')}")])
        self.assertEqual(status, 401)
        self.assertFalse(reached)

    def test_a_basic_header_for_an_unknown_user_is_refused(self):
        status, _, _ = self.call(
            [("Authorization", f"Basic {self.basic('mallory', 'x')}")])
        self.assertEqual(status, 401)

    def test_any_other_authorization_scheme_is_refused(self):
        # The guard used to dispatch on the scheme, so "bearer " was the only
        # string it looked at. Anything else simply fell through to the
        # session, which is gone — so these must be refused rather than
        # reaching the app unidentified.
        for value in ("Digest response=abc", "Token mvk_alice_key",
                      "Negotiate dG9rZW4=", "mvk_alice_key"):
            status, reached, _ = self.call([("Authorization", value)])
            self.assertEqual(status, 401, msg=f"{value!r} was not refused")
            self.assertFalse(reached)

    def test_a_session_cookie_does_not_override_a_valid_access_key(self):
        # Both are present and they name different people. The cookie is never
        # read, so the key decides — pinning that makes "the cookie is ignored"
        # a statement rather than an accident of which check ran first.
        status, _, scope = self.call(
            [("Authorization", "Bearer mvk_bob_key")], session_user="alice")
        self.assertEqual(status, 200)
        self.assertEqual(self.vault_user(scope), "bob")

    def test_an_invalid_key_does_not_fall_back_to_a_session_cookie(self):
        # The old ladder fell through, so a revoked or guessed key on a request
        # that also carried a cookie still authenticated as the cookie's owner.
        # There is nothing to fall through to now, and that has to be pinned:
        # "it still works because of the cookie" is indistinguishable from
        # "the key was accepted" unless the cookie names someone else.
        status, reached, _ = self.call(
            [("Authorization", "Bearer mvk_guessed")], session_user="alice")
        self.assertEqual(status, 401)
        self.assertFalse(reached)

    def test_a_spoofed_identity_header_is_replaced_not_trusted(self):
        status, _, scope = self.call(
            [("X-Vault-User", "victim"), ("Authorization", "Bearer mvk_bob_key")])
        self.assertEqual(status, 200)
        self.assertEqual(self.vault_user(scope), "bob")
        self.assertEqual(
            [v for k, v in scope["headers"] if k.lower() == b"x-vault-user"],
            [b"bob"],
            "There must be exactly one identity header, and it must be the verified one",
        )

    def test_the_401_names_the_only_way_in_and_advertises_it(self):
        # A bare 401 on /mcp is indistinguishable from a wrong URL, a proxy
        # misconfiguration, or a revoked key. The body and the
        # WWW-Authenticate header are the only places the remedy can live — and
        # the header used to say Basic, which is a credential this endpoint no
        # longer accepts at all, so it actively pointed at the wrong answer.
        guard = self.Guard(self._echo_app)
        scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": [],
                 "query_string": b""}
        captured = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            captured.append(message)

        asyncio.run(guard(scope, receive, send))
        start = next(m for m in captured if m["type"] == "http.response.start")
        headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        body = b"".join(m.get("body", b"") for m in captured
                        if m["type"] == "http.response.body")
        text = body.decode()
        self.assertIn("Access Keys", text)
        self.assertIn("Bearer mvk_", text)
        # Say what is refused too, or the operator re-adds Basic and gets a 401
        # again with nothing in the logs to say why it worked yesterday.
        self.assertIn("session cookie", text)
        self.assertIn("Basic", text)
        self.assertTrue(headers["www-authenticate"].startswith("Bearer"),
                        f"WWW-Authenticate must offer Bearer, got {headers['www-authenticate']!r}")

    def test_a_websocket_upgrade_is_not_gated_on_http_logic(self):
        # Passing through unchecked is deliberate: the MCP app here is
        # transport="http", and a pass-through is visible to a test whereas a
        # silently-broken websocket is not.
        reached = []

        async def app(scope, receive, send):
            reached.append(True)

        guard = self.Guard(app)

        async def receive():
            return {}

        async def send(message):
            pass

        asyncio.run(guard({"type": "websocket", "headers": []}, receive, send))
        self.assertEqual(reached, [True])

    @staticmethod
    def vault_user(scope):
        for key, value in scope["headers"]:
            if key.lower() == b"x-vault-user":
                return value.decode()
        return None


# ---------------------------------------------------------------------------
# The pieces that make the guard reachable
# ---------------------------------------------------------------------------

class WiringTests(unittest.TestCase):
    """The parts that are not in the guard itself but decide whether it runs."""

    @classmethod
    def setUpClass(cls):
        cls.server = _read("server.py")
        cls.gui = _read("gui.py")
        cls.nginx = _read("..", "nginx_snippet.conf")
        cls.dashboard = _read("templates", "dashboard.html")

    def test_the_mcp_app_is_mounted_behind_the_guard(self):
        self.assertIn("McpAuthGuard(mcp_app)", self.server)

    def test_nginx_no_longer_authenticates_the_mcp_route(self):
        # Not cosmetic: nginx can only check a user and password against a file
        # on the host, so with auth_basic still present a Bearer PSK is rejected
        # before it is ever forwarded and the guard below never runs.
        block = self.nginx.split("location /mem-mcp/mcp")[1].split("location")[0]
        # Comments are stripped first: this file explains at length why
        # $remote_user is gone, and a token search would read its own
        # explanation as the thing it forbids.
        directives = "\n".join(
            line.split("#")[0] for line in block.splitlines()
        ).lower()
        self.assertNotIn("auth_basic", directives,
                         "auth_basic on the MCP location blocks access keys entirely")
        self.assertNotIn("remote-user", directives,
                         "$remote_user is empty now that nothing authenticates it")

    def test_the_session_middleware_is_the_persistent_one(self):
        self.assertIn("VaultSessionMiddleware", self.gui)
        self.assertNotIn("starlette.middleware.sessions", self.gui,
                         "A cookie-resident session cannot survive a rebuild")

    def test_login_clears_the_session_before_writing_the_user(self):
        # clear() first is what makes the middleware mint a *new* id rather
        # than upgrading the one the browser already had.
        body = _function_source("gui.py", "api_login")
        self.assertLess(body.index("session.clear()"), body.index('session["user"]'))

    def test_no_page_can_still_print_a_password(self):
        for name in ("dashboard", "landing"):
            source = _read("templates", f"{name}.html")
            self.assertNotIn("AUTH_PASS", source,
                             f"{name}.html still renders a password")
            self.assertNotIn("AUTH_BASE64", source,
                             f"{name}.html still renders a base64 Basic credential")

    def test_the_bridge_takes_its_key_from_the_environment(self):
        # A downloaded script with a key baked in is a plain-text credential
        # that lands in backups, screen shares and every copy of the file.
        bridge = _read("templates", "mcp-bridge.mjs")
        self.assertIn("MEM_VAULT_PSK", bridge)
        self.assertNotIn("Basic {{", bridge)

    def test_access_keys_are_managed_inside_the_setup_page(self):
        setup = self.dashboard.split('<div id="page-setup"')[1]
        graph = self.dashboard.split('<div id="page-graph"')[0]
        # The section belongs after page-graph in the file; what matters is
        # that it is before page-setup closes, i.e. inside it.
        self.assertIn('id="psk-list"', setup)
        self.assertIn("createPSK()", setup)
        self.assertIn("revokePSK(", setup)
        self.assertNotIn('id="psk-list"', graph)

    def test_the_access_keys_list_is_loaded_once_per_visit(self):
        # Same shape as loadSavepoints: init() and switchTab('setup') both fire
        # on first paint, so without the gate that is two requests.
        self.assertIn("psksLoaded = false", self.dashboard)
        self.assertIn("if (psksInFlight) return psksInFlight", self.dashboard)
        self.assertIn("if (psksLoaded && !force) return Promise.resolve()", self.dashboard)

    def test_the_psk_renderer_does_not_refetch_its_own_input(self):
        # A renderer that refetches loops: render -> fetch -> render. The
        # backup panel measured 500+ requests for one finished backup this way.
        body = _js_function_source("templates/dashboard.html", "renderPSKs")
        self.assertNotIn("api.get", body)
        self.assertNotIn("loadPSKs", body)


class SetupPageAuthGuidanceTests(unittest.TestCase):
    """The Setup page must tell a user how to authenticate, not just where.

    The one thing a user cannot infer from a 401 is which of three mechanisms
    failed. Observed while writing this: a client reported "Incompatible auth
    server: does not support dynamic client registration", which is what an
    MCP client says when it gets a 401, assumes the server speaks OAuth, and
    finds that it does not. The actual cause was a proxy answering with its own
    Basic challenge before the key ever arrived. Nothing in the UI said either
    of those things, so the only way to find it was to read the source.

    These are guards on guidance text, which is unusual, and the reason is that
    the text is the fix. Each assertion below names a failure a user cannot
    self-diagnose from the error they are shown.
    """

    @classmethod
    def setUpClass(cls):
        cls.dashboard = _read("templates/dashboard.html")
        cls.landing = _read("templates/landing.html")

    def _between(self, start, end):
        """The slice between two markers.

        Asserting a word appears somewhere in a 5700-line template is the
        `assertIn`-over-the-whole-file trap: it passes on any page that happens
        to contain the word, which is not the same as the guidance existing.
        Each assertion below is scoped to the block that owns the text.
        """
        i = self.dashboard.find(start)
        self.assertTrue(i > 0, msg=f"missing marker: {start!r}")
        j = self.dashboard.find(end, i)
        self.assertTrue(j > i, msg=f"missing closing marker after {start!r}: {end!r}")
        return self.dashboard[i:j]

    def test_the_intro_says_the_key_is_sent_on_every_request(self):
        # "every request" is the property that explains why a client that
        # worked once can still fail later, and why this is not a login.
        intro = self._between("<h2 style=\"margin-top:0;\">🔌 MCP Setup",
                              "setup-grid")
        self.assertIn("every", intro.lower())
        self.assertIn("no OAuth login", intro,
                      msg="the intro must say there is nothing to log into")

    def test_the_oauth_misdiagnosis_is_named_on_the_page(self):
        # The exact string the client prints. A user searching their error
        # message must land on the paragraph that explains it, and it must say
        # the cause is a missing header rather than blaming OAuth.
        block = self._between("When a client refuses to connect",
                              "</div>\n\n    <div style=\"margin-top: 2.5rem")
        lower = block.lower()
        self.assertIn("dynamic client registration", lower)
        self.assertIn("missing header", lower)

    def test_the_surface_table_names_all_three_paths(self):
        # One credential per surface is the rule the guard enforces, and the
        # table is the only place a user learns it before trying.
        table = self._between("Which credential goes where",
                              "any other path")
        for path in ("/mcp", "/gui", "/api/*"):
            self.assertIn(path, table,
                          msg=f"the credential table does not cover {path}")

    def test_the_mcp_row_says_a_cookie_and_basic_are_refused(self):
        # Scoped to the /mcp row alone: "refused" appearing anywhere on the page
        # says nothing about whether the row that matters says it.
        table = self._between("Which credential goes where", "any other path")
        mcp_row = table.split("/gui, /api/*")[0]
        self.assertIn("refused", mcp_row.lower(),
                      msg="the /mcp row must say the other credentials are refused")
        self.assertIn("Bearer", mcp_row)

    def test_the_page_warns_about_a_proxy_challenge(self):
        # The failure this section was written for: nginx answers auth_basic
        # itself and never forwards the key, so the app is never even asked.
        block = self._between("When a client refuses to connect", "Local Proxy Bridge")
        self.assertIn("auth_basic", block)
        # And the tell the user can actually observe, not just the mechanism.
        self.assertIn("Basic", block)

    def test_a_key_can_be_tested_without_a_client(self):
        # The curl snippet is the diagnostic, not decoration: it distinguishes a
        # bad key (JSON 401 from this app) from a blocked request (HTML 401
        # from the proxy) in one step, which no client-side message does.
        # The paragraph explaining what to read in the response sits after the
        # snippet, so the block runs to the end of the section rather than to
        # the first </pre>.
        block = self._between("Check it by hand", "Local Proxy Bridge")
        self.assertIn("curl", block)
        self.assertIn("-X POST", block)
        # -i is not decoration. The block tells the user to read the
        # WWW-Authenticate line, and without -i curl prints no response
        # headers, so the diagnostic silently stops working while this test
        # stays green. Removing -i re-injection did not fail until this
        # assertion existed.
        self.assertIn("curl -i ", block,
                      msg="-i must be there or the WWW-Authenticate line is invisible")
        self.assertIn("WWW-Authenticate", block)

    def test_both_snippets_are_filled_when_a_key_is_created(self):
        # A placeholder the user has to hand-edit is where a wrong header comes
        # from. Both snippets are filled from the same key at creation time.
        body = _js_function_source("templates/dashboard.html", "showNewPSK")
        self.assertIn("mcp-connect-cmd", body)
        self.assertIn("mcp-connect-json", body)
        # Both must carry the same Bearer header, or the two paths disagree.
        self.assertIn("Authorization: Bearer ' + key", body)
        self.assertIn("headers: { Authorization: 'Bearer ' + key }", body)

    def test_the_json_snippet_is_written_as_text_not_markup(self):
        # showNewPSK is the one place a secret is written into the page. The
        # key is browser-generated so it cannot contain markup today; this is
        # not a reason to hand it a parser.
        #
        # This assertion pins the assignment, not the word "textContent".
        # The first version searched from the getElementById call, whose window
        # covers three unrelated textContent writes plus a comment mentioning
        # both words -- so rewriting the secret-bearing write as innerHTML
        # passed green. Re-injection is what caught it.
        body = _js_function_source("templates/dashboard.html", "showNewPSK")
        self.assertIn("json.textContent = JSON.stringify({", body,
                      msg="the key must reach the snippet through textContent")
        self.assertNotIn("json.innerHTML", body,
                         msg="the key must never be written through a markup parser")

    def test_the_config_snippet_is_valid_json_once_rendered(self):
        # The snippet is emitted as literal markup for the placeholder state and
        # rewritten by JS. Hand-editing the placeholder JSON is the failure the
        # rewrite exists to prevent, so it has to parse as-is: a missing comma
        # or a stray backslash in the template is invisible until a client
        # silently refuses the config.
        try:
            import json
        except ImportError:
            self.skipTest("json is stdlib but unavailable here")
        raw = self.dashboard.split('<code id="mcp-connect-json">')[1]
        raw = raw.split("</code>")[0]
        placeholder = "mvk_REPLACE_ME"
        text = (raw.replace("&lt;", "<").replace("&gt;", ">")
                   .replace("&amp;", "&").replace("&quot;", '"')
                   .replace("{{MCP_URL}}", "/mem-mcp/mcp")
                   .replace("{{PSK_PREFIX}}", placeholder)
                   # The template shows a prefix plus a written-out placeholder,
                   # which is not a key; substitute the whole thing so what is
                   # parsed is the shape showNewPSK actually emits.
                   .replace(placeholder + "<your-access-key>", placeholder))
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            self.fail(f"the config-file snippet is not valid JSON: {exc}\n{text}")
        server = parsed["mcpServers"]["memory-vault"]
        self.assertEqual(server["url"], "/mem-mcp/mcp")
        self.assertEqual(server["headers"]["Authorization"],
                         f"Bearer {placeholder}")

    def test_the_landing_page_creates_the_key_before_asking_for_the_command(self):
        # Step order is the whole content of that page. Telling someone to run
        # a command containing a key that step 2 then tells them to create is
        # backwards, and the placeholder is what they would run.
        create = self.landing.find("Create an access key")
        run = self.landing.find("claude mcp add")
        self.assertTrue(create > 0, msg="the landing page lost the create-a-key step")
        self.assertTrue(run > 0, msg="the landing page lost the connect command")
        self.assertLess(create, run,
                        msg="the key must be created before the command that needs it")


def _function_source(relative_path, name):
    """The source of one top-level Python function, by name."""
    source = _read(*relative_path.split("/"))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"{name} is not defined in {relative_path}")


def _js_function_source(relative_path, name):
    """The source of one top-level JS function, by brace matching.

    The dashboard is a template, so `ast` cannot read it, and a substring search
    for the function name would match the call sites in the onclick handlers --
    which is how a guard on a renderer ends up asserting about its caller.
    Matching the body is what makes this about the body.
    """
    source = _read(*relative_path.split("/"))
    marker = f"function {name}("
    start = source.find(marker)
    if start == -1:
        raise AssertionError(f"{name} is not defined in {relative_path}")
    depth, index = 0, source.index("{", start)
    begin = index
    while index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise AssertionError(f"unbalanced braces in {name}")


def _module_node(relative_path, node_name):
    """The AST node for a top-level def/class, for tests that must not match prose."""
    tree = ast.parse(_read(*relative_path.split("/")))
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == node_name:
            return node
    raise AssertionError(f"{node_name} is not defined in {relative_path}")


def _lift_undecorated(relative_path, node_name, namespace):
    """As `_lift`, but drops decorators.

    `@web_app.middleware("http")` is how these functions are registered, and
    `ast.get_source_segment` includes it — so exec'ing the segment as written
    would call `web_app.middleware` on a stub and register nothing, returning
    the *undecorated* function. Which happens to be what a test wants, but only
    by accident, and only for this decorator. Dropping it explicitly means the
    test reads as "the function itself", and a future decorator on the function
    under test does not silently change what is being exercised.
    """
    source = _read(*relative_path.split("/"))
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == node_name:
            # Rebuild the node without its decorators, keeping its kind -- an
            # AsyncFunctionDef reconstructed as a FunctionDef silently stops
            # being awaitable and fails as "NoneType can't be awaited", which
            # reads like a defect in the middleware rather than in the harness.
            bare = type(node)(
                name=node.name, args=node.args, body=node.body,
                decorator_list=[], returns=node.returns, type_comment=None,
                lineno=node.lineno, col_offset=node.col_offset,
                # end_lineno/end_col_offset are not optional here: without them
                # get_source_segment returns None and compile() rejects it. The
                # span runs from `async def` (which is what node.lineno points
                # at, not the decorator) to the end of the body, so the
                # decorator is excluded by construction rather than by slicing.
                end_lineno=node.end_lineno, end_col_offset=node.end_col_offset)
            segment = ast.get_source_segment(source, bare)
            exec(compile(segment, relative_path, "exec"), namespace)
            return namespace[node_name]
    raise AssertionError(f"{node_name} is not defined in {relative_path}")


# ---------------------------------------------------------------------------
# /gui and /api/* — the OTHER half of the trust chain
# ---------------------------------------------------------------------------

def _leaky_extract(headers):
    """What extract_user_from_headers does: trust whatever names a user.

    Prefixed to make its role obvious at the injection site, because a stub that
    looks like a helper is a stub somebody will "clean up" later.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    if lowered.get("x-vault-user"):
        return lowered["x-vault-user"].strip()
    auth = lowered.get("authorization", "")
    if auth.lower().startswith("basic "):
        parts = auth.split()
        if len(parts) == 2:
            decoded = base64.b64decode(parts[1]).decode("utf-8", "replace")
            if ":" in decoded:
                return decoded.split(":", 1)[0]
    for name in ("remote-user", "x-remote-user", "x-user", "x-forwarded-user"):
        if lowered.get(name):
            return lowered[name]
    return "anonymous"


class _Request:
    """The three attributes auth_guard and _user actually touch."""

    def __init__(self, path, headers=None, session=None):
        self.url = types.SimpleNamespace(path=path)
        self.headers = headers or {}
        self.session = {} if session is None else session
        self.state = types.SimpleNamespace()


class _Response:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.status_code = kwargs.get("status_code")


class ApiAuthTests(unittest.TestCase):
    """The GUI/API gate, which is a *different* gate from McpAuthGuard.

    Everything asserted here was reachable with
    `Authorization: Basic base64(alice:anything)`. `_check_session_auth`
    decoded the header, took the username and discarded the password, so
    `_require_user` in all ~60 handlers was a "did somebody hand us a username"
    check rather than an authentication check. McpAuthGuard verifies the same
    header — but it wraps only the MCP mount, and nginx has no auth_basic on
    the GUI location, so nothing verified it on this path.

    These are lifted from gui.py rather than reimplemented, because a
    hand-written copy of an auth check is worth exactly nothing.
    """

    PASSWORDS = {("alice", "correct-horse"), ("bob", "hunter2")}
    KEYS = {"mvk_alice_key": "alice", "mvk_bob_key": "bob"}

    @classmethod
    def setUpClass(cls):
        namespace = {
            "base64": base64,
            "logging": _NullLogger(),
            "HTTPException": type("HTTPException", (Exception,), {}),
            "Request": object,          # only ever an annotation
            # `mem` carries a faithful extract_user_from_headers, not just
            # BASE_URL. Without it, re-introducing the header fallback in `_user`
            # fails as an AttributeError on the stub -- the suite goes red, but
            # on a gap in the harness rather than on the property under test,
            # and a defect that read the headers inline would slip past
            # entirely. A stub must be complete enough that the *defect* is what
            # the assertion sees.
            "mem": types.SimpleNamespace(
                BASE_URL="/mem-mcp",
                extract_user_from_headers=_leaky_extract),
            "JSONResponse": _Response,
            "RedirectResponse": _Response,
            "resolve_psk": lambda key, **kw: (
                None if key not in cls.KEYS else {"user_id": cls.KEYS[key]}),
            # Records every verification so a test can assert *that* it happened
            # and not merely that the outcome was right.
            "_verify_htpasswd": cls._verify,
        }
        cls.verifications = []
        ns = dict(namespace)
        ns["_verify_htpasswd"] = lambda u, p: ApiAuthTests._verify(u, p)
        cls.check = staticmethod(_lift("gui.py", "_check_session_auth", ns))
        # staticmethod, or `self.user` binds it and every call arrives with
        # an extra self -- the same trap as HeaderPrecedenceTests.extract.
        cls.user = staticmethod(_lift("gui.py", "_user", ns))
        ns2 = dict(ns)
        ns2["_check_session_auth"] = cls.check
        cls.auth_guard = staticmethod(
            _lift_undecorated("gui.py", "auth_guard", ns2))

    @classmethod
    def _verify(cls, username, password):
        cls.verifications.append((username, password))
        return (username, password) in cls.PASSWORDS

    def setUp(self):
        self.verifications.clear()

    @staticmethod
    def basic(user, password):
        raw = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {raw}"}

    def run_guard(self, path="/api/memories", headers=None, session=None):
        """Run auth_guard over one request. Returns (verdict, request, reached).

        `verdict` is whatever the middleware returned instead of calling on —
        a _Response stub for a 401 or a redirect, None when it passed through.
        """
        request = _Request(path, headers, session)
        reached = []

        async def call_next(_request):
            reached.append(_request)
            return "passed through"

        verdict = asyncio.run(self.auth_guard(request, call_next))
        return verdict, request, bool(reached)

    # -- the password is actually verified ---------------------------------

    def test_a_correct_basic_password_is_accepted(self):
        verdict, request, reached = self.run_guard(
            headers=self.basic("alice", "correct-horse"))
        self.assertTrue(reached)
        self.assertEqual(request.state.user, "alice")

    def test_a_wrong_basic_password_is_refused(self):
        # The whole point. Before, this reached every route as alice.
        verdict, _, reached = self.run_guard(headers=self.basic("alice", "wrong"))
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 401)

    def test_an_unknown_user_with_any_password_is_refused(self):
        verdict, _, reached = self.run_guard(headers=self.basic("mallory", "x"))
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 401)

    def test_the_password_is_handed_to_the_verifier_whole(self):
        # partition, not split(":", 1): a password containing a colon used to
        # be truncated, so a correct password could not verify.
        self.check(_Request("/api/x", self.basic("alice", "a:b:c")))
        self.assertIn(("alice", "a:b:c"), self.verifications)

    def test_a_basic_header_with_no_colon_is_refused(self):
        raw = base64.b64encode(b"alice").decode()
        verdict, _, reached = self.run_guard(headers={"Authorization": f"Basic {raw}"})
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 401)

    def test_the_basic_scheme_is_case_insensitive(self):
        verdict, request, reached = self.run_guard(
            headers={"Authorization": "basic " + self.basic("bob", "hunter2")["Authorization"].split()[1]})
        self.assertTrue(reached)
        self.assertEqual(request.state.user, "bob")

    def test_a_malformed_basic_header_is_a_401_not_a_500(self):
        verdict, _, reached = self.run_guard(
            headers={"Authorization": "Basic not-base64!!"})
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 401)

    # -- the other two credentials still work here -------------------------

    def test_a_session_cookie_is_accepted_without_spawning_htpasswd(self):
        verdict, request, reached = self.run_guard(session={"user": "alice"})
        self.assertTrue(reached)
        self.assertEqual(request.state.user, "alice")
        self.assertEqual(self.verifications, [],
                         "the cookie path must not shell out to htpasswd")

    def test_an_access_key_is_accepted(self):
        verdict, request, reached = self.run_guard(
            headers={"Authorization": "Bearer mvk_bob_key"})
        self.assertTrue(reached)
        self.assertEqual(request.state.user, "bob")

    def test_an_unknown_access_key_is_refused(self):
        verdict, _, reached = self.run_guard(
            headers={"Authorization": "Bearer mvk_guessed"})
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 401)

    def test_a_bare_bearer_is_refused(self):
        verdict, _, reached = self.run_guard(headers={"Authorization": "Bearer"})
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 401)

    # -- the allow-list ----------------------------------------------------

    def test_the_event_stream_is_not_public(self):
        # It was on the unauthenticated allow-list. The handler filtered events
        # per user, but the user it filtered by came from the unverified header
        # sources — so the filter selected an attacker-chosen vault's feed.
        verdict, _, reached = self.run_guard(path="/api/events")
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 401)

    def test_the_login_endpoint_is_the_only_open_one(self):
        for path in ("/api/auth/login", "/api/auth/logout"):
            _, _, reached = self.run_guard(path=path, session={})
            self.assertTrue(reached, msg=f"{path} must be reachable to log in")

    def test_a_gui_path_without_a_credential_redirects_rather_than_401(self):
        verdict, _, reached = self.run_guard(path="/gui")
        self.assertFalse(reached)
        self.assertEqual(verdict.status_code, 302)

    def test_the_mcp_path_is_outside_this_guard(self):
        # Documents *why* McpAuthGuard exists: this middleware only matches
        # /gui* and /api*, so /mcp falls straight through to the mount.
        _, _, reached = self.run_guard(path="/mcp")
        self.assertTrue(reached)

    def test_no_dead_ping_entry_is_left_on_the_allow_list(self):
        # `/api/ping` was allow-listed with no route behind it — an allow-list
        # slot waiting for someone to implement a public health endpoint.
        #
        # Scoped to auth_guard's own source, not to gui.py: the string also
        # appears twice more in the log-suppression lists (EndpointFilter and
        # log_gui_requests), which are harmless, so a whole-file assertion would
        # pin contributors to the number rather than the property.
        # An AST walk, not a substring search. A `#` comment is not in the tree
        # at all, so the explanatory comment sitting directly above this
        # allow-list -- which names both of these paths on purpose -- cannot
        # satisfy or break the assertion. A substring search over the function
        # source would match that comment and report green on the bug it was
        # written to catch.
        node = _module_node("gui.py", "auth_guard")
        literals = {
            child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value.startswith("/api/")
        }
        # `literals` holds plain strings, not nodes.
        self.assertNotIn("/api/events", literals,
                         "/api/events is on the unauthenticated allow-list again")
        self.assertNotIn("/api/ping", literals,
                         "the dead /api/ping allow-list entry is back")
        # Only /api/auth may be open, and it is a prefix test rather than a
        # list entry -- so assert that is what is actually there.
        self.assertIn("/api/auth", literals,
                      "the /api/auth prefix exemption is missing")

    # -- _user does not re-derive an identity from headers ------------------

    def test_headers_alone_cannot_name_the_user(self):
        # The proxy identity headers are the weakest link in
        # extract_user_from_headers. The MCP path needs that function to read
        # the guard's verified x-vault-user stamp, but a handler here must not
        # consult it: there is no guard on this path that verified anything.
        for headers in (
            {"Remote-User": "alice"},
            {"X-Remote-User": "alice"},
            {"X-User": "alice"},
            {"X-Forwarded-User": "alice"},
            self.basic("alice", "whatever"),
            {"x-vault-user": "alice"},
        ):
            request = _Request("/api/memories", headers)
            self.assertEqual(self.user(request), "anonymous", msg=f"{headers}")

    def test_the_session_and_the_guard_stamp_are_both_accepted(self):
        self.assertEqual(self.user(_Request("/x", session={"user": "alice"})), "alice")
        request = _Request("/x")
        request.state.user = "bob"
        self.assertEqual(self.user(request), "bob")


# ---------------------------------------------------------------------------
# CORS and the auto-registered documentation routes
# ---------------------------------------------------------------------------

class CorsAndDocsTests(unittest.TestCase):
    """Two endpoints that needed no credential because nothing matched them."""

    def _origins_for(self, base_url):
        ns = {"mem": types.SimpleNamespace(BASE_URL=base_url), "urlsplit": urlsplit}
        fn = _lift("server.py", "_cors_origins", ns)
        return fn()

    def test_cors_origins_come_from_base_url(self):
        self.assertEqual(self._origins_for("https://host/mem-mcp"), ["https://host"])
        self.assertEqual(
            self._origins_for("https://host:8443/mem-mcp"), ["https://host:8443"])

    def test_an_origin_is_scheme_and_host_with_no_mount_point(self):
        # BASE_URL carries the nginx prefix; an Access-Control-Allow-Origin
        # must not, or it matches nothing.
        for base, expected in (
            ("https://host/mem-mcp", "https://host"),
            ("http://host:8086/mem-mcp", "http://host:8086"),
        ):
            self.assertEqual(self._origins_for(base), [expected], msg=base)

    def test_an_unset_or_relative_base_url_yields_no_origin_at_all(self):
        # Not a fallback to "*": no BASE_URL means no CORS headers, and
        # same-origin browser use never needed any.
        for base in ("", "   ", None, "/mem-mcp", "mem-mcp"):
            self.assertEqual(self._origins_for(base), [], msg=repr(base))

    def test_the_web_app_cors_is_not_a_wildcard_with_credentials(self):
        # allow_origins=["*"] + allow_credentials=True + allow_headers=["*"]
        # and registered last, so it is the OUTERMOST middleware: it answered
        # before auth_guard or McpAuthGuard ran, and let any site read and
        # mutate a named user's vault from the victim's browser.
        tree = ast.parse(_read("server.py"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_middleware"
            and node.args
            and getattr(node.args[0], "id", None) == "CORSMiddleware"
        ]
        self.assertTrue(calls, "no web_app.add_middleware(CORSMiddleware, ...) found")
        for call in calls:
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            origins = kwargs.get("allow_origins")
            self.assertIsNotNone(origins, "allow_origins was dropped entirely")
            # Assert on the *shape*: a call to _cors_origins(), not a literal.
            self.assertIsInstance(origins, ast.Call, msg=(
                "allow_origins must be derived from BASE_URL, not a literal: "
                f"got {ast.dump(origins)}"))

    def test_the_interactive_docs_and_schema_are_switched_off(self):
        # FastAPI registers /docs, /redoc and /openapi.json inside __init__,
        # before every route here and before the / mount, so auth_guard — which
        # only matches /gui* and /api* — never saw them. They enumerate every
        # route, parameter and schema.
        tree = ast.parse(_read("gui.py"))
        ctors = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FastAPI"
        ]
        self.assertTrue(ctors, "web_app = FastAPI(...) not found")
        for ctor in ctors:
            kwargs = {kw.arg: kw.value for kw in ctor.keywords}
            for name in ("docs_url", "redoc_url", "openapi_url"):
                self.assertIn(name, kwargs, msg=f"{name} is not pinned")
                self.assertIsInstance(
                    kwargs[name], ast.Constant, msg=f"{name} must be a literal")
                self.assertIsNone(kwargs[name].value, msg=f"{name} must be None")
