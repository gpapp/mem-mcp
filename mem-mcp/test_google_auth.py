"""
test_google_auth.py – verification of Google ID tokens.

Every test here *calls* google_auth.py. Unlike most of the suites in this repo
it does not need `ast.get_source_segment` and a pile of stubs: PyJWT and
cryptography are real dependencies, so the module under test imports directly
and the interesting half of it — the RSA signature verification — really runs.

That matters because the failures worth guarding here are all "this looks right
and is wrong". A stubbed `get_signing_key_from_jwt` returning a truthy object
would let a test pass against a verifier that checked no signature at all, which
is the entire reason this module delegates to a library (see its docstring). So
the seam is deliberately narrow: only the HTTP fetch of the key set is replaced.
The key is a real RSA key generated per run, the signature is produced by PyJWT,
and `jwt.decode` is what accepts or rejects it.

The headline property is the **audience** check, not the signature. A valid,
correctly-signed token minted by Google for a *different* client id must be
refused here, because otherwise anyone who can get a token for an application
they control authenticates against this vault.

Run:  python3 -m unittest -v test_google_auth.py
"""

import base64
import json
import logging
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import google_auth

CLIENT_ID = "1234567890-abcdefghijklmnopqrstuvwxyz.apps.googleusercontent.com"
OTHER_CLIENT_ID = "9999999999-zzzzzzzzzzzzzzzzzzzzzzzzzz.apps.googleusercontent.com"
SUBJECT = "110248495921238986420"
EMAIL = "someone@example.com"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_int(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _generate_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(key, kid: str) -> dict:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u_int(numbers.n),
        "e": _b64u_int(numbers.e),
    }


def _claims(**overrides) -> dict:
    """A claim set shaped like Google's, with the fields a test wants changed."""
    now = int(time.time())
    claims = {
        "iss": google_auth.GOOGLE_ISSUERS[0],
        "aud": CLIENT_ID,
        "sub": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
        "name": "Someone Example",
        "iat": now,
        "exp": now + 3600,
    }
    for name, value in overrides.items():
        if value is None:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


