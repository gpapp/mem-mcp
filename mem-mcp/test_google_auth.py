"""Tests for google_auth.py — the OAuth 2.0 authorization-code flow.

This module **imports its subject directly**, which is only possible because
google_auth.py is stdlib-only. Nothing here needs a network, a JWT library or a
web framework, and the one place the module talks to the network
(`_request_json`) takes an injected `opener`, so every test drives the real URL
building, the real parameter validation and the real error text against a fake
transport.

Why the network is faked but nothing else is: the attack surface of an OAuth
client is exactly what sits above the transport. A test that stubbed
`exchange_code` itself would pass against a client that sends the wrong grant
type, leaks the client secret into a query string, or forgets `state` — and the
fake transport is the only part that cannot be exercised without a network.
"""

import json
import os
import unittest
import urllib.error
import urllib.parse

import google_auth


class FakeResponse:
    """The two things `_request_json` uses from a urlopen result."""

    def __init__(self, payload, raw=None):
        if raw is None:
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingOpener:
    """A urlopen stand-in that records every call and returns a fixed body."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({"url": request.full_url, "data": request.data,
                           "headers": dict(request.headers), "timeout": timeout})
        body = self.bodies.pop(0) if self.bodies else {}
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)

    @property
    def form(self):
        """The first call's POST body as a dict."""
        data = self.calls[0]["data"] or b""
        return dict(urllib.parse.parse_qsl(data.decode("ascii")))


class EnvCase(unittest.TestCase):
    """Every test runs with the Google env vars set to a known pair."""

    CID = "1234.apps.googleusercontent.com"
    SECRET = "GOCSPX-test-secret-value"
    ENV = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in self.ENV}
        os.environ["GOOGLE_CLIENT_ID"] = self.CID
        os.environ["GOOGLE_CLIENT_SECRET"] = self.SECRET
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ConfigTests(EnvCase):
    def test_the_credentials_come_from_the_environment(self):
        self.assertEqual(google_auth.client_id(), self.CID)
        self.assertEqual(google_auth.client_secret(), self.SECRET)

    def test_a_present_but_blank_variable_reads_as_absent(self):
        # A compose line with no value passes an empty string, and
        # `os.getenv(name, "fallback")` would return that empty string rather
        # than the fallback -- so a half-configured deployment would look
        # configured. This is the `or ""` form's whole reason for existing.
        # One test covers the group because the reader, the exact read-back and
        # the `configured()` verdict are the same three assertions for either
        # variable; only the input differs, so each case keeps its own value.
        for name, blank, reader, expected in (
            ("GOOGLE_CLIENT_ID", "", google_auth.client_id, ""),
            ("GOOGLE_CLIENT_SECRET", "   ", google_auth.client_secret, ""),
        ):
            with self.subTest(variable=name, value=repr(blank)):
                os.environ[name] = blank
                self.assertEqual(reader(), expected)
                self.assertFalse(google_auth.configured())

    def test_both_halves_are_required(self):
        # The secret is what makes the code redemption confidential. With only a
        # client id, a login can be started and cannot be finished, so offering
        # the button would send someone to Google and then to an error.
        self.assertTrue(google_auth.configured())
        os.environ.pop("GOOGLE_CLIENT_SECRET")
        self.assertFalse(google_auth.configured())
        os.environ["GOOGLE_CLIENT_SECRET"] = self.SECRET
        os.environ.pop("GOOGLE_CLIENT_ID")
        self.assertFalse(google_auth.configured())

    def test_the_callback_url_is_derived_from_base_url(self):
        # Not configured separately: a redirect URI that disagrees with the URL
        # the app is served on fails at Google with an error page naming neither.
        # One test covers the group because the property is the single rule
        # "strip the trailing slash, append the path", and each case carries its
        # own exact expected string -- so the degenerate empty BASE_URL stays as
        # specific as it was as a test of its own.
        for base, expected in (
            ("https://hass.example/mem-mcp",
             "https://hass.example/mem-mcp/api/auth/google/callback"),
            ("https://hass.example/mem-mcp/",
             "https://hass.example/mem-mcp/api/auth/google/callback"),
            ("", "/api/auth/google/callback"),
        ):
            with self.subTest(base_url=repr(base)):
                self.assertEqual(google_auth.callback_url(base), expected)


