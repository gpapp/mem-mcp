# Task History

Completed work, newest first. Open work lives in `tasks.md`; the convention is
recorded in `AGENTS.md` (Task Tracking).

---

---

# Live notes — ASR WebSocket proxy (2026-10-27)

Shipped in `5e36c71` on `feature/live-notes`.

- **L5 — the proxy.** `WS /api/live/stream/{transcriptionId}` in `gui.py` accepts
  the browser socket, resolves the user from `websocket.scope["session"]`
  (`auth_guard` does not run for a WebSocket scope), checks `Origin` through
  `live_notes.origin_allowed` with `BASE_URL` and the request origin as
  fallbacks, verifies ownership, reads the caller's token from
  `sessions.get_asr_token`, and only then calls `LIVE_GATE.begin_recording`.
  Every refusal accepts first, sends an actionable `{"type":"error"}` and closes
  with `4401` / `1008` / `4403` / `4409` / `4502`; refusing before `accept()`
  collapses all of them into an opaque 403.

- **Nothing is opened for a failed check.** The tests pin the order in the route
  source: ownership, token and `begin_recording` all precede `_ws_connect`.
  The upstream URL gets `X-API-Key` (never the browser) and `?language=` merged
  into an existing query. The websockets client is imported with
  `additional_headers` for 14+ and `extra_headers` for older versions.

- **Audio first, storage second.** `asr_reader` forwards each ASR message to the
  browser *before* parsing it, then `live_notes.asr_segment` maps a non-empty
  `transcript` to `{text, speaker, startSec, endSec}` with the connection offset
  added, and a single writer task drains an `asyncio.Queue`. `seq` is assigned
  from the stored maximum by `db_append_segment`, so a reconnect needs no client
  state. The tests pin the forward-before-queue order and the offset maths.

- **Pause/resume.** Pause waits the ASR detector's 0.8 s hangover before closing
  upstream, drains the reader (which lets a flushed tail and `stats` through),
  waits `queue.join()`, sets status `paused`, and only then sends
  `{"type":"paused"}`. Resume opens a new upstream connection at
  `offset = max(endSec)`, because asr-mcp timestamps restart at zero per
  connection. A close we initiated suppresses the "service disconnected" error
  via a per-connection quiet flag; an unexpected drop still reports it.

- **Priority.** `LIVE_GATE` is one process-wide gate in `live_manager.py`.
  `begin_recording` runs before the upstream connect and `end_recording` is in
  the `finally` block, so a browser crash cannot leave the vault permanently
  "recording". When `MEM_LIVE_UNLOAD_ON_RECORD` is on, a background task evicts
  the live model after the socket is accepted; it never delays the microphone,
  and it only calls `unload_ollama_model` when the snapshot says the model is
  actually resident.

The lesson to carry: a pause is not a disconnect. Closing the upstream must
first let the detector's hangover and post-roll finish, and the reader must be
allowed to drain its final messages before it is cancelled — otherwise the last
sentence of every section is silently missing from a recording that otherwise
looks complete.

---
# Live notes — transcription storage and note links (2026-10-27)

Shipped in `8d43536` on `feature/live-notes`.

- **L4 — graph model and CRUD.** New `live_manager.py` stores
  `(:Transcription)-[:HAS_SEGMENT]->(:TranscriptionSegment)` in Neo4j only; raw
  transcripts are deliberately not embedded. A note is an ordinary `:Fact` in
  category `Notes`, linked one-way with `(:Fact)-[:FROM_TRANSCRIPTION]->(:Transcription)`
  and `(:Fact)-[:REFERENCES]->(:Fact)`; `REFERENCES` is written directly rather
  than through `db_link_facts`, which would create the reverse edge and put the
  note on every referenced fact's card. `Transcription.noteId` and the segment
  `consumedBy`/`consumedAt` markers are the state that stops a pause
  re-summarising the whole transcript. Every create/update/delete and both ends
  of every link stamp `updatedAt`, because the backup fingerprint reads only
  that property and a count.