def _sign(key, claims: dict, kid: str = "test-key-1", algorithm: str = "RS256") -> str:
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def _unsigned_token(claims: dict) -> str:
    """A hand-built `alg: none` JWS. Assembled by hand on purpose: asking a
    library to mint one would test the library, not this module's refusal."""
    header = _b64u(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64u(json.dumps(claims).encode())
    return f"{header}.{payload}."


class StaticSource:
    """A key source backed by literal keys, standing in for PyJWKClient's fetch.

    Duck-typed on `get_signing_key_from_jwt` because that is the only method
    google_auth.py calls. Raises the same exception type PyJWKClient raises for
    an unknown kid, so the "Google rotated and we do not have that key" path is
    reachable in a test rather than only in production.
    """

    def __init__(self, keys: dict):
        self.keys = dict(keys)
        self.calls = 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.DecodeError:
            raise jwt.PyJWKClientError("Not enough segments") from None
        if kid not in self.keys:
            raise jwt.PyJWKClientError(
                f"Unable to find a signing key that matches: {kid}"
            )
        return jwt.PyJWK(self.keys[kid])


class GoogleTokenTests(unittest.TestCase):
    """verify_google_token and google_identity, against a real RSA key."""

    @classmethod
    def setUpClass(cls):
        cls.key = _generate_key()
        cls.other_key = _generate_key()
        cls.jwk = _public_jwk(cls.key, "test-key-1")
        cls.source = StaticSource({"test-key-1": cls.jwk})

    def setUp(self):
        self.source.calls = 0

    # -- the happy path ----------------------------------------------------
    def test_a_valid_token_is_accepted_and_its_claims_survive(self):
        token = _sign(self.key, _claims())
        verified = google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertEqual(verified["sub"], SUBJECT)
        self.assertEqual(verified["aud"], CLIENT_ID)

    def test_google_identity_returns_the_subject_and_display_fields(self):
        token = _sign(self.key, _claims())
        identity = google_auth.google_identity(token, CLIENT_ID, source=self.source)
        self.assertEqual(
            identity,
            {
                "subject": SUBJECT,
                "email": EMAIL,
                "email_verified": True,
                "name": "Someone Example",
                "audience": CLIENT_ID,
            },
        )

    def test_the_subject_is_the_identity_not_the_email(self):
        # Two tokens for the same person with different emails must resolve to
        # the same subject, because that is what a vault is keyed on.
        first = _sign(self.key, _claims(email="old@example.com"))
        second = _sign(self.key, _claims(email="new@example.org", name="X Y"))
        one = google_auth.google_identity(first, CLIENT_ID, source=self.source)
        two = google_auth.google_identity(second, CLIENT_ID, source=self.source)
        self.assertEqual(one["subject"], two["subject"])
        self.assertNotEqual(one["email"], two["email"])

    def test_both_issuer_spellings_are_accepted(self):
        # Google has used both, and which one a token carries has changed over
        # time; rejecting one of them is an outage that looks like a bug.
        for issuer in google_auth.GOOGLE_ISSUERS:
            with self.subTest(issuer=issuer):
                token = _sign(self.key, _claims(iss=issuer))
                verified = google_auth.verify_google_token(
                    token, CLIENT_ID, source=self.source
                )
                self.assertEqual(verified["iss"], issuer)

    def test_a_bare_key_object_from_the_source_is_accepted(self):
        # PyJWKClient returns a PyJWK wrapper; verify_google_token uses
        # getattr(signing_key, "key", signing_key) so a source handing back the
        # cryptography key directly also works. If the getattr were dropped this
        # is the test that notices, because the key object has no `.key`.
        class BareSource:
            def get_signing_key_from_jwt(self, token):
                return self.key.public_key()

        source = BareSource()
        source.key = self.key
        verified = google_auth.verify_google_token(
            _sign(self.key, _claims()), CLIENT_ID, source=source
        )
        self.assertEqual(verified["sub"], SUBJECT)

    # -- the audience check: the headline property -------------------------
    def test_a_token_minted_for_another_client_is_refused(self):
        token = _sign(self.key, _claims(aud=OTHER_CLIENT_ID))
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertIn("InvalidAudienceError", str(caught.exception))

    def test_a_token_with_no_audience_is_refused(self):
        token = _sign(self.key, _claims(aud=None))
        with self.assertRaises(google_auth.GoogleTokenError):
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)

    def test_the_audience_check_is_not_skipped_when_no_client_id_is_configured(self):
        # An unset client id must be refused, not treated as "check the
        # signature and nobody's audience".
        token = _sign(self.key, _claims())
        for empty in ("", "   ", None):
            with self.subTest(client_id=empty):
                with self.assertRaises(google_auth.GoogleTokenError) as caught:
                    google_auth.verify_google_token(token, empty, source=self.source)
                self.assertIn("client id", str(caught.exception))
        # And the key source is never even consulted in that case.
        self.assertEqual(self.source.calls, 0)

    # -- the other required claims -----------------------------------------
    def test_a_token_from_someone_other_than_google_is_refused(self):
        token = _sign(self.key, _claims(iss="https://accounts.evil.example"))
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertIn("InvalidIssuerError", str(caught.exception))

    def test_an_expired_token_is_refused(self):
        token = _sign(self.key, _claims(exp=int(time.time()) - 120))
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertIn("ExpiredSignatureError", str(caught.exception))

    def test_a_token_just_inside_the_clock_skew_is_accepted(self):
        # The whole point of the leeway: a token that expired one second ago is
        # not a security event, and rejecting it only trades a refusal for a
        # support ticket. Pinned at both sides of the boundary so the constant
        # cannot drift without this noticing.
        just_expired = _sign(self.key, _claims(exp=int(time.time()) - 10))
        self.assertTrue(
            google_auth.verify_google_token(just_expired, CLIENT_ID, source=self.source)
        )
        well_past = _sign(self.key, _claims(exp=int(time.time()) - 120))
        with self.assertRaises(google_auth.GoogleTokenError):
            google_auth.verify_google_token(well_past, CLIENT_ID, source=self.source)

    def test_leeway_is_not_applied_when_the_caller_explicitly_forbids_it(self):
        # Otherwise CLOCK_SKEW_SECONDS is not a constant, it is a suggestion:
        # `leeway=0` has to actually mean zero for anything that wants it.
        token = _sign(self.key, _claims(exp=int(time.time()) - 10))
        with self.assertRaises(google_auth.GoogleTokenError):
            google_auth.verify_google_token(
                token, CLIENT_ID, source=self.source, leeway=0
            )

    def test_every_required_claim_is_required(self):
        # `require` is a whitelist, not a hint. A token missing any of them is
        # refused rather than defaulted, and defaulting `sub` would mean
        # inventing the identity a vault is keyed on.
        for name in google_auth.REQUIRED_CLAIMS:
            with self.subTest(missing=name):
                token = _sign(self.key, _claims(**{name: None}))
                with self.assertRaises(google_auth.GoogleTokenError) as caught:
                    google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
                self.assertIn("MissingRequiredClaimError", str(caught.exception))

    def test_a_token_whose_subject_is_empty_is_refused_by_the_identity_helper(self):
        # google_identity re-checks `sub` after verification. A whitespace-only
        # subject is *present*, so `require` passes it and the defence below is
        # the only thing standing between it and a vault keyed on "".
        token = _sign(self.key, _claims(sub="   "))
        verified = google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertEqual(verified["sub"].strip(), "")
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.google_identity(token, CLIENT_ID, source=self.source)
        self.assertIn("subject", str(caught.exception))

    # -- the signature -----------------------------------------------------
    def test_a_token_signed_by_a_different_key_is_refused(self):
        # Same kid, same header, different key material: this is the attack a
        # signature check exists for, and a stubbed verifier would pass it.
        token = _sign(self.other_key, _claims(), kid="test-key-1")
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertIn("InvalidSignatureError", str(caught.exception))

    def test_a_tampered_payload_is_refused(self):
        token = _sign(self.key, _claims())
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["sub"] = "999999999999999999999"
        claims["email"] = "attacker@example.com"
        forged = ".".join([header, _b64u(json.dumps(claims).encode()), signature])
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(forged, CLIENT_ID, source=self.source)
        self.assertIn("InvalidSignatureError", str(caught.exception))

    def test_a_token_declaring_itself_unsigned_is_refused(self):
        # `alg: none` — the token asks to skip verification. google_auth pins
        # algorithms=["RS256"] from its own side, so the token's own header is a
        # request and not a decision.
        token = _unsigned_token(_claims())
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        message = str(caught.exception)
        self.assertIn("none", message)
        self.assertIn("RS256", message)
        # Refused *before* the key lookup, so the RS256 pin is never the thing
        # that caught it and no key set is fetched for a token that asked to
        # skip verification. Pinning only the outcome would still pass if the
        # key lookup had refused it first for want of a kid.
        self.assertEqual(self.source.calls, 0)

    def test_a_token_signed_with_a_symmetric_algorithm_is_refused(self):
        # HMAC confusion: the public key used as an HMAC secret. RS256-only
        # means the token's declared algorithm never selects the verifier.
        token = jwt.encode(_claims(), "secret", algorithm="HS256",
                           headers={"kid": "test-key-1"})
        with self.assertRaises(google_auth.GoogleTokenError):
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)

    def test_an_unknown_kid_reports_a_key_problem_not_a_token_problem(self):
        # "Google rotated and we do not have that key yet" and "that is not a
        # JWT" send an operator to completely different places, so the reasons
        # must stay distinguishable.
        token = _sign(self.key, _claims(), kid="a-key-we-do-not-have")
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertIn("signing keys", str(caught.exception))
        self.assertNotIn("JSON Web Token", str(caught.exception))

    def test_something_that_is_not_a_jwt_is_reported_as_such(self):
        for value in ("mvk_abcdef", "not-a-token", "a.b", "...."):
            with self.subTest(value=value):
                with self.assertRaises(google_auth.GoogleTokenError) as caught:
                    google_auth.verify_google_token(value, CLIENT_ID, source=self.source)
                self.assertIn("JSON Web Token", str(caught.exception))

    def test_a_key_fetch_failure_fails_closed(self):
        class BrokenSource:
            def get_signing_key_from_jwt(self, token):
                raise TimeoutError("connection timed out")

        token = _sign(self.key, _claims())
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(token, CLIENT_ID, source=BrokenSource())
        self.assertIn("signing keys", str(caught.exception))

    # -- the cheap refusals ------------------------------------------------
    def test_an_empty_or_non_string_token_is_refused_before_any_lookup(self):
        for value in ("", "   ", None, 12345, b"bytes", ["a"]):
            with self.subTest(value=repr(value)):
                with self.assertRaises(google_auth.GoogleTokenError) as caught:
                    google_auth.verify_google_token(value, CLIENT_ID, source=self.source)
                self.assertIn("no token", str(caught.exception))
        self.assertEqual(self.source.calls, 0)

    def test_an_oversized_token_is_refused_before_any_lookup(self):
        # The bound is about not base64-decoding an arbitrary attacker-supplied
        # string on a path an MCP client retries, so it has to happen before the
        # key fetch rather than after.
        value = "eyJ." + "x" * google_auth.MAX_TOKEN_CHARS
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(value, CLIENT_ID, source=self.source)
        self.assertIn("too large", str(caught.exception))
        self.assertEqual(self.source.calls, 0)

    def test_a_token_exactly_at_the_size_bound_is_not_rejected_for_its_size(self):
        # A guard that rejects at the bound and accepts at bound+1 passes a test
        # asserting only the rejection. Assert the other side too, so the bound
        # is a ceiling and not an off-by-one that eats a real token.
        token = _sign(self.key, _claims())
        padding = google_auth.MAX_TOKEN_CHARS - len(token)
        self.assertGreater(padding, 0)
        padded = token + "x" * padding
        self.assertEqual(len(padded), google_auth.MAX_TOKEN_CHARS)
        # It is no longer a valid signature, so this is refused — but for the
        # signature, which is the point: the size check let it through.
        with self.assertRaises(google_auth.GoogleTokenError) as caught:
            google_auth.verify_google_token(padded, CLIENT_ID, source=self.source)
        self.assertIn("InvalidSignatureError", str(caught.exception))
        self.assertNotIn("too large", str(caught.exception))

    def test_surrounding_whitespace_on_a_pasted_token_is_ignored(self):
        # The value arrives from a textarea, so it arrives with a newline.
        token = "\n  " + _sign(self.key, _claims()) + "  \n"
        verified = google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        self.assertEqual(verified["sub"], SUBJECT)

    # -- nothing sensitive escapes ----------------------------------------
    def test_no_failure_message_contains_any_part_of_the_token(self):
        # An exception message is the one thing here that routinely reaches a log
        # file, a response body and a client's stderr. PyJWT's own messages quote
        # the token, so the wrapper's reason must not be built from str(exc).
        token = _sign(self.key, _claims(aud=OTHER_CLIENT_ID))
        fragments = [token, token.split(".")[0], token.split(".")[1]]
        cases = [
            (token, CLIENT_ID, "wrong audience"),
            (_sign(self.other_key, _claims()), CLIENT_ID, "wrong key"),
            (_sign(self.key, _claims(iss="evil")), CLIENT_ID, "wrong issuer"),
            (_unsigned_token(_claims()), CLIENT_ID, "alg none"),
            ("mvk_secret_value_here", CLIENT_ID, "not a jwt"),
        ]
        for value, client_id, label in cases:
            with self.subTest(case=label):
                with self.assertRaises(google_auth.GoogleTokenError) as caught:
                    google_auth.verify_google_token(value, client_id, source=self.source)
                message = str(caught.exception)
                for fragment in fragments + ["mvk_secret_value_here"]:
                    if fragment:
                        self.assertNotIn(fragment, message)

    def test_nothing_logged_contains_any_part_of_the_token(self):
        token = _sign(self.key, _claims(aud=OTHER_CLIENT_ID))
        with self.assertLogs("memory-vault", level="DEBUG") as captured:
            with self.assertRaises(google_auth.GoogleTokenError):
                google_auth.verify_google_token(token, CLIENT_ID, source=self.source)
        blob = "\n".join(captured.output)
        self.assertNotIn(token, blob)
        for segment in token.split("."):
            self.assertNotIn(segment, blob)
        # The reason is still there — a log line that says only "rejected" is the
        # absence-of-a-log-line bug this repo has been bitten by before.
        self.assertIn("InvalidAudienceError", blob)

    def test_every_failure_is_the_one_exception_type_the_caller_catches(self):
        # A caller cannot act differently on "expired" than on "signed by
        # someone else" without learning something about tokens it does not hold,
        # so there is exactly one type and the reason rides inside it.
        for value, label in (
            (_sign(self.key, _claims(aud=OTHER_CLIENT_ID)), "audience"),
            (_sign(self.other_key, _claims()), "signature"),
            ("garbage", "shape"),
            ("", "empty"),
        ):
            with self.subTest(case=label):
                with self.assertRaises(google_auth.GoogleTokenError):
                    google_auth.verify_google_token(value, CLIENT_ID, source=self.source)