class AuthorizationUrlTests(EnvCase):
    URL = "https://hass.example/mem-mcp/api/auth/google/callback"

    def test_the_url_carries_everything_google_needs(self):
        url = google_auth.authorization_url("state-abc", self.URL)
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path,
                         google_auth.GOOGLE_AUTH_URL)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(q["client_id"], self.CID)
        self.assertEqual(q["redirect_uri"], self.URL)
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["state"], "state-abc")
        self.assertEqual(q["scope"], google_auth.GOOGLE_SCOPES)

    def test_only_openid_email_and_profile_are_asked_for(self):
        # A consent screen saying the app wants to see and change Gmail is a
        # consent screen nobody accepts.
        scopes = google_auth.GOOGLE_SCOPES.split()
        self.assertEqual(sorted(scopes), ["email", "openid", "profile"])

    def test_the_account_picker_is_forced(self):
        # Without it, a browser that has signed in before silently re-authorises
        # the previous Google account — which on a shared machine signs you in as
        # whoever used it last.
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(
            google_auth.authorization_url("s", self.URL)).query))
        self.assertEqual(q["prompt"], "select_account")

    def test_a_missing_state_is_refused(self):
        # `state` is the CSRF token for the whole flow. Without it a code minted
        # for someone else's login can be posted at this app's callback and
        # signed in as them.
        for bad in ("", "   ", None):
            with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
                google_auth.authorization_url(bad, self.URL)
            self.assertIn("state", str(ctx.exception))

    def test_a_missing_redirect_uri_is_refused(self):
        with self.assertRaises(google_auth.GoogleOAuthError):
            google_auth.authorization_url("s", "")

    def test_building_the_url_does_not_need_a_secret(self):
        # Only the code exchange needs the secret, so a deployment that has a
        # client id can at least build a consent URL. Refusing here would be
        # stricter than the flow requires.
        os.environ.pop("GOOGLE_CLIENT_SECRET")
        url = google_auth.authorization_url("s", self.URL)
        self.assertIn("client_id=", url)

    def test_without_a_client_id_it_is_refused(self):
        os.environ.pop("GOOGLE_CLIENT_ID")
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.authorization_url("s", self.URL)
        self.assertIn("not configured", str(ctx.exception))

    def test_a_state_with_url_specials_is_encoded_not_concatenated(self):
        url = google_auth.authorization_url("a&b=c d", self.URL)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(q["state"], "a&b=c d")

    def test_an_override_beats_the_environment(self):
        url = google_auth.authorization_url("s", self.URL, client_id_override="other")
        self.assertIn("client_id=other", url)