- **Deletion is a two-way hook.** `fact_manager.db_delete_memory` checks
  `f.source == "live_notes"` before calling `release_note_segments`, so an
  ordinary fact deletion pays two queries for a note it never was. Deleting the
  note clears every `consumedBy`/`consumedAt` and `Transcription.noteId`, which
  makes those segments eligible again; without that, deleting a note would look
  like losing the transcript. Deleting a transcription keeps the note by default
  and removes its `FROM_TRANSCRIPTION` edge first, so the survivor is an ordinary
  fact rather than a dangling live one.

- **Candidates are the identified client only.** `db_note_fact_candidates`
  returns `{id, name}` for facts with `FOR_CLIENT` to the session's client, plus
  facts under the selected project, excluding `Notes`. It never returns a body
  and never widens through another client's `RELEVANT_TO` edge. A session with
  no client yields no candidates at all rather than the whole vault.

- **Tests.** `test_live_notes.py` grew to 70 tests. The new L4 tests lift the
  real functions out of `live_manager.py` (it cannot be imported without
  `httpx`) and drive them against a fake Neo4j session, pinning that a segment
  edit names only `seg`, a segment delete is `DETACH DELETE seg` with no Fact,
  note deletion releases segments and clears `noteId`, and references are
  written in one direction. `test_cypher_safety` adds its automatic lint to
  every new query. The focused set (`test_live_notes`, `test_cypher_safety`,
  `test_sessions`, `test_env_wiring`, `test_matching_regressions`) is 392 tests
  green.

The lesson to carry: a query that deletes two variables from an `OPTIONAL MATCH`
(`DETACH DELETE seg, t`) can try to delete `null` when the optional side is
empty. Split it into an unconditional segment delete and a separate
transcription delete; the empty match simply deletes nothing.

---

# Live notes — configuration, ASR token store, pure helpers (2026-10-27)

Shipped in `06bd3f0` on `feature/live-notes`.

- **L1 — configuration.** `websockets` is a direct dependency because uvicorn is
  not installed with `[standard]`; it serves the browser socket and is the
  outbound asr-mcp client. The mem-mcp service gained
  `extra_hosts: host.docker.internal:host-gateway` (asr-mcp is on its own compose
  network and publishes 8087 on the host), and all eight `MEM_LIVE_*` /
  `MEM_ASR_WS_URL` knobs are both documented in `.env.example` and forwarded in
  the compose `environment:` block. `MEM_LIVE_CONTEXT_TOKENS` must track
  `OLLAMA_CONTEXT_LENGTH` (`16332`). `test_env_wiring` passes.

- **L2 — per-user ASR token store.** `asr_tokens(user_id PK, token, updated_at)`
  in `sessions.db`, with `set_asr_token` / `get_asr_token` / `clear_asr_token` /
  `asr_token_hint`. The token is plaintext at rest because the proxy replays it
  in `X-API-Key`; the database sits in the `MEM_SESSION_DIR` bind mount. `set`
  returns only `{set, hint, updated_at}` and never the value; the hint is the
  last four characters and cannot authenticate. `test_sessions` covers
  round-trip, isolation, clear, empty/oversized refusal and a fresh-process read
  (123 tests green).

- **L3 — pure helpers and priority gate.** New `live_notes.py` is stdlib-only
  and **called** by `test_live_notes.py` (62 tests). It owns the four-block
  prompt (existing note, unconsumed segments, referenced titles, candidate
  titles; no fact bodies), candidate ranking by title overlap, strict note-JSON
  validation (unknown ids dropped, existing references unioned, over-long note
  refused rather than sliced), the context-minus-prompt output budget with an
  inclusive floor, segment batching that never splits a segment, ASR time-offset
  maths, the `BASE_URL` origin check (absent Origin allowed, `null` refused),
  and `LivePriorityGate`. The gate increments a per-user generation on
  `begin_recording`, invokes and clears the **LLM-phase** cancel hook outside
  its lock, and `assert_idle` raises `RecordingActiveError` for a 409.

The lesson to carry: the prompt's four blocks are asserted as a **heading
count**, not by looking for expected text. A block added later is invisible to
`assertIn` over the block it did not touch, and the prompt is the one place a
fact body can leak into the context window.

---
# Backup skip: timestamps instead of a content digest (2026-10-27)