class PrefilterTests(unittest.TestCase):
    """looks_like_a_google_token — the cheap gate before a key fetch."""

    def test_a_real_token_passes(self):
        self.assertTrue(
            google_auth.looks_like_a_google_token(_sign(_generate_key(), _claims()))
        )

    def test_an_access_key_does_not(self):
        # This is the reason the function exists: an `mvk_…` lookup must not
        # trigger a network round trip to Google.
        self.assertFalse(google_auth.looks_like_a_google_token("mvk_abcdef0123456789"))
        self.assertFalse(google_auth.looks_like_a_google_token("mvk_REPLACE_ME"))

    def test_a_session_id_does_not(self):
        session_id = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA"
        self.assertFalse(google_auth.looks_like_a_google_token(session_id))

    def test_the_truth_table(self):
        cases = [
            ("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig", True),
            ("eyJ.a.b", True),
            ("a.b.c", False),            # no JWT header prefix
            ("eyJ..b", False),           # empty payload segment
            ("eyJ.a.", False),           # empty signature segment
            ("eyJ.a", False),            # two segments
            ("eyJ.a.b.c", False),        # four segments
            ("", False),
            ("   ", False),
            (None, False),
            (12345, False),
            (b"eyJ.a.b", False),
        ]
        for value, expected in cases:
            with self.subTest(value=repr(value)):
                self.assertIs(google_auth.looks_like_a_google_token(value), expected)

    def test_a_padded_token_still_passes(self):
        # Padded with whitespace because it came out of a textarea.
        self.assertTrue(
            google_auth.looks_like_a_google_token(
                "\n eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig \n"
            )
        )


