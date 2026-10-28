# Memory MCP — Memory Vault

A self-hosted MCP (Model Context Protocol) server that gives AI agents a **persistent memory layer** using vector embeddings and a Knowledge Graph. Includes an advanced editable web dashboard with relationship tracking, a client/project scoping model, and an admin surface for the operations that act on the whole vault rather than one account.

## Architecture

```
mem-mcp/
├── server.py              # Entry point – unified FastAPI server (Port 8080)
├── memory.py              # Facade: re-exports from common, fact_manager, diary_manager, client_manager
├── common.py              # Config, DB clients (Qdrant, Neo4j, Ollama), helpers
├── fact_manager.py        # Fact CRUD, search, dedup, graph operations
├── diary_manager.py       # Diary CRUD, search, keyword extraction, consistency
├── client_manager.py      # Client/Context nodes, scope inference, status management
├── migrate_client_context.py  # One-time migration: Client/Project facts → Client/Context nodes
├── mcp_tools.py           # FastMCP tool definitions
├── mcp_skills.py          # MCP prompts and resource definitions for skills
├── mcp_logging.py         # MCP tool call logging/monitoring
├── gui.py                 # FastAPI web app: REST API, Landing Page, and Dashboard
├── matching_utils.py      # Dependency-light matching, merge, and LLM candidate helpers
├── chunking.py            # Splitting a long record into several vectors, and searching them
├── scoped_ids.py          # The one place client/context ids are derived (uuid5)
├── sessions.py            # Stdlib-only: sessions, access keys, credential store, registration
├── backup.py              # Savepoints: Qdrant snapshot + graph export, and restore
├── status_monitor.py      # Ollama status snapshot, change fingerprint, SSE subscribers
├── google_auth.py         # OAuth 2.0 authorization-code client over stdlib urllib
├── migrate_vault_user.py  # CLI: hand one user's whole vault to another
├── reindex_chunks.py      # CLI: backfill chunking for existing long records
├── reindex_diary_keywords.py  # CLI: backfill keyword extraction for existing diary entries
├── test_*.py              # 16 dependency-light regression suites (see Testing)
├── skills/                # Markdown skill workflows, loaded at request time
├── templates/             # Landing page and dashboard
├── requirements.txt
├── Dockerfile
└── .dockerignore
```

## Features