Superseded the content-digest fingerprint from `be36b29` per the operator's
correction: *"that's excessive. add update timestamps, ensure it is updated on
each write and link (both sides), use that for checking changes. drop hashing."*

`be36b29`'s fingerprint paginated the whole graph twice per scheduled run and
sha256'd every node and relationship body. The replacement reads one aggregate
per label — `MATCH (n) RETURN labels(n), count(*), toString(max(n.updatedAt))` —
plus `MATCH ()-[r]->() RETURN count(r)`, so a scheduled check is two small reads
instead of a full scan.

The write side is where the work was, and it is the part a future reader would
otherwise rediscover: **every create, update and link now stamps
`updatedAt = datetime()`**, and every link stamps **both** endpoints. That is
what makes a reclassify visible — it rewrites `FOR_CLIENT` / `IN_CONTEXT` edges
without touching any content property — and stamping both ends means moving a
same number of edges still moves a stamp. Touched: `fact_manager.py`
(`db_add_memory` CREATE, `db_update_memory`, both add/remove relevant,
`db_link_facts` / `db_unlink_facts` in all four branches),
`diary_manager.py` (`db_save_diary`, `db_update_diary`,
`db_update_diary_metadata`, `_auto_link_people`, `sync_diary_mentions`, link /
unlink mention, add / remove relevant), `client_manager.py` (client and context
create, rename, activate, cross-client, `_stamp_manual_scope`, the four
`link_*_to_*` helpers, and the clear statements in `db_set_fact_scope` /
`db_set_diary_scope`), and `migrate_client_context.py` (`_link_diary_relevant_to`,
`clear_scope_links_batch`).

Two things are load-bearing and easy to undo by accident. A **delete stamps
nothing**, so the per-label count is what catches it — which is why the count
lives in the same entry as the stamp. And the clear queries in
`db_set_*_scope` had to become `SET x.updatedAt = datetime() WITH x MATCH …`:
a `SET` after a `MATCH` that rebinds the variable does not apply to the rows the
delete matched, and `WITH` is what carries it.

`backup.py` lost `import hashlib`, `_digest_line()` and `_vault_digest()`;
`_vault_stamp()` replaced them. `fingerprint_matches()`,
`_collection_point_count()`, `latest_fingerprint()` and the scheduler-only skip
are unchanged, and so are their reasons: an unreadable store still yields an
empty fingerprint that never matches, so a failure takes the savepoint instead
of silently skipping it.

Tests: `test_backup_compression.py`'s `FingerprintTests` now pins the stamp shape
(count plus newest write per label, a null stamp as `""`, a label with no records
absent from both sides), a later write, a delete through the count, a same-count
edge move through the node stamps, an unreadable fingerprint, the failed
savepoint baseline and a pre-feature savepoint reading as unknown.
`_FakeSession` dispatches on the aggregate's `labels(n)` SELECT list rather than
on `"MATCH (n)"`, because the edge query is `MATCH ()-[r]->()` and a looser match
would answer it with node rows and pass on a wrong query.

Full 19-suite battery green; documented in `AGENTS.md` → Backup & Restore,
including the consequence for future write paths: a write that does not stamp
`updatedAt` is invisible to the check, so the vault can go unsaved with nothing in
the log to say so.

---

# Project/Context Sharing — Execution Plan

Status: **planning only**. No code changes yet. Created: 2026-10-18.

## 0. Goal and scope

Allow a project/context **owner** to share a single client or context (project)
with other signed-in users, via a unique link, with two permission levels:

- **read** — recipient can view the records scoped to that client/context.
- **read-write** — recipient can also create, edit, delete and re-scope records
  under that client/context.

The owner manages the set of recipients and their permissions from a settings
surface. Every access and modification performed through a share is written to
an audit log in the database.

Out of scope for v1 (explicit):

- Sharing an entire vault or arbitrary fact sets.
- Transitive sharing (a recipient cannot re-share).
- Anonymous (unauthenticated) recipients — a share link requires sign-in.
- Per-record sharing; sharing is always at client/context granularity.
- Real-time collaboration / concurrent edit conflict resolution (last write wins,
  audited).