class ExchangeCodeTests(EnvCase):
    REDIRECT = "https://hass.example/mem-mcp/api/auth/google/callback"

    def test_the_code_is_redeemed_with_the_secret_over_a_form_post(self):
        opener = RecordingOpener({"access_token": "ya29.a0-token", "expires_in": 3600})
        token = google_auth.exchange_code("the-code", self.REDIRECT, opener=opener)
        self.assertEqual(token, "ya29.a0-token")
        self.assertEqual(opener.calls[0]["url"], google_auth.GOOGLE_TOKEN_URL)
        self.assertEqual(opener.calls[0]["timeout"], google_auth.HTTP_TIMEOUT)
        form = opener.form
        self.assertEqual(form["code"], "the-code")
        self.assertEqual(form["client_id"], self.CID)
        self.assertEqual(form["client_secret"], self.SECRET)
        self.assertEqual(form["redirect_uri"], self.REDIRECT)
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(opener.calls[0]["headers"].get("Content-type"),
                         "application/x-www-form-urlencoded")

    def test_the_secret_is_only_ever_in_the_body_never_the_url(self):
        opener = RecordingOpener({"access_token": "t"})
        google_auth.exchange_code("c", self.REDIRECT, opener=opener)
        # Presence first, so this cannot pass by the secret going missing
        # altogether.
        self.assertEqual(opener.form["client_secret"], self.SECRET)
        self.assertNotIn(self.SECRET, opener.calls[0]["url"])

    def test_a_bad_input_is_refused_before_any_request_is_issued(self):
        # One test covers the group because all six cases are the same property
        # -- exchange_code judges the call on its own parameters and never opens
        # a socket -- and each case keeps the exact `no calls made` claim plus
        # its own expected message wherever the original pinned one.
        saved = {name: os.environ.get(name) for name in self.ENV}
        cases = (
            ("empty code", (), "", self.REDIRECT, None),
            ("blank code", (), "   ", self.REDIRECT, None),
            ("absent code", (), None, self.REDIRECT, None),
            ("oversized code", (),
             "c" * (google_auth.MAX_CODE_CHARS + 1), self.REDIRECT, "plausible"),
            ("missing secret", ("GOOGLE_CLIENT_SECRET",), "c", self.REDIRECT, None),
            ("missing redirect_uri", (), "c", "", None),
        )
        for label, unset, code, redirect, expected in cases:
            with self.subTest(case=label):
                for name in self.ENV:
                    os.environ[name] = dict(zip(self.ENV, (self.CID, self.SECRET)))[name]
                for name in unset:
                    os.environ.pop(name, None)
                opener = RecordingOpener({})
                with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
                    google_auth.exchange_code(code, redirect, opener=opener)
                self.assertEqual(opener.calls, [],
                                 "a refusal must not have issued a request")
                if expected is not None:
                    self.assertIn(expected, str(ctx.exception))
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_the_bound_is_a_ceiling_not_off_by_one(self):
        opener = RecordingOpener({"access_token": "t"})
        google_auth.exchange_code("c" * google_auth.MAX_CODE_CHARS,
                                  self.REDIRECT, opener=opener)
        self.assertEqual(len(opener.calls), 1)

    def test_a_token_response_that_is_missing_or_oversized_is_refused(self):
        # Not a thing Google's server does; a thing a proxy or captive portal
        # does. Refusing is the only answer that does not sign someone in as
        # nobody. One test covers the group because both cases are the single
        # property "the value handed back must be a plausible token", and each
        # keeps its own expected message -- the oversized case pinned none.
        for label, body, expected in (
            ("no access_token", {"error": "nope"}, "no access token"),
            ("implausibly long access_token",
             {"access_token": "t" * (google_auth.MAX_TOKEN_CHARS + 1)}, None),
        ):
            with self.subTest(case=label):
                with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
                    google_auth.exchange_code("c", self.REDIRECT,
                                              opener=RecordingOpener(body))
                if expected is not None:
                    self.assertIn(expected, str(ctx.exception))

    def test_a_body_that_is_not_a_json_object_is_refused(self):
        # A captive portal or intercepting proxy. Parsing it leniently is how an
        # HTML error page becomes a username. One test covers the group because
        # both are the same property -- a 200 that is not a dict is refused --
        # and each case keeps its own expected message.
        for label, body, expected in (
            ("html error page", b"<html>hi</html>", "could not be read"),
            ("json array", b"[1, 2, 3]", None),
        ):
            with self.subTest(case=label):
                with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
                    google_auth.exchange_code("c", self.REDIRECT,
                                              opener=RecordingOpener(body))
                if expected is not None:
                    self.assertIn(expected, str(ctx.exception))

    def test_google_refusal_is_reported_in_its_own_words(self):
        # invalid_grant is the single most common failure here — the code was
        # spent or expired — and it looks like nothing else.
        err = urllib.error.HTTPError(google_auth.GOOGLE_TOKEN_URL, 400, "Bad Request", {},
                                     None)
        err.read = lambda: json.dumps(
            {"error": "invalid_grant",
             "error_description": "Bad Request"}).encode("utf-8")
        opener = RecordingOpener(err)
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.exchange_code("c", self.REDIRECT, opener=opener)
        message = str(ctx.exception)
        self.assertIn("Bad Request", message)
        self.assertIn("authorization code", message)

    def test_an_unreadable_error_body_still_produces_a_usable_message(self):
        err = urllib.error.HTTPError(google_auth.GOOGLE_TOKEN_URL, 500, "x", {}, None)
        err.read = lambda: b"<html>proxy error</html>"
        opener = RecordingOpener(err)
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.exchange_code("c", self.REDIRECT, opener=opener)
        self.assertIn("500", str(ctx.exception))

    def test_a_network_failure_says_try_again_rather_than_refused(self):
        opener = RecordingOpener(
            urllib.error.URLError(TimeoutError("timed out")))
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.exchange_code("c", self.REDIRECT, opener=opener)
        self.assertIn("try again", str(ctx.exception))

    def test_a_200_that_is_not_json_is_refused(self):
        # A captive portal or intercepting proxy. Parsing it leniently is how an
        # HTML error page becomes a username.
        opener = RecordingOpener(b"<html>hi</html>")
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.exchange_code("c", self.REDIRECT, opener=opener)
        self.assertIn("could not be read", str(ctx.exception))



    def test_overrides_beat_the_environment(self):
        opener = RecordingOpener({"access_token": "t"})
        google_auth.exchange_code("c", self.REDIRECT, client_id_override="cid2",
                                  client_secret_override="sec2", opener=opener)
        self.assertEqual(opener.form["client_id"], "cid2")
        self.assertEqual(opener.form["client_secret"], "sec2")