- **Semantic Memory** — Store and search facts by vector similarity using Qdrant.
- **Knowledge Graph** — Facts are linked in Neo4j, enabling relationship tracking and graph traversal.
- **Advanced Metadata** — Facts support rich JSON metadata (tags, source, confidence, etc.).
- **Knowledge Patterns** — Automatically identifies recurring themes and associations via graph analysis.
- **Diary** — Narrative entries with Markdown support, LLM-powered keyword extraction, and vector similarity search.
- **Smart Search** — LLM query rewriting (see `MEM_EXTRACT_MODEL`) decomposes natural language into keyword phrases; multi-query expansion merges results from multiple vector searches.
- **Memory Deduplication** — Scope-aware multi-signal similarity clustering (vector, name, alias, email) with weighted fuzzy evidence, core-member filtering, and a guided merge workflow.
- **LLM-Assisted Resolution** — LLMs review bounded People and duplicate candidates with diary, scope, metadata, and record context; returned IDs are validated before use.
- **Recoverable Merges** — Neo4j/Qdrant merge cleanup records pending Qdrant deletions and retries them through startup orphan reconciliation when a store is temporarily unavailable.
- **Dashboard Deduplication** — The Service → Data sub-tab scans scope-compatible clusters, lets you select the records to merge, generates an editable consolidated draft with the LLM, and requires confirmation before merging.
- **Split Chat Model Roles** — High-volume extraction — diary keywords, people names, search-query rewriting — runs on `MEM_EXTRACT_MODEL`; the judgement calls, scope classification and merge drafts, run on `MEM_SCOPE_MODEL` and `MEM_MERGE_MODEL`. See [Chat models](#chat-models) for why the split is not a preference.
- **Skills System** — Pluggable skill workflows (e.g., `process-transcription`, `memory-deduplication`) loaded from Markdown files.
- **Unified Web UI** — A modern, proxy-aware dashboard to manage memories, view diary history, and explore insights.
- **Multi-user Isolation** — Secure per-user vaults, resolved from a verified dashboard session, a password-checked `Basic` credential, or an access key. Nothing else names a user.
- **Client & Context Scoping** — Facts and diary entries can be scoped to a Client (e.g. "Deutsche Bank") and a Context within it (e.g. "SAP Implementation") via `FOR_CLIENT` / `IN_CONTEXT` graph links.
- **Scope-Priority Search** — An explicit `client`/`context` **prioritises** results (sort key `(scopeStrength, score, confidence)`) but **never excludes** them. A record filed under another client, or under none, still comes back — ranked lower. That is what makes it work with two people called John at the same bank: asked for one project, the right one comes first and the other is still findable.
- **Scope Inference** — When no explicit client is passed, search infers the client from the query text.
- **Inactive Client Handling** — Clients untouched for 90 days are auto-deprioritized (−0.15 score, never hidden). Status can be manually pinned via `set_client_status`, `PUT /api/clients/{id}`, or the Clients-tab toggle; pinned clients are never auto-changed.
- **Clients & Projects Tab** — The client list with activate/deactivate, rename, delete, and the projects nested under each client.
- **Setup Tab** — MCP connection details and **Access Keys** (revocable `Bearer mvk_…` credentials for MCP clients), plus your live browser sessions.
- **Admin-Only Service Tab** — Operations that act on the whole vault or the whole server rather than on one account live here, behind `MEM_ADMIN_USER`, in three sub-tabs:
  - **Users** — every account from all three credential stores (registered signups, Google-linked vaults, and the operator's `htpasswd` file) with enable/disable and an irreversible delete. Also shows whether signup is currently on, read-only.
  - **Models** — what Ollama holds in memory, and the controls to unload it. On a shared GPU card two resident chat models evict each other, so unloading the one you are not using is the cheapest way to stop paying the cold-load cost.
  - **Data** — backups and restores, scope reclassification, deduplication, and a consistency check plus a repair pass over the graph and the vector index.
- **Consistency Management** — The graph and the vector index are two stores holding the same records. A read-only check reports every place they disagree, and a repair fixes what has an automatic fix: filling in missing diary titles and timestamps, deleting vector points with no record behind them, re-embedding records with no vector, and pruning links that cross between vaults. Both used to run only at boot and only to the log.
- **Persistent Sessions** — Login survives a server restart and a container rebuild; sessions live in SQLite on a bind-mounted volume rather than in the cookie, and the plaintext password is no longer stored.
- **Daily Backups & Restore** — An in-process scheduler writes a vault-wide savepoint (graph + both vector collections) each night, keeps the last `MEM_BACKUP_KEEP`, and Service → Data → Backup & Restore can take one immediately or restore any of them.
- **Long-Record Chunking** — A fact or diary entry that runs past the embedding budget is indexed as several vectors, so a query about a detail in the middle of a 40k-char transcription can still find the record. Search collapses a chunk family back to one result and returns the full text.
- **Lazy-Loaded Lists** — Memories and diary sidebar render in batches of 50 with infinite scroll; calendar has a 📅 jump-to-today button.

## Ports & Access

The server is **unified** on port **8080** (mapped to **8086** in Docker).

| Component | Path | Description |
*   **Landing Page** | `/` | Onboarding, MCP setup instructions, and auto-detected credentials.
*   **Web Dashboard** | `/gui` | The main interactive dashboard (Memories, Diary, Graph, Clients, Projects, Setup, and a Service tab only for the configured admin).
*   **MCP Endpoint** | `/mcp` | The Model Context Protocol entry point for AI clients.
*   **REST API** | `/api/*` | Backend endpoints used by the GUI.

## Authentication

Two gates check credentials, and they do not accept the same ones:

| Credential | Where it is verified | Use it for |
|---|---|---|
| Dashboard session cookie | Issued by `POST /api/auth/login`, against the registered-account table then `htpasswd` | Browsing the dashboard and `/api/*` from a browser |
| `Authorization: Basic <base64 user:pass>` | `auth_guard`, against the same store: `credentials` first, `htpasswd` only for accounts the app does not own | Scripted calls to `/api/*` |
| `Authorization: Bearer mvk_…` | `McpAuthGuard`, against the access-key store | **All MCP clients** — the only thing `/mcp` accepts |
| Google sign-in (OAuth redirect) | `GET /api/auth/google/start` → `…/callback` | Signing in from the browser; ends in the same session cookie |

MCP requests are authenticated **by the application**, not by nginx. This is a
change from earlier versions, where nginx's `auth_basic` on the `/mem-mcp/mcp`
location was the only check and the app trusted whatever it was handed.

The MCP endpoint takes only a **per-call access key**. It refuses a session
cookie, a `Basic` header and a Google sign-in, and that is deliberate:
a cookie is a credential the browser replays on its own and cannot be scoped to
one device, and Basic is the account password, which also unlocks `/api/*` and
cannot be revoked for one lost laptop without changing it for everyone. So an MCP
client that used to send a username and password needs a key instead. Your
dashboard login is unaffected; it authenticates `/gui` and `/api/*` exactly as
before, and signing in with Google is a browser flow that ends in the same
session cookie.

**Access keys** are created in **Setup → Access Keys**. A key is shown exactly
once, at creation: only a SHA-256 hash is stored, so it cannot be displayed
again or recovered. Revoking one takes effect immediately, with no file to edit
and no restart. Give each device its own key so a lost laptop is one revoked key
rather than a changed password.

```bash
# Setup → Access Keys → create one, then:
claude mcp add --transport http memory-vault https://<your-host>/mem-mcp/mcp \
  --header "Authorization: Bearer mvk_..."
```

**Sessions** are stored server-side (SQLite) rather than inside the cookie, so
they survive a container rebuild: the cookie carries only an opaque random id
and the record lives in `MEM_SESSION_DIR`, which compose bind-mounts to
`./mem-mcp-data/sessions`. A session is valid for 30 days from creation (not
sliding) and can be ended by logging out. The session no longer stores your
password — that is what the access key replaced.

### Google sign-in

Sign in from the front door: the landing page's **Continue with Google** button
runs a normal **OAuth 2.0 authorization-code redirect**. Set `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` in `.env` (both are required — the secret is what makes the
code redemption confidential), register `{BASE_URL}/api/auth/google/callback` as the
callback in Google Cloud, and the button appears.

A first-time Google account needs no setup at all: the app creates the vault,
keyed on the email address Google reports, and signs you in. A returning account
resolves to the same vault. No paste box, no token, no Setup section.

### Registration

Self-service sign-up is **off unless you turn it on**: set
`MEM_REGISTRATION_ENABLED=1` and restart. With it off the signup card is not
rendered and the routes answer 404, so nothing is exposed by upgrading. Each
method has its own further requirement, so turning the flag on is not the same as
having a usable form:

- **Email and password** — a **username**, an address and a password of at least
  10 characters. Needs an SMTP server (`MEM_SMTP_HOST`, `MEM_SMTP_USER`,
  `MEM_SMTP_PASSWORD`, `MEM_SMTP_FROM`, and `MEM_SMTP_STARTTLS`); with SMTP
  unconfigured the form is **not shown at all**, because a form whose only job is
  to send a confirmation mail would take an address and then refuse to use it.
- **Continue with Google** — needs `GOOGLE_CLIENT_ID` *and*
  `GOOGLE_CLIENT_SECRET`, as above.

**Your username is the account name**, and every account gets its own vault —
accounts share only the server, nothing in the data. A Google sign-in is named
after the address Google reports, since there is no username to choose.

**The address is verified by a confirmation link**, so the account cannot sign in
until it is clicked. **There is no password reset** and no address change; the
landing page says so, because a user who does not know that will eventually rely
on "reset it with that address".

Registered passwords are stored as **scrypt hashes in the app's own SQLite
database**, not in `htpasswd`, which stays read-only. Existing htpasswd accounts
are therefore completely unaffected, and an account registered here cannot
overwrite one that already exists — a name or address already known is refused.

The signup route is rate-limited per client address (10 attempts / 10 minutes).
That is in-process, so a deployment behind more than one worker wants a limit in
the proxy as well.

`/mcp` is **unchanged** by this: it still accepts only an access key, never a
session cookie. Signing up gives you a dashboard, not an MCP credential.

### Proxy header trust

**The GUI, the REST API and `/mcp` all require a verified credential.** A
dashboard session, a `Basic` password checked against the app's credential store
or its `htpasswd`, or an access key. Nothing else names a user.

The one exception is `/api/auth/*` — login, logout, signup, email confirmation
and the two Google OAuth routes — which is unauthenticated *by design*, because
it is where you exchange a credential for a session. That is also why those
routes carry their own rate limiter rather than inheriting one: with the
registration flag off, that limiter is the only thing between a script and the
signup route.

In particular, `Authorization: Basic` is *verified*. It used to be decoded and
its username trusted without ever checking the password, which meant
`base64(alice:anything)` authenticated as alice and reached every route,
including the vault-wide restore endpoint. If you have a script that was relying
on a wrong password working, it will now get a `401`.

The proxy identity headers (`Remote-User`, `X-Remote-User`, `X-User`,
`X-Forwarded-User`) are **no longer a way to authenticate**. They were the
weakest link in the chain — a proxy that forwards a client-supplied `Remote-User`
lets anyone name any user — and the app now strips the requirement instead of
the risk: the header-reading resolver survives only on the `/mcp` path, where it
reads the app's own verified `X-Vault-User` stamp and nothing else. If you were
running an authenticating proxy in front of this app and relying on it to supply
identity, that no longer works; put it in front of `/mcp` and have it inject
`X-Vault-User` only if you have read the section above on why that header is
stripped on ingress.

`nginx_snippet.conf` sets none of these headers on either location — `$remote_user`
is empty now that `auth_basic` is gone — and the app stamps the verified
identity itself as `X-Vault-User` after stripping any inbound copy.

## MCP Tools (Advanced Suite)

| Tool | Description |
|---|---|
| `add_fact` | Store a new fact with optional category, rich metadata, and client/context scope (auto-created). |
| `search_facts` | Semantic search for facts with optional category and client/context scoping plus LLM query rewriting. |
| `list_clients` | List all clients with their contexts for the current user. |
| `create_client` | Explicitly create a new client (idempotent). |
| `set_client_status` | Pin a client active/inactive (pinned status is never auto-changed). |
| `link_facts` | Create semantic relationships (e.g., `WORKS_ON`) between two facts or diary entries. |
| `unlink_facts` | Remove a relationship between two facts or diary entries. |
| `get_fact_neighborhood`| Traverse the knowledge graph around a fact (context exploration). |
| `update_fact` | Partial updates to text, category, or metadata of existing facts. |
| `delete_fact` | Remove a fact from both vector and graph stores. |
| `list_categories` | List all distinct categories currently used in the vault. |
| `find_patterns` | Discover recurring themes and category clusters in the graph. |
| `find_duplicates` | Find potential duplicate entries using multi-signal similarity clustering. |
| `suggest_merge` | Analyze a cluster of duplicates and suggest a master record for merging. |
| `merge_facts` | Execute a merge: update master, move relationships, delete duplicates. |
| `diary_save_entry` | Create/update a narrative diary entry with automatic keyword extraction and optional client/context scope. |
| `diary_search_entries` | Semantic search across diary entries with keyword boosting and optional client/context scoping. |
| `list_diary_entries` | List diary entries within an optional time range. |
| `diary_delete_entry` | Delete a diary entry by ID. |
| `find_skills` | Scan the skills directory and list available skill workflows. |
| `get_skill_workflow` | Retrieve the detailed Markdown workflow for a specific skill. |

## Quick Start (Docker)

1. **Configure secrets**: `cp .env.example .env` and set `MEM_NEO4J_PASSWORD`.
2. **Launch (GPU host — the default)**: `docker compose up -d`. The base file requests the NVIDIA device, so this needs an NVIDIA driver and the NVIDIA Container Toolkit.
3. **Launch (CPU-only host)**: `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d`, and set `MEM_GPU_EXPECTED=0` in `.env` so the status strip does not warn about the expected state. Ollama does fall back to the CPU by itself, but only once its container exists — compose refuses to create one whose device reservation names a device the host does not have — so on a machine with no NVIDIA driver the reservation is removed rather than left to fail at runtime. That overlay needs Compose ≥ 2.24 (`!reset`) and costs roughly tens of times the latency of the GPU path.
4. **Verify Ollama actually got the card** (GPU hosts): `docker exec ollama ls /dev/nvidia0` must succeed and `docker exec ollama ollama ps` must say `100% GPU`. `100% CPU` on a GPU host means the model loaded with no GPU layers; see "Critical Config" in `AGENTS.md`, because that has two causes and only one of them is a deploy mistake.
5. **Initialize Embedder**: `docker exec ollama ollama pull nomic-embed-text`
6. **Initialize the chat models**: the four roles are split across two models, and
   the split is deliberate — see the model table below.
   ```bash
   docker exec ollama ollama pull nemotron-3-nano:4b   # scope + merge
   docker exec ollama ollama pull granite3.3:2b        # extraction
   ```
   `MEM_EXTRACT_MODEL` covers the high-volume extraction work (diary keywords,
   people names, search rewriting) and `LLM_QUERY_MODEL` / `MEM_SCOPE_MODEL` /
   `MEM_MERGE_MODEL` cover the judgement work. Do not collapse them onto one
   model: a scope verdict is stamped and never revisited, so a **null** there is
   recoverable and a **guess** is permanent, which is what decided the split. A
   2b model also completes fields rather than reasoning about them — it will
   confidently name a project the record never mentions. On a 4 GB card these
   two cannot both stay resident, so expect a cold load when a search follows a
   reclassify.
7. **After code changes**: rebuild the image with `docker compose up -d --build mem-mcp` (dependency downloads are cached by BuildKit, so this is fast after the first build)

Visit **http://localhost:8086/** for the interactive setup guide.

### Chat models

Four roles, split across two models. The split is not a preference — it is the
asymmetry between an error you can repeat and one you cannot.

| Role | Variable | `.env.example` | Job |
|---|---|---|---|
| Extraction | `MEM_EXTRACT_MODEL` | `granite3.3:2b` | Diary keywords, people names, search-query rewriting |
| Query | `LLM_QUERY_MODEL` | `nemotron-3-nano:4b` | Fallback when no model is given for a role |
| Scope | `MEM_SCOPE_MODEL` | `nemotron-3-nano:4b` | Classifying a record's client and project |
| Merge | `MEM_MERGE_MODEL` | `nemotron-3-nano:4b` | Dashboard merge-draft generation |

The **code defaults** are narrower: `LLM_QUERY_MODEL` falls back to
`qwen3.5:0.8b`, `MERGE_MODEL` to `gemma4:e2b`, and `EXTRACT_MODEL`/`SCOPE_MODEL`
to `LLM_QUERY_MODEL`. So a bare install with an empty `.env` gets one small
model for all four roles, and the split above is something `.env.example` opts
you into rather than something the app does by default.

That asymmetry is why the split exists. Extraction failures are recoverable —
re-running the extractor is cheap and overwrites the bad result. **A scope null
is not**: it is stamped with `scopeCheckedSig` and never revisited, so it is
permanent scope loss. Recoverable-versus-permanent, not speed, is what decided
it.

Two of these cannot stay resident on a 4 GB card at once, so a role change
evicts the other. That is the intended trade — the cheap high-volume role
reloads fast, the expensive one stays warm across a reclassify — but it means a
search immediately after a reclassify pays a load.

**`MEM_EMBEDDER_MODEL` is not one of these.** It must stay an embedding model
(`nomic-embed-text`). Pointing it at a chat model silently invalidates every
vector already in Qdrant, and nothing errors.

If your `.env` disagrees with `.env.example`, the `.env` is what is running —
the example is the recommendation, not the enforced default. In particular,
check that `MEM_SCOPE_MODEL` is the **reasoning** model. Putting it on
`granite3.3:2b` is not a speed-up: that model answers quickly and confidently,
and a scope verdict is stamped and never revisited, so every wrong one is
permanent.

## Testing

Run the focused dependency-light regression suite. Any `python` or `python3` on
`PATH` runs all sixteen of them, and none of them needs Neo4j, Qdrant or Ollama:

```bash
cd mem-mcp
MEM_SESSION_DIR=/tmp/mem-vault-test-sessions python3 -m unittest -v \
  test_matching_regressions test_embedding_reliability test_chunking \
  test_cypher_safety test_backup_compression test_people_extraction \
  test_graph_scope test_mobile_layout test_llm_reliability test_env_wiring \
  test_status_monitor test_sessions test_auth_guard test_google_auth \
  test_registration test_vault_migration
```

`MEM_SESSION_DIR` matters: without it the session and PSK tests write a
`sessions.db` into the source tree.

The suites cover scope-aware duplicate matching, weighted scoring,
bridge-cluster rejection, merge target ownership, merge mutation ordering,
People candidate resolution, scope-name and `Client:`-header resolution, LLM
prompt contracts, the chunking split, embedding retry/fallback behaviour, the
Cypher written in each module, snapshot compression, the diary people/keyword
extraction windowing, graph scope and capping, the mobile stylesheet, LLM chat
timeouts and log content, that every documented variable actually reaches the
container, the session and PSK stores, which credential each gate accepts, the
OAuth redirect flow, the registration routes, and the vault-move planner.

Most suites cannot import their subject — `gui.py`, `common.py` and
`backup.py` need FastAPI, httpx and the database drivers. Those suites lift the
real functions out of the source with `ast.get_source_segment` and run them
against stubs, so they test the shipping code rather than a copy. A few modules
(`sessions.py`, `matching_utils.py`, `google_auth.py`, `status_monitor.py`) are
stdlib-only by design precisely so they *can* be imported and called directly.

## Backups

The vault writes a **savepoint** every day and keeps the most recent
`MEM_BACKUP_KEEP` of them. Manage them from **Service → Data → Backup & Restore**,
which requires the admin account.

- A savepoint holds the whole vault: the graph plus both vector collections. It
  lives in `mem-mcp-data/backup/` on the host (bind-mounted to `/app/backup`).
- **Back up now** takes an immediate one instead of waiting for the nightly slot.
- **Restore** replaces all current data with the selected savepoint. Take a fresh
  savepoint first if you have recent work you want to keep.
- Restoring a savepoint that did not finish is not possible — incomplete
  savepoints are listed as *Unusable*.
- Everything in a savepoint is gzipped — both the graph export and the two
  vector-collection snapshots. The vector snapshots compress well because a long
  record is stored as several vectors that share the same payload, so the raw
  file is mostly repetition. Savepoints taken before compression were added
  still restore normally.

Schedule and retention are controlled in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `MEM_BACKUP_ENABLED` | `1` | `0` disables the nightly run; manual backups still work |
| `MEM_BACKUP_HOUR` / `MEM_BACKUP_MINUTES` | `3` / `0` | Local server time of the nightly run |
| `MEM_BACKUP_KEEP` | `14` | How many completed savepoints to keep |
| `MEM_BACKUP_DIR` | `<dirname LOG_DIR>/backup` | Where savepoints are written |

Savepoints are copies, not a live mirror — if the host disk is lost, they are
lost with it. Copy `mem-mcp-data/backup/` off the machine if you need a
disaster-recovery copy.

## Administration

Operations that act on the **whole vault or the whole server** rather than on
one account are not gated on "whoever is logged in". A restore overwrites every
vault, a reclassify rewrites every client assignment, a backup snapshots both
collections and the graph, and unloading a model evicts it from a GPU other
people are paying a cold load for. So they require the one account named in:

```bash
MEM_ADMIN_USER=your-admin-username
```

Leave it **empty** and the Service tab is not rendered and no admin endpoint is
reachable — which is the right default for a shared vault. With it set, the
server decides (`ctx["IS_ADMIN"]` comes from the verified session, never from a
request parameter), so the tab is **not rendered for other users at all**, not
merely hidden: every handler behind it independently answers `403` to a
non-admin. Hiding the UI is the friendly half; the guard is the boundary.

### Service → Users

Every account is a row, drawn from **all three credential stores**, because a
user is three stores and the admin table is the only place they are seen
together:

| Store | How the account got there | Suspend | Delete |
|---|---|---|---|
| Registered signup | `POST /api/auth/register`, then the email link | ✅ | ✅ |
| Google sign-in | First sign-in creates the vault, keyed on the verified address | ❌ no password row | ✅ |
| Operator's `htpasswd` | Edited by hand on the host | ❌ | ❌ |

The two ❌ cases are different failures, and the UI names which is which:

- An **`htpasswd`** account is managed on the host. The file is mounted `:ro` on
  purpose, so a Disable here would close the password path and leave the
  `htpasswd` path still working — worse than saying plainly that nothing here
  can change it.
- A **Google-only** vault has no password to suspend. Delete still works, since
  it removes the identity link as well.

**Disable** suspends a *registered password* account: it cannot sign in, and its
vault stays exactly where it is. Enable puts it back. This is the reversible
option. A Google-only or `htpasswd` account has no `Disable` at all — there is
no password to suspend — and the row says which of the two reasons applies
rather than just showing no button.

**Delete is irreversible** and removes the account together with *every*
credential attached to it: the credentials row, the Google link, every access
key, and every browser session. It does **not** delete the records — the facts,
diary entries and clients stay in the graph and the vector store, because they
are keyed by `userId` and nothing here deletes by it. The response says so
(`vault_records_kept`), and the UI says so before it asks twice.

Deleting every credential rather than just the password is deliberate: `/mcp`
accepts **only** an access key, so removing the credentials row while leaving a
key behind would leave a client able to read a vault whose account no longer
exists. Two deletions are refused outright — the configured `MEM_ADMIN_USER` and
the account you are signed in as — because either would leave the deployment
with no admin at all.

The signup flag is shown **read-only**. `MEM_REGISTRATION_ENABLED` is read once at
startup, so a runtime toggle would be a second source disagreeing with the one
the landing page actually renders from.

### Service → Models

What Ollama currently holds in memory, updated live, with the controls to
unload it. Unloading is safe — the next request that needs the model reloads it, at a
cost of 9-20s (measured on the 4 GB card this ships against, not a constant in
the code). Unload validates the name against the live snapshot first,
because unloading a model Ollama has not loaded answers `200` and does nothing,
which would otherwise be an operation that reports success and changes nothing.

Every signed-in user can already see the resident models in the sidebar status
widget; this panel is the admin-only detail and the controls.

### Service → Data

Backups and restores (see above), scope reclassification, deduplication, and the
consistency tools.

The consistency check is **read-only** and reports every place the graph and the
vector index disagree. Expect the count mismatches to be non-empty and expect
them to be *fixed by a repair rather than by a check*: the boot order runs the
checks before the reconcile step, so a mismatch means "the repair has this many
to fix", not "data is lost". The repair fills in missing diary titles and
timestamps, deletes vector points with no record behind them, re-embeds records
with no vector, and prunes links that cross between vaults. It takes the
maintenance lock, and it re-embeds records — so run the check first, and read it
before you press the button.

## Embedding Reliability

Every fact and diary entry is embedded through a single choke point. Ollama is
flakier than the rest of the stack — a model still loading, a GPU under memory
pressure, a container mid-restart — so the client retries transient failures and
tries both the legacy `/api/embeddings` and the modern `/api/embed` route.

If it still cannot embed, the save is rejected **before** anything is written, so
a failed embedding never leaves a record without a vector. The error names the
model and the reason Ollama gave, instead of a bare `500`.

| Variable | Default | Meaning |
|---|---|---|
| `MEM_EMBED_RETRIES` | `2` | Retries per route for a transient failure |
| `MEM_EMBED_RETRY_BACKOFF` | `1.5` | Seconds multiplied by the attempt number |
| `MEM_EMBED_MAX_CHARS` | `8000` | Character budget per embedding (measured: the embedder refused 11189 chars, accepted 6000) |

A missing model is not retried — the error tells you to run
`docker exec ollama ollama pull <model>`.

Ollama serves embedding models with a smaller context window than the model
advertises, so a long fact or diary entry can exceed it — and Ollama reports
that as a `500`, not a `413`. Text over the budget is truncated before sending
(the beginning and the end are kept, since that is where the subject and the
conclusions live), and if Ollama still rejects the length the input is halved
and retried. That case is never retried with backoff, because re-sending the
same oversized text is what made the first two production failures take five
requests each and still fail.

### Log levels

Embedding is the high-volume path, so it does not log at `WARNING` per call.
At `LOG_LEVEL=WARNING` (the production setting) you get:

- `WARNING` — the input was over budget and got truncated, which means the
  stored vector is a lossy summary of a long record. Rare, and worth knowing.
- `ERROR` — the embed failed. Logged once, with Ollama's own reason and the
  `ollama pull` command to fix it. Nothing is written in this case.
- `DEBUG` — every individual attempt and retry, if you turn the level down.

`WARNING` otherwise belongs to the chat/LLM traffic, which is what you are
usually looking for.

## Long Records & Chunking

One vector for a 40k-char transcription is a lossy average of the whole thing: a
query about a decision in the middle scores poorly against it and against
everything else. Truncating to fit the embedding window bounds the damage but
discards the middle entirely. So a record that runs past the budget is indexed
as **several vectors**, one per chunk.

- A short record is stored exactly as before — one point, and nothing to
  migrate. Only records that exceed the budget get chunked.
- A long record becomes N points sharing the record's category, client, context
  and keywords, so filtering behaves identically whichever chunk matched.
- Search collapses a family to its best chunk, so a record still appears once,
  and the full text is returned regardless of which chunk matched.
- Updating a record replaces the whole family. Shortening the text removes the
  now-stale trailing chunks.

| Variable | Default | Meaning |
|---|---|---|
| `MEM_CHUNK_CHARS` | `3000` | Target characters per chunk |
| `MEM_CHUNK_MAX` | `16` | Embedding calls per save (a cost budget, not a coverage cap — the chunk size grows to keep every part covered) |
| `MEM_CHUNK_OVERLAP` | `200` | Characters repeated between neighbouring chunks so a sentence on a boundary is not lost |
| `MEM_CHUNK_FETCH_MULTIPLIER` | `3` | Widens the vector result window, since one record can now occupy several slots |

Existing long records are converted with:

```bash
python mem-mcp/reindex_chunks.py --dry-run   # report only, writes nothing
python mem-mcp/reindex_chunks.py             # rewrite the long ones
```

The script is idempotent — it skips anything already chunked — and each record
costs one embedding call per chunk, so start with the dry run.

You do not actually need to run it: the server converts these records itself on
the next start, in the background, so a restart is usually enough. `MEM_RECHUNK_ENABLED=0`
turns that off, and `MEM_RECHUNK_LIMIT` caps how many records a single restart
converts — the rest carry over to the following one.

## Claude Desktop Setup

Run this command to add the vault to your Claude configuration:
```bash
# Create a key first in Setup → Access Keys, then paste it in place of <key>:
claude mcp add --transport http memory-vault https://<your-host>/mem-mcp/mcp --header "Authorization: Bearer <key>"
```
*(The Setup page fills this command in with your real key when you create one, and
the landing page links straight to it. `/mcp` accepts this header and nothing
else — a client still configured with a username and password will get a 401
naming the access-key header it needs.)*

## Tech Stack

- **Frameworks**: FastAPI, FastMCP
- **Databases**: Qdrant (Vector), Neo4j (Graph)
- **AI/ML**: Ollama (`nomic-embed-text` for embeddings; `granite3.3:2b` for extraction, `nemotron-3-nano:4b` for scope/search/merge — see [Chat models](#chat-models))
- **Frontend**: Vanilla JS, Modern CSS3

## Project/Context Sharing

A vault owner can share a single client or context with another signed-in user via a link or direct grant, with read or read-write permission.

- Feature flag `MEM_SHARING_ENABLED` (default 0). Enable to turn on.
- Endpoints: `POST /api/share/links`, `GET /api/share/links`, `DELETE /api/share/links/{id}`, `POST /api/share/accept/{token}`, `POST /api/share/grants`, `GET /api/share/grants`, `PUT /api/share/grants/{id}`, `DELETE /api/share/grants/{id}`, `GET /api/share/mine`, `GET /api/audit`, plus `GET /share/{token}` (the landing page a link opens).
- MCP tools: `list_shared_with_me`, `accept_share_link`, `list_share_grants`.
- Records created under a shared target are stored under the owner's userId with `createdBy`/`updatedBy` set.
- Audit log in a separate SQLite DB (`MEM_AUDIT_DB`), 90-day retention (`MEM_AUDIT_KEEP_DAYS`). The grants are the sole source of truth — there is no Neo4j mirror, so `audit.db` is copied into every savepoint.
- Link TTL default 30 days (`MEM_SHARE_LINK_TTL_DAYS`), max 50 recipients (`MEM_SHARE_MAX_RECIPIENTS`).
- **What a grantee sees:** their own vault plus records under the granted client or project — including records that only *mention* it via `RELEVANT_TO`. A project grant keeps its parent client visible for navigation, but only that project underneath; sibling projects are never shared. A record reachable only through a related project shows `(private)` instead of the owner's own client/project names.
- **What a grantee cannot do on read-only:** rename, delete, reclassify, extract participants, change scope, or link. The controls are hidden rather than offered and refused.
- A link opens `/share/{token}`, which asks an anonymous visitor to sign in and offers a signed-in visitor an Accept button. The token is stored as a SHA-256 digest and its plaintext is shown once, at creation.