## 1. Open decisions (resolve before Phase 1)

- [x] **Where do records created under a shared context live?** Proposed:
      stored with the **owner's `userId`** (shared workspace semantics) with a
      `createdBy` attribute naming the acting user. Alternative: stored under the
      acting user's `userId`, which fragments the shared view. Decision affects
      every read query.
- [x] **Read access to the owner's other clients.** A shared context must not
      leak sibling clients/contexts or unscoped facts. Scope filters are already
      edge-based; confirm no code path returns the owner's full client list to a
      grantee.
- [x] **Expiry of share links.** Proposed default: 30 days, revocable anytime.
      Also allow "never expires" for long-lived links (owner's choice).
- [x] **Maximum recipients per shared target.** Proposed 50; revisit if needed.
- [x] **Audit log storage.** Proposed: a dedicated SQLite database
      `audit.db` next to the session store (stdlib only, like `sessions.py`) so
      it is importable/testable without DB drivers. Alternative: Neo4j nodes.
      SQLite keeps audit writes cheap and queryable.
- [x] **Audit log retention.** Proposed 90 days, configurable
      (`MEM_AUDIT_KEEP_DAYS`).
- [x] **Should a read-only recipient see the owner's name in the UI?** Yes
      (attribution), but never the owner's other data.
- [x] **MCP exposure.** Should an access key (mvk_) be able to act on shared
      targets on behalf of its owner? Proposed: yes, but only targets the key's
      owner has access to (owned or granted). No new credential type.

## 2. Architecture overview

The existing system partitions everything by `userId` (Neo4j `userId` property,
Qdrant payload `userId`, SQLite `psks.user_id`). Sharing introduces a second
axis: **access grants**. A grantee's effective readable set is:

```
owned:    userId = self
shared:   userId = owner  AND  target in granted_targets(self)
```

Every read path that is user-scoped must be widened to this union, and every
write path must check the grant's permission level. The owner's identity stays
the partition key of the stored records; grants are metadata on top.

New concepts:

- `ShareLink` — a revocable, expiring token minted by an owner for one target.
- `ShareGrant` — a (grantee, target, permission) record created when a link is
  accepted (or when an owner directly adds a user).
- `AuditLog` — append-only rows recording who did what, when, on which target.

## 3. Data model

### 3.1 Neo4j

Add to `:User` hub (already exists). New nodes/edges:

- `(:ShareLink {id, tokenHash, ownerUserId, targetType, targetId, permission,
  createdAt, expiresAt, revokedAt, useCount, maxUses})`
- `(:ShareGrant {id, ownerUserId, granteeUserId, targetType, targetId,
  permission, grantedAt, revokedAt, viaLinkId})`
- Edges: `(owner:User)-[:MINTED]->(link:ShareLink)`,
  `(grantee:User)-[:HAS_GRANT]->(grant:ShareGrant)`,
  `(grant)-[:FOR_TARGET]->(client:Client|context:Context)` (polymorphic by label).
- Indexes: `ShareLink(tokenHash)`, `ShareGrant(granteeUserId, targetId)`,
  `ShareGrant(ownerUserId, targetId)`.

`targetType` is `'client'` or `'context'`. A client share implicitly covers its
contexts unless the owner shares a context alone.

### 3.2 SQLite (audit + grants mirror)

New module `audit.py` (stdlib only, like `sessions.py`) operating on
`audit.db` next to the session store.

Tables:

```sql
CREATE TABLE share_links (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  token_prefix TEXT NOT NULL,          -- 10-char display prefix
  owner_user_id TEXT NOT NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('client','context')),
  target_id TEXT NOT NULL,
  permission TEXT NOT NULL CHECK (permission IN ('read','write')),
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  max_uses INTEGER
);

CREATE TABLE share_grants (
  id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL,
  grantee_user_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  permission TEXT NOT NULL CHECK (permission IN ('read','write')),
  granted_at TEXT NOT NULL,
  revoked_at TEXT,
  via_link_id TEXT,
  UNIQUE (owner_user_id, grantee_user_id, target_type, target_id)
);

CREATE TABLE audit_log (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  owner_user_id TEXT,                 -- null for own-vault actions
  target_type TEXT,                   -- 'client'|'context'|'fact'|'diary'|...
  target_id TEXT,
  action TEXT NOT NULL,               -- 'read','create','update','delete','link',...
  permission TEXT,                    -- effective permission used
  shared INTEGER NOT NULL DEFAULT 0,  -- 1 if action was on a shared target
  detail TEXT                         -- JSON, no secrets
);
CREATE INDEX audit_log_ts ON audit_log(ts);
CREATE INDEX audit_log_actor ON audit_log(actor_user_id, ts);
```

Grants are mirrored in both Neo4j (for graph queries/scope) and SQLite (for
fast permission checks and admin UI). Neo4j is authoritative for graph traversal;
SQLite is authoritative for the user-management list and audit.

### 3.3 Qdrant

No schema change. Access filtering happens at the application/Neo4j layer by
record id sets; Qdrant queries are widened with accessible owners. Performance
concern → Phase 3 task to benchmark.

## 4. Phases and tasks

### Phase 1 — Foundations (data model + core helpers)

- [x] P1.1 Create `audit.py` (stdlib only) with `share_links`, `share_grants`,
      `audit_log` tables, migration-on-open, and helpers:
      `mint_share_link`, `resolve_share_link`, `revoke_share_link`,
      `list_share_links`, `grant_access`, `revoke_grant`, `list_grants_for_target`,
      `list_grants_for_user`, `log_access`, `query_audit_log`.
- [x] P1.2 Add Neo4j schema helpers in `client_manager.py`:
      `db_mint_share_link`, `db_resolve_share_link`, `db_revoke_share_link`,
      `db_grant_access`, `db_revoke_grant`, `db_list_grants`, `db_has_access`,
      `db_accessible_client_ids`, `db_accessible_context_ids`,
      `db_accessible_fact_ids` (for a grantee).
- [x] P1.3 Add permission helpers (new `sharing.py` or extend `client_manager.py`):
      `can_read(user, target)`, `can_write(user, target)`,
      `is_owner(user, target)`. Must consult grants + ownership.
- [x] P1.4 Env wiring: `MEM_AUDIT_DB`, `MEM_AUDIT_KEEP_DAYS`,
      `MEM_SHARE_LINK_TTL_DAYS` (default 30), `MEM_SHARE_MAX_RECIPIENTS`
      (default 50), `MEM_SHARING_ENABLED` (default 0). Add to `.env.example`
      and `docker-compose.yml` environment block (per env-wiring rule).
- [x] P1.5 Document in `scoped_ids.py` docstring that shared targets reuse
      owner's ids (no new id derivation).

### Phase 2 — Share link lifecycle (backend)

- [x] P2.1 `POST /api/share/links` (owner only): body `{targetType, targetId,
      permission, expiresInDays?, maxUses?}` → returns `{token (once), prefix,
      url, expiresAt}`. Token is `secrets.token_urlsafe(32)`; store SHA-256 hash
      + 10-char prefix (mirrors PSK pattern).
- [x] P2.2 `GET /api/share/links?targetType=&targetId=` (owner only): list
      active/revoked links for a target with use counts.
- [x] P2.3 `DELETE /api/share/links/{id}` (owner only): revoke; idempotent,
      scoped by owner, returns one answer for not-found/not-yours/already-revoked
      (enumeration-safe, mirrors `revoke_psk`).
- [x] P2.4 Accept endpoint `POST /api/share/accept/{token}`: requires
      authenticated user; resolves link (hash compare, not expired, not revoked,
      under maxUses); creates grant (idempotent upsert of permission);
      increments use_count; logs audit. Returns `{targetType, targetId, owner,
      permission, alreadyHadAccess}`.
- [x] P2.5 Share landing page route `GET /share/{token}` (or
      `/gui/share/{token}`) that prompts sign-in if anonymous, then redirects to
      accept. Must not leak target existence to unauthenticated callers.
- [x] P2.6 Owner direct-add: `POST /api/share/grants` body `{targetType,
      targetId, username, permission}` — creates grant without a link (for
      users already known). Owner only.
- [x] P2.7 `PUT /api/share/grants/{id}` change permission; `DELETE` revoke.
      Owner only, scoped.
- [x] P2.8 `GET /api/share/grants?targetType=&targetId=` list recipients for
      owner management UI.
- [x] P2.9 `GET /api/share/mine` — targets shared with the current user
      (for "Shared with me" view).

### Phase 3 — Access control in read paths

- [x] P3.1 `db_list_clients` / `db_list_contexts`: include shared clients/
      contexts for the current user, flagged `shared: true`, `owner`,
      `permission`. Distinguish owned vs shared in the payload.
- [x] P3.2 Fact search (`db_search_memories`, `db_search_diary`): widen Qdrant
      filter to accessible owners; post-filter by accessible record ids.
      Benchmark; if too slow, add a `sharedOwnerIds` payload index or resolve
      accessible ids first.
- [x] P3.3 `db_get_graph`, `db_get_neighborhood`, `db_focus`: include shared
      records when their client/context is in accessible set. Respect category
      and scope filters.
- [x] P3.4 Diary list / calendar: include shared diary entries under shared
      contexts. Attribute owner.
- [x] P3.5 Memory list (`db_list_memories`): include shared facts.
- [x] P3.6 Client/context items endpoints: allow grantee read for shared
      targets; 404 otherwise (no existence leak).
- [x] P3.7 MCP tools (`mcp_tools.py`): every read tool resolves accessible
      targets through the same helpers; no separate code path.

### Phase 4 — Write paths and permission enforcement

- [x] P4.1 Central permission gate helper `_require_target_access(request,
      target_type, target_id, level='read'|'write')` used by all endpoints and
      MCP tools. Returns owner userId (for storage) or raises 403.
- [x] P4.2 Create fact/diary under a shared target: permission write; store
      with owner's userId (per decision 1); set `createdBy`/`updatedBy`.
- [x] P4.3 Update/delete fact/diary: permission write on the record's target;
      owner can always; grantee only if write grant exists and not revoked.
- [x] P4.4 Scope changes (`db_set_*_scope`) on shared records: write permission
      on both old and new target.
- [x] P4.5 Link/unlink between records: write permission on both endpoints'
      targets (or both owned by same user).
- [x] P4.6 Client/context metadata edits (rename, active toggle, crossClient):
      owner only. Grantees cannot rename shared targets.
- [x] P4.7 Delete client/context: owner only.
- [x] P4.8 MCP write tools: same gates; read-only grants yield a clear error
      message naming the permission.

### Phase 5 — Audit logging

- [x] P5.1 `log_access(actor, owner, target_type, target_id, action, permission,
      shared, detail)` called from every shared read and write. Own-vault actions
      optionally logged (config `MEM_AUDIT_LOG_OWN`, default false) to keep
      volume manageable.
- [x] P5.2 Audit writes are fire-and-forget (async, swallowed on failure, logged
      at WARNING) so they never break the request.
- [x] P5.3 Retention purge: `purge_audit_log(older_than_days)` in the
      scheduled backup loop or a separate daily task.
- [x] P5.4 `GET /api/audit?targetType=&targetId=&actor=&since=&limit=` owner
      only, paginated, for the settings UI.
- [x] P5.5 Ensure no secrets/tokens in `detail` (prefix only, never full token).

### Phase 6 — UI

- [x] P6.1 Projects page: badge shared clients/contexts ("shared by X"); filter
      chips for "Owned / Shared with me".
- [x] P6.2 Share dialog on a client/context (owner only): permission picker,
      expiry, max uses, "Copy link", list of existing links with revoke.
- [x] P6.3 Recipients management panel (owner only): list grants, change
      permission, revoke; direct-add by username.
- [x] P6.4 "Shared with me" section listing targets grouped by owner.
- [x] P6.5 Read-only indicator in record editors (disable save when no write).
- [x] P6.6 Audit log viewer in Settings/Service (owner sees own targets; admin
      sees all).
- [x] P6.7 Share acceptance landing page with sign-in redirect and success/
      error states.
- [x] P6.8 Mobile layout tests for new panels (follow existing
      `test_mobile_layout.py` pattern).

### Phase 7 — MCP and API surface

- [x] P7.1 Document new endpoints in README.
- [x] P7.2 MCP tools: `list_shared_with_me`, `accept_share_link` (if token
      passed), `list_share_grants` (owner), and permission-aware existing tools.
- [x] P7.3 OpenAPI disabled (already) — no schema leak.

### Phase 8 — Tests

- [x] P8.1 New `test_sharing.py` suite covering: link mint/hash/verify,
      expiry, revocation, max uses, enumeration-safe revoke, grant idempotency,
      permission checks (read vs write), cross-user access, audit writes.
- [x] P8.2 Cypher safety tests: new queries added to `test_cypher_safety.py`
      list-parameter lint; alias tests for any new multi-match reads.
- [x] P8.3 Env wiring test auto-covers new vars.
- [x] P8.4 Mobile layout tests for new panels.
- [x] P8.5 Re-injection verification for any guard that is "looks right but
      wrong" (per AGENTS.md lesson).

### Phase 9 — Migration, docs, rollout

- [x] P9.1 Backfill: no data migration needed (grants are new). Ensure old
      vaults unaffected.
- [x] P9.2 Backup/restore: include `audit.db` in savepoint as an additional
      file in the savepoint directory. Update `backup.py`.
- [x] P9.3 Update AGENTS.md with sharing section and new env vars.
- [x] P9.4 Update README with user-facing sharing docs.
- [x] P9.5 Staged rollout: feature flag `MEM_SHARING_ENABLED` (default 0)
      initially, like registration. Start with read-only sharing only, then add
      write in a follow-up.

## 5. Risk register

- **Performance**: read paths widened to union of owners may multiply Qdrant
  queries. Mitigate with accessible-id sets and benchmarking in Phase 3.
- **Data leakage**: a bug in access filter exposes owner's whole vault. Mitigate
  with tests that assert a grantee cannot see non-shared clients/facts.
- **Audit volume**: per-read logging can be large; default own-vault logging off
  and sample shared reads if needed.
- **Permission bypass via MCP**: ensure MCP tools use the same gates, not a
  parallel implementation.
- **Link leakage**: tokens in URLs hit browser history/proxies. Mitigate with
  short TTL, revocation, and never logging full token.

## 6. Suggested implementation order

Phase 1 → Phase 2 → Phase 5 (logging infra) → Phase 3 → Phase 4 → Phase 6 →
Phase 7 → Phase 8 (continuous) → Phase 9.

Estimated size: large (multi-week). Recommend shipping behind `MEM_SHARING_ENABLED`
and starting with read-only sharing only, then adding write in a follow-up.

---

# Backup skip when nothing changed — Task (added 2026-10-26)

Don't run a backup if there was no data change since the last backup.

- [x] Detect "no data change since the last savepoint": fingerprint the vault
      (record counts per store + newest updated_at timestamp in Neo4j, plus
      Qdrant point counts) before each scheduled run and compare against the
      fingerprint recorded in the last *complete* savepoint's `manifest.json`.
- [x] On no change, skip the savepoint (log it at INFO) and keep the existing
      savepoint as the newest — `prune_savepoints()` retention must not treat
      the skipped night as missing.
- [x] `POST /api/backup/run` (manual) ignores the fingerprint — an explicit
      request always takes a savepoint.
- [x] A restore or a failed/aborted backup resets the fingerprint (the stored
      fingerprint must describe the *current* data, not a stale one).
- [x] Tests: fingerprint stability (two polls, no writes → equal), change
      detection (a write bumps it), prune interplay, manual run unaffected.

**Implementation note (2026-10-26):** the fingerprint is a sha256 over the
*content* of every node and relationship (plus both collections' point counts),
not over timestamps. `Fact.updatedAt` is only written by fact updates and
`DiaryEntry` carries no update timestamp at all, so a time-based check would
have missed every diary edit and every edit of an older fact. Edges are hashed
too, because a reclassify rewrites scope links without touching a node. A
fingerprint that cannot be read is empty and never matches, so an unreadable
store takes the savepoint rather than silently skipping it. `run_backup` accepts
the fingerprint the scheduler already took, so the comparison and the snapshot
describe the same moment.