class FetchUserinfoTests(EnvCase):
    BODY = {"sub": "1234567890", "email": "Alice@Example.COM",
            "email_verified": True, "name": "Alice Example"}

    def test_the_signed_in_user_is_returned_normalised(self):
        opener = RecordingOpener(self.BODY)
        info = google_auth.fetch_userinfo("ya29.token", opener=opener)
        self.assertEqual(info["subject"], "1234567890")
        self.assertEqual(info["email"], "alice@example.com")
        self.assertIs(info["email_verified"], True)
        self.assertEqual(info["name"], "Alice Example")
        self.assertEqual(sorted(info),
                         ["email", "email_verified", "name", "subject"])

    def test_the_token_is_sent_as_a_bearer_credential(self):
        opener = RecordingOpener(self.BODY)
        google_auth.fetch_userinfo("ya29.token", opener=opener)
        self.assertEqual(opener.calls[0]["url"], google_auth.GOOGLE_USERINFO_URL)
        self.assertEqual(opener.calls[0]["headers"].get("Authorization"),
                         "Bearer ya29.token")
        self.assertEqual(opener.calls[0]["timeout"], google_auth.HTTP_TIMEOUT)

    def test_the_subject_is_never_derived_from_the_address(self):
        # An address can be renamed, reassigned or made an alias; `sub` cannot.
        # Everything that keys a vault off Google keys off this.
        opener = RecordingOpener({"sub": "s1", "email": "a@example.com"})
        first = google_auth.fetch_userinfo("t", opener=opener)
        opener.bodies.append({"sub": "s1", "email": "b@elsewhere.com"})
        second = google_auth.fetch_userinfo("t", opener=opener)
        self.assertEqual(first["subject"], second["subject"])
        self.assertNotEqual(first["email"], second["email"])

    def test_a_response_with_no_subject_is_refused(self):
        # Inventing one would produce a vault whose owner cannot sign in again.
        opener = RecordingOpener({"email": "a@example.com"})
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.fetch_userinfo("t", opener=opener)
        self.assertIn("no account id", str(ctx.exception))

    def test_only_an_explicit_true_counts_as_verified(self):
        # The failure that matters is the one where a non-boolean reads as
        # present, so "true", 1 and a non-empty string are all unverified.
        for value, expected in ((True, True), (False, False), ("true", False),
                                (1, False), (None, False), ("", False)):
            opener = RecordingOpener({"sub": "s", "email_verified": value})
            info = google_auth.fetch_userinfo("t", opener=opener)
            self.assertIs(info["email_verified"], expected, msg=repr(value))

    def test_a_missing_address_is_empty_rather_than_fatal(self):
        # The address is display text and the vault key for a new account; an
        # absent one is handled where it is used, not here.
        opener = RecordingOpener({"sub": "s"})
        info = google_auth.fetch_userinfo("t", opener=opener)
        self.assertEqual(info["email"], "")
        self.assertEqual(info["name"], "")

    def test_oversized_display_fields_are_truncated(self):
        opener = RecordingOpener({"sub": "s", "email": "a" * 400 + "@x.com",
                                  "name": "n" * 400})
        info = google_auth.fetch_userinfo("t", opener=opener)
        self.assertLessEqual(len(info["email"]), google_auth.MAX_EMAIL_CHARS)
        self.assertLessEqual(len(info["name"]), google_auth.MAX_NAME_CHARS)

    def test_an_implausible_token_is_refused_before_any_request(self):
        # One test covers the group: blank, absent and oversized are the same
        # property — the token is rejected on its own shape with no request
        # made — so each case only supplies its own input.
        for label, token in (
            ("empty", ""), ("blank", "   "), ("absent", None),
            ("oversized", "t" * (google_auth.MAX_TOKEN_CHARS + 1)),
        ):
            with self.subTest(case=label):
                opener = RecordingOpener(self.BODY)
                with self.assertRaises(google_auth.GoogleOAuthError):
                    google_auth.fetch_userinfo(token, opener=opener)
                self.assertEqual(opener.calls, [])