class KeySourceTests(unittest.TestCase):
    """jwk_source() caching — a new client per request defeats cache_keys."""

    def setUp(self):
        google_auth.reset_jwk_source()

    def tearDown(self):
        google_auth.reset_jwk_source()

    def test_the_same_source_is_returned_every_time(self):
        first = google_auth.jwk_source()
        self.assertIs(first, google_auth.jwk_source())
        self.assertIsInstance(first, google_auth.GoogleJWKSource)

    def test_reset_rebuilds_it(self):
        first = google_auth.jwk_source()
        google_auth.reset_jwk_source()
        self.assertIsNot(first, google_auth.jwk_source())

    def test_building_the_source_does_no_io(self):
        # It is on the authentication path of an MCP client that retries, so a
        # constructor that fetched would turn a cache into a latency source.
        source = google_auth.GoogleJWKSource()
        self.assertIsNotNone(source.url)
        self.assertEqual(source.url, google_auth.GOOGLE_JWKS_URL)

    def test_the_cache_settings_are_the_ones_the_comment_claims(self):
        # The comment says the fetched key set is retained for `lifespan`. If
        # cache_jwk_set or the lifespan were dropped the comment becomes false
        # and every request refetches, which is the original defect (a client
        # rebuilt per call) in a subtler form. Pin the value rather than the prose.
        source = google_auth.GoogleJWKSource()
        self.assertEqual(source._client.jwk_set_cache.lifespan,
                         google_auth.JWKS_CACHE_SECONDS)
        self.assertEqual(source._client.timeout, google_auth.JWKS_FETCH_TIMEOUT)


if __name__ == "__main__":
    logging.getLogger("memory-vault").addHandler(logging.NullHandler())
    unittest.main()