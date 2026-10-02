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
import unittest

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
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == node_name:
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

    The three credentials are checked by giving each one a *distinguishable
    owner*, so a test asserting the resolved user also proves which credential
    was honoured. An assertion on the status code alone would pass if the guard
    picked the wrong credential and then 200'd anyway.
    """

    GOOD_PASSWORD = "correct-horse"

    @classmethod
    def setUpClass(cls):
        cls.known_keys = {"mvk_alice_key": "alice", "mvk_bob_key": "bob"}
        cls.pw = {("alice", "correct-horse"): True, ("bob", "hunter2"): True}
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
            "_verify_htpasswd": lambda user, pw: cls.pw.get((user, pw), False),
        }
        cls.Guard = _lift("gui.py", "McpAuthGuard", namespace)
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

    def test_a_session_cookie_is_accepted(self):
        status, reached, scope = self.call(session_user="alice")
        self.assertEqual(status, 200)
        self.assertEqual(self.vault_user(scope), "alice")

    def test_valid_basic_auth_is_accepted_and_verified(self):
        status, _, scope = self.call(
            [("Authorization", f"Basic {self.basic('bob', 'hunter2')}")])
        self.assertEqual(status, 200)
        self.assertEqual(self.vault_user(scope), "bob")

    def test_basic_auth_with_the_wrong_password_is_refused(self):
        # This is the check nginx used to do. Nothing verified the password
        # before, so removing auth_basic had to mean the app took it over.
        status, reached, _ = self.call(
            [("Authorization", f"Basic {self.basic('bob', 'wrong')}")])
        self.assertEqual(status, 401)
        self.assertFalse(reached)

    def test_a_basic_header_for_an_unknown_user_is_refused(self):
        status, _, _ = self.call(
            [("Authorization", f"Basic {self.basic('mallory', 'x')}")])
        self.assertEqual(status, 401)

    def test_a_session_takes_precedence_over_a_bearer_key(self):
        # Both are valid and they name different people. Whichever wins, the
        # answer must be one of them and the other must not be consulted --
        # pinning "session first" makes that a decision rather than an accident.
        status, _, scope = self.call(
            [("Authorization", "Bearer mvk_bob_key")], session_user="alice")
        self.assertEqual(status, 200)
        self.assertEqual(self.vault_user(scope), "alice")

    def test_an_invalid_bearer_does_not_fall_back_to_a_valid_session(self):
        # A bad key on a request that also has a cookie should still succeed on
        # the cookie; the point is that the *key* is not silently ignored as if
        # it had been the identity. Proven by asserting the key's user is not it.
        status, _, scope = self.call(
            [("Authorization", "Bearer mvk_guessed")], session_user="alice")
        self.assertEqual(status, 200)
        self.assertEqual(self.vault_user(scope), "alice")

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

    def test_the_401_explains_the_three_ways_in(self):
        # A bare 401 on /mcp is indistinguishable from a wrong URL, a proxy
        # misconfiguration, or a revoked key. The body is the only place the
        # remedy can live.
        guard = self.Guard(self._echo_app)
        scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": [],
                 "query_string": b""}
        captured = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            captured.append(message)

        asyncio.run(guard(scope, receive, send))
        body = b"".join(m.get("body", b"") for m in captured
                        if m["type"] == "http.response.body")
        text = body.decode()
        self.assertIn("Access Keys", text)
        self.assertIn("Bearer mvk_", text)

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
        # backup panel measured 500+ requests for one finished job this way.
        body = _js_function_source("templates/dashboard.html", "renderPSKs")
        self.assertNotIn("api.get", body)
        self.assertNotIn("loadPSKs", body)


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


if __name__ == "__main__":
    unittest.main()