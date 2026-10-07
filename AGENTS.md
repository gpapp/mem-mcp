# AGENTS.md

## Dev Commands

```powershell
# Setup
cp .env.example .env
# Edit .env: set MEM_NEO4J_PASSWORD and NEO_PASS

# Infra (must run first)
docker-compose up -d
docker exec ollama ollama pull nomic-embed-text
docker exec ollama ollama pull qwen3.5:0.8b
docker exec ollama ollama pull gemma4:e2b

# Local dev (infra must be running)
.\.venv\Scripts\Activate.ps1
python mem-mcp\server.py
```

## Code Quality

After completing any code changes:
1. Run `python -m py_compile <file.py>` to verify syntax
2. If successful, commit and push to git
3. If running in Docker, rebuild and restart: `docker-compose up -d --build mem-mcp` (or pull + rebuild on the target machine)
4. **Do NOT attempt to access Docker or databases directly** — containers run on a remote machine; only use git to push changes (the ops team deploys)

### Focused Tests

The dependency-light regression suite covers matching, scope compatibility, scope-name and `Client:`-header resolution, duplicate scoring and clustering, merge validation, merge callback ordering, People candidate resolution, LLM prompt contracts, and the chunking split. Two more suites need no database either: `test_embedding_reliability.py` lifts the real functions out of `common.py` with `ast.get_source_segment`, and `test_cypher_safety.py` lints the Cypher in every module (see Gotchas for why). `test_backup_compression.py` uses the same `ast` lift for the snapshot compression helpers, since `backup.py` also cannot be imported without `httpx`. `test_people_extraction.py` lifts `people_extract_windows` and the whole of `extract_diary_keywords` out of `diary_manager.py` for the same reason, and *calls* the keyword extractor against a stub LLM rather than only reading its source. It also holds `ReclassifyIsScopeOnlyTests`, which lifts `_classify_and_link_diary` out of `migrate_client_context.py` and runs it with every people extractor bound to a stub that raises, so the test passes only if the function genuinely cannot reach them — a substring guard on that module could not see a twin defined in a sibling, and did not when it mattered. `test_graph_scope.py` lifts the graph scoping and cap policy out of `fact_manager.py` the same way. `test_matching_regressions.py` also holds the `RELEVANT_TO` selection tests — `RelatedClientSelectionTests` **calls** `_related_clients_for` lifted out of `migrate_client_context.py`, and `ScopePromptTests` pins the prompt *sentences* above, because a prompt is text and no other test in the suite notices when a sentence changes. `test_llm_reliability.py` lifts the chat path out of `common.py` the same way and, unlike the others, *raises* from its fake client: the defect it guards was the absence of a log line, which no source assertion can see. Its `ConflictLoggingTests` extends that to the 409 path, and is the reason `_def_segment()` exists — slicing a function to the next `"\ndef "` runs to EOF when the PEP8 two-blank-line gap intervenes, which an `assertIn` cannot detect and an `assertNotIn` only detects by finding something in the rest of the file. `test_mobile_layout.py` reads `templates/dashboard.html` and asserts the properties of the responsive stylesheet — no browser is available here, so it cannot check that the page looks right, only that the things a regression would silently undo are still in place. `test_env_wiring.py` reads `.env.example` and `docker-compose.yml` and asserts every documented variable actually reaches the container (see Gotchas for the incident).

**A test of a helper is not a test of its call site.** `OllamaModelMatchTests` covers `_ollama_model_matches` directly, and re-injecting the old `if model in installed` into `ensure_ollama_models` left all of them green — the download bug lived in the caller. There is now a tenth test that asserts the call site, for the same reason `WriteOrderingGuardTests` exists. Whenever a bug is a wrong call rather than a wrong function, pin the call.

`test_status_monitor.py` is the eleventh suite and the only one that imports its subject directly: `status_monitor.py` is standard-library-only (no `httpx`, nothing from `common`), so its snapshot builder, change fingerprint and subscriber plumbing are **called** rather than lifted out of the source. That matters because every defect worth guarding there is a "this looks right and is wrong" — a cold model labelled `100% CPU`, a fingerprint that fires on every poll, a publish that blocks on a browser that stopped reading — and none is visible in the shape of the code. The same file also lifts `fetch_ollama_status` / `unload_ollama_model` out of `common.py` and `api_unload_model` / `_status_snapshot` out of `gui.py` with `ast.get_source_segment`, because those three cannot be imported here. `ImportDisciplineTests` imports the module in a **subprocess with httpx blocked at the import hook** rather than searching the source for the string: a docstring mentioning httpx is not an import of it, and that is the `assertIn`-over-a-whole-file lesson again. `test_mobile_layout.py::StatusWidgetLayoutTests` pins where the widget is allowed to sit — see "Server Status Widget" for why that is not a free choice.

`test_sessions.py` is the twelfth suite and it *imports and calls* `sessions.py`, which is stdlib-only by design for exactly this reason. It covers the session lifecycle (absolute-not-sliding expiry, the exact deadline boundary, expired rows deleted on read, logout-everywhere), the credential guard (`session["pass"]` raising at three layers), the middleware itself driven with real ASGI messages — including that the response body passes through untouched, which is the SSE property — and the PSK store (no plaintext on disk, a 10-char prefix that cannot authenticate, a session id refused as a PSK before the DB, ownership-scoped revocation). It also holds `Python311AnnotationTests`; see Gotchas for why that one is structural.

`test_auth_guard.py` is the thirteenth, and it covers *which credential wins* rather than whether one is valid, so it is separate on purpose: `HeaderPrecedenceTests` pins the order, `McpAuthGuardTests` drives the real guard class with each credential carrying a **distinguishable owner** so a status-only assertion cannot hide having picked the wrong one, and `WiringTests` pins the things that would silently re-open the hole — no `auth_basic` in the nginx MCP location, `session.clear()` before login writes `user`, no template rendering `AUTH_PASS`, the bridge reading `MEM_VAULT_PSK`. `ApiAuthTests` and `CorsAndDocsTests` cover the *other* half of the chain (the `/gui` + `/api/*` gate and the two unmatched surfaces), and every one of the six defects they guard was verified to bite by re-injection. That matters because every defect worth guarding there is a "this looks right and is wrong" — a cold model labelled `100% CPU`, a fingerprint that fires on every poll, a publish that blocks on a browser that stopped reading — and none is visible in the shape of the code. The same file also lifts `fetch_ollama_status` / `unload_ollama_model` out of `common.py` and `api_unload_model` / `_status_snapshot` out of `gui.py` with `ast.get_source_segment`, because those three cannot be imported here. `ImportDisciplineTests` imports the module in a **subprocess with httpx blocked at the import hook** rather than searching the source for the string: a docstring mentioning httpx is not an import of it, and that is the `assertIn`-over-a-whole-file lesson again. `test_mobile_layout.py::StatusWidgetLayoutTests` pins where the widget is allowed to sit — see "Server Status Widget" for why that is not a free choice.

`test_google_auth.py` is the fourteenth, and after the OAuth rewrite it is a
stdlib suite that **imports its subject directly** — `google_auth.py` depends on
nothing but `urllib`, which is the practical payoff of dropping the pasted-token
flow. It has one test seam, the `opener` argument threaded through
`_request_json`, and everything else is the real function: `ExchangeCodeTests`
asserts the *form* posted to Google's token endpoint (the secret in the body, never
in the URL), `FetchUserinfoTests` covers the profile mapping including that `sub`
is never derived from the address and that only `is True` counts as verified, and
`SecrecyTests` plus `FailureReportingTests` pin that no code, token or secret
appears in any exception message — the message reaches a response body, and one
exception type for every failure is deliberate, because a caller distinguishing
six classes will render the wrong one. `assertLogs` there is not decoration: a
network failure that raises without logging is the absence-of-a-log-line bug this
repo has already been bitten by once.

`test_vault_migration.py` is the sixteenth suite, and it **imports
`scoped_ids.py` directly** — that module is standard-library-only (`uuid`) for the
same reason `sessions.py` and `matching_utils.py` are: the id derivation is the
one part of a vault move that has to agree with `db_create_client`, and a helper
that cannot be imported on a plain box cannot be compared against it here.
`migrate_vault_user.py` cannot be imported (no `qdrant_client`, no `common`), so
its Qdrant and graph functions are lifted with `ast.get_source_segment` and
**called** against fakes — the properties worth guarding are behavioural, since
the whole module is about an order of operations that is invisible in the shape
of the code. `PerformMoveOrderTests` is the call-site guard for the defect that
shaped the planner: `IdRemap.contexts` and `IdRemap.context_parents` have the
same shape and the same key type, so passing one where the other belongs is
silent, and `MoveGraphTests` cannot see it because it calls `move_graph` directly
with its own dict. `PaginationTests` models Qdrant's `offset` as a cursor over
the collection's own order rather than an index into the filtered result, because
`move_qdrant_user` mutates the filter it is paginating over and the other model
would make it drop points. `VerifyTests` and `RunTests` likewise **call** the
verification pass and the entry point against fakes — a `_StubCommon` is
installed in `sys.modules` because `run()` does `from common import …` inside its
own body and a namespace entry cannot reach that.

`test_registration.py` is the fifteenth suite, and it is deliberately its own file rather than more classes in `test_sessions.py`: registration is a **third** credential surface, and the only one that is *unauthenticated by design* — the signup routes live under the `/api/auth` allow-list, which is what makes an account creatable at all — so burying it in a suite about the two properly-protected gates would hide the one fact that matters about it. It **calls** `sessions.py` directly for the credential store, the scrypt hashing and the throttle, and lifts `_verify_account`, `_require_registration`, `registration_config` and both signup routes out of `gui.py` with `ast.get_source_segment` (gui.py is not importable on a machine with no fastapi). The lifted routes are async, and the harness drives them through `asyncio.run` for a reason that is worth stating because it is not visible in the code: an un-awaited coroutine enters no function at all, so every guard in the class would have reported green against routes that were never called. That was a real harness bug while the suite was being written, and it is invisible from the outside — the tests pass either way, they just test nothing. `CallSiteTests` pins what no source test above can otherwise see: both login paths going through `_verify_account` rather than each picking a store for itself (an address can be in `credentials` *and* htpasswd, and the two stores have different rotation rules); the throttle textually *before* the flag check, so turning registration **off** removes the rate limit from a route that is still mounted and the counter only ever sees successful attempts; and `McpAuthGuard` containing no `register` at all, which is the `/mcp` half of the rule stated above. `LandingPageTests` pins the template's `minlength` against `sessions.MIN_PASSWORD_CHARS` — a cross-file contract between two modules nothing else would notice drifting, and one that fails silently as a signup form that rejects passwords the backend would have accepted. After the rewrite to username + verified email it also pins each half of the form to **its own** flag, and asserts the page contains no pasted-token field at all; `VerificationMailTests` covers the confirmation mail, and `GoogleSignInTests` / `GoogleSignupTests` drive the redirect endpoints end to end with a fake opener. One harness lesson from that rewrite is worth keeping: **the routes `return` a `RedirectResponse`, they do not raise**, so `assertRaises(_Redirect)` against a route that forgets to return one passes on a route that was never correct — the helper asserts the return value is a redirect, which fails loudly instead.

```bash
cd mem-mcp
python3 -m unittest -v test_matching_regressions.py test_embedding_reliability.py test_chunking.py test_cypher_safety.py test_backup_compression.py test_people_extraction.py test_graph_scope.py test_mobile_layout.py test_llm_reliability.py test_env_wiring.py test_status_monitor.py test_sessions.py test_auth_guard.py test_google_auth.py test_registration.py test_vault_migration.py
```

This was a Windows path (`C:/tools/miniconda3/python.exe`) with PowerShell `Push-Location`/`Pop-Location`, and it does not exist on the Linux host these files are edited from — every suite "failed" on an interpreter that is not there. Any `python`/`python3` on `PATH` runs all sixteen; none of them import a DB driver.

Run `git -c core.whitespace=cr-at-eol diff --check` after documentation or code edits (the `cr-at-eol` avoids false positives on the CRLF files). The suites use pure helpers and fake callbacks so they do not require Neo4j, Qdrant, or Ollama.

### Image Build

All three backing services are pinned to an exact tag — `qdrant/qdrant:v1.19.1`, `neo4j:5.26.0`, `ollama/ollama:0.34.4`. They were all `:latest` at one point, which meant `docker-compose up -d --build` silently upgraded them. Two concrete consequences, not hypothetical ones: Ollama moved its embed route across versions (hence the dual `/api/embeddings` + `/api/embed` client), and `backup.py` depends on Qdrant's snapshot create/download/upload API, so an unpinned major bump there would make a savepoint unrestorable rather than fail visibly. When bumping, change the tag deliberately and expect to re-run the backup round trip.

`mem-mcp/Dockerfile` pins `# syntax=docker/dockerfile:1` and installs Python deps with `RUN --mount=type=cache,target=/root/.cache/pip`. Wheels live in a BuildKit cache that survives rebuilds, so a rebuild after a source-only change no longer re-downloads PyPI. Do not reintroduce `--no-cache-dir` — it disables the mount and is the reason the layer grew on every build. `mem-mcp/.dockerignore` keeps `__pycache__`, `logs/`, and `backup/` out of the build context, since `COPY . .` would otherwise ship them into the image.

## Architecture

| Component | Port | Notes |
|-----------|------|-------|
| App | 8086 (Docker), 8080 (internal) | Unified: GUI `/gui`, MCP `/mcp`, REST `/api/*` |
| Qdrant | 6333 | Internal only, not exposed to host |
| Neo4j | 7687 | Internal only |
| Ollama | 11434 | Internal only |
| nginx | /mem-mcp | Proxy mount point (must include in all URL configs) |

## Nginx Config

**IMPORTANT**: Update `nginx_snippet.conf` before deploying. Two locations:

- `/mem-mcp/mcp/` → MCP (**authenticated by the app**, see Authentication below)
- `/mem-mcp/` → GUI/API (session auth via app, cookie passthrough)

**Do not re-add `auth_basic` to the MCP location.** It used to be there, and
removing it is what lets `Authorization: Bearer mvk_…` reach the app at all:
nginx can only check a username and password against a file on the host, so it
rejects a PSK before the request is ever forwarded. The app took that job over
in `McpAuthGuard` (gui.py), which accepts an **access key and nothing else** —
not a session cookie, not `Basic`. Re-adding `auth_basic` silently breaks PSK
support with nothing in the logs to say so.

The MCP location also no longer sets `Remote-User`, because nothing sets
`$remote_user` once `auth_basic` is gone, and a stale header is worse than
none: `extract_user_from_headers` trusts it (see Authentication).

## Authentication

**There are two gates, and they no longer check the same credentials.**

| | gate | accepts | covers |
|---|---|---|---|
| `/mcp` | `McpAuthGuard` (gui.py), wrapping the mount | **`Authorization: Bearer mvk_…` and nothing else** | every route fastmcp registers |
| `/gui`, `/api/*` | `auth_guard` via `_check_session_auth` | session cookie → `Basic` (verified) → `Bearer mvk_…` | ~60 handlers |
| `/api/auth/*` | **none** — this *is* the allow-list | nothing, by design | login, logout, signup, email confirmation, and the two Google OAuth routes |

`/mcp` takes only an access key. A session cookie, a `Basic` header and a Google
sign-in are each refused with a 401 whose body names the one way in and whose
`WWW-Authenticate` header says `Bearer` (it used to say `Basic`, which pointed at
a credential this endpoint no longer accepts). On `/gui` and `/api/*` the order
is session → `Basic` → `Bearer`, and only `/api/auth/*` is reachable without a
credential, because that is where you exchange one for a session.

**Both gates resolve a `Bearer` token through one function,
`resolve_bearer_token` (gui.py), and the point of that survives the ladder
collapsing to one rung.** It had three once — access key, then two steps for a
Google ID token — and one shared function is what stopped the two gates drifting,
because one gate accepting a revoked key the other rejects is exactly the bug a
second copy of the ladder invites. **A Google sign-in is deliberately not a rung
here.** It was one, for as long as a pasted ID token was something an MCP client
could hold. The redirect flow ends in a session cookie instead, and a cookie is
not a credential a client presents — so a rung for it would be a way in that no
other gate offers. `BearerLadderTests` drives what remains and asserts the Google
store is **never consulted**, with two must-not-be-called counters, because "no
rung" is a property of what is *not* called. `BearerCallSiteTests` pins that both
gates call the ladder and that the MCP guard's own `__call__` resolves no identity
of its own.

**Every failure returns an actionable reason, not a bare 401**, because the
reason reaches the client body and a generic one is indistinguishable between the
ways a token can be wrong. "no token was presented" and "that is not a known
credential" name two different fixes — send a header at all, or mint a key. The
second is deliberately **not** phrased as an invalid credential, because a client
holding a Google ID token has to sign in through a browser and needs to be told
that; a remedy pointing at the wrong thing costs the reader more than the
rejection does. The reason never quotes the token
(`test_a_reason_never_quotes_the_token`).

**`google_auth.py` no longer parses or verifies a JWT, and that is the point of
the redirect flow.** It is an OAuth 2.0 authorization-code client over stdlib
`urllib`: build the authorization URL, redeem the code at Google's token endpoint
*with the client secret*, read the profile from userinfo. There is nothing to
verify locally because nothing arrives signed — the code and the userinfo both
cross TLS, and the secret is what makes the redemption confidential. Hand-rolled
RSA verification bought nothing here and cost two dependencies, one dead
`except jwt.DecodeError` branch (`PyJWKClientError` is not a `DecodeError`, so it
collapsed "that is not a JWT" into "could not resolve Google's signing keys"), and
a second credential to leak. **A leaked `GOOGLE_CLIENT_SECRET` is now a real
credential** — normal for a confidential client, and the reason it lives in the
environment rather than in SQLite where the Setup page used to write it.

**`/mcp` is token-only on purpose; the other two credentials are the point, not
an oversight.** A session cookie is a *bearer* credential the browser replays by
itself, so accepting one on the MCP path hands every client something that cannot
be scoped to a device and cannot be revoked without ending the user's own
dashboard session — the opposite of what an access key is for. `Basic` is not
refused for its mechanism, which is fine (per-call and stateless, same as a
key), but for its *credential*: it is the account password, so it also unlocks
`/api/*`, it rotates only when a human changes it, and one lost laptop cannot
have it revoked without changing it for everyone. Keeping it would have made
every access key revocable in name only. The dashboard keeps its sessions —
`auth_guard` is untouched, and `ApiAuthTests` pins that a cookie still opens
`/api/*`.

**`auth_guard` verifies the `Basic` password, and that is the whole point.** It
is now the only gate that accepts `Basic` at all, and it had **never** been
verifying it:

```python
decoded = base64.b64decode(auth_header.split(" ")[1]).decode("utf-8")
if ":" in decoded:
    return decoded.split(":", 1)[0]        # username trusted, password discarded
```

nginx has no `auth_basic` on the GUI location, so that decode was the entire
gate for every route — `Authorization: Basic base64(alice:anything)` was alice's
vault, including `POST /api/backup/restore/{id}` and `POST /api/psks`. A
username is not a secret and the header is client-supplied, so returning one is
not authentication. Verified with `_verify_account`, which consults the
`credentials` table first and only shells out to `htpasswd -vb` when the name is
not one registration created — a browser on the cookie path never pays it, a
registered user's Basic auth never pays it, and Basic is for scripted clients.

**`auth_guard` must match `/gui` and `/api` only, and `/api/events` must not be
on its allow-list.** The SSE stream used to be public. Its handler filtered
events per user correctly, but the *user it filtered by* came from the
unverified header sources below — so the filter selected an attacker-chosen
vault's change feed. Filtering by a value the caller chose is not access
control. The dead `/api/ping` entry went with it: an allow-list slot with no
route behind it is an allow-list slot waiting for someone to implement a public
health endpoint. `ApiAuthTests` pins the allow-list with an **AST walk, not a
substring search** — the explanatory comment above it names `/api/events` on
purpose, and a `#` comment is not in the tree.

**`_user` must not parse headers.** It ended with `mem.extract_user_from_headers(...)`,
which made `_require_user` in every handler a "did somebody hand us a username"
check rather than an authentication check. The MCP path needs that function —
it is how the guard's verified stamp reaches the tools — but `/gui` and
`/api/*` must not, because nothing on those paths verified anything. It now
reads `session["user"]` and `request.state.user`, both verified upstream, and
nothing else. **Consequence: the proxy identity headers no longer authenticate
anything**, on any path. If a deployment was relying on an authenticating proxy
to supply identity, that has to change.

**`extract_user_from_headers` returns `"anonymous"` rather than raising**, and
that is load-bearing for the MCP tools, which call it with no guard beneath
them. A bare `Bearer` with no token must return `"anonymous"` and must **not**
fall through to `Remote-User` — a bad credential is not an absent one
(`HeaderPrecedenceTests`).

