"""Verification of Google ID tokens.

A Google account is an external identity provider for this app, so a token it
mints is a credential that has to be checked properly: the signature, the
audience, the issuer and the expiry. This module does that check and nothing
else — it turns a token into a *verified identity* (a stable subject plus a
display email), and knows nothing about vaults.

Two decisions in here are load-bearing.

**The signature is verified by a library, not by arithmetic written here.**
RSA verification is `pow(signature, e, n)` followed by a padded-DigestInfo
comparison, which is twenty lines and entirely doable — and those twenty lines
are the whole attack surface of the credential. `alg: none` (a token that
declares itself unsigned), HMAC confusion (a public key used as an HMAC secret),
and a padded comparison that accepts a mangled tail are all mistakes available in
that twenty lines, and none of them announce themselves. PyJWT pins
`algorithms=["RS256"]` from *our* side rather than reading the token's own
header, so `alg` is a decision here and not a request, and `cryptography` does
the rest. The cost is two lines in requirements.txt, which is cheaper than a
hand-written verifier nobody outside this repo will ever audit.

**The audience is checked, and that is what stops a token for another app.**
A Google ID token is not a general-purpose bearer credential: Google mints it for
one specific client, and `aud` names which. Verifying the signature alone proves
"Google signed this" and says nothing about "Google signed this *for us*", so a
token minted by an attacker for an application they control would otherwise
authenticate here in full. This is the same class of mistake as accepting a
cookie minted for a different host.

The subject (`sub`) is the identity, never the email. `sub` is stable for the
lifetime of the (provider, client) pair; an email address is a property of the
person and gets reassigned when a domain changes hands, so keying on one silently
hands access to whoever inherits the mailbox. The email is kept only to show a
human which account is being linked.

Nothing here decides *which vault* a verified subject may open — see
sessions.py's identity links and gui.py's bearer ladder. That separation is why
a valid token for an unlinked account is a refusal and not a new empty vault.
"""

import logging
import threading

import jwt
from jwt import PyJWKClient

logger = logging.getLogger("memory-vault")

# Google's signing keys. Google's certificate endpoint (…/v1/certs) serves the
# same key set and was considered as a fallback for a blocked or moved primary;
# it was dropped instead, because a path that only runs on a partial failure is
# a path nothing in this repo can exercise, and a credential check that fails
# closed on a timeout is better than an untested second chance at succeeding.
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Both spellings are in circulation: Google has always accepted one as an alias
# of the other, and which one a given token carries has changed over time. An
# issuer outside this set is not Google.
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Never "none" and never a symmetric algorithm, whatever the token's own header
# says. See the module docstring.
GOOGLE_ALGORITHM = "RS256"

# A Google ID token is ~1-2 KB. This bound is not about Google's tokens; it is
# about not base64-decoding an arbitrarily long attacker-supplied string on the
# authentication path of an MCP client that retries.
MAX_TOKEN_CHARS = 8192

# Google's clock and ours disagree by a little, and a token that expired one
# second ago is not a security event. Seconds, not minutes: this is applied to a
# token that is also signature- and audience-checked, so it cannot be leveraged —
# it only stops a token being rejected at the boundary of the same request.
CLOCK_SKEW_SECONDS = 60

# How long a fetched key set is reused, and therefore how long a rotated-out key
# stays accepted here. Bounded by the token lifetime anyway, since `exp` is
# checked, so this is about not refetching per request rather than about window.
JWKS_CACHE_SECONDS = 3600

# A fetch timeout is short on purpose: this sits in front of authentication, and
# a slow key fetch is a request that hangs rather than one that fails.
JWKS_FETCH_TIMEOUT = 10

# Claims a token must carry to be usable here at all. `require` is a whitelist,
# not a hint: a token with no `sub` has no identity to key a vault on, and
# defaulting it would be inventing one.
REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")

# Display fields are untrusted input about to be rendered in a list, so they are
# bounded here rather than at the renderer. sessions.py's MAX_LABEL_CHARS does
# the same job for PSK labels.
MAX_EMAIL_CHARS = 254   # RFC 5321's practical maximum
MAX_NAME_CHARS = 120


class GoogleTokenError(Exception):
    """A presented token is not a usable Google ID token.

    Carries a short reason for the log. Never carries any part of the token: an
    exception message is the one part of this code that routinely ends up in a log
    file, an error page and a client's stderr.
    """


class GoogleJWKSource:
    """Fetches Google's signing keys, cached, behind one method.

    The rest of the module depends only on `get_signing_key_from_jwt`, which is
    PyJWKClient's own interface. That is the seam the test suite uses: it swaps
    in a source backed by a literal key, so a test exercises real RSA signature
    verification without a network round trip and without trusting a stubbed
    "signature verified" boolean.
    """

    def __init__(self, url: str = GOOGLE_JWKS_URL, timeout: int = JWKS_FETCH_TIMEOUT):
        self.url = url
        # cache_jwk_set keeps the fetched key set for `lifespan`, so a rolling
        # Google key rotation is picked up without a restart and without
        # refetching on every request.
        self._client = PyJWKClient(url, cache_keys=True, cache_jwk_set=True,
                                   lifespan=JWKS_CACHE_SECONDS, timeout=timeout)

    def get_signing_key_from_jwt(self, token: str):
        return self._client.get_signing_key_from_jwt(token)


_jwk_source = None
_jwk_source_lock = threading.Lock()


def jwk_source():
    """The process-wide key source, built on first use.

    A module global behind a lock rather than a cache decorator: this is reached
    from the event loop and from worker threads, and the value it holds is a
    client object rather than anything request-specific.
    """
    global _jwk_source
    if _jwk_source is not None:
        return _jwk_source
    with _jwk_source_lock:
        if _jwk_source is None:
            _jwk_source = GoogleJWKSource()
        return _jwk_source


def reset_jwk_source() -> None:
    """Drop the cached key source, so the next call re-fetches. For tests."""
    global _jwk_source
    with _jwk_source_lock:
        _jwk_source = None


def verify_google_token(token, client_id, source=None, leeway: int = CLOCK_SKEW_SECONDS) -> dict:
    """Verify a Google ID token and return its claims, or raise GoogleTokenError.

    `client_id` is the OAuth client id this app registered with Google, and it is
    required: an empty or unset value is refused rather than skipped, because
    "verify the signature and nobody's audience" is the mistake described in the
    module docstring with the check simply left out.

    `source` overrides the key source, which is how the test suite drives this
    with a real key and no network.
    """
    if not isinstance(token, str) or not token.strip():
        raise GoogleTokenError("no token was presented")
    token = token.strip()
    if len(token) > MAX_TOKEN_CHARS:
        raise GoogleTokenError("token is too large to be a Google ID token")
    if not client_id or not str(client_id).strip():
        raise GoogleTokenError("no Google client id is configured")
    client_id = str(client_id).strip()

    # The header is inspected here rather than being left to the key lookup, and
    # that is not tidiness — it is the difference between two true reasons and
    # one false one. `PyJWKClient.get_signing_key_from_jwt` raises
    # `PyJWKClientError`, which is *not* a subclass of `jwt.DecodeError`, so a
    # value that is not a JWT and a token signed by a key we do not have arrive
    # as the same exception type and would otherwise both be reported as "could
    # not resolve Google's signing keys". That sends an operator looking at the
    # key cache for a malformed paste.
    #
    # Checking the declared `alg` here also means the RS256 pin below is never
    # reached by an `alg: none` token, which is the point of the pin: a token
    # that asks to skip verification must not get as far as the verifier. Doing
    # it before the key lookup means it also costs no network round trip.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError as exc:
        logger.info("google: the presented value is not a JWT")
        raise GoogleTokenError("that is not a JSON Web Token") from exc
    declared = str(header.get("alg") or "")
    if declared != GOOGLE_ALGORITHM:
        # The reason names the algorithm without quoting anything else from the
        # token, and without implying the signature was ever checked.
        logger.info(f"google: refused a token declaring alg {declared!r}")
        raise GoogleTokenError(
            f"the token declares {declared or 'no'} signature algorithm, "
            f"and only {GOOGLE_ALGORITHM} is accepted"
        )

    source = source or jwk_source()
    try:
        signing_key = source.get_signing_key_from_jwt(token)
    except Exception as exc:
        # Covers a fetch failure, an unparseable key set and a kid that is not in
        # it. Deliberately not `str(exc)`: those messages can quote the URL and
        # the token, and this one reaches a client response body.
        logger.warning(f"google: no usable signing key ({type(exc).__name__})")
        raise GoogleTokenError("could not resolve Google's signing keys") from exc

    try:
        return jwt.decode(
            token,
            # PyJWKClient returns a PyJWK wrapper; a test's source may return a
            # bare key object. PyJWT's own convention is the wrapper, so use it
            # when there is one.
            getattr(signing_key, "key", signing_key),
            algorithms=[GOOGLE_ALGORITHM],
            audience=client_id,
            issuer=GOOGLE_ISSUERS,
            leeway=leeway,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        logger.info(f"google: rejected an ID token ({type(exc).__name__})")
        raise GoogleTokenError(f"the token was rejected ({type(exc).__name__})") from exc


def google_identity(token, client_id, source=None) -> dict:
    """Verify a token and return just the identity, shaped for storage and display.

    Returns `subject`, `email`, `email_verified`, `name` and `audience`.
    `subject` is the only field anything is ever keyed on; the rest exist so a
    human can tell which account is being linked.

    Raises GoogleTokenError when the token is not a usable Google ID token. One
    exception type on purpose: the caller cannot usefully act differently on
    "expired" than on "signed by someone else" without learning something about
    tokens it does not hold.
    """
    claims = verify_google_token(token, client_id, source=source)
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        # Unreachable via verify_google_token's `require`, kept as defence: a
        # vault keyed on an invented subject is worse than a refusal.
        raise GoogleTokenError("the token carries no subject")
    return {
        "subject": subject,
        "email": str(claims.get("email") or "").strip()[:MAX_EMAIL_CHARS],
        "email_verified": bool(claims.get("email_verified")),
        "name": str(claims.get("name") or "").strip()[:MAX_NAME_CHARS],
        "audience": str(claims.get("aud") or "").strip(),
    }


def looks_like_a_google_token(token) -> bool:
    """Cheap prefilter so a failed access-key lookup does not trigger a key fetch.

    A Google ID token is a JWS: three base64url segments separated by dots. An
    access key is `mvk_…` and a session id is urlsafe base64 with no dots, so
    this accepts neither one — and the full verification still runs afterwards,
    so a token that passes this and is not valid simply fails there.
    """
    if not isinstance(token, str):
        return False
    parts = token.strip().split(".")
    return len(parts) == 3 and all(parts) and parts[0].startswith("eyJ")