class SecrecyTests(EnvCase):
    """No failure message may carry a credential. The text reaches a browser."""

    def _collect_messages(self, opener):
        messages = []
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.exchange_code("the-secret-code", "https://x/cb", opener=opener)
        messages.append(str(ctx.exception))
        return messages

    def test_the_code_never_appears_in_a_failure_message(self):
        err = urllib.error.HTTPError(google_auth.GOOGLE_TOKEN_URL, 400, "Bad", {}, None)
        err.read = lambda: b'{"error":"invalid_grant"}'
        for message in self._collect_messages(RecordingOpener(err)):
            self.assertNotIn("the-secret-code", message)

    def test_the_client_secret_never_appears_in_a_failure_message(self):
        err = urllib.error.HTTPError(google_auth.GOOGLE_TOKEN_URL, 401, "no", {}, None)
        err.read = lambda: b'{"error":"invalid_client"}'
        for message in self._collect_messages(RecordingOpener(err)):
            self.assertNotIn(self.SECRET, message)

    def test_a_very_long_error_description_is_bounded(self):
        # The string is interpolated into a message that reaches the browser,
        # and a proxy's error page can be arbitrarily long.
        err = urllib.error.HTTPError(google_auth.GOOGLE_TOKEN_URL, 400, "Bad", {}, None)
        err.read = lambda: json.dumps(
            {"error_description": "x" * 5000}).encode("utf-8")
        with self.assertRaises(google_auth.GoogleOAuthError) as ctx:
            google_auth.exchange_code("c", "https://x/cb", opener=RecordingOpener(err))
        self.assertLess(len(str(ctx.exception)), 400)


class FailureReportingTests(EnvCase):
    """One exception type, and every failure is logged with its reason."""

    def test_every_failure_is_the_same_exception_type(self):
        cases = [
            lambda: google_auth.authorization_url("", "https://x/cb"),
            lambda: google_auth.exchange_code("c", "https://x/cb",
                                              opener=RecordingOpener({})),
            lambda: google_auth.exchange_code(
                "c", "https://x/cb",
                opener=RecordingOpener(urllib.error.URLError("boom"))),
            lambda: google_auth.fetch_userinfo("",
                                               opener=RecordingOpener({})),
        ]
        for call in cases:
            with self.assertRaises(google_auth.GoogleOAuthError):
                call()

    def test_every_failure_is_logged_with_its_own_reason(self):
        # The defect this repo has been bitten by before was a missing log line,
        # and only a call that genuinely raises can assert one. One test covers
        # the group because the property is the same — a WARNING record exists
        # carrying the reason — and each case asserts its own substrings, so a
        # dropped line for one kind cannot hide behind the other.
        err = urllib.error.HTTPError(google_auth.GOOGLE_TOKEN_URL, 400, "Bad", {}, None)
        err.read = lambda: b'{"error_description":"Bad Request"}'
        cases = (
            ("network failure",
             RecordingOpener(urllib.error.URLError("dns is down")),
             ("could not reach Google", "dns is down")),
            ("google refusal", RecordingOpener(err), ("400", "Bad Request")),
        )
        for label, opener, expected in cases:
            with self.subTest(case=label):
                with self.assertLogs("memory-vault", level="WARNING") as captured:
                    with self.assertRaises(google_auth.GoogleOAuthError):
                        google_auth.exchange_code("c", "https://x/cb", opener=opener)
                joined = "\n".join(captured.output)
                for needle in expected:
                    self.assertIn(needle, joined)

    def test_nothing_is_logged_on_a_successful_exchange(self):
        with self.assertNoLogs("memory-vault", level="WARNING"):
            google_auth.exchange_code("c", "https://x/cb",
                                      opener=RecordingOpener({"access_token": "t"}))
            google_auth.fetch_userinfo("t", opener=RecordingOpener({"sub": "s"}))


class EndpointTests(unittest.TestCase):
    """The endpoints are the ones Google documents, pinned so they cannot drift."""

    def test_the_endpoints_are_googles_https_urls(self):
        for url in (google_auth.GOOGLE_AUTH_URL, google_auth.GOOGLE_TOKEN_URL,
                    google_auth.GOOGLE_USERINFO_URL):
            self.assertTrue(url.startswith("https://"), url)
        self.assertIn("accounts.google.com", google_auth.GOOGLE_AUTH_URL)
        # Not the legacy accounts.google.com/o/oauth2/token: the
        # oauth2.googleapis.com host is current and returns JSON.
        self.assertIn("oauth2.googleapis.com", google_auth.GOOGLE_TOKEN_URL)

    def test_the_module_imports_without_a_jwt_library(self):
        # The whole reason this module is stdlib-only: it can be imported and
        # called on a machine with no web framework, no network and no
        # cryptography, which is what the suite runs on.
        import importlib
        import sys
        saved = {name: sys.modules.get(name) for name in ("jwt", "cryptography")}
        sys.modules.pop("jwt", None)
        sys.modules.pop("cryptography", None)
        try:
            reloaded = importlib.reload(google_auth)
            self.assertTrue(reloaded.configured() in (True, False))
        finally:
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()