**The verified identity is stamped as header `x-vault-user`, and that header is
stripped from every inbound request** by `VaultSessionMiddleware` before
anything reads it. The two halves must stay together, and *both* must lowercase:
`extract_user_from_headers` gives `x-vault-user` top precedence because no
client can supply it. `VaultSessionMiddleware` used to compare against the
lowercase constant directly, so `X-Vault-User` sailed past — uvicorn happens to
lowercase header names in both its h11 and httptools implementations, which is
why nothing broke. `test_stripping_is_case_insensitive` was asserting
`b"x-vault-user" not in [b"X-Vault-User"]` and passed on the bug it was written
to catch; it now lowercases the surviving keys.

**CORS on `web_app` is derived from `BASE_URL`, not `["*"]`.** It was
`allow_origins=["*"]` + `allow_credentials=True` + `allow_headers=["*"]`, and
being registered last it is the **outermost** middleware — it answered before
either gate ran. Any site could then read and mutate a named user's vault from
the victim's browser with a `Basic` header. `SameSite=Lax` does not help,
because no cookie was needed. `_cors_origins()` (server.py:33) returns the
scheme+host of `BASE_URL`, and an unset or relative `BASE_URL` yields `[]` —
no `Access-Control-Allow-Origin` at all, which is correct, since same-origin
browser use never needed one. There is deliberately no knob: `BASE_URL` is
already required and already the value an operator gets right. The MCP CORS
(`mcp_cors`) keeps its wildcard on purpose — it sits *inside* `McpAuthGuard`, so
it only runs on an already-authenticated request.

**`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`.** FastAPI
registers `/docs`, `/redoc` and `/openapi.json` inside `__init__`, i.e. before
every route and before the `/` mount, and `auth_guard` only matches `/gui*` and
`/api*` — so all three were reachable with no credential and enumerated every
route, parameter and schema. Nothing consumes the schema, so they are off
rather than authenticated.

`monitor_mcp_tool` still only logs; the gates are what authorize. Do not add
per-tool checks to the decorator.

## Sessions & Access Keys

`sessions.py` is **stdlib-only by design** — no fastapi, no httpx, nothing from
`common` — so both stores can be imported and *called* by the test suites on a
machine with no web framework and no database. That is why it is importable
where `common.py` and `gui.py` are not, and why `test_sessions.py` asserts on
behaviour (expiry, revocation, ownership) instead of lifting source segments
with `ast.get_source_segment` the way most of the other suites must.

**`VaultSessionMiddleware` is raw ASGI, not `BaseHTTPMiddleware`, and that is
not a style choice.** `/api/events` and `/api/status/stream` are SSE; a
`BaseHTTPMiddleware` in that path buffers the stream it exists to pass through.
Wrapping `send` is what lets a `Set-Cookie` be appended to the response start
message without touching the body. It reads and writes `scope["headers"]`
directly, which is also what keeps the starlette import out of the file.

- **Sessions are server-side; the cookie is only an opaque id.** That is what
  makes login survive a container rebuild — but only because `MEM_SESSION_DIR`
  points at the bind mount. The dir defaults to `<dirname LOG_DIR>/sessions`,
  which is *inside the container* and therefore lost on rebuild. If you change
  the compose volume, change the default too.
- **`expires_at` is absolute, `last_seen_at` slides.** 30 days from creation.
  A sliding session means an attacker who steals a cookie keeps it alive by
  using it — which is indistinguishable, from the server, from the owner.
- **Login clears the session first** (`api_login` calls `request.session.clear()`
  before writing `user`). The middleware reads that as "delete the old row and
  mint a new id" — which is what defeats session fixation. An id planted in the
  browser before login must not still be the logged-in id afterwards.
- **`session["pass"]` raises.** `VaultSession.__setitem__` rejects
  `pass`/`password`/`passwd` at the point of writing, and `create_session` /
  `update_session_data` reject them too, because a dict-level guard alone would
  leave a direct `create_session(data={"pass": ...})` writing one layer down.
  The Setup page used to rebuild a Basic header out of the session password
  purely to display it; it now shows an access key instead, and there is no
  reason to restore the old behaviour.
- **Expiry is enforced on read.** A row past its deadline is deleted by the read
   that found it, so a dead cookie cannot be probed for existence by timing. The
   boot purge in `server.py` and `_session_gc_loop` are the belt to that braces;
   a failed write is logged and swallowed, never a 500. **That log line is
   load-bearing for diagnosis, not decoration:** a swallowed failure here emits
   no `Set-Cookie` at all, so the request succeeds and the flow only breaks one
   request later. A `NameError` in `_write_session` reproduces the exact
   user-visible symptom of the bug below while naming a different cause, so read
   the `session:` lines before believing the message on the landing page.
 - **A session is persisted when it has *data*, not when it has a *user*.** This
   is not a distinction anyone would invent, so here is why it exists. The OAuth
   flow stores `state` in the session between the consent redirect and the
   callback, and at that moment there is no user — the person is mid-sign-in.
   `_write_session` used to refuse to persist a session without a `user`, on the
   reasonable-sounding grounds that "a session with no identity is not a
   session". The consequence was that **every Google sign-in failed, always**:
   `/api/auth/google/start` set `oauth_state` and nothing else, so the middleware
   answered with `Max-Age=0`, and the callback arrived with a dead cookie, found
   no state, and reported *"the sign-in did not come back from the browser that
   started it"*. The empty session is what a logout looks like, and that is the
   only shape that must not become a row. `create_session` therefore accepts an
   empty username **only** when the payload carries no `user`, and skips
   `payload.setdefault("user", …)` for it — that skip is the security property,
   not a convenience, because `_check_session_auth` trusts `session["user"]`
   without asking how it got there, so a placeholder name would be a way in. The
   invariant is therefore **"a row that names somebody is only ever created from
   a real `user` value"**, and such a row stays unreachable through
   `active_sessions_for_user` / `delete_sessions_for_user`, which both refuse an
   empty name. A row that names nobody also gets `PRE_AUTH_MAX_AGE` (10 minutes)
   rather than the 30-day session lifetime: it exists to carry one `state` across
   one redirect, so the long window buys nothing, leaves a row behind for every
   abandoned sign-in attempt, and widens the replay window for a stolen state.
 - **A route test with a dict `session` stub cannot see any of this.** Every test
   of `api_google_start` / `api_google_callback` passes `_Request(session={...})`,
   a plain dict, so nothing in `test_registration.py` ever runs
   `VaultSessionMiddleware`. The state "survived" in every one of those tests
   while never surviving in production, and the suite was green. The fix was
   pinned from both ends: `test_sessions` asserts the middleware persists a
   pre-auth key, reaches the next request and carries no `user`, and
   `CallSiteTests.test_the_oauth_state_lives_in_the_session_and_nowhere_else`
   asserts the route writes the state *into the session* rather than a query
   string — because a state in a URL survives in browser history, in a `Referer`
   and in every proxy log on the way back from Google. **When two modules meet
   at a boundary, test each one's half and pin the seam; do not assume a test
   that drives one of them covers the other.**

**A PSK is stored as a SHA-256 hash and its plaintext is returned exactly once.**
Only a 10-char display prefix is persisted, and `resolve_psk` refuses a key that
does not carry the `mvk_` prefix *before* touching the database — a session id
is a bearer credential too, and without that check it would be hashed and
looked up, which is a cross-mechanism path from "stolen cookie" to "full MCP
access" waiting to be closed by accident.

`list_psks` keeps revoked rows visible (`status: revoked`) because a key that
vanishes from the list is indistinguishable from one that was never revoked.
`revoke_psk` returns one answer for "no such id", "not yours" and "already
revoked" so the endpoint cannot be walked to enumerate real key ids, and it
scopes by `user_id` in the UPDATE rather than reading a row and comparing in
Python.

## Critical Config

- `MEM_NEO4J_PASSWORD` (also mapped to `NEO_PASS` in docker-compose)
- Embedder: pull the configured `MEM_EMBEDDER_MODEL` into Ollama (defaults to `nomic-embed-text`)
- Query LLM: `LLM_QUERY_MODEL` (used for search rewriting, scope classification and both diary extractors). **Set `MEM_SCOPE_MODEL` as well** — the scope classifier is the slow path, and because `MEM_SCOPE_MODEL` defaults to the query model, setting only one of them leaves the expensive half of the work on a different quant than the one you measured.
- **The GPU reservation is in a SEPARATE file, and dropping it is silent.** `docker-compose.gpu.yml` holds the ollama device reservation (`count: all`). It is *not* in `docker-compose.yml`, and there is no `docker-compose.override.yml` to make it automatic, so the deploy command is `-f docker-compose.yml -f docker-compose.gpu.yml`. Running bare `docker compose up -d ollama` recreates the container with no GPU: it starts healthy, answers every request, and produces entirely plausible latencies — it is just CPU inference. This bit again on 2026-09-29 while reducing `OLLAMA_CONTEXT_LENGTH`, which looked like a clean change. Adding a `deploy:` block to the base file is *not* the fix; it duplicates the overlay and drifts from it. The only reliable check is `docker exec ollama ollama ps`, and `100% CPU` means the overlay was dropped.
- **`OLLAMA_CONTEXT_LENGTH` must be sized against the merge path, not the extractors.** Everything else is windowed — `MEM_PEOPLE_WINDOW`/`MEM_KEYWORD_WINDOW` are *chars*, ~3.6 per token, and a call only ever sees one window, so a 30k-char diary entry costs ~2000 tokens regardless of its length. The merge draft is the one call that is not windowed: it `json.dumps` the full text of every selected record with no cap. Measured against real facts (longest 3055 chars), `max_cluster` 4/8/12/20 → 3783/6393/8801/**13103** prompt tokens. So 16332 covers the full allowed range — barely, 2.3k spare at 20 — and 8192 would have broken at `max_cluster=12`. Cutting it bought 3 points of GPU residency (80%→83%) and cost over half the merge headroom. The merge is the one path where a dropped tail is silent data loss, so it sets the ceiling, not the largest window.
- **A cold model load is 9-20s and is paid on the first call after any idle gap.** `OLLAMA_KEEP_ALIVE=30m` now pins it. Deliberately not `-1`: the card is 4 GB shared with ASR (1.25 GB), and holding a 3.3 GB chat model resident indefinitely starves the transcriber.
- **Verify the model is actually on the GPU before sizing anything against VRAM.** This was measured wrong for days: the Ollama service had no `deploy.resources.reservations.devices` and no `/dev/nvidia*` inside the container, so `ollama ps` reported `100% CPU` and every latency figure was CPU inference on 15 GiB of RAM with 7.1 GiB of swap already used. `docker exec ollama ls /dev/nvidia*` and `docker exec ollama ollama ps` are the two checks; `ollama ps` must say `100% GPU` for a model to be resident. Once the GPU was actually enabled, `qwen3.5:2b-q4_K_M` reported `100% GPU` and `gemma3:4b-it-q4_K_M` reported `49%/51% CPU/GPU` at 3.8 GB — i.e. the 4 GB card cannot hold it, so it pays a load per call. Registry sizes on a 4 GB card shared with ASR (1.25 GB): `qwen3.5:2b` 2.74 GB (Q8_0), `qwen3.5:2b-q4_K_M` 1.95 GB, `gemma3:4b-it-q4_K_M` 3.34 GB, `gemma4:e2b` 7.16 GB.
- **Correctness beats residency, and the deployment disagrees with the default.** `qwen3.5:2b` is the documented default but a 2b model completes fields rather than reasoning about them: on the same scope item, `qwen3.5:2b-q4_K_M` answered `{"client": "EPAM", "context": "PPC"}` in 32.4 s while `gemma3:4b-it-q4_K_M` answered the correct verdict in 5 consistent runs. The current `.env` sets **both** `MEM_SCOPE_MODEL` and `LLM_QUERY_MODEL` to `gemma3:4b-it-q4_K_M`. It also fixed a search-rewrite hallucination (`Thomas Kraikauer` → `["Thomas Kraikauer", "German actor", "film director"]` became `["Thomas Kraikauer", "Deutsche Bank"]`). That gemma3 run is now history — `.env` moved on to `phi4-mini` and then to `nemotron-3-nano:4b`, see above — but the reasoning stands: the model that answers correctly is the one to keep, and the gemma3 measurements are the evidence for why the 2b default was abandoned. Budget for the load cost: gemma3 stays warm, but a cold first call in each window is much slower, and a full reclassify is a multi-minute job under `MEM_LLM_TIMEOUT=300`.
- **The four chat roles are split across two models, and one knob cannot be right for all of them.** `MEM_EXTRACT_MODEL` (`granite3.3:2b`) covers extraction — diary keywords, people names, search rewriting — while `LLM_QUERY_MODEL`, `MEM_SCOPE_MODEL` and `MEM_MERGE_MODEL` stay on `nemotron-3-nano:4b` for judgement. `MEM_EXTRACT_MODEL` defaults to `LLM_QUERY_MODEL`, so an operator who sets nothing changes nothing.

  The split is not a preference; it is the asymmetry between an error you can repeat and one you cannot. Extraction failures are recoverable — re-running the extractor is cheap and overwrites the bad result. **A scope null is not**: it is stamped with `scopeCheckedSig` and never revisited, so it is permanent scope loss, and that is why the null count was the deciding measurement rather than speed or latency. Measured on this host, same prompts, against the real vault:

  | role | granite3.3:2b | nemotron-3-nano:4b |
  |---|---|---|
  | diary keywords | 99.0% precision, **0 invented** | 79.7%, 5 invented, 10 corrupted |
  | people names | F1 **0.938** | F1 0.920 |
  | speed on those two | **2.3-4.2x faster** | baseline |
  | scope nulls (permanent) | 2 | **0** |
  | merge adjudication | `review` on a true duplicate | **`merge`** (correct) |
  | merge draft retention | 90.7/5.1/9.3/14.6 — drops 3 of 4 records | 100/99.1/100/98.8 |

  **Do not collapse the split back to one model.** `ModelRoleRoutingTests` in `test_env_wiring.py` pins the call sites, and two of those tests are behavioural rather than source-shaped: they read the `model=` the lifted function was actually handed. That matters because the rewrite function swallows its own exceptions, so a missing `EXTRACT_MODEL` in the test's namespace degrades it to the heuristic fallback and the cache tests still pass while testing nothing.

  **Two models on one 4 GB card cannot both stay resident** (2.34 + 2.63 GB, plus ASR's 1.25 GB), so a role change evicts the other. That is the intended trade — the cheap high-volume role reloads fast and the expensive one stays warm across a reclassify — but it means a search immediately after a reclassify pays a load. `OLLAMA_KEEP_ALIVE=30m` covers the common case where one role dominates a session.

  **Search rewrite breaches `MEM_SEARCH_LLM_TIMEOUT=45` on a cold load on both models** (granite 65.5 s, nemotron 58.7 s; warm 2.6 s and 6.1 s). This is pre-existing and unchanged by the split, but it is now granite's load rather than nemotron's, and it is the one remaining measurement that argues for a higher `MEM_SEARCH_LLM_TIMEOUT`.

- **The live `.env` set all three chat models to `nemotron-3-nano:4b`** (now four roles across two models, above), and that replaced the `gemma3:4b-it-q4_K_M` recommendation below. It was changed because of a measured defect, not a preference. `phi4-mini` (the interim value, never documented) returned `[]` from the people extractor for *every* window of a 6.3k-char entry that opens with a `## Participants` heading naming six people — the answer was in the first 100 characters. Short inputs worked, so the failure was length-dependent, not a parsing bug and not truncation (`prompt_eval_count=1571` proves the prompt was fully evaluated). On the same entry, same prompt, same request shape, `nemotron-3-nano:4b` returned 20 names including all six participants. Search rewrite (12.5 s warm, inside `MEM_SEARCH_LLM_TIMEOUT=45`) and scope classification (16.2 s, valid `{"client", "context", "related"}`) were both re-verified on nemotron. **The people-extraction prompt needed no change** — the model was the defect. Do not "fix" that prompt.
- **A model swap is not free of re-verification.** The three chat roles have different contracts, and the one that was broken is not the one you would guess. Measure each role against the real input before declaring the swap good.
- **Nemotron echoes the `(none)` context placeholder back as a context value.** This is the third string it (or its predecessor) has lifted out of the client list — see the list at `migrate_client_context.py:689-694`. The guard at `_classify_scope` drops an answered context when the client has no projects, so today this is harmless, but *only because all seven clients currently have zero contexts*. Give one client a project and that branch stops being a no-op.
- **What is not a chat model: `MEM_EMBEDDER_MODEL`.** It must stay an embedding model (`nomic-embed-text`). "Use the new model for everything" is not a safe reading of the chat knobs — pointing the embedder at a chat model silently invalidates every vector already in Qdrant, and nothing errors. Verified in the running container's env, not just in `.env`.
- Merge LLM: `MEM_MERGE_MODEL` defaults to `gemma4:e2b` and is used only for dashboard merge-draft generation; override it if the host GPU cannot run that model

- Scope backfill LLM: `MEM_SCOPE_MODEL` (defaults to the query LLM) classifies unlinked facts/diary entries against existing clients on startup; `MEM_SCOPE_BACKFILL=0` disables it, `MEM_SCOPE_CONCURRENCY` (default 3) caps parallel classifications
- Backups: `MEM_BACKUP_ENABLED` (default 1), `MEM_BACKUP_HOUR` / `MEM_BACKUP_MINUTES` (local server time, default 03:00), `MEM_BACKUP_KEEP` (default 14), `MEM_BACKUP_DIR` (default `<dirname LOG_DIR>/backup`)
- Graph: `MEM_GRAPH_MAX_NODES` (600) caps the records in one graph response, keeping the most connected — see "Build Graph Mode"
- Chunking: `MEM_CHUNK_CHARS` (3000), `MEM_CHUNK_MAX` (16), `MEM_CHUNK_OVERLAP` (200), `MEM_CHUNK_FETCH_MULTIPLIER` (3), and startup re-chunking `MEM_RECHUNK_ENABLED` / `MEM_RECHUNK_LIMIT` (200) / `MEM_RECHUNK_CONCURRENCY` (2) — see "Long Records & Chunking"
- Chat timeouts: `MEM_LLM_TIMEOUT` (default 300, background passes), `MEM_SEARCH_LLM_TIMEOUT` (default 45, search rewrite), `MEM_LLM_CONNECT_TIMEOUT` (default 10) — see "LLM Chat Reliability"
- Chat logging: `MEM_LLM_LOG_CHARS` (default 1000, `0` = sizes only) — how much of each prompt and answer reaches `memory-vault.log`
- Query rewrite cache: `MEM_QUERY_CACHE_TTL` (default 900s) and `MEM_QUERY_CACHE_MAX` (default 256 entries); either `0` disables the cache — see "Query Rewrite Cache"
- Embedding retries: `MEM_EMBED_RETRIES` (default 2) and `MEM_EMBED_RETRY_BACKOFF` (default 1.5, in seconds), `MEM_EMBED_MAX_CHARS` (default 8000) — see "Embedding Reliability"
- Server status widget: `MEM_STATUS_POLL_SECONDS` (default 10) and `MEM_STATUS_HTTP_TIMEOUT` (default 8) — see "Server Status Widget"
- User vault resolved from a session cookie, `Authorization: Basic`, an access key, or proxy headers — see Authentication
- Sessions and access keys: `MEM_SESSION_DIR` (must be the bind mount, or a container rebuild logs everyone out) and `MEM_SESSION_SECURE` (adds `Secure` to the session cookie; only enable when the app is reached over HTTPS, since the process cannot detect the proxy's scheme and a `Secure` cookie on a plain-HTTP visit is silently dropped). `MEM_SESSION_SECRET` is no longer used to sign anything.
- Registration: `MEM_REGISTRATION_ENABLED` (default 0, and it should stay 0 — a route that creates accounts is something an operator turns on). The two methods have their own further requirements: email/password needs `MEM_SMTP_*`, Google needs `GOOGLE_CLIENT_ID` **and** `GOOGLE_CLIENT_SECRET`. Each is checked separately, so turning the flag on is not the same as having a usable form.
- Outbound mail: `MEM_SMTP_HOST`, `MEM_SMTP_PORT` (587), `MEM_SMTP_USER`, `MEM_SMTP_PASSWORD`, `MEM_SMTP_FROM`, `MEM_SMTP_STARTTLS` (default 1) and `MEM_SMTP_TIMEOUT` (20s). `smtp_configured()` needs only host and From, because those are what a signup attempt must have to be worth offering a form at all.
- Google sign-in (OAuth 2.0 redirect): `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, plus the callback `{BASE_URL}/api/auth/google/callback` registered in Google Cloud. **Both are required** — the secret is what makes the code redemption confidential. Note `test_env_wiring._DECL` had to grow a `GOOGLE_[A-Z0-9_]+` branch: without it a documented `GOOGLE_*` variable silently escapes the "every documented variable reaches the container" test, which is the same class of quiet as an unwired knob.
- `BASE_URL` must include `/mcp` prefix when behind nginx

## Diary Consistency & Auto-Fix

On startup, the lifespan runs in this order:

0. `migrate_client_context()` — Client-category facts → Client nodes, `WORKS_FOR` → `FOR_CLIENT`, Project facts → Context nodes (idempotent, skips migrated users)
0b. `llm_backfill_scope()` — Ollama classifies every fact/diary WITHOUT a `FOR_CLIENT` link against existing clients/contexts and links matches (self-limiting; model can only pick existing names-or-null)
1. `run_consistency_checks()` — Neo4j/Qdrant fact count mismatch, dangling MENTIONS, orphan categories, untitled facts (fact_manager.py, read-only)
2. `run_diary_consistency_checks()` — diary entry count mismatch, dangling MENTIONS, untitled/bad-timestamp entries (diary_manager.py, read-only)
3. `fix_diary_entries()` — sets `name = 'Untitled Diary Entry'` and/or `timestamp = now` on null/empty properties (diary_manager.py)
4. `sync_orphans()` — deletes Qdrant-only points, re-embeds Neo4j-only entries, `DETACH DELETE` orphan categories (fact_manager.py)

Encapsulation rule: diary persistence and consistency logic lives in `diary_manager.py`, not `fact_manager.py`. The server calls both independently.

**A count mismatch logged at steps 1–2 is not data loss.** The checks run *before*
`sync_orphans()`, so by construction they report the work step 4 is about to do.
Production log on a normal boot: `Diary count mismatch — Neo4j: 447, Qdrant: 371`
followed by 76 `in Neo4j only` entries, which are then re-embedded. Read the
mismatch as "step 4 has that many to repair", and the number of `Ollama embed
input too long` lines immediately after it as the ones that cost a retry. It
only becomes a real problem if the same mismatch appears on *every* boot with
the same count, which means step 4 is failing — check for a `sync_orphans` error
below it.

## Scope Reclassification Reliability

The full reclassify (`POST /api/maintenance/reclassify`) and the single-item paths rewrite every `FOR_CLIENT` link. The fixes below each address a way that used to silently produce wrong or missing scope.

- **Evidence snapshot before clearing.** The classifier's strongest signal is the `[client: X]` tag on a neighbouring fact, and those tags come from links the run is about to delete. `_capture_scope_snapshot()` reads the current assignments into memory first, so the signal is constant for the whole run instead of decaying as links disappear. Removing this is what made a long run progressively "forget" earlier items' clients.
- **Being named on an item is not evidence against being its client.** This is the rule to get right, and the direction was wrong twice. The first version said *"a participant's employer is not the client — decide from what the work is FOR"*, reasoned from a handover meeting about a bank's SAP estate. The user corrected the target answer: that meeting **is** EPAM's own engagement, run by EPAM's own architects, so the correct verdict is `EPAM` with **no** project. The shipped prompt says the opposite of the old rule — an item about an organisation's own people, systems and engagement **has** that organisation as its client, and "being named on an item is not evidence against being its client". Do not reintroduce a rule that treats a participant's employer as a disqualifier.
- **A `[own client: X]` tag is who that person is, and it agrees with the item.** The prompt once called a neighbour's tag "the strongest signal available" and told the model to "strongly prefer that client". It now says a MENTIONS/RELATED tag describes the *person*, and that when the item is about that person's employer, the tag **agrees** with the item rather than competing with it. `_scope_tag()` also relabels a `People` neighbour's tag to `[own client: X]`, because a 2b model reads the token, not the prose around it. `ScopePromptTests` pins the sentences: nothing else in the suite notices when a prompt sentence changes, and `cls.prompt` there is `ast.literal_eval`'d because the assignment is a chain of adjacent string literals.
- **The context list order is not a ranking.** An answer chose `PPC` because it is first in EPAM's list. The prompt says so explicitly, because "do not assign a context just because it is the only one available" was already there and the model filled the field anyway. See the guard below — the prompt alone was never enough.
- **A second subject is a `RELEVANT_TO` edge, not a competing `FOR_CLIENT`.** `_classify_scope` now returns a 4th element, `related` — the clients the item is *also* about — taken from the same LLM call, because a host slow enough to need a 300s budget cannot afford a second pass per window. `classify_scope_full` unions it across windows (the same reasoning as the client union) and strips the winner, since an item linked to its own client twice appears twice in a filtered list. `_related_clients_for()` then unions the model's list with every `client_tags_in_text()` tag, resolves each through `resolve_scope_name`, and drops the primary.
- **That is what makes a wrong primary guess survivable.** A neighbour's scope tag is *wrong* as a reason to file an item under that person's employer and exactly *right* as a reason to also link it there — the item really does discuss them. So the tag stopped being an override and became a cross-reference: even when the primary is wrong, the item is still findable under the attendees' employer.
- **A project the item never names is dropped in code, not by prose.** The classifier is reliable about the **client** and unreliable about the **context**: asked for both it fills both. Measured on the 4160-char handover entry, whose text names no project at all, it answered `PPC` in **six** prompt variants — including one that listed an explicit `(undefined)` option first and told the model to prefer it. The system prompt demonstrably reaches the model (without it, or with an unrelated one, it stops returning JSON entirely), so this is not a comprehension failure: it is a bias to complete the field. `context_named_in_text()` in `matching_utils.py` therefore checks the pairing where it is a fact — a project the item never mentions is not the item's project. `_classify_scope` drops such a context and logs it at INFO. It also drops a context that is not on the chosen client's own list (a cross-client pair, from the model taking a project off another line). Verified end-to-end: 3/3 runs now store `(EPAM, None, [])`. **Six prompt variants could not achieve what one token check did**; do not spend another six attempts on prompt wording for this.
- **`RELEVANT_TO` is written on both node types, and on the null-primary path too.** `_link_diary_relevant_to` takes a `label` parameter (it was hardcoded to `DiaryEntry`, so facts silently lost the feature entirely — the worst kind of partial support, because it looked like it worked). Both `_classify_and_link_fact` and `_classify_and_link_diary` call `_write_related_links` **before** their `return False`, because a null primary is exactly when `RELEVANT_TO` is the only signal left. `_write_related_links` swallows its own failures: a secondary association must not turn a correct primary scope into a failed reclassification, and `MERGE` makes a partial write idempotent.
- **Failure is not the same as "generic".** `_classify_scope()` returns `(client, context, related, ok)`. `ok=False` means the LLM call or JSON parse failed; the item is left unstamped so the next boot retries it. Only `ok=True` writes `scopeCheckedSig`. An Ollama timeout used to be recorded as a genuine "this is generic" verdict, permanently marking the item unclassifiable.
- **A hallucinated name is a real null, not a retry.** If the model names a client that matches nothing, the item is stamped (there is no point retrying the same prompt) but the mismatch is logged at WARNING.
- **Name resolution, not exact compare.** LLM answers vary ("Deutsche Bank" for "Deutsche Bank (DB)"). `resolve_scope_name()` in `matching_utils.py` resolves through an evidence ladder — exact → token subset → high token coverage → containment → fuzzy ratio — and always returns the *stored* spelling. It returns nothing when the top two candidates are within `SCOPE_NAME_AMBIGUITY_MARGIN`: an ambiguous winner is worse than no answer because it writes a confidently wrong link.
- **Explicit `Client:` headers are read, not guessed.** `client_header_value()` in `matching_utils.py` handles bold, bullets, heading markers and `:`/`-`/`–` separators, and cuts trailing prose. Keep it in `matching_utils.py` (not `migrate_client_context.py`) so it stays testable without the DB drivers.
- **Clearing is batched and off the event loop.** `clear_scope_links_batch()` / `clear_scope_links_async()` replace one blocking query per item with chunked round trips via `asyncio.to_thread`. The old per-item sync clear stalled the whole server for the length of a run.
- **A list in a MATCH property map matches nothing, and it fails silently.** `clear_scope_links_batch()` reads `MATCH (n {userId: $userId}) WHERE n.id IN $ids` — the id test must be `IN`. It was `MATCH (n {id: $ids, userId: $userId})`, which reads like "n whose id is in `$ids`" and is not: on the right of a property test a list is compared for **equality**, so it matches a node whose `id` literally *is* that list — no node, ever. The query returned `cleared = 0`, which is indistinguishable from "nothing to clear", and because clearing is how a reclassify *overwrites* the previous verdict, every run only ever **added** links. A real entry accumulated two `FOR_CLIENT` edges and an `IN_CONTEXT` left over from answers the classifier had stopped giving, `REMOVE n.scopeCheckedSig` silently never ran, and re-running the reclassify appeared to do nothing at all. **`test_cypher_safety.ListParameterInPropertyMapTests` lints this across every module** and derives the list-valued kwargs from the AST rather than guessing from a plural parameter name, because `where_ids` is a list and `status` is not. Do not reintroduce a list on the right of a property test; the existing test's `assertIn("$ids", query)` passed on the broken query, which is why a behavioural rule needed a lint.
- **`scopeManual` means a human set this scope, and a single-item reclassify is that human changing their mind.** The marker is written by `client_manager._stamp_manual_scope` and read in four places, all as `coalesce(..., false)`. A **bulk** reclassify skips marked items, which is the marker's whole purpose: an unattended run must not silently overwrite a hand-set scope. But the **single-item** path (`_reclassify_single_fact` / `_reclassify_single_diary`, GUI-only) used to raise `ManualScopeError` → 409 as well, and that made the feature a dead end: the only remedy the error could suggest was to clear the scope by hand first, i.e. exactly the destructive step the marker existed to make deliberate. The refusal is now `_clear_manual_scope()`, a `REMOVE n.scopeManual`, and `ManualScopeError` is deleted outright — a per-item reclassify is an explicit request for one item, which is the override.
  - **The marker is cleared *before* `clear_scope_links_async`, and that order is load-bearing.** The reverse leaves a window in which a crash means the item has no scope **and** still carries the flag that makes every later reclassify skip it — unrecoverable without raw Cypher. Marker-first's worst case is merely "no longer protected", which is what the user just asked for.
  - **Every read must coalesce an unset property to false, and dropping it fails silently in three different directions.** `coalesce(n.scopeManual, false)` is not decoration: bare `AND NOT n.scopeManual` evaluates `NOT null` → `null` → **row excluded**, so the *whole vault* is silently skipped; a bare count query under-counts; a bare `RETURN` alias is falsy in Python and only safe by luck. `test_cypher_safety.ManualScopeGuardTests::test_every_scopeManual_read_treats_an_unset_property_as_false` lints every Cypher occurrence and identifies a *write* by shape — preceded by `REMOVE <var>.`, or followed by `=` — because a `SET n.scopeManual = true` and a read of the same property are the same token.
  - **A 409 must say why, and it says it at both ends.** Server-side, `_conflict(exc)` in `gui.py` logs `f"api 409: {type(exc).__name__}: {exc}"` at **INFO** and returns the `HTTPException` — a refusal is the guard working, so ERROR would bury it in real failures, and the reason is data-dependent, so the access log (status only) never had it. Client-side, `apiFail(r)` in `dashboard.html` clones and parses the JSON body into `{status, detail}` instead of rejecting with the bare `Response`, and `toastApiError(e, fallback)` shows `e.detail`. The clone matters: a proxy error page must not be able to replace the status. Both are needed and neither is sufficient — the server's reason was being formatted into a detail and then discarded by the client.
- **A bad item does not abort the run.** Each item is wrapped; a failure increments the `errors` counter and leaves that item unstamped for retry. Items are drained from a queue by a fixed worker pool, not one `gather` task per row, so live tasks stay bounded by `MEM_SCOPE_CONCURRENCY` instead of the vault size.
- **Single-item reclassify touches one point.** `_backfill_qdrant(only_ids={item_id})` restricts both the Cypher and the Qdrant read to that item. The unbounded pass is O(vault) — it scrolls every point in both collections — which made a one-row UI action cost a full-vault scan.
- **Reclassify is scope-only. It must not extract participants.** `_classify_and_link_diary` used to open by calling a *private* copy of people extraction — `_extract_people_names` / `_link_missing_people`, defined right there in `migrate_client_context.py`, with its own `_PEOPLE_SYSTEM` prompt. So "Reclassify scope" in the UI also rewrote `MENTIONS` edges. The twin resolved to `SCOPE_MODEL` while the real extractor, `diary_manager._auto_link_people`, resolved to `EXTRACT_MODEL` — the same task on two models, and the one a reclassify happened to trigger was the judgement model, so every reclassify paid for participant extraction at window-count price (`MEM_SCOPE_TEXT_WINDOW` per window) on top of the classification it was asked for. All five twin functions are deleted. **Do not reintroduce participant extraction into the reclassify path**; its entry points are the save and update paths and the UI's *Extract participants* action, which is also where the `EXTRACT_MODEL` routing lives. The twin's docstring is the evidence for why it was a duplicate: it says the `content[:2000]` prefix was "still here after the twin in diary_manager was fixed", so the divergence was already known and had been papered over rather than removed.
- **Deleting the write is safe because the reclassify only ever read those edges.** `_fast_diary_scope`'s strongest signal is a unanimous client across the entry's `MENTIONS` neighbours, and `clear_scope_links_batch` deletes only `FOR_CLIENT` and `IN_CONTEXT` — so the evidence survives the clear intact. That is the property to preserve: if the scope clear ever grows a `MENTIONS` clause, the fast path loses its input and a reclassify silently drops to the LLM for every entry.
- **An assertion that counts occurrences is asserting on the dead code too.** `test_the_windowing_helper_is_actually_used` read `assertEqual(source.count("text_windows("), 2)` — two, because the twin called it as well as the classifier. Deleting the twin broke a test that was correct about the classifier. The count was never the property; the classifier's own call site is, and it is now asserted by name. **This is the `assertIn`-over-a-whole-file lesson again:** a guard that pins a number pins every contributor to that number, including the one you just deleted, so it fails on correct code and would have kept passing had the twin stayed while the classifier's call was the thing removed. Both halves were re-injected separately to confirm each bites.

- **The client list is JSON, and the header rule was inverted. Both were measured, not preferred.**
  `_classify_scope` used to render clients as `- Deutsche Bank (DB): DB AI Adoption` / `- SAP SE: (none)` and the system prompt told the model an explicit `**Client:**` header was "authoritative — use it and do not override it with content keywords". Production logs then showed both halves failing, and each failure is permanent because the verdict is stamped:
  - `"client": "DB"` — the model's natural abbreviation of a stored `Deutsche Bank (DB)`. `resolve_scope_name` misses it at **every** rung: token containment needs ≥2 tokens on each side, the bare-first-word fallback needs a token of ≥5 characters, and the fuzzy ratio clears nothing. An unresolved name is a real null, so the item was stamped with **no client**.
  - `"client": "Daimler AG (MBAG)"` — taken verbatim from the `## Client:` header of an MBAG RFI. The header names the **end customer**; the vault's clients are the **consulting firms**. Two namespaces, and "authoritative" told the model to echo one into the other's field. Not a stored client → stamped null. The model's own `context: "MBAG"` was the correct signal and was thrown away.

  The client list is now `[{"client": "Deutsche Bank (DB)", "contexts": ["DB AI Adoption"]}, {"client": "SAP SE", "contexts": []}]` — the pairing is *stated* rather than described, and an empty array says "no project" without needing the `(none)` placeholder explained in prose twice. The header rule now says the header is evidence of who the work is **for**, that it usually names the customer, and that it may never be returned by itself.

  Retested against the live model (`nemotron-3-nano:4b`, temperature 0, `think: false` — see Gotchas for why all three matter), five items, scored through the real resolver rules: **client 3/5 → 4/5, context 5/5 held.** The client gain is attributable to the format, not the prompt: the same prompt with the old line format still scores 3/5, and the same format with the old prompt scores 3/5. Only the combination scores 4/5.
  - **The `sapse` item is 4/5, not 5/5, and that is a labelling disagreement, not a defect** — every variant answers `SAP`, because the item is about SAP's own RAM/GRC platform, which is the rule the prompt states ("an item about an organisation's own systems has that organisation as its client"). SAP SE is only named as a participant's employer.
  - **A shorter prompt measured *worse*, and was not shipped.** A 3807→2483-char rewrite of the same rules scored 3/5 client and 4/5 context: it lost a correct context (`MBAG` → `PPC`) and, more seriously, stopped returning nulls — the deliberately generic item came back confidently stamped `EPAM`, where the long prompt correctly returned nothing. Null discipline is the thing to protect here, for the reason above: a null is recoverable and a guess is permanent. `ScopePromptTests` pins the sentences that carry it; do not compress the prompt without re-running the harness above.
- **Relabel the evidence instead of arguing with it in prose.** A reclassify run with the rules above still answered `{"client": "EPAM", "context": "PPC"}` for the handover entry. The entry's own text named no client and no project at all; the only client name anywhere in the prompt was `[client: EPAM]` on the two Enterprise Architects who ran it. Telling the model in prose that the tag was "NOT who the item is about" did not help — a 2b model reads the tag, not the instruction. `_scope_tag()` now renders a `People` neighbour as `[own client: X]`, which states the meaning in the token itself, and `client_tags_in_text` reads both forms so the `RELEVANT_TO` union still works. A prose rule only binds behaviour when the evidence it argues with is not competing for the same token.
- **A null has to be offered as a real answer, and a client with no contexts is not a dead end.** The same entry was stamped `context: "PPC"` — EPAM's first context — when it named no project. The prompt now says plainly that a null is "a real, correct, expected answer" and that it must "never fill a field to avoid leaving it empty", because a null is recoverable and a guess is stamped. Separately, `SAP SE` — whose name the text *does* contain — was never considered as the primary while `EPAM` was, because SAP SE has no contexts and EPAM has three; the prompt now says `(no contexts)` is a normal, valid choice and never a reason to prefer another client. A field the model can always complete is a field it will always complete.
- **The classifier reads the whole item, and the read is recorded.** This is the
  one that is a *correctness* bug rather than a precision loss, which is why it
  deserves its own rule. `_classify_scope` used to receive
  `item_text if len(item_text) <= 1500 else item_text[:1500] + "…"`, and the
  module's own `_extract_people_names` sent `content[:2000]` — the same defect
  the diary save path had already been fixed for. A long transcription is not
  a long document for the *classifier*; it is a 40k-char document whose client
  is named on page three. Because the reclassify verdict is stamped
  (`scopeCheckedSig`), a fragment-fed verdict is not merely wrong for one run —
  it is *permanent*, and no later run re-reads the text. `text_windows()` in
  `matching_utils.py` is the single pure implementation both LLM passes here
  use, and `classify_scope_full()` runs it: a client named by **any** window
  counts, disagreement is settled by most-frequent and then by the earliest
  window, and `ok=False` is returned only when no window found anything *and* at
  least one window failed — so a sibling timeout cannot discard a verdict that
  already has evidence behind it.
- **Do not cap the window count.** `SCOPE_TEXT_WARN_WINDOWS` is a warning about
  cost, deliberately not a limit; a cap would reintroduce exactly the silent
  tail-drop this replaces, just at a larger offset. The cost is one LLM call per
  window and it is only paid on items long enough to need it.

Do not reintroduce byte-exact name matching, per-item synchronous clears, a stamp written on LLM failure, or a prefix slice of the item text anywhere in the reclassify path. `test_matching_regressions.py::ScopeClassificationInputTests` walks the AST for a prefix slice applied to a text carrier — see the "gotcha" below for why it is an AST walk and not a substring search.

## Backup & Restore

`mem-mcp/backup.py` maintains **savepoints** under `BACKUP_DIR` (default `<dirname LOG_DIR>/backup`, i.e. `/app/backup` in Docker with `./mem-mcp-data/backup` bind-mounted there).

- A savepoint is a directory: `manifest.json`, `neo4j.jsonl.gz`, and one `qdrant-<collection>.snapshot.gz` per collection.
- **Ordering is load-bearing.** Qdrant is snapshotted *first*, then Neo4j is exported. A fact written in between then exists in Neo4j but not in the vector snapshot, and `sync_orphans()` re-embeds it. The reverse order strands a fact with no vector and nothing to rebuild it from.
- **Qdrant uses its own snapshot API**; the server-side copy is deleted after download so repeated runs cannot fill the volume. Restore is `DELETE` the collection then `PUT .../snapshots/upload`.
- **Qdrant snapshots are gzipped, and the restore path has two traps.** A chunked long record repeats its whole payload across every chunk, so the raw snapshot is mostly near-duplicate JSON — measured 3% of original on a repeating chunk family. Download streams through `gzip.GzipFile(fileobj=..., mtime=0)` (handed a file object, not a path, so the target's basename is not recorded in the header, and `mtime=0` so two exports of identical data are byte-identical and a diff is meaningful). On restore, `_open_snapshot()` decides by **filename suffix, not manifest version** — a version check would strand every savepoint taken before compression, and the suffix is the only thing on disk that cannot lie. `_upload_name()` **rebuilds** the multipart filename as `<collection>.snapshot` because Qdrant validates the extension and rejects `*.snapshot.gz` outright, after the collection has already been dropped.
- **Neo4j Community has no online backup** (`neo4j-admin database backup` is Enterprise; `dump`/`load` need the DB stopped), so the graph is exported over Bolt and replayed. Relationships resolve endpoints by **business key, never `elementId`** — elementIds are reassigned by any dump/load, so keying on them produces edges that point at nothing after a restore.
- **Temporal properties are type-tagged.** `_encode_value()` writes `{"__t": "datetime", "v": "<iso>"}`; the replay re-applies them with the matching Cypher constructor. Without this, a `ZonedDateTime` returns as a plain string and the corruption does not surface until something queries a date.
- Dynamic relationship types are rebuilt with `apoc.create.relationship()` (APOC is already required by compose).
- After a restore, `_reconcile_after_restore()` re-runs `sync_qdrant_scope()`, `restore_scope_links()` and `sync_orphans()` so the two stores agree again.
- **Savepoints are vault-wide, not per-user** — the Qdrant snapshot API is per-collection and the graph export is a full dump. The GUI session still gates who may trigger one.
- **The scheduler is an in-process task** started in the lifespan (`scheduled_backup_loop()`); the container has no Docker socket, no `neo4j-admin` and no APScheduler. A restart after the scheduled hour waits for the next day.
- **Savepoints are never watched on a timer.** The panel fetches once on initial load (deduped via `savepointsLoaded`/`savepointsInFlight`), again on manual refresh, and otherwise only while a job is actually in flight. `startBackupPolling()` arms a 2.5s interval; the first terminal state read calls `stopBackupPolling()`, so an idle vault makes no requests.
- **`renderBackupStatus()` must stay a pure renderer.** Completion side effects live in `applyBackupCompletion()`, reached only from `backupTick()` and guarded by `backupSettled`. A `render*` function that re-fetches its own input is a loop: `renderBackupStatus('done')` → `loadSavepoints()` → `renderBackupStatus('done')` … was measured issuing 500+ requests for a single finished backup.
- `prune_savepoints()` keeps the newest `MEM_BACKUP_KEEP` **complete** savepoints only, so a failed attempt is never promoted into the retention window.

### Maintenance lock and the vault move

`migrate_vault_user.py` is the one rewrite that takes **no** maintenance lock.
Every other job that rewrites the graph in bulk does — see the section below —
and the reason does not apply here: a vault move is operator-initiated and is
expected to be the only thing happening, and it takes a per-user lock for a user
whose lock the running server is not holding. What that costs is precise and
documented rather than fixed: the migration's completeness check compares exact
counts taken before the first write, so a `rechunk_unindexed_records()` or
`sync_orphans()` running against either vault mid-migration makes a healthy move
report dirty. Do not run the migration and those background passes at the same
time.

### Maintenance lock

`claim_maintenance()` / `release_maintenance()` in `common.py` are a per-user in-process mutex. Reclassification and backup/restore both rewrite large parts of the graph; running them together interleaves the writes. Every maintenance job takes the lock when it starts and releases it in a `finally` block, and the API answers `409` with a human-readable reason when it cannot.

The scheduled backup is the one exception: it is vault-wide, so there is no single user whose lock it could take. `scheduled_backup_loop()` calls `active_maintenance()` first and defers by `_RETRY_SECONDS` if any user holds the lock. Keep it that way — calling `run_backup()` directly from the scheduler would snapshot a graph mid-reclassify.

## Moving a Vault Between Users

`mem-mcp/migrate_vault_user.py` hands everything belonging to one vault user to
another. Both usernames are arguments, so neither is ever written into the
repository, and the run **is a dry run unless `--apply` is passed** — the
opposite of `reindex_chunks.py`, because this moves the only copy of somebody's
data and there is no undo.

```bash
python mem-mcp/migrate_vault_user.py --from OLD_USER --to NEW_USER            # report only
python mem-mcp/migrate_vault_user.py --from OLD_USER --to NEW_USER --apply
```

**Take a savepoint first** (Setup → Backup, or `POST /api/backup/run`). The two
stores are not transactional together, so a failure between them leaves a vault
mid-move. Re-running converges **once the destination is empty again** — and
after a partial move it may not be, because the Qdrant half can land first. Clear
the destination's leftovers (or restore the savepoint) and re-run; a savepoint is
cheaper than working out which half got there.

### A user is four stores, not a profile row

The username is the partition key of all four. Touching three leaves a vault
that looks empty in one place and full in another:

| store | what carries the user |
|---|---|
| Neo4j | `:Fact`, `:DiaryEntry`, `:Client`, `:Context` (`userId`), plus a `:User {id}` hub |
| Qdrant | `userId` in the `ea_memories` and `ea_diary` payloads |
| SQLite (`sessions.db`) | `psks.user_id` — the access keys |
| htpasswd / `credentials` | **deliberately not touched** |

`:Category` is **global** — it carries no `userId` — and is never rewritten.

### The ids are derived from the username, and that is the whole trap

`client_id_for` / `context_id_for` in **`scoped_ids.py`** are the only place that
formula lives:

```
client_id  = uuid5(NAMESPACE_DNS, f"client_{user_id}_{name.strip().lower()}")
context_id = uuid5(NAMESPACE_DNS,
                   f"context_{user_id}_{client_id}_{name.strip().lower()}")
```

`db_create_client` and `db_create_context` call them. **Do not re-inline the
`uuid5` in either place.** A migration that rewrites `userId` alone moves the
data and leaves those ids stale, and the damage is invisible in the vault you
just moved: the next create with an existing name derives a *different* uuid, so
the vault ends up with two `Client` nodes of the same name — which reads as the
scope filter being broken. A `Context` also stores its parent's id in a property,
so it is re-derived against the parent's **new** id, and `IdRemap.context_parents`
carries that separately from `IdRemap.contexts`. The two maps have the same shape
and the same key type, so passing one where the other belongs points every
Context at a sibling that does not exist.

`plan_id_remap()` builds both maps and the refusal conditions as a **pure
function over plain dicts**, so the whole plan is testable with no database. It
returns `duplicate_names` as well: two same-named Clients, or two same-named
Contexts under one parent, derive one id — and `destination_occupied` only
inspects the destination, so without that the move would leave two nodes sharing
an id. The Context key is `(parent, name)` rather than the name alone, because
two contexts of one name under *different* clients are two different nodes with
two different ids and must not be refused.

### Qdrant is written first, and within it the scope retarget goes before the re-own

`perform_move()` is a function rather than a run of statements in `run()` for the
same reason the rest of this repo pins call sites: asserted from source text it
only proves two `await`s are on the right lines, and it stays green through the
refactor that hoists them into a helper and reverses them there. It is driven
with fakes in `PerformMoveOrderTests`.

Both facts come from one place: **every read is a scroll filtered by the old
`userId`**, so once a point has been re-owned it is invisible to the step that
still has work to do on it. The wrong order is not an error — it is a silent
success that patches nothing. A `Context` id in a payload is retargeted the same
way; `clientName`/`contextName` are not, because a name is not derived from the
username.

**The scope retarget uses the id-list form of `set_payload`, not per-point
payloads.** `set_payload` takes either one payload applied to a list of point ids,
or a list of per-point payloads — and the second needs a `PointStruct`, a pydantic
model whose **`vector` field is required**, a field `set_payload` ignores
entirely. An earlier version omitted it, no local test could see it because the
fake was more permissive than the real model, and the container raised a
`ValidationError` on the first real point. Points are therefore grouped by their
patch (a handful of distinct pairs in any vault) and written with
`points=[ids]` — the form `client_manager` already uses in production, so there
is no model to construct and no version-sensitive field to get wrong. **A fake
that is more permissive than the dependency it stands in for is a guard that
reports green on the defect it is standing in for**; `FakeQdrant.set_payload`
now rejects the per-point form outright rather than accepting it.

Grouping has a sharp edge worth stating: `set_payload` writes whatever it is
handed, so the flush **drops the keys it is not changing**. A group key is a
fixed-width tuple with `None` marking an absent half, and carrying those through
would write `contextId: null` over a point that never had one.

Note that `move_qdrant_user` **mutates the filter it is paginating over**. That is
only safe because Qdrant's `offset` is a cursor over the collection's own point
order with the filter applied per point — not an index into the filtered result.
`PaginationTests` models it that way deliberately; modelling it as a filtered
index would demand a "fix" for correct code.

### What it refuses, and what it leaves behind

Refused, with a reason: source and destination the same user; an empty source
(facts, diary, scope nodes or points — a scope-node-only source is not empty);
a **non-empty destination** — merging two populated vaults has to reconcile ids,
duplicates and scope across both, and is a different operation this does not
attempt; a destination already holding any record, client, context **or Qdrant point**
(the preflight reads all four — a destination whose Neo4j was emptied while its
vector points survived is what a stopped `sync_orphans` leaves behind, and
comparing the source's point count against a destination total that already
included those reported a healthy move as dirty); a destination already holding
one of the names; **duplicate names in the source** (two same-named Clients, or two same-named Contexts under one client —
they derive one id, and `destination_occupied` cannot see it because it only
inspects the target); and a destination that gained a record between the
preflight and the write, which is re-checked **inside** the transaction. That re-check sees the
graph only — it cannot see Qdrant, so a destination-only vector write landing
between the preflight and the transaction is unguarded, exactly as the preflight
is for the other direction.

That abort is a **dedicated exception type**, not a bare `ValueError`, and the
distinction is load-bearing: the handler in `run` turns it into a clean refusal,
so a bare `except ValueError` around the whole of `perform_move` would also
swallow a failure from the credential half — *after* Qdrant and Neo4j are fully
rewritten, with no `FAILED:` line. That is the exact failure the abort message
exists to prevent, reached by a different route. For the same reason **its
message must not claim nothing was written**: the Qdrant half is already
rewritten by the time it fires.

Left behind on purpose:

- **`DiaryEntry.id` is user-derived but is not rewritten.** Chunk 0 of a Qdrant
  family *is* the record id, so re-deriving it means re-embedding every chunk of
  every entry — a large, lossy-risk operation bought to avoid a collision that
  needs the same timestamp to the stored resolution. `Fact.id` is a `uuid4` and
  was never affected.
- **Live sessions are revoked, not re-pointed** (`delete_sessions_for_user`). A
  session open across the move would otherwise keep writing under whichever vault
  it started with. `transfer_psks` hands the access keys over in one scoped
  `UPDATE`, so an MCP client holding a key keeps working and starts seeing the new
  vault — which is the point.
- **The old account itself is untouched**: htpasswd entry, `credentials` row and
  `google_identities` row all stay, so it can still sign in and finds an empty
  vault. The leftover `:User` hub node is reported as `source_user_node_left`.

### It ends with a verification pass, and exits non-zero if it is not clean

`verify()` re-counts both stores and looks for Qdrant payloads whose scope id
matches no node. **The Client and Context id sets are kept apart**: merged into
one set, a Context whose `clientId` wrongly holds a *context* id tests as present,
so the one corruption the check exists to catch would report as clean.

Anything left behind or dangling prints `FAILED:` and returns 1 — a migration
that reports a dirty result and exits 0 is the failure mode worth precluding,
because the operator reads "Done" and stops looking. Seven conditions feed it, in five rows:

| condition | why it is not obvious |
|---|---|
| records still in the source | **including clients and contexts**, not just facts and diary — a scope node left behind is a whole client the user cannot see |
| Qdrant points still carrying the source's `userId` | invisible to any graph count |
| dangling `clientId` / `contextId` payloads | counted separately per kind |
| facts, diary or points **missing at the destination** (three comparisons) | "nothing was left behind" and "everything arrived" are different claims, and only the first was being made — the pre-move counts are recorded and compared |
| access keys that did not move | SQLite is the one store verification cannot see: run with a `MEM_SESSION_DIR` different from the app's, `transfer_psks` opens a *different* database, moves nothing, and everything else reports clean |

**The completeness comparison is exact, so run it with nothing else writing.**
It compares the destination against a snapshot taken before the first write, and
two documented background passes can move those numbers underneath it:
`rechunk_unindexed_records()` **adds** points for large records and
`sync_orphans()` deletes Qdrant-only ones. Either running against either vault
mid-migration makes a healthy move report dirty. This script takes **no
maintenance lock** — see "Maintenance lock" below — because a vault move is
operator-initiated and expected to be the only thing happening; do not run it
alongside a reclassify, a backup or a boot-time reconcile.

`_count_keys` reads SQLite and is called **only on the `--apply` path**,
because `sessions.db_path()` creates its directory: counting keys during a dry
run would write a database and then print "Nothing was written", which would be
false. `CountKeysTests` asserts that — by collecting the calls and checking the
list is empty, since checking only the exit code passes on the defect it names. It returns `None` rather than raising when the store is unreadable, so an
unavailable SQLite reports a skipped check instead of aborting a migration that
has not started.

`RunTests` **drives `run()` end to end** for every one of those, because every
other assertion about it reads the source and flipping `return 1` to `return 0`
is invisible to that. The dirty conditions, the ways a destination can be
non-empty, and the remaining refusals are each one table-driven test rather than
one test per row: they share a fixture and a single exit-code assertion, and the
subTest label carries the same information a method name would.

`MigrationScriptTests` is deliberately short for the same reason. Everything it
once asserted by `assertIn` over the source — the refusals, the ordering, the
access-key handoff, the dangling check — is now driven against fakes elsewhere in
the file, and a substring assertion is strictly weaker: it pins a token, not the
property. What is left is the three things no behavioural test can reach: the
argparse surface, the fact that `run()` delegates its writes to a single
testable function, and the set of credential functions the tool must never call.

`move_graph` returns `RETURN count(...)` rather than `len(rows)`, so a row whose
`MATCH` matched nothing shows as 0 instead of hiding.

There is deliberately **no "every id would come out unchanged" refusal**. Its only
reachable case was a same-user move, which is refused above, so in practice it
caught exactly one thing: a vault holding records but no `Client`/`Context` nodes
at all. Clients are created on demand, so that is an ordinary vault and the move
does real work — re-owning every fact, diary entry and Qdrant point.

It is handed `remap.client_rows()` rather than `remap.clients`, and the two differ
in exactly one case: a row whose stored id is **already the id the destination
would derive**. A hand-written id is not that case — it is *changed* to a derived
id and lands in `clients` normally. The row that is already derived is a
pre-existing inconsistency (an id minted for another username, or one migrated by
hand), and it still needs its `userId` rewritten or it stays behind in the source
vault while everything around it moves.

`IdRemap` has **three** row accessors, not two. `context_rows()` is the Context
counterpart of `client_rows()`; `context_parent_rows()` is a third thing and
exists because `context_parents` is populated for **every** row rather than only
the changed ones — a `Context` whose own id comes out unchanged can still have a
stale `clientId` property, and defaulting a missing entry to the context's own id
writes a node pointing at itself. That was a real bug here, and `verify()` reads
Qdrant payloads only, so nothing downstream reported it.

### No vault names belong in this repository

`NoVaultNamesCommittedTests` in `test_vault_migration.py` asserts that neither
username argument has a default, that the two new modules bind no string literal
with the shape of an account name (three shapes: lowercase-with-separators,
CamelCase, and spaced capitalised words — nothing forces the stored spelling to be
lowercase), and that the usage block uses placeholders. Two things about that
guard are deliberate. It judges literals by **value**, not by the name they are
bound to: a version keyed on the constant's name passes on a real vault name
assigned to something innocuously named, and on any `AnnAssign` or in-function
binding. And it is **scoped to the two new modules** on purpose: over `sessions.py`
and `client_manager.py` it needs ~90 legitimate column names in the allowlist
and would rot on the next schema change, and a guard that needs updating whenever
unrelated code moves is a guard that gets deleted.

## Long Records & Chunking

One embedding vector over a long fact or diary entry is a lossy average of the whole document: a query about a detail in the middle scores poorly against everything else. `mem-mcp/chunking.py` splits a long record into several vector points instead. It is dependency-light (no DB drivers) so it is unit-testable on its own, like `matching_utils.py`.

- **Chunk 0 keeps the record id.** Every existing id-based path keeps working for chunk 0, and a record short enough not to chunk is written byte-identically to before, so existing data needs no migration. Chunks 1..N-1 get deterministic ids from `chunk_point_id()` (`uuid5` under a pinned `CHUNK_NS`), which makes a rewrite idempotent instead of accumulating orphan points.
- **Every chunk carries the full record payload**, plus `parentId`, `chunkIndex`, `chunkCount`. The scope/category/keyword filters are evaluated against the payload at query time, so a chunk with a partial payload would be invisible to those filters.
- **Only chunk 0 carries `text`/`content`.** Chunks 1..N-1 carry `chunkText` and omit the full text entirely, so a search that forgets to hydrate cannot return a fragment as if it were the whole record. Hydration is a single batched `retrieve` per search.
- **`parent_of(point_id, payload)` is the only correct way to get a record id from a Qdrant point.** Every reconciliation pass must use it. Raw `str(point.id)` is wrong in both directions: chunk ids would look like orphans, and a delete would leave the rest of the family searchable.
- **Search collapses a family to its best chunk.** Results are keyed by parent and a record appears once. Because a chunked record contributes N points to every vector query, `fetch_limit` is multiplied by `CHUNK_FETCH_MULTIPLIER` (default 3) — without it a single long entry can occupy the entire top-N window and no other record is ever returned.
- **An update deletes the old family before upserting.** Shortening a text produces fewer chunks, and the leftover high-index points would keep answering for text that no longer exists. `replace=True` does this, and it happens *after* all chunks are embedded so a rejected embed leaves the previous version searchable.
- **Scope is a property of the record, not the point.** `_backfill_qdrant` diffs per record and writes to every point of the family; `restore_scope_links` links once per record. Keying either by point id makes every chunk mismatch and silently strips the scope keys, which then surfaces as searches returning facts the user filtered out.
- **The same rule applies to a manual scope change, and to deleting a client or context.** `db_set_fact_scope` / `db_set_diary_scope` in `client_manager.py` patch and drop `clientId`/`contextId` in Qdrant, so they resolve the family through `_scope_targets()` first. Addressing one point by the record id is chunk 0 only, which is why it looks right until a record is long enough to chunk. `_drop_scope_payload` is the sharper version of the same trap: `db_delete_client` / `db_delete_context` pass it **record** ids read out of Neo4j, so passing them straight through would leave chunks 1..N-1 holding a `clientId` for a node that was just `DETACH DELETE`d — the record keeps answering a client-filtered search for a client that no longer exists. It expands them through `_scope_targets()` too.
- **Chunk ids are derived, never stored as an index.** The count of points for a record is discovered by scrolling on `parentId` (`find_chunk_family`). Deriving it from `chunkCount` would need the payload, which is gone by the time a delete runs, and a stale `chunkCount` would orphan points.
- `MEM_CHUNK_CHARS` (3000), `MEM_CHUNK_MAX` (16), `MEM_CHUNK_OVERLAP` (200) and `MEM_CHUNK_FETCH_MULTIPLIER` (3) tune it. `CHUNK_MAX` is a *call budget*, not a coverage limit: when honouring it would drop the tail, `chunk_limit()` raises it to whatever keeps every part inside the embed ceiling. Silently abandoning the end of a document is the failure mode chunking exists to prevent.

Existing long records are converted with `python mem-mcp/reindex_chunks.py --dry-run` first, then without. It is idempotent and skips anything already chunked.

**Startup re-chunking does the same work automatically.** `rechunk_unindexed_records()` in `fact_manager.py` runs as a background task in the lifespan, *after* `sync_orphans()` so the two stores are already consistent. Two filters keep it cheap, and both matter: a record is only a candidate if it is **large enough to split** and **not already chunked**. Re-chunking an already-chunked record costs one embedding call per chunk to rebuild identical vectors, so `_chunked_record_ids()` reads the whole collection's chunk state in a single scroll rather than one lookup per candidate, and it returns `None` on failure — an empty set there would make every large record look un-chunked and rewrite all of them on every boot. Candidates are sorted longest-first so a boot that hits `RECHUNK_LIMIT` spends it where recall is worst, and the remainder is left for the next restart rather than started and abandoned. A failed record is counted and the run continues. Tunables: `MEM_RECHUNK_ENABLED`, `MEM_RECHUNK_LIMIT` (200), `MEM_RECHUNK_CONCURRENCY` (2).

## Embedding Reliability

`get_embedding()` in `common.py` is the single choke point for every vector write, and Ollama fails it more often than the rest of the stack: a model still loading, a GPU under memory pressure, or a busy container behind a rolling restart. A bare `raise_for_status()` turns any of those into a `httpx.HTTPStatusError` whose only text is `500 Internal Server Error` — the operator learns nothing about whether the model is missing, the host is OOM, or the route moved.

- **Two routes, legacy first.** `("/api/embeddings", {"model","prompt"})` then `("/api/embed", {"model","input"})`. Both response shapes are accepted (`{"embedding": [...]}` and `{"embeddings": [[...]]}`). `ollama/ollama:latest` is unpinned in compose, so the surface can genuinely shift between deploys — a 404 on the legacy route drops straight through to the modern one instead of burning retries on a 404 that will never recover.
- **Retry only what can recover.** `_EMBED_RETRY_STATUS` is `408/409/429/500/502/503/504`; backoff is `EMBED_RETRY_BACKOFF * attempt`. A `400`/`404` (missing model, bad request) is not retried on that route — it moves to the other route and then fails.
- **Log the reason, not the status.** `_ollama_detail(resp)` prefers Ollama's own `{"error": ...}` body and falls back to the raw text. Without this, the traceback said `500` and nothing else. The reason is carried in `last_detail` and surfaced once in the terminal error, not per attempt — see the logging policy below.
- **An over-long input is a length error wearing a 500.** Ollama answers `the input length exceeds the context length` with HTTP 500, so it looks retryable and the loop re-sent the same oversized text five times before failing with a message that never mentioned length. `_is_input_too_long()` classifies it as deterministic: the remedy is a *smaller input*, not another attempt, so the text is halved and re-sent with no backoff. Do not reclassify these hints as transient — that is what made the second production incident take five requests and still fail.
- **Truncation keeps head and tail.** `_truncate_for_embed()` cuts to `EMBED_MAX_CHARS` and keeps 75% from the front and the rest from the end, with the `\n...\n` marker counted *inside* the budget. A transcription puts the subject at the top and the conclusions at the bottom, and a search query is far more likely to match the tail. The marker being inside the budget is not cosmetic — measuring it showed a 1000-char budget producing 1005 chars, which defeats the point of a ceiling.
- **`EMBED_MAX_CHARS` is a budget, not the model's actual ceiling.** Ollama enforces a real token limit below it, so a record that passes the budget check can still be rejected as too long. Production log: an 11189-char input was refused and only 6000 was accepted, which puts the real window at ~2048 tokens — about 2000 tokens of English. The default is **8000** for that reason, and the old 12000 was over budget for this model. The shrink ladder absorbed it, so nothing was lost, but every long record paid a rejected request first. The two log lines to compare are `input is N chars, over the 8000 budget — truncating` (budget hit) and `Ollama embed input too long on /api/embeddings (N chars) — retrying with` (the real ceiling is below N). `chunking.EMBED_CEILING` reads the same variable and **must keep the same default** — it sizes chunk parts, so a mismatch would size every chunk for a ceiling the embedder refuses.
- **The cache key stays the original text.** Truncation is a transport detail; keying the cache on the truncated form would let two different long facts that share a prefix collide, and a search query that truncated onto a stored fact's prefix would get that fact's vector back as its own answer.
- **Final failure is a `RuntimeError`** naming the model and quoting the reason plus the `docker exec ollama ollama pull <model>` fix. Nothing upstream catches `httpx.HTTPStatusError`, so the exception type change is safe.
- **A tag is part of the model's name, and `MEM_EMBEDDER_MODEL` usually omits it.** Ollama reports `nomic-embed-text:latest` in `/api/tags`, so `model in installed` never matched and `ensure_ollama_models` re-pulled a 274MB embedder on every boot (23 times in the week it was found). `_ollama_model_matches()` treats a tagless name as satisfied by `:latest` or `:any`, while a name that *does* carry a tag must still match that tag or `:latest` — `qwen3.5:0.8b` must never be satisfied by an installed `qwen3.5:2b`, or the vault is silently embedded with a different model than configured.
- **The log level is a contract, not cosmetics.** Embedding is the high-volume path — one call per query variant per search, plus one per write — and production runs at `LOG_LEVEL=WARNING`. Per-attempt failures and retries are `DEBUG`; **input-too-long is `WARNING`** (rare, and it means the stored vector is lossy); **a failed embed is `ERROR`, logged once**, carrying Ollama's own reason and the `ollama pull` fix. `WARNING` is reserved for chat/LLM traffic. Emitting a `WARNING` per embed call buries the chat traffic that the level exists to surface. `test_embedding_reliability.py` asserts all three of these.
- **A failed embedding never leaves a half-written record.** Every write path (`db_add_memory`, `db_update_memory`, `db_save_diary`, `db_update_diary`) embeds and upserts to Qdrant *before* it writes Neo4j, so a raise means the write never happened — the user gets an error, not a record with a missing vector. It also means the two stores never disagree about the text. `db_update_diary` had the two steps the other way round and left Neo4j holding text the vector store could no longer find; `WriteOrderingGuardTests` now asserts the order against the source, because nothing about that failure is loud. Do not call `get_embedding()` directly on these paths — route it through `_upsert_fact_points`/`_upsert_diary_points` so the guard covers it and the chunk family is handled.

Tests for all of the above live in `mem-mcp/test_embedding_reliability.py`. `common.py` cannot be imported without the DB drivers, so that file lifts the real functions out of the source with `ast.get_source_segment` and execs them against stubs — it tests the shipping code, not a copy of it.

## Query Rewrite Cache

`rewrite_search_query` is on the critical path of every search and costs one LLM call — measured at 12.5 s warm on `nemotron-3-nano:4b`, tens of seconds on a slower quant. It is the one expensive call in the request path whose answer does not depend on the contents of the vault: its inputs are `(query, category, client, context, expand)`. Repeating a search therefore re-asked a settled question and got a slightly different answer each time, which is a worse property than being slow.

- **Cache the rewrite, not the results.** Qdrant answers change as the vault changes; a cached result list would be a correctness bug. The rewrite is pure over its inputs.
- **Only a real answer is cached.** The `_expand_query` heuristic fallback and the exception path are deliberately not stored. A timeout is transient, and pinning the degraded result for the TTL would turn one slow call into a permanently worse search. `test_matching_regressions.RewriteSearchQueryCallSiteTests` pins this by counting LLM calls, not by reading the source.
- **Both paths out of the cache return a copy.** `list(cached)` on the read path, and `list(variants)` on the write path — the list handed to `put()` *is* the cached object, so returning it directly let the first caller's in-place append rewrite what every later search saw. That was a real bug, caught by the call-site test, and the two copies are the fix.
- **`TTLCache` uses an injectable clock.** Expiry is testable without sleeping, so the TTL boundary is pinned exactly instead of raced. It expires on *read*, not only on write: a cache that evicts only on write still holds the memory of every query ever asked, which is the thing the bound exists to prevent. Eviction is oldest-first for free, because `put` re-inserts at the end of the dict.
- **The key normalizes `None` and unhashable parts.** Filters are optional strings the caller does not control; a stray list raising `TypeError` on the lookup path is a 500 the caller can do nothing about.
- `TTLCache(ttl<=0)` or `max_entries<=0` is inert rather than unbounded — a cache that cannot expire must not retain entries.
- `TTLCache` is not thread-safe by design. It holds values derived from a model call, never authoritative state, so the worst a lost update costs is one extra LLM call.

## LLM Chat Reliability

`get_llm_response()` in `common.py` is the single choke point for every chat call. It is the *other* Ollama path and it failed differently from the embedding one: not a bad status code, but a call that simply never came back.

A reclassify returned `503 Service Unavailable` and was read as "out of memory loading the model". It was neither. The log held the request line, then a **60.07s gap**, then the next request — the signature of `httpx.AsyncClient(timeout=60.0)` firing. The reclassify had *not* failed: `classify_scope_full`'s per-window `except` caught the failed window, set `ok=False`, and the entry was left unstamped for retry, then the next window succeeded. The 503 the user actually saw came from their **second click** hitting the maintenance lock held by the still-running first. Nothing about that was visible.

- **The read budget is its own knob, and it is not 60s.** `LLM_TIMEOUT` (`MEM_LLM_TIMEOUT`, default **300**) for background passes; `SEARCH_LLM_TIMEOUT` (`MEM_SEARCH_LLM_TIMEOUT`, default 45) for the search-query rewrite, which sits between the user and their results and must not inherit the background budget; a per-call `timeout=` argument for anything else. The measured reason: a 4420-char prompt took **46s** on `qwen3.5:2b` on the production host. A 60s cap is not a safety margin on a machine doing CPU inference, it is a coin flip.
- **Connect stays short and independent.** `LLM_CONNECT_TIMEOUT` (10s) so a genuinely dead Ollama fails fast instead of burning the full read budget first. It is a separate argument to `httpx.Timeout`, not the same value.
- **A chat timeout used to write nothing at all.** The request line was logged at WARNING *before* the call and the response line only *after* it returned, with no `except` in between — so a timeout was indistinguishable from a request that never happened, which is why this took a log dive to explain. Every failure mode now logs at ERROR with the model, the prompt size, the elapsed time and the knob to turn, and raises `RuntimeError` naming the remedy. Nothing upstream caught `httpx.ReadTimeout`, so the type change is safe.
- **An empty answer is logged, not raised.** A model that loads and then produces nothing is the memory-pressure shape the user suspected — and it used to return `""` in total silence while callers fell back to the raw query or to no keywords, so the vault quietly got worse. Raising here would break those deliberate fallbacks and turn a degraded search into a failed request, so the fix is the ERROR line, not an exception. Keep it that way: *log* the empty answer, never throw on it.
- **The log carries the content, not just the size.** `prompt_chars=3572` tells you a call was big; it does not tell you what the model was shown or what it said, so a mis-scoped or hallucinating verdict could not be checked without reproducing the call. `_llm_excerpt()` adds `system=`, `prompt=` and `content=` (and the raw `body=`) capped at `LLM_LOG_CHARS` (`MEM_LLM_LOG_CHARS`, default 1000), with `…+N more chars` so the total is still known. It **escapes newlines, tabs and backslashes** — prompts are multi-line by nature, and a raw newline in a `RotatingFileHandler` record makes one event span several lines, at which point `grep` reports a fragment as if it were the whole message. `MEM_LLM_LOG_CHARS=0` restores sizes-only, and that is a privacy switch, not an "unlimited" one: this writes entry and meeting text into the log file. The 1K cap is per field, not per line, so a 40k-char entry cannot put 40k chars on a reclassify line.
- **A 503 must say why.** All 44 `except RuntimeError` handlers in `gui.py` route through `_service_unavailable(e)`, which logs the reason before building the response. The detail used to be formatted into an `HTTPException` and discarded: the access log recorded the status and nothing recorded the cause. "Another maintenance operation is running" and "Ollama timed out" are completely different problems, and the most informative message in the codebase was the one that never got logged. Do not reintroduce a bare `raise HTTPException(status_code=503, detail=str(e))` — `ServiceUnavailableLoggingTests` checks for it.
- **A 503 from a single-item reclassify is usually a lock collision, not a failure.** `reclassify_single_*` holds the per-user maintenance lock for the whole run and releases it in a `finally`. With the windowing above, a reclassify is several LLM calls at tens of seconds each, so a second click is very likely to arrive while the first is still running and get an instant 503 with `"Another maintenance operation (backup/restore/reclassify) is running"`. Read the lock message as "the first one is still going", and check whether it completed before assuming the reclassify failed.

Tests live in `mem-mcp/test_llm_reliability.py`, using the same `ast.get_source_segment` lift as the embedding suite. They are behavioural rather than source-shape checks **on purpose**: the defect was the *absence* of a log line, and only a fake client that actually raises can assert that. Reinjecting the old body makes 17 of the 23 fail and drops the recorded ERROR lines from 1 to 0.

## Server Status Widget

A status strip pinned to the bottom of the left rail of the **Memories** and **Diary** tabs, and a full model panel on **Setup**, both fed by one snapshot pushed over SSE from one poller. It answers the question the "must say `100% GPU`" rule in Critical Config keeps needing answered, and it puts an unload button where the model it evicts is named.

- **One poller for the process, not one per browser tab.** `status_monitor.broadcast_loop(mem.fetch_ollama_status, mem.STATUS_POLL_SECONDS)` is started in the `server.py` lifespan and cancelled with the other tasks. `GET /api/status/stream` subscribes; `GET /api/status` returns the stored snapshot (and probes once on the cold path). N tabs must not mean N polls of a GPU that is already contended.
- **The change fingerprint excludes the wall clock, and that is the whole design.** `status_monitor.signature()` covers `ok`, `version`, `error`, `warnings`, `maintenance` and the per-model state — and deliberately *not* `checked` or `expiresAt`, both listed in `VOLATILE_SNAPSHOT_KEYS`. Fingerprint the snapshot wholesale and every poll looks like a change, so the widget is rewritten six times a minute to say nothing; fingerprint too narrowly and a model that loaded never appears. The keep-alive countdown is therefore a **client-side timer over an absolute timestamp** (`fmtEta` + `updateEtas`), not part of the pushed state. Do not move a timestamp into the signature.
- **`/api/ps` has no processor field, so the label is recomputed.** `processor_label(size, size_vram)` applies the rule `ollama ps` prints (no VRAM → `100% CPU`, VRAM covers the model → `100% GPU`, otherwise a split, **CPU share first**). This is the widget's real payload: AGENTS.md's only reliable GPU check, made visible without `docker exec`.
- **Only a resident model gets a label.** Deriving it from an installed model's byte count paints every cold model `100% CPU`, which reads as a model running on the processor — and hides the one thing the widget exists to show. `resident` and `installed` are separate fields and both matter: a configured model Ollama has never heard of is the state an operator most needs to see.
- **A failed route degrades the widget; a failed service kills it.** `fetch_ollama_status` records a non-200 or an unparseable body as a `warnings` entry and keeps `ok=True`, and sets `error` only when a transport failure means nothing answered. The alternative is a moved route painting the whole service down, or a broken route hiding behind a green light.
- **Unloading is `keep_alive: 0` and nothing else.** There is no DELETE in Ollama's API. A model that is not resident answers 200 and does nothing, so `api_unload_model` **validates the name against the live snapshot first** and 400s with the real names — otherwise a typo is an operation that reports success and changes nothing. It then re-probes and broadcasts rather than patching the snapshot locally: what actually left VRAM is Ollama's answer, not our assumption about it.
- **`publish()` answers whether it changed, not what the snapshot is.** `_status_snapshot` in `gui.py` therefore calls it and *then* reads `last_snapshot()` back; assigning the return value hands the caller a `True`, which surfaces two lines later as an `AttributeError` in a different endpoint. `StatusEndpointTests` runs it, because the line reads correctly on its own.
- **Subscriber queues are bounded to one and evict the oldest.** A widget one tick stale is much cheaper than a poller wedged on a browser that stopped reading. The SSE generator also sends a `ping` every 15s, because an idle stream is the normal case and a proxy in front of the app closes it without one. The browser keeps a 30s `GET /api/status` poll running *only* while `readyState !== OPEN`, so a proxy that keeps dropping the stream cannot leave a frozen widget that still looks live.
- **The widget is pinned inside the rail, not below the layout.** Both rails are flex columns with `overflow: hidden` and the widget is `flex: 0 0 auto; margin-top: auto`. A strip *under* `.memories-layout` pushes the page past `height: calc(100vh - 140px)` and makes a three-pane reading screen scroll — which is the one thing that layout avoids. `.memories-sidebar` consequently became `overflow: hidden` and its scroller moved to `#categories-sidebar` (with `flex: 1 1 auto`, not a zero basis — same trap as the mobile panes, in miniature). `StatusWidgetLayoutTests` pins the placement, the rail structure and the absence of a sibling strip.

## Gotchas

- Qdrant not accessible from host—interact via app only
- Long timeouts (600s) for LLM operations—don't timeout-hunt
- Collection named `ea_memories` (hardcoded in memory.py)
- **A knob in `.env.example` that is not in the compose `environment:` block does not exist.** Docker Compose passes only the variables listed on a service, so a documented variable that is missing from that block is unreachable: it sits at its code default forever and editing `.env` does nothing. **24 documented variables were in exactly that state**, including `MEM_LLM_TIMEOUT` — shipped in `514e184` as the fix for the hardcoded 60s chat timeout, documented in `.env.example` and Critical Config, and genuinely untunable. Nothing errored: the app started normally, the default applied, and the only symptom was "I changed the env and nothing happened", which is indistinguishable from a caching problem. `test_env_wiring.py` derives the variable set from `.env.example` rather than hardcoding it, so a newly documented knob is covered the moment it is documented. Add a new knob in **both** files, with an inline `${VAR:-default}` so the effective value is visible in one place.
  - **Two `os.getenv` forms, only one of them dangerous.** `os.getenv(NAME, "fallback")` returns the empty string when the variable is present-but-empty, so a bare `${NAME}` in compose defeats the code's own default; `os.getenv(NAME) or "fallback"` treats empty as absent and is safe. The first version of that test asserted every bare interpolation was a crash, which was **false** — `LLM_QUERY_MODEL`, `MEM_SCOPE_MODEL` and `BASE_URL` are all the safe `or` form, and `BASE_URL`'s fallback is literally `""`. The assertion had to be narrowed to the two-argument form with a non-empty fallback before it said anything true.
  - **The two files also fail in opposite directions when a test is copied out of tree.** `test_env_wiring.py` and `test_mobile_layout.py` both resolve their inputs relative to their own location and walk up a directory, so a scratch copy needs the real layout (`mem-mcp/test_*.py` plus `.env.example` one level up). Running the copy in a flat directory produced 6 `FileNotFoundError`s that read exactly like a suite failing on the defect.
- **A middleware that is registered last is the outermost, and the outermost is
  the one that answers first.** `web_app.add_middleware(CORSMiddleware,
  allow_origins=["*"], allow_credentials=True, allow_headers=["*"])` looked
  like a permissive default and was a cross-origin hole: Starlette inserts at
  position 0, so it wrapped `auth_guard` and `McpAuthGuard` both. Combined with
  the unverified-Basic gate below, any page could read and mutate a named
  user's vault from the victim's browser — and `SameSite=Lax` does not help,
  because the attack used a header, not the cookie. The MCP copy of the same
  middleware is *inside* `McpAuthGuard` and has always been fine, which is the
  part that makes this easy to misread: two identically-configured middlewares,
  one safe and one not, differing only in where they sit.
- **A decoded credential is not a verified one, and the difference is invisible
  in the code.** `if ":" in decoded: return decoded.split(":", 1)[0]` reads like
  an auth check. It is a *parse*. The same function had been reviewed, shipped,
  and exercised by a live GUI for a long time with the password discarded on
  the next line, because the interesting line was the one returning a string
  and the discarded one was the point. It is the same class of bug as the
  `MATCH (n {id: $ids})` list-comparison one below: the code looks like it
  asserts something and in fact only parses it. When a function *extracts* an
  identity, ask what verified it, and if the answer is "the caller", check that
  every caller has a guard above it — `/gui` had none.
- **An allow-list entry with no route behind it is a trap, not dead code.**
  `/api/ping` was allow-listed for unauthenticated access with no such handler
  in the codebase. It is a loaded gun aimed at whoever eventually writes a
  health endpoint, and the natural way to write one is to match the existing
  list. Delete the entry rather than leaving it documented.
- **A substring guard on source is not a guard, and it will pass on the bug.** `ScopeClassificationInputTests` forbids a text prefix slice like `item_text[:1500]`. Written as `assertNotIn("item_text[:", source)` it matched the *docstring I had just written*, which quotes the very slice it forbids — so the guard reported green on the file that contains the bug. It also failed to bite when the bug was genuinely re-injected, because I had checked `slice.lower` when `text[:N]` slices the **upper** bound. A blanket "no numeric prefix slice" rule then failed on the legitimate `kws[:10]`, a deliberate cap on a keyword list. What actually works is an `ast` walk for a `Slice` with `lower is None` and a numeric `upper`, applied only to names in a declared set of text carriers — docstrings are `ast.Constant` and cannot trip it. Related: `assertNotIn` over a whole 2500-line file makes unittest echo the entire file into the failure output; use `assertFalse(needle in src, msg)`. The same applies to `assertIn` over a single *function* — `ResolverInputTests` dumps kilobytes of source unless it uses `assertTrue(needle in segment, msg)`.
- **`assertIn(needle, query)` asserts the needle is somewhere, not that it is where it must be.** This is the same lesson as the substring guard above, in a different place, and it hid a production bug for days: `ClearScopeQueryTests` asserted the batch clear contains `"$ids"`, which the broken query `MATCH (n {id: $ids, userId: $userId})` satisfies exactly. The parameter was present; it was in the wrong clause, on the wrong side of a property test, where a list is compared for equality and therefore matches nothing. Any guard that checks a token appears is checking the token, not the property. Derive the property: the fix was a lint (`ListParameterInPropertyMapTests`) that resolves each `s.run(...)` to the names bound to lists in its enclosing function and fails if one is used as a property-map value.
- **A "guard verified to bite" claim is only true when the failure is the assertion.** My first reinjection of the keyword slice was written at 4-space indent into an 8-space block, so the file raised `IndentationError` and all 38 tests errored — which looks identical to "the guard caught it" in a summary, and proved nothing. Always `ast.parse` the injected file before running the suite, and check the failure names the assertion.
- **The dev box and the container are different Python versions, and that gap hides import errors.** `mem-mcp/Dockerfile` is `python:3.11-slim`; the host running these tests is Python **3.14**, and it is the only interpreter installed (no 3.11, no 3.12, no pyenv). So anything that is legal on 3.14 and illegal earlier passes the entire local suite and then fails in the container — and if it fails at *import* time it takes the whole app down, not just the feature. The instance that did it: `sessions.py` defines `VaultSessionMiddleware` (line 550) before `VaultSession` (line 711), and `_commit`'s annotation `vault: VaultSession` therefore referenced an unbound name. On 3.11 annotations are evaluated eagerly at `def` time and the class body raised `NameError`, so `import sessions` failed and the container would not start. On 3.14 **PEP 649 defers annotation evaluation by default**, so it imported fine and `python3 -c "import sessions"` printed OK. Note the consequence for testing: you cannot reproduce it here even by reverting the fix — the annotations stay lazy on 3.14 with or without `from __future__ import annotations`, because PEP 649 is the 3.14 *default*, not something the future import switches on. The defect is unobservable from this box by construction.
  - **The fix is `from __future__ import annotations`**, guaranteed by the language spec since 3.7 rather than by a 3.11 guess. The guard is `Python311AnnotationTests` in `test_sessions.py`, which is **structural**: it `ast.parse`s the file text (it never imports it — `gui.py` and `common.py` are not importable on a machine with no fastapi and no httpx, which is the same constraint as every other suite here) and fails on any annotation naming a module-level binding that is not available until a later line, *unless* the module defers annotations. That disjunction is the whole test: a forward reference is only fatal when the annotation is actually evaluated, so "no forward refs" **and** "defers annotations" are two ways to be safe and asserting only the first reports the fixed file as broken. It also pins that `VaultSessionMiddleware` still precedes `VaultSession`, because if someone reorders them the future import becomes unnecessary — and that test says so in its failure message, so it gets deleted along with the hazard rather than left describing nothing.
  - **The rest of the surface was audited statically, not empirically**: no PEP 695 type-parameter syntax, and none of `itertools.batched`, `pathlib.Path.walk`, `sqlite3.Connection.autocommit`, `typing.override`/`TypeAliasType`/`ReadOnly`, or `enum.member` in the new code. New code in `gui.py`, `common.py` and `server.py` was checked for the same three classes and is clean. If you add a module that needs to import on 3.11, that audit is the part to repeat — and prefer the stdlib-only rule already applied to `sessions.py` and `matching_utils.py`, since it is also what keeps a file testable here at all.
- **These files are not all one line-ending, and a round-trip rewrite will churn them.** `test_matching_regressions.py` is CRLF except for **36** bare-LF lines, in **three** contiguous blocks left by an earlier scripted splice, so a decode → edit → re-encode pass cannot be used on it. The safe forms are a **byte-level** replace (both anchor and replacement carrying `\r\n`), or an insertion at the tail. `mem-mcp/matching_utils.py` is the same shape — 1007 CRLF and 101 bare-LF lines — and AGENTS.md listed only the test file, which is how two mutation harnesses silently no-op'd on it by assuming an LF anchor. **Check the file you are editing rather than the file you read about.** Detect with `raw.count(b'\r\n') == raw.count(b'\n')`; if that is false, insert or splice byte-wise, do not rewrite. Verify afterwards by locating the bare-LF lines and comparing their *content* against `git show HEAD:<file>` — their line numbers shift, so a count matching is not enough. And `open(p, 'wb')` truncates *before* the write — one failed write wiped a whole test suite and it came back with `git checkout`.
- **Validate `templates/dashboard.html` JS with `node --check` after any template edit.** `py_compile` and the Python suites structurally cannot see a JS syntax error, and one stray `await` in a non-async function took down the entire panel — `<body onload="init()">` reported `init is not defined` only as a downstream symptom of the script block failing to parse.
- **Three request fields decide whether a live retest measures anything at all.** Scoring the classifier by hand-crafting an Ollama request gave four empty answers in a row, which reads as "the prompt broke the model". It had not: `get_llm_response` sends `"think": False`, and without it `nemotron-3-nano:4b` spends the entire `num_predict` budget on a `thinking` block and returns `content: ""` with `done_reason: "length"`. It also pins `temperature: 0.0`. A harness that omits any of the three is not testing the prompt, and its failure mode is indistinguishable from a model regression. Check `done_reason` before believing an empty answer.
- **A model can be *argued* into a client it was never offered, and the retry is not obvious.** The old prompt called an explicit `**Client:**` header "authoritative". On an MBAG RFI whose header reads `## Client: Daimler AG (MBAG)`, the model returned that name verbatim — which is the end customer, not one of the stored clients — so `resolve_scope_name` matched nothing and the item was stamped unscoped. The severity is what makes it worth fixing: the header rule looked protective and instead deleted the scope for every customer-named document, and the loss is silent because a null is a normal outcome the UI renders the same as any other.
- **Cypher cannot be parsed locally** — there is no Neo4j and no driver in this environment, so a syntax error ships to production and surfaces as `neo4j.exceptions.CypherSyntaxError` on first execution. `test_cypher_safety.py` exists because of this: it extracts every Cypher string constant and f-string fragment and lints `FOREACH (v IN <list> | ...)` for a variable referenced inside its own list. A `FOREACH (x IN ... ELSE [x] END | DELETE x)` is a parse error, not a runtime one, and it was the reason every full reclassify aborted on its first call. The suite also pins the scope-clear query's shape. Add to it when you add a query. It skips **docstrings**: a docstring is prose that happens to quote a query, and linting it is a false positive that only ever gets "fixed" by rewording a comment, when the check is about Cypher and not English.
- **Chained `OPTIONAL MATCH`es return a cross product, and `collect(DISTINCT ...)` does not undo it.** A pattern on a relationship multiplies the rows it produces, so two of them in the same clause chain return their *product* — and a `collect(DISTINCT x)` in the `RETURN` deduplicates values *within* a row, it never merges rows. `db_list_diary` chained four patterns (`FOR_CLIENT`, `IN_CONTEXT`, `MENTIONS`, `RELEVANT_TO`) with a `WITH d, cl, ctx` in between, so every entry with five auto-linked participants and one cross-reference came back **five times with the same `d.id`**. The client assigns that list straight into `diaryEntries`, so the UI rendered one card per combination — and only for entries written since auto-linking existed, which is why it was reported as "the new ones are duplicated" rather than as a data problem. `db_list_memories` had the same defect through `RELEVANT_TO` alone, which is many-valued by design. The fix is one aggregating `WITH` after each pattern, **including the last one**: an entry with two `RELEVANT_TO` links is two rows just as surely as one with two `MENTIONS`, so an earlier version of this fix that collapsed every pattern *except* the trailing one only turned a mentions×relevant product into a single relevant multiplier. `OneRowPerRecordTests` derives the property from the clause structure, and is deliberately **scoped to those two functions** — they are the only two whose rows are pushed one-per into a client array and rendered. Roughly thirty other chained-pattern queries exist; none is reachable as a rendered list, and no Cypher can be executed here to check a rewrite, so they are left alone on purpose. When one of those becomes a record list, the analyzer is already written — that is what it is for.
  - **Every desktop scroll pane here is a flex item with a zero flex basis**, and the mobile block has to release *all* of them, not the one you happen to be looking at. `flex: 1` (and `flex: 1 1 0` with an explicit `min-height: 0` on `#diary-dates-list`) is basis 0. Stacked, the parent column is `height: auto`, a scroll container is sized from that basis, the parent resolves against zero, and the content renders into a box with no height — no error, nothing to scroll, the tab just looks empty. The fix needs **both** halves, `flex: 0 0 auto` *and* `overflow-y: visible`; either alone reproduces it. This bit twice, one level apart: the memories pane was released and `.diary-main` was not, then `.diary-main` was released and `#diary-dates-list` — the date/search-results list, not a detail pane — was not, so searching returned nothing visible while the entries pane looked fine. The sidebars then become the bounded scroll region (`max-height` + `overflow-y: auto`), giving one scroll area per sidebar rather than a nested one. `test_mobile_layout.py::StackedPaneVisibilityTests` derives the whole class from the stylesheet — any full-width selector that declares vertical scrolling *and* a zero flex basis — so a new tab cannot silently repeat this. The same test class is the worked example of **assert presence before asserting a negative**: an earlier version only checked that `overflow-y: auto` was *absent*, and `_decls` returns `""` for a missing selector, so deleting the rule outright made it pass. Two diary columns are bounded now, not one — see "Diary Screen Layout".
  - **CSS is not validated by anything here, and an inline style beats a media query.** Three page layouts are fixed-width columns — memories `180px + 280px + flex`, diary `flex + 340px + 250px`, graph `200px + flex` — and each pane sits inside `height: calc(100vh - Npx)` with its own `overflow-y: auto`, so on touch the *page* cannot scroll: you drag inside a pane that may be one line tall. `test_mobile_layout.py` pins the `@media (max-width: 900px)` / `640px` rules that fix both. Four things it is protecting, each of which fails silently:
  - **An inline `style` on `.graph-layout` disables the entire mobile block.** Inline styles win at every specificity, so one re-added attribute reverts the graph to two 200px columns with no error anywhere. The `display: flex` lives in the stylesheet for exactly this reason.
  - **`input, textarea, select { font-size: 16px }` is a functional fix, not a style preference.** Below 16px iOS zooms the viewport on focus and the zoom cannot be undone without a reload, so the form never recovers its layout.
  - **`overflow-x: clip` on `body`, never `hidden`.** `hidden` makes `body` a scroll container, which unsticks the `position: sticky` nav.
  - **`network.on("hold")` is the only way to reach the graph context menu on touch.** `oncontext` is a right-click; vis fires `hold` on long-press. Without the handler, Build Graph mode is unusable on a phone — the menu is the only way to add a neighbour. The `ResizeObserver` alongside it matters for the same reason: vis sizes its canvas once at construction, so stacking the layout leaves the canvas at the old width and clipped, and it must be disconnected in `rebuildNetwork()` before `network.destroy()`.
  - **A control that lives inside a rail that becomes a scroller is not a styling problem.** The client filter is the last flex child of `.tabs`, and the mobile block made `.tabs` a horizontal scroller — so the filter sat after all seven tab buttons, off the right edge, with the scrollbar hidden. It was present in the DOM and fully functional; nothing about restyling the control would have found it. The fix is structural: `.tabs` wraps instead of scrolling, and `.cf-bar` (`order: 99; width: 100%`) takes a row of its own. The second half is the one that is easy to miss — `.cf-panel` is `position: absolute`, so in a scroller it is **clipped to the rail** and opens as a sliver; it needs `position: static` once the rail stops scrolling. `test_mobile_layout.py::ClientFilterOnMobileTests` pins all of it, including a test that the filter is still a child of `.tabs` — if it is ever moved out, the wrapping rules go inert silently.
  - **The graph's own boxes carried inline styles, so the mobile rules for them were inert.** `.graph-sidebar` had `width: 200px` inline (making the block's `width: auto` a no-op) and `#graph-container` had `height: 70vh; min-height: 500px` inline (making its `height: 55vh` rule a no-op) — the *same* trap as `.graph-layout`, which was fixed two years of commits earlier while these two were left behind. vis.js sizes its canvas once from that box, so the rule that never applied was the rule deciding how much room the user has to pan. The mobile fix is `flex: 0 0 auto` plus an explicit `62vh`/`420px`: `flex: 1` is a **zero** flex basis, and in the stacked auto-height column the mobile block creates, that resolves against no available space — the same zero-basis trap as the scroll panes. `LayoutInlineStyleTests` now lints all five layout containers rather than three, and `GraphSizeOnMobileTests` pins the basis and the size.

## Matching, Deduplication & Merge Safety

- `matching_utils.py` is the dependency-light home for shared matching, scope, merge-validation, candidate-resolution, and duplicate-scoring helpers.
- Duplicate candidates are filtered by compatible client/context scope. Exact identity evidence can be decisive; fuzzy evidence is blended with vector similarity and cannot override a weak vector by itself.
- Clusters must contain a threshold-qualified core member, which prevents weak bridge-shaped transitive clusters from being presented as duplicates.
- LLMs adjudicate bounded candidate sets and return validated IDs. They do not directly select arbitrary graph nodes or mutate storage.
- `merge_facts` resolves every target for the current user before calling update or delete operations. The merge master records `pendingQdrantDeletes` after Neo4j deletion; `sync_orphans()` retries those deletions and clears the marker only after Qdrant accepts the cleanup.
- Merge work remains cross-store rather than globally transactional. Keep the recovery marker and startup reconciliation path intact when changing merge behavior.

## Search Scoring & Confidence

`db_search_memories` returns three independent numbers per result. Do not confuse them:

- `score` — legacy blended value (vector similarity + additive name heuristics, range ~0.3–3.2). Used for **ranking only**. It is not a similarity and must never be thresholded.
- `confidence` — normalized 0–1 from `identity_confidence()` in `matching_utils.py`. `top_p` filters on **this**.
- `evidence` — the identity label behind the confidence: `exact` / `alias` / `first+last` / `first_name` (same record) vs `surname_strong` / `fuzzy_name` / `partial_token` / `name_conflict` / `none` (near miss).
- `scopeStrength` — the **third** number, and it is not about the query at all: how well the record matches the client/project the caller *asked for*. Sort key is `(scopeStrength, score, confidence)`. See "Scope-Priority Search" below; the one thing to carry from here is that `scopeStrength` must never be folded into `confidence`.

Rules that matter when changing this code:

- `top_p` is a real confidence gate. Results at or above it are returned first; results below are appended after, flagged `weak: True` and capped at `WEAK_RESULT_LIMIT`. The escape hatch preserves recall, so a tighter floor alone does not cause silent "no results".
- A name-shaped query with weak identity evidence has its **vector** component damped (`VECTOR_DAMP_MISS` / `VECTOR_DAMP_CONFLICT`). Without this, a misspelling of someone absent returns a confident stranger: `'Radoslav'` scored a success against `'Oleg Tolstashov'`.
- A clearly conflicting first name is a veto (`EVIDENCE_CONFLICT`), not a deduction. A shared surname cannot rescue it — `'Ben Deutsche'` vs `'Lukas Deutsch'` is two different people.
- `people_match_allowed` is the trust boundary for auto-created MENTIONS links (`diary_manager.py`, `migrate_client_context.py`). It gates on confidence and rejects anything `weak`. It previously gated on the blended `score` with 1.2/1.6 thresholds, which passed a wrong person at 1.627.
- `db_search_diary` applies `top_p` to its own blended `score`. This is inconsistent with fact search by design of that older path — do not assume the two are interchangeable.
- Per-query ranking data is written to `logs/search_stats.log` via `log_search_stats()` in `common.py`. That logger is pinned to INFO and independent of `LOG_LEVEL` (production runs WARNING), so search data is never silenced by the app log level.

## Scope-Priority Search

`search_facts` and `diary_search_entries` take `client` / `context`, and both **prioritise and never exclude**. The motivating case is two people called John, both at Deutsche Bank: asked for DB/AI Adoption, `John Lochas` must beat `John Benjamin Porter`; asked for SAP, the SAP people must come first. A record filed under another client, or under none, still comes back — it ranks lower. That is a design rule, not a tuning knob, and it is the reason the feature works at all.

- **The old implementation filtered, and that was wrong three times over.** It was a Qdrant `FieldCondition` on the payload key `clientName` *and* a Cypher `EXISTS((f)-[:FOR_CLIENT]->(:Client {name: $clientName}))`; both compared a **raw user string against the stored node name**, so `"DB"` against `Deutsche Bank (DB)` matched nothing and the search returned an empty list with no error anywhere. The `+0.3` scope boosts that were supposed to reorder the survivors ran *after* the filter, so they could never fire on a record the filter had kept. `ScopeIsRankedNotFilteredTests` in `test_cypher_safety.py` pins the absence of both shapes, and re-injecting either one fails it.
- **Resolution is a pure function, and the abbreviation path is the load-bearing half.** `plan_search_scope(client, context, clients)` in `matching_utils.py` returns `(client, context, client_evidence, context_evidence)`, always in the **stored** spelling. It tries a name's self-declared parenthetical first — `Deutsche Bank (DB)` declares `(DB)`, so `DB` is an *exact* hit — and only then falls back to `resolve_scope_name`. Without it, `resolve_scope_name('DB')` returns nothing: `SCOPE_NAME_MIN_CHARS = 4` drops every short name, which is right for fuzzy matching and fatal for an abbreviation. A context is resolved **only against the resolved client's own `contexts`**, because the failure this replaces was SAP SE being handed Deutsche Bank's `DB AI Adoption`; an unresolvable or ambiguous answer is `(None, …, SCOPE_EVIDENCE_NONE)`, which means "no scope signal" and never "no results". `db_plan_search_scope` in `client_manager.py` is the thin `db_list_clients` wrapper.
- **Tiers are `assigned > relevant > inferred`, and `context` is worth double.** `scope_strength()` scores the client axis and the context axis separately and sums them (`SCOPE_CONTEXT_WEIGHT = 2.0`) — the project is the finer discriminator, so it decides between two records of the same client. Per axis: an `IN_CONTEXT`/`FOR_CLIENT` edge is `assigned` (1.0), a `RELEVANT_TO` edge is `relevant` (0.45), and the name appearing in the record's own text is `inferred` (0.30). Anything else is the floor, `SCOPE_UNSCOPED = 0.10`, which is deliberately not zero: a misclassified record must still be findable, and at 0.0 "assigned to somebody else" and "not classified at all" would tie.
- **The context signal comes from the record text, not the `IN_CONTEXT` edge.** Both Johns have *no* `IN_CONTEXT` edge, and Lohcas has none precisely because the classifier's own `context_named_in_text` guard dropped it from a terse People fact — while that same text is what separates them (`DB AI Adoption` is in his, not in Porter's). So `scope_context_tier` infers from text. `ScopeSeparatesTheJohnsTests` pins the exact pair of real texts.
- **`RELEVANT_TO` has to be read explicitly.** `_backfill_qdrant` never writes it into a Qdrant payload, so `_db_relevant_scope()` in `fact_manager.py` reads it per search. A null-primary record's *only* scope evidence is a `RELEVANT_TO` link — the classifier writes those before it gives up on a primary — so a partition that reads only `clientName` demotes exactly the records the feature exists to rescue. That query's two halves are pinned because neither is guessable: `(n:Fact OR n:DiaryEntry)`, and `CASE WHEN rc:Context THEN 'context' END` (the manual UI path can point `RELEVANT_TO` at a project).
- **An explicitly requested client is never demoted for being inactive.** `INACTIVE_PENALTY` still applies to a pinned-inactive client found by an unscoped search, but it is skipped when the caller's own `client` is that client: `SAP SE` is `statusPinned=True` and holds 414 edges including Joao Bonin, and a caller who named it has said what they want. `ScopePrioritySearchTests` pins both halves.
- **`confidence` must never carry scope.** `identity_confidence()` returns `max(identity_strength, vector_confidence)` and `IDENTITY_STRENGTH["none"]` is `0.0`, so a scope term in that `max` would *outrank identity evidence* — a record assigned to the requested client whose name does not match at all would clear `top_p` and be rendered `confidence: 1.00 (name_conflict)`, on a docstring that tells the model ✅ records are safe to merge. `ScopeStaysOutOfConfidenceTests` asserts `identity_confidence` has no scope parameter at all. Note `raw_vector=0.9` saturates `vector_confidence` to 1.0, so those assertions use a mid vector.
- **`db_search_diary` keeps its own inconsistency** — `top_p` on the blended `score`, not on `confidence`. Scope ranking is applied to it identically; do not "fix" the threshold while you are in there.

## Features

### Access Keys & Sessions
Setup → **🔑 Access Keys** manages the credential an MCP client presents.

- **The Setup page documents the authentication model, not just where the keys
  live.** `/mcp` takes `Authorization: Bearer mvk_…` on every call; `/gui` and
  `/api/*` take the session cookie, a verified `Basic` header, or a key. The
  table under **Which credential goes where** is the only place a user learns
  that before trying, and **🩺 When a client refuses to connect** maps each
  client-side error to a cause. Both exist because the observed failure was
  undiagnosable from the error alone: a client reported *"Incompatible auth
  server: does not support dynamic client registration"*, which is what an MCP
  client says when it receives a 401, assumes the server speaks OAuth, and
  finds that it does not. The actual cause was nginx still answering
  `auth_basic` itself, so the key never reached the app. Guidance text is worth
  a guard for that reason — `SetupPageAuthGuidanceTests` pins the sections and
  scopes each assertion to the block that owns the text, because `assertIn` over
  a 5700-line template passes on any page containing the word.
- **`-i` on the curl snippet is load-bearing.** The diagnostic tells the user to
  read the `WWW-Authenticate` line; without `-i` curl prints no response
  headers, so the section's stated purpose — distinguishing an app 401 from a
  proxy 401 — silently stops working while the test stays green. Removing `-i`
  did not fail the suite until that assertion existed.
- **Both connection snippets are rewritten from the same key** at creation time
  (`showNewPSK`), the config-file one via `textContent` because that is the one
  place a secret is written into the page. Both are pinned on the exact
  assignment, not the word `textContent`: an earlier version searched from the
  `getElementById` call, a window covering three unrelated writes plus a
  comment mentioning both words, so rewriting the secret-bearing write as
  `innerHTML` passed green.

- **Create a key** with an optional label and an optional expiry (never / 30 / 90 / 365 days).
- **The plaintext is returned once**, by `POST /api/psks` and nothing else. Only a
  SHA-256 hash and a 10-character display prefix are stored, so the Setup page
  cannot show it again — the client snippet is filled in with the real key at
  creation time and never re-fetchable. Do not add an endpoint that returns it.
- **Revoke** takes effect on the next request, with no file edit and no restart:
  `DELETE /api/psks/{id}`. Revoked rows stay in the list with
  `status: revoked` and render without an action button.
- The **live browser sessions** for the signed-in user are listed underneath, so
  "log out everywhere" is something an operator can see before they do it.
- Each key belongs to the `userId` it was minted for, and `userId` *is* the
  username — there is no user table. A key therefore grants exactly the vault
  its owner has, which is the entire point of it being revocable per device.

**Creating a key must not be repeatable into the same key.** `POST /api/psks`
called twice mints two keys, not one; the UI treats the response as single-use.
A `GET /api/psks` that leaked a plaintext would defeat the whole design, so
`test_auth_guard.WiringTests` pins that no route other than the POST reaches it.

### Google sign-in
Setup → nothing. A Google account signs in **from the front door**: the landing
page's "Continue with Google" button is a link to `/api/auth/google/start`, and
there is **no Setup section and no token box any more** — the pasted-ID-token flow
is gone, along with its `oauth_clients` table and the seven `/api/google/*` routes
it needed.

- **It is a real OAuth 2.0 authorization-code redirect**, so the client id *and*
  secret come from `.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`) and not from
  a form. The operator registers the callback as
  `{BASE_URL}/api/auth/google/callback` in Google Cloud. `configured()` demands
  **both**, because the secret is what makes the redemption confidential and a
  half-configured deployment can start a login it cannot finish.
- **`/api/auth/google/start` is gated on `configured()` and deliberately *not* on
  the registration flag.** Switching registration off must not lock out existing
  users; signing in and creating an account are different requests.
- **The state lives in the server-side session, and is consumed once.** The
  callback compares with `hmac.compare_digest` and deletes it, so a replayed
  callback has nothing to match. A mismatch is `/?google=state`, and Google's own
  `error` parameter is `/?google=declined`.
- **A first-time Google account just works; there is no link step.** `_google_signup`
  creates the vault keyed on the **Google email address** and links the subject.
  It refuses when registration is closed, when Google reports the address
  unverified or absent, and when `user_id_taken(address)` — because Google proved
  *who the person is*, not *which of their accounts* they meant, and only an
  explicit name can say that. A returning subject resolves straight through
  `google_identities` and never reaches that function.
- **The identity is `sub`, never `email`.** The address names the vault on first
  sign-in and is display after that; the primary key is `(provider, subject)`, so
  one subject maps to exactly one vault forever.
- **No verification mail for a Google signup, deliberately.** Google already
  verified the address, and sending a link the person cannot act on (the account
  is signed in already) would be theatre.
- **`session.clear()` before writing `user`** on the callback, the same
  session-fixation defence as the password path.
- The whole exchange runs in `asyncio.to_thread` — `urllib` is blocking, and this
  runs in front of a page load.

### Registration

`MEM_REGISTRATION_ENABLED=1` adds a **Create an account** card to the landing
page with two methods. Both are GUI-only, both are opt-in, and both are the
weakest surface in the app — which is why this section is mostly about what sits
in front of them.

- **Off by default, and it should stay off.** `REGISTRATION_ENABLED` in
  `sessions.py` is read once at import from an unset variable, so the routes are
  not something an operator inherits by upgrading. Same reasoning as `auth_basic`
  on the nginx MCP location: a route that creates accounts should be something
  someone turned on.
- **Two methods, reported separately.** `registration_config()` (gui.py) returns
  `{"email", "google"}` and the template branches on each, because the two halves
  fail independently — the flag opens the routes, an email form needs
  `smtp_configured()` and a Google button needs both client values. One
  "registration is on" flag would have to render a form that 404s.
  `registration_enabled()` takes a **method** name, not a provider key, and an
  unknown one raises rather than being assumed on.
- **The email form is not rendered at all when SMTP is unset**, rather than
  rendered and refused. A signup form whose only purpose is to send a
  confirmation mail is worse than no form when there is no mail server: it takes
  an address and then refuses to use it.
- **A disabled route answers 404, not 403.** A 403 says "this exists and you may
  not"; a disabled signup route should be indistinguishable from one that was
  never mounted, so turning the flag off does not advertise the feature.
- **`/api/auth/*` is unauthenticated, and that is what makes signup possible.**
  It is the only prefix `auth_guard` lets through with no credential. Both routes
  sit there by necessity, so **they are the only endpoints in the app that need
  their own gate**, and `_require_registration` is that gate. Do not move them
  out of `/api/auth` without re-reading `auth_guard`.
- **The throttle is inside `_require_registration`, and that placement is the
  point.** It runs *before* the flag check and counts every attempt regardless of
  outcome. Counting after the flag check would mean an operator who turns
  registration **off** has just removed the rate limit from a route that is still
  mounted, and that the counter only ever sees successful attempts — which is not
  what an attacker does. `allow_registration_attempt` (sessions.py) is an
  in-process sliding window, 10 attempts / 10 minutes per client key. It raises
  the cost of a script and does nothing about a botnet, which its docstring says
  rather than glosses.
- **The client key is the *rightmost* `X-Forwarded-For` hop.** nginx uses
  `$proxy_add_x_forwarded_for`, which *appends* the peer it saw, so a forged
  `X-Forwarded-For: 1.2.3.4` arrives as `1.2.3.4, <real peer>`. The leftmost entry
  is whatever the client sent, and trusting it would hand every attacker a fresh
  quota per request. The socket-peer fallback is nginx itself behind the proxy,
  which buckets everyone together — the right way for that fallback to fail.
- **The vault key is the lowercased *username*, not the address.** `user_id` *is*
  the `userId` on every Fact, DiaryEntry and Client in the other two stores, so
  whatever string is chosen is the key and no existing query changes. The address
  is stored separately, in its own column with a partial unique index
  (`credentials_email_unique ... WHERE email <> ''`), because it is for
  confirmation and not for identity. `create_credentials` **refuses** a taken
  name or a taken address rather than upserting, and creates the row **unverified**.
- **A password signup is not usable until the address is confirmed**, and
  `verify_account_password` treats an unverified account exactly like a wrong
  password. The *reason* is what differs, and it matters: "open the link we sent to
  your email address" is the difference between a person who can fix the problem
  and one who resets a password that was never wrong. `api_login` answers **403**
  when it has a reason and 401 when it does not.
- **The confirmation token is spent by a single UPDATE** that stamps
  `email_verified_at` *and* clears the token, so a link clicked twice verifies
  once and a double-click cannot leave two callers each believing they verified.
  There is **no password reset** and no address change; the landing page says so,
  because a user who does not know that will eventually rely on "reset it with
  that address".
- **`user_id_taken` checks the htpasswd file as well as `credentials`.** A name
  can already belong to an htpasswd user (the operator's own account), or to a
  vault a Google sign-in created. `email_taken` deliberately does **not** consult
  htpasswd: an htpasswd file holds no addresses, so there is nothing to collide
  with, and refusing on a coincidence would lock out a legitimate signup. Creating a password row over either produces two
  ways into one vault with independent passwords — or silently re-points an
  existing vault at a password the registrant just chose, which is account
  takeover dressed as a signup. `create_credentials` **refuses** rather than
  upserting, because an upsert here would reset an existing account's password.
- **There is no separate Google registration request.** The Google button is the
  same endpoint as Google sign-in: `_google_signup` creates the vault on a first
  arrival and does nothing on a later one. That is the point of removing the
  pasted-token flow — "register" and "sign in" are now literally the same click,
  because the only thing that used to distinguish them was a box to paste into.
- **Registered passwords live in SQLite, not in htpasswd.** The htpasswd file is
  mounted **`./mem-mcp-data/htpasswd:/app/htpasswd:ro`** — read-only — precisely
  so the app cannot rewrite a file an operator also edits by hand. Login goes
  through `_verify_account` (gui.py), which checks `credentials` **first** and
  falls back to htpasswd. The order matters: an address could be in both stores,
  and the row this app owns wins because it is the one the app can rotate. The
  reverse makes a password change made in the UI a silent no-op.
  **`_verify_account` returns `(key, reason)`, not a bool** — a session holding
  `Alice` when the key is `alice` is a session that looks signed in and sees an
  empty vault, and the reason is what lets login say *why* rather than "invalid
  credentials". It checks disabled and unverified **before** the password, and
  only falls through to the subprocess when the name is not one it registered.
- **Passwords are scrypt, and `dklen` travels inside the stored string.** scrypt
  over PBKDF2 because PBKDF2's only cost knob is iterations, and iterations are
  cheap on a GPU. `n/r/p/dklen` are all in the encoded value so they can be raised
  later without invalidating anyone's password. **A verifier that derived `dklen`
  from the stored digest would compute a digest of that same length and compare
  equal** — which is what the first version did, and it meant anyone who could
  shorten the stored hash by one byte had made it verify against anything.
- **A corrupt stored hash is a False, not an exception.** The caller is a login
  path; a row restored from a damaged savepoint or hand-edited must read as "this
  account cannot log in" rather than 500ing every request that touches it.
- **Disabling is not deleting, and `delete_credentials` is neither.** The first
  keeps the name occupied, so it cannot be registered again and come back
  pointing at a vault whose records are still there. The second is the undo for a
  signup whose confirmation mail could not be sent — `api_register` calls it on
  that failure rather than leaving an account that can never be verified and whose
  name cannot be reclaimed.
- **No `:User` node is created at signup.** It appears by itself the first time
  the new account writes anything, and an empty account has nothing to write.
- **`sessions.py` still must not import `google_auth`.** It is stdlib-only so the
  suites can import it on a box with no web framework; the OAuth module no longer
  needs PyJWT, so the rule is now about the *dependency direction* rather than
  about a heavy import, and it is still worth keeping: `registration_enabled("google")`
  needs `configured()` and a plain function call is cheaper than reasoning about a
  module cycle.

### Build Graph Mode
Build your own focused subgraph starting from any memory.

1. Go to **Graph** tab → click **Build Graph** mode toggle
2. In **Memories** or **Diary** tab, open a record's menu and choose **🕸️ Show in Graph**
   (the `📍 Show on map` button on a memory card does the same thing)
3. Node appears centered with its connections
4. **Right-click** any node for context menu (long-press on touch):
   - **Go to fact** → navigate to memory details
   - **Show on map** → add this node and its connections
   - **Show all connected** → add every direct neighbour
   - **Remove from map** → drop the node again
5. Click **🗑️ Clear** to reset the build graph

There is no "add by relationship type" item. `showConnectionType()` and the
`.submenu` stylesheet block are dead code — the function has no callers and no
markup references it. Do not document it as a feature without also wiring it
up.

The graph obeys the **same client / project selection as the memory list**. Two
filters exist and they are independent:

- **Scope** — the client (and optionally the project) picker. Handed to the API
  as `clientId` / `contextId` and resolved in `db_get_graph`, so the server does
  the filtering rather than the browser.
- **Categories** — the chips in the graph sidebar. Client-side, applied to every
  path that adds a node, including "Show all connected".

#### Scope is resolved server-side, from the edges

`db_get_graph(user_id, client_id, context_id, limit)` filters through
`_scope_and_cap_graph`, which decides membership from the `FOR_CLIENT` /
`IN_CONTEXT` **relationships**, not the denormalised `clientId` property. The
property is written by the scope classifier and can lag the edge it mirrors, and
a fact whose link was just deleted has to leave the filtered view immediately
rather than on the next reclassify. `gui.py` forwards `clientId` / `contextId`
on `/api/graph`, `/api/graph/neighbors/{id}` and `/api/graph/focus/{id}`.

**Diary entries are scoped from their edges too, and only from their edges.**
`db_get_graph` used to read a diary entry's client from a `clientId` property.
**No Fact or DiaryEntry node has that property in Neo4j** — scope lives on the
`FOR_CLIENT` / `IN_CONTEXT` edges, and `clientId` is only ever a *Qdrant payload*
key. So that read returned `None` for every entry, every diary entry came out
unscoped, and a client or project filter dropped all of them. The two scope
passes also matched `:Fact` only, so a diary entry's scope edge produced no
graph edge at all. Both now match `Fact` or `DiaryEntry` and the property read
is gone. The Fact/DiaryEntry test is `node_map[id]["label"] == "Fact"` rather
than a label in Cypher, because both passes run after every node is in
`node_map`. `diary_scope` holds a **list**, not a tuple, because the passes
assign into it by index and a tuple raises `TypeError` on the first edge. The
`r["clientId"]` reads in `fact_manager` / `diary_manager` are fine — those are
Cypher `AS clientId` aliases off the edges, not properties. Guarded by
`DiaryScopeSourceTests` and `DiaryScopeTests` in `test_graph_scope.py`.

Client, Context and Category nodes are **always** kept under a scope filter. A
graph that has been filtered to one client and then omits that client's node
reads as "this client has no facts", which is worse than a slightly noisy graph.

`clientFilter` is `'all' | 'active' | <clientId>`. Only the third is a node id;
`graphScopeParams()` deliberately drops the first two rather than sending a
literal `'all'` as an id that matches nothing. `'active'` is expanded client-side
to the active ids in `clientListCache` when filtering node lists.

#### The cap

`MEM_GRAPH_MAX_NODES` (default 600) caps the records in one response. Over the
cap the **most connected** records are kept — a graph of leaves explains nothing,
and that is the case that makes "full graph" feel useless. The response carries
`truncated` and `total`; the UI shows a banner rather than silently rendering a
partial graph. Edges touching a dropped record are dropped with it.

#### Neighbours are not just facts

`db_get_neighborhood` used to end its Cypher in `(neighbor:Fact)`, so "Show all
connected" could only ever add other facts. It now returns Fact, DiaryEntry,
Client and Context nodes — the connections a person actually recognises. Category
is still excluded: it is shared across the whole vault and only adds clutter.
`_filter_neighborhood_scope` keeps a Client/Context node that **is** the active
scope, for the reason given above.

#### A Cypher `RETURN` alias and the key Python reads back are a contract

`db_get_graph` raised `KeyError: 'f'` on **every** call in production, so the
whole graph tab was dead. The two scope passes had been changed to match
`(n:Fact OR n:DiaryEntry)` and return `c, n` / `ctx, n, c`, but the Python kept
reading `cr["f"]` and `xrr["f"]`. The variable had been renamed and the read had
not.

`DiaryScopeSourceTests` passed the whole time, because every fact it asserted
was individually true on the broken file: the query mentions `DiaryEntry`,
nothing reads a `clientId` property, and the edge passes do write into
`diary_scope`. None of that says whether the key the loop reads is a key the
query returns. A source test can check the query and the writes and still miss
the seam between them.

`ReturnAliasTests` closes it: it walks the AST, pairs each `for <var> in <res>`
loop with the `s.run(...)` immediately above it, and asserts that every
`var["key"]` in the loop body appears in that query's `RETURN` clause
(`type(r) as rel_type` -> `rel_type`). When you rename a Cypher variable,
rename the read — and prefer a name that is true of *both* node types: a
variable that can be a Fact **or** a DiaryEntry should not be called `f`.

**Every node-adding path filters through `nodeInCategory()`**, and reports what
it skipped in the toast. A path that ignores the category chips puts nodes on the
map that the sidebar says are hidden, which is indistinguishable from the filter
being broken.

#### "Show in Graph" has to say when it cannot help

`openInGraph(id)` checks the id is in the current DataSet before focusing it.
`network.focus()` on an absent id is a silent no-op: the tab switches, the canvas
looks identical, and the user concludes the button is broken. It now toasts
"not in the current graph — outside the selected client / project / categories".

Diary entries have a **Show in Graph** menu item (`_diaryMenuGraph`). They did
not, which meant a diary entry could not reach the graph by any route at all.

#### Related entries

`RELEVANT_TO` targets a Client **or** a Context — the relationship type is the
same for both, and the target was only pinned to `:Client` by the Cypher label,
so widening it needs no migration and no backfill. The read side emits
`kind: 'client' | 'context'`, which the chips use for their `📁` prefix and the
"add" picker, which enumerates `client.contexts` alongside the clients.

Do not reintroduce a `:Client`-only match here. A project stored as relevant
would be unreachable, and the existing `:Client` links are indistinguishable
from the new ones except by label.

### Link Management
Modify or delete links between memories directly from the UI.

- Each link badge shows ✏️ (edit) and 🗑️ (delete) buttons
- Click ✏️ to change the relationship type inline
- Click 🗑️ to delete the link (with confirmation)
- The **Link to…** target is a search field, not a dropdown of every fact. Candidates exclude the source record and anything already linked, cap at `_PANE_LINK_LIMIT` (12), and match with the same expression `renderMemories()` uses, so the two lists agree on what a query means.
- **Diary entries are candidates in the *fact* pane's picker, and that is not cosmetic.** `db_link_facts` has always had a `Fact`/`DiaryEntry` branch that MERGEs `MENTIONS` from the entry to the fact, `db_unlink_facts` matches it, `db_list_memories` returns it (`target_label`), and `renderLinkSection` draws it as a 📝 badge — so the edge was fully supported on every side *except* the one control that creates it, which searched `memories` only. Do not "simplify" `paneLinkCandidates` back to a fact-only loop. Three properties are load-bearing:
  - Each candidate is `{kind, rec}`, and the kind survives into `st.targetKind`, because the relationship type is **meaningful only for a fact**. The server discards `relType` on a Fact→DiaryEntry link, so the field is hidden with an explanatory note (`#pane-link-relnote-<id>`) and `savePaneLink` sends `'MENTIONS'` itself — requiring a type there would block the link on an input that is thrown away.
  - `loadDiary` is lazy (the diary tab fetches on first visit), so a picker opened on the Memories tab has an empty `diaryEntries`. `ensureDiaryEntries()` fetches the list *without* `loadDiary`'s side effects (it selects an entry and re-renders the sidebar, month pager and graph category sidebar) and de-duplicates concurrent calls.
  - An entry already linked to the fact needs no separate exclusion: it arrives in `source.links` as an incoming `MENTIONS`, so the existing `linked` set already covers it. A second MENTIONS to the same fact is a duplicate, not a new link.

### Diary Screen Layout

Three columns, **left-anchored**. `.diary-layout` is a flex *row*, so the DOM order is
the visual order and it is the design:

| column | width | holds |
|---|---|---|
| `.diary-sidebar` | 250px | search box, then the month pager and the month grid |
| `.diary-list-col` | 340px | the filtered entry list — `＋ New`, a count, `#diary-dates-list` |
| `.diary-main` | `flex: 1`, `min-width: 0` | the selected entry (viewer or editor) |

The calendar and search rail is first, so it lands top-left, and the entry being
read takes the remaining width on the right. Three properties are load-bearing,
and each is pinned by `test_mobile_layout.py::DiaryColumnOrderTests`:

- **`min-width: 0` on `.diary-main`.** A flex item's default `min-width: auto` refuses
  to shrink below its content, so one long transcription would widen the detail pane and
  squeeze both rails instead of wrapping.
- **`#diary-dates-list` moved out of `.diary-sidebar`.** It is the zero-basis scroller
  (`flex: 1 1 0` + `min-height: 0`) that the mobile block releases, and it is also the
  `IntersectionObserver` root for the lazy batches. Leaving it inside a bounded rail
  would nest two scroll regions, and the count would then be bounded twice.
**`DiaryColumnOrderTests` asserted the opposite order for several commits, and nobody
decided who was wrong.** The columns had been swapped by hand in the template — that was
the intent — and the test and this section still described detail-first. A failing test
that everybody reads as a stale artefact is worse than no test: it is a permanently red
suite that every later run has to re-classify, and the one thing it could have caught
(the columns being swapped back by a careless edit) is the thing it had stopped being
able to say anything about. **When a layout test and the shipped layout disagree, ask
which one was changed on purpose before "fixing" either.** The rail-first test also had
a second, independent bug: `layout.split('class="diary-sidebar"')[1]` is a *tail* slice,
so with the rail first it ran to the end of the layout and then complained that the
list's scroller was "in the rail" — the assertion was reading a region three times larger
than the rail. It now cuts each column at the next column's opening tag.

- **`.diary-date-item.active` marks its *left* edge.** The detail pane is to the list's
  right, so the old `border-right` pointed *at* it instead of away from it. This is the
  edge that is correct for rail → list → main; it was right by accident for a while,
  because the columns had been swapped by hand and a `border-right` was equally wrong
  there for the mirror-image reason.

**Filling the page is two separate rules, and the second one is derived from the first.**
`.page` is `max-width: 1100px; margin: 0 auto`, which is right for a form page and wrong
for a three-pane reading screen; `#page-memories` has always overridden it and `#page-diary`
did not, so the diary tab was capped and centred with a gutter on both sides. But the pane
height is `calc(100vh - 140px)`, and that constant is *the chrome above the pane* — which
includes this page's own padding. So the two defects **compound**: widen the page and leave
the constant alone and the row is now shorter than the space it has, showing a band of dead
space at the bottom. The diary budget had been `180px`, sized for the old `1.5rem` page
padding (48px), which is exactly the leftover the report was about. It now equals
`.memories-layout`'s, and the two pages pad alike, because they sit under identical chrome.
`test_mobile_layout.py::DiaryFillsThePageTests` pins all three facts — the cap override, the
budget equality, the padding equality — and the budget test is what makes the padding test
mean something.

`setDiaryListCount()` writes `N` or `N of M` into `#diary-list-count` on every render
(`renderDiarySidebar` and `renderDiarySearchResults`), which is the one number the list
itself cannot show: what the current month or search narrowed away.

On a phone the columns stack, and DOM order is visual order, so the mobile block
re-ranks them with `order`: rail first, then the entry list, then the entry they select.
That happens to match the desktop DOM order today, so the rules are currently a no-op —
they are kept because the stacked layout is a flex *column* whose children are otherwise
ordered by DOM, and re-ordering the desktop columns is exactly the edit that would
otherwise move the pickers below the fold. Both
diary columns are bounded (`max-height` + `overflow-y: auto`) below 900px, which is why
`StackedPaneVisibilityTests` carries two diary selectors rather than one.

### Diary Metadata Without the Body

`diary_save_entry` takes `entryId` with an **empty `content`** as "change
metadata only", and `list_diary_entries` now reports each entry's client,
project and `original_file` so a caller can see what is already there first.

**The empty-body case exists because the alternative destroys data.**
`db_save_diary` writes `SET d.content = $content` unconditionally, so a caller
wanting to attach a filename to a 40k transcription it cannot afford to
resend had no way to do it — and sending an empty body was not a no-op, it was
the loss. `db_update_diary_metadata` is that other path: it merges the keys,
writes `d.metadata`, and patches the Qdrant payload across the chunk family.
**No embed, no chunk rebuild, no keyword regeneration** — keywords derive from
the name and the body, and neither is changing, so the extractor would spend an
LLM call to arrive at the same string.

- **Keys are merged, not replaced.** `original_file` arriving on an entry that
  also carries `keywords` must add a field, not silently drop the other. A
  replace is what a caller who cannot see the current metadata would have to
  assume. The Qdrant payload is patched with `set_payload` over
  `_scope_targets()`, not re-upserted — the same reasoning as
  `db_set_diary_scope`: chunk 0 keeps the record id, so addressing one point by
  it looks right until the entry is long enough to chunk.
- **A Qdrant patch failure is logged and swallowed, not fatal.** Neo4j holds the
  canonical value and a stale payload is recoverable by any reconcile pass; the
  reverse order loses the edit.
- **Both refusals matter and both are destructive if dropped.** An empty body
  with no `entryId` is refused rather than stored — it is indistinguishable
  from a mistake, and storing it creates an empty entry that looks real in
  every list. An `entryId` with empty content and **no** `metadata` is refused
  too, because otherwise the condition silently means "do nothing" and returns
  success, which reads as an update that happened.
- **The routing is one condition and both of its mistakes are data loss.** It
  turns the wrong way and `db_save_diary` runs and wipes the body; it always
  turns and a real save never happens. `DiarySaveEntryRoutingTests` drives it
  with fakes and pins both directions, plus that the old entry is *not* deleted
  (this is an update, not a move).

**`list_diary_entries` reads scope from the edges, and every `OPTIONAL MATCH`
is closed by an aggregating `WITH`.** Both are the documented diary bugs, and
both fail in the same direction — silently. No `DiaryEntry` node carries a
`clientId` property at all (that key is Qdrant-only), so a property read
answers `None` for every entry and the whole vault looks unclassified. And a
chain of `OPTIONAL MATCH`es returns the *product* of the rows each produces, so
an entry with one client and one project comes back twice unless each pattern
is aggregated — **including the trailing one**, which is the one that gets
forgotten: collapsing every pattern except the last turns a mentions × relevant
product into a single relevant multiplier.

Names are returned, not ids. `diary_save_entry` takes `client`/`context` by
name, so an id here would not round-trip, and returning both invites a caller to
pick the wrong one. `null` means unclassified **or classified as generic**,
which is a real outcome — the classifier's nulls are permanent, so the tool
description says so rather than implying missing data.

### Write-Scope Matching

`resolve_write_scope(client, context, user_id)` in `client_manager.py` is the
only way `diary_save_entry` turns a caller's names into node ids. The ladder is
**declared abbreviation → case-insensitive exact → evidence ladder → one LLM
approximation → `ValueError`**. Nothing creates a node.

- **Creating on a near-miss was worse than no scope at all.** The old path did
  `db_resolve_client(...) or await db_create_client(...)`, so `"EPAM Systems"`
  minted a second `Client`. Both nodes then match a client filter, the counts
  disagree, and neither is the node other queries resolve to — and the
  duplicate is *permanent*: the classifier only ever picks from existing
  names, so the near-miss copy never gets linked and never gets a second look.
- **The context is resolved only against the resolved client's own projects.**
  The LLM is handed just that list, so `PPC` (EPAM's project) can never be
  paired with `SAP SE` — the cross-client guess the classifier is already
  documented as making. A context given *without* a client resolves against
  every project's list and reports the owning client back, because an entry
  filed under a project no client filter can find is not scoped.
- **An LLM answer outside the candidate list is discarded**, not written. The
  model's whole job here is to pick among names that already exist; an invented
  name is a new reference, which is what this path exists to avoid. A failed or
  empty model answer is a `ValueError`, never a silent fallback to creation.
- **A match failure is an error naming the candidates**, so the caller learns
  what it could have meant rather than discovering an unscoped entry later.
  The tool response returns the **stored** spellings in `client`/`context`, so
  a caller can see which node the entry actually landed under.
- **The tool source must not reference `db_create_client` / `db_create_context`
  / `db_resolve_client` / `db_resolve_context` at all.** `DiarySaveScopeRoutingTests`
  pins both halves: `WriteScopeResolutionTests` proves the resolver behaves
  (lifted with `ast.get_source_segment` — `client_manager.py` imports `common`,
  so it cannot be imported here), and the routing tests prove the *tool calls
  it*, because a helper tested in isolation is not a test of its call site —
  the create-on-miss path lived one line away from a perfectly green resolver
  test.

### Diary Search
Search diary entries from the calendar rail.

- Type in the **Search entries…** box at the top of the diary rail (left-hand column)
- Instant client-side substring filter runs as you type
- After 400 ms a server-side **vector similarity search** (`GET /api/diary/search?q=`) fires and updates results
- Clicking a result navigates to that date's entries
- Clearing the input restores the full date history list
- API endpoint: `GET /api/diary/search?q=<text>&limit=10&top_p=0.4`

### Dashboard Deduplication
Setup → **🧹 Deduplicate memories** provides a review-first merge workflow. It is a section of the
**Settings** page, not a top-level tab: the tab rail held eight tabs and this one is a maintenance
task, not a daily view. The six controls (`#dedup-category`, `#dedup-threshold`,
`#dedup-max-cluster`, `#dedup-scan-btn`, `#dedup-status`, `#dedup-clusters`) live inside
`#page-setup`'s single `.card`, between the Maintenance and Backup sections.

- Scan a category with a configurable similarity threshold and maximum cluster size.
- Select the records to merge, choose the master, and use the LLM to generate an editable merged title and text draft from only those records.
- Merges require explicit confirmation and use the same ownership validation and recovery marker as the MCP `merge_facts` tool.
- GUI endpoints: `GET /api/duplicates`, `POST /api/duplicates/draft`, and `POST /api/duplicates/merge`.

**Moving a page out of the tab rail has a persistence hazard.** `activeTab` is saved to
`localStorage` under `mem_vault_session_state`, so anyone who left the Deduplicate tab open has
`'deduplicate'` in their saved state forever. A `switchTab` that does
`document.getElementById('page-' + tab).classList.add('active')` then dereferences `null` and takes
the whole page down on load. `switchTab` now resolves the element first and falls back to
`memories` with a `console.warn`. Do not reintroduce the direct dereference, and keep
`renderDuplicateEmptyState()` in the `if (tab === 'setup')` branch — without it the panel is a
blank div until Scan is pressed, which reads as a broken button.

**The merge path is budgeted in two directions, and a constant budget is wrong in the one
direction nobody checks.** The merge draft is the only call whose prompt is uncapped, so its size
is `max_cluster` × record length. Two generations of this bug, both a *fixed* `num_predict`:
**900**, below what a four-record merge actually spends (nemotron-3-nano:4b used **2,776** tokens),
and then **4,000**, sized from that same four-record run, which served it and truncated a
twelve-record draft that costs **5,657**. The failure is always the same shape: the model runs out
of budget mid-JSON, never emits the closing brace, `re.search(r"\{.*\}")` matches nothing, and an
over-budget request comes back as a **502** — an LLM fault for something with a known remedy.
**The output need scales with the selection, so a constant is either too small for the largest
allowed cluster or wastefully large for the smallest. There is deliberately no output-budget knob.**
The budget is whatever the context has left after the prompt, computed by
`merge_draft_output_budget()` in `matching_utils.py` and passed straight to `num_predict`, so the
budget the guard checked and the budget the model is given cannot drift apart — the request
cannot be issued unless the helper returned. `MEM_MERGE_NUM_PREDICT` became
`MEM_MERGE_MIN_NUM_PREDICT` (3000), which is the **refusal floor, not the budget**: below it no
draft can complete, so the selection is refused with a 400 naming the record count, the estimate
and how much source text would fit, rather than attempted.

`max_cluster` was validated 2–20, which is a lie, and it is now `MERGE_MAX_CLUSTER` (12) — but
the reason has changed, and the old reason was the wrong one. Measured prompt tokens for cumulative
prefixes of the 20 longest facts: 4 → 3,783, 8 → 6,393, 12 → 8,801, 18 → 12,167,
20 → 13,123. The **answer** runs out before the prompt does: at 12 the remainder is 7,531
against a measured 5,657, so the largest allowed selection completes with room to spare; at 18 the
remainder is 4,165 against an output need that grows with the selection. The cap is also enforced
in `api_generate_duplicate_draft`, which is a separate POST and used to accept any number of
records — the cap the UI advertised was not the cap the draft honoured.

**`MergeDraftTooLarge` is a `ValueError`, so the `except` order is load-bearing.** It converts the
refusal to a 400, and the generic `except (ValueError, json.JSONDecodeError)` below it is the 502.
Reorder them and the guard still reads as present, still runs, and every over-budget request is
reported as a model failure — the exact failure it was added to prevent. The caller-side test
asserts both facts together, because neither is sufficient alone, and requires exactly one handler
per caught name so "which one runs first" has one answer.

- The char/token ratio **drifts**: 3.21 at 6.7k chars, 3.31 at 43.5k. A ratio that is safe on a
  small prompt is not safe on a large one, so `MERGE_PROMPT_CHARS_PER_TOKEN` is 3.0 — rounded
  down, because the guard must over-estimate tokens, not under-estimate them.
- `MERGE_CONTEXT_TOKENS` must track `OLLAMA_CONTEXT_LENGTH` in `docker-compose.yml`. They are
  separate knobs because one is read by Ollama and the other by the app, and a silent divergence
  means the guard protects a context window that is not the real one.
- `MERGE_MAX_CLUSTER` reaches the template via `ctx["MERGE_MAX_CLUSTER"]` in `get_gui`, so the
  input's `max` attribute, the client-side check and the server's 400 cannot drift apart.
- The merge budget is split across the two suites on purpose, and the split follows what each can
  see. `MergeDraftBudgetTests` in `test_matching_regressions.py` **calls**
  `merge_draft_output_budget()` against the measured numbers — at 12 records the budget must cover
  the 5,657 tokens that draft costs, the whole remainder must be returned with no hidden ceiling,
  and the floor must be inclusive at exactly `min_predict` and refuse one token below it. A guard
  that cannot fire fails those; an AST shape check would not have. The dead-comparison lint this
  replaced (`if estimated > 0` reads like a guard and never fires) is now covered behaviourally.
  `MergeDraftBudgetTests` in `test_cypher_safety.py` then pins only the **call site**, which the
  helper's own tests cannot see: that `num_predict` is the helper's return value, bound exactly
  once, on a line above the `get_llm_response` call; that the `except MergeDraftTooLarge` handler
  precedes the generic `ValueError` one and there is exactly one handler per caught name; and that
  the record cap in the draft endpoint is a comparison with no `ast.Constant` on either side that
  drives a 400. A first version of the handler-order test took the *last* matching index, so
  re-injecting a duplicate generic handler made it pass; and a first version of the cap test only
  asserted the constant appears in the function, which a message quoting the cap satisfies even
  when the check itself is `if False`. Both are the `assertIn`-checks-a-token lesson again.
  `MergeDraftEndpointTests` there **runs** the endpoint instead: it lifts
  `api_generate_duplicate_draft` with `ast.get_source_segment` (gui.py is not importable here) and
  drives it against a stub model, so the pair of properties neither of the other two can see is
  asserted directly — a twelve-record selection is handed at least the 5,657 tokens that draft
  costs, and that budget is exactly `context - prompt_tokens` rather than a second computation of
  it; an over-budget selection raises a **400** without the request being issued at all; and
  thirteen records is refused by the count guard even though the text would fit. Re-injecting the
  old `num_predict = 4000` fails three of them, which is what makes this the test that would have
  caught the defect rather than one that describes it.
- `DedupUnderSetupTests` in `test_cypher_safety.py` pins that the six dedup controls live inside
  `#page-setup` and that no `switchTab('deduplicate')` survives.

### Backup & Restore
Setup → Maintenance → **Backup & Restore** manages the savepoints.

- **Back up now** takes an immediate savepoint; one is also written automatically at `MEM_BACKUP_HOUR:MEM_BACKUP_MINUTES` server time, keeping the newest `MEM_BACKUP_KEEP`.
- Each savepoint lists its time, whether it was automatic, and the node/link counts and size.
- **Restore** replaces the entire vault with that savepoint, after an explicit warning. It restores both vector collections and the graph, then reconciles the two.
- A savepoint that did not finish is shown as **Unusable** and cannot be restored.
- The list loads once when the page opens and refreshes on **↻ Refresh**. It does not poll; the status line updates only while a backup or restore you started is still running, then stops.
- API endpoints: `GET /api/backup/savepoints`, `GET /api/backup/status`, `POST /api/backup/run`, `POST /api/backup/restore/{id}`.
- Restore is blocked with `409` while a reclassification is running, and reclassification is blocked while a backup or restore is running.

### Skills System
Pluggable skill workflows loaded from Markdown files in `mem-mcp/skills/`.

- `find_skills` — lists all available skill `.md` files
- `get_skill_workflow(name)` — returns the full Markdown content of a skill
- MCP prompts (`process-transcription`, `memory-deduplication`) inject skill instructions into the conversation
- Add new skills by creating a `.md` file in `mem-mcp/skills/` — no server restart required

### Diary Keyword Extraction
Every diary save/update triggers automatic keyword extraction via the query LLM (`qwen3.5:0.8b`).

- Keywords are extracted **per window over the whole entry** and the union is capped at `KEYWORD_LIMIT` (20, `MEM_KEYWORD_LIMIT`), stored in both Qdrant payload and Neo4j node
- Keywords boost vector search relevance in `diary_search_entries`
- Backfill existing entries: `python mem-mcp/reindex_diary_keywords.py -u <user_id>`
- CLI options: `-f/--force` (re-extract even if keywords exist), `-d/--dry-run`, `-c/--concurrency` (default 3)

**The union is capped; the window count is not.** `KEYWORD_LIMIT` bounds what a
long entry can add, and the truncation keeps the *front* of the list because each
window's prompt asks for the most important keywords first. `KEYWORD_EXTRACT_WARN_WINDOWS`
(6) only warns about LLM cost — capping the window count would reintroduce the
silent tail-drop the windowing exists to prevent.

**The 10-keyword cap was per window, not per entry.** Do not reintroduce a slice
of the *entry* to keep the prompt fast: the old `text[:1500]` meant a 40k
transcription got keywords describing its opening, so a query about anything in
the last thirty pages scored as though the entry had never mentioned it. That is
a silent loss — the boost is simply absent, with no error to explain it. The
entry name is prepended to **every** window, not just the first: a window from the
middle of a long entry has no other way to know which entry it belongs to.
`KeywordWindowTests` asserts on the prompt that reached the model, because a
call-count assertion cannot tell the two implementations apart — both make calls.

### People Candidate Resolution

`resolve_people_candidates()` in `matching_utils.py` binds extracted names to
People records, and it now runs **once per window** of the entry too, unioning
the accepted bindings. It used to send `(content or '')[:2500]`, and this is the
sharpest form of the truncation defect class: the prompt's job is to
disambiguate, so a person discussed on page three had no supporting context and
the model either declined to bind the mention or — worse — bound it to a
different, similarly-named candidate who *was* in the opening. That writes a
**wrong `MENTIONS` edge**, which is a false statement, not a recall loss.

Two properties of this function are load-bearing:

- **The merge is a plain union, and deliberately so.** The candidate set is
  already filtered by `people_match_allowed` and does not vary by window, and
  the original code discarded the LLM's confidence value and kept only accepted
  ids. So there is no cross-window confidence strategy to get right. If a
  confidence value is ever *kept* for ranking, the merge has to grow one.
- **The 0.8 gate applies in every window.** A thin window is not a weaker gate.
  Note `bool` is an `int` subclass, so `confidence >= 0.8` accepts `True` unless
  it is excluded explicitly — the code does.

`matching_utils.py` keeps **zero app imports** (`os`, `re`, `json`, `difflib`
only) so it can be imported without the DB drivers and tested directly. Do not
add a `logger` from `common` for the per-window failure paths: a failed window
is skipped silently, as the whole call used to be, and the union means the
surviving windows still apply. `ResolverInputTests` pins the import set.

Because the resolver is pure and importable, its tests **call** it rather than
reading its source. That is the reason it is worth testing behaviourally: a
truncation is invisible to a call-count assertion, so the tests have to inspect
what the prompt actually contained and which bindings survived it.


### Diary People Extraction

Diary people extraction runs the **whole entry**, in overlapping windows, not a prefix. `_extract_people_names()` in `diary_manager.py` iterates `people_extract_windows(content)` and unions the results.

The prefix form is the one to avoid. It sent `content[:2000]`, so on a 40k-char transcription every person named after character 2000 was silently missed — no error, just a missing `MENTIONS` edge nobody was looking for. Keyword extraction (above) now windows the same way, so the two paths agree on what "the entry" means.

- `people_extract_windows(content, window=0, overlap=0)` is pure and unit-tested (`test_people_extraction.py`): blank → `[]`, a doc that fits the window → one element (short entries cost what they always did), otherwise `body[i:i+size] for i in range(0, len(body), size - overlap)`.
- The overlap exists because a name straddling a boundary is cut in half, and both halves then look like garbage to the extractor. `step` is clamped to `size - 1` so a degenerate overlap cannot make the loop non-terminating.
- `MEM_PEOPLE_WINDOW` (6000) and `MEM_PEOPLE_OVERLAP` (600) tune it.
- **Do not cap the window count.** Capping would reintroduce exactly the silent tail-drop the windowing fixed. Above `PEOPLE_EXTRACT_WARN_WINDOWS` (6) the entry is expensive (one LLM call per window) and logs a WARNING naming the knob instead. This is chat traffic, so WARNING is the right level per the logging policy in "Embedding Reliability".
- Each window is wrapped in its own `try/except` inside the loop, so one bad window cannot discard the names the other windows found. `clean_extracted_people_names(found)` dedupes the union, since a name spanning the overlap is found twice.