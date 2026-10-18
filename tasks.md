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

- [ ] **Where do records created under a shared context live?** Proposed:
      stored with the **owner's `userId`** (shared workspace semantics) with a
      `createdBy` attribute naming the acting user. Alternative: stored under the
      acting user's `userId`, which fragments the shared view. Decision affects
      every read query.
- [ ] **Read access to the owner's other clients.** A shared context must not
      leak sibling clients/contexts or unscoped facts. Scope filters are already
      edge-based; confirm no code path returns the owner's full client list to a
      grantee.
- [ ] **Expiry of share links.** Proposed default: 30 days, revocable anytime.
      Also allow "never expires" for long-lived links (owner's choice).
- [ ] **Maximum recipients per shared target.** Proposed 50; revisit if needed.
- [ ] **Audit log storage.** Proposed: a dedicated SQLite database
      `audit.db` next to the session store (stdlib only, like `sessions.py`) so
      it is importable/testable without DB drivers. Alternative: Neo4j nodes.
      SQLite keeps audit writes cheap and queryable.
- [ ] **Audit log retention.** Proposed 90 days, configurable
      (`MEM_AUDIT_KEEP_DAYS`).
- [ ] **Should a read-only recipient see the owner's name in the UI?** Yes
      (attribution), but never the owner's other data.
- [ ] **MCP exposure.** Should an access key (mvk_) be able to act on shared
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

- [ ] P1.1 Create `audit.py` (stdlib only) with `share_links`, `share_grants`,
      `audit_log` tables, migration-on-open, and helpers:
      `mint_share_link`, `resolve_share_link`, `revoke_share_link`,
      `list_share_links`, `grant_access`, `revoke_grant`, `list_grants_for_target`,
      `list_grants_for_user`, `log_access`, `query_audit_log`.
- [ ] P1.2 Add Neo4j schema helpers in `client_manager.py`:
      `db_mint_share_link`, `db_resolve_share_link`, `db_revoke_share_link`,
      `db_grant_access`, `db_revoke_grant`, `db_list_grants`, `db_has_access`,
      `db_accessible_client_ids`, `db_accessible_context_ids`,
      `db_accessible_fact_ids` (for a grantee).
- [ ] P1.3 Add permission helpers (new `sharing.py` or extend `client_manager.py`):
      `can_read(user, target)`, `can_write(user, target)`,
      `is_owner(user, target)`. Must consult grants + ownership.
- [ ] P1.4 Env wiring: `MEM_AUDIT_DB`, `MEM_AUDIT_KEEP_DAYS`,
      `MEM_SHARE_LINK_TTL_DAYS` (default 30), `MEM_SHARE_MAX_RECIPIENTS`
      (default 50), `MEM_SHARING_ENABLED` (default 0). Add to `.env.example`
      and `docker-compose.yml` environment block (per env-wiring rule).
- [ ] P1.5 Document in `scoped_ids.py` docstring that shared targets reuse
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

- [ ] P8.1 New `test_sharing.py` suite covering: link mint/hash/verify,
      expiry, revocation, max uses, enumeration-safe revoke, grant idempotency,
      permission checks (read vs write), cross-user access, audit writes.
- [ ] P8.2 Cypher safety tests: new queries added to `test_cypher_safety.py`
      list-parameter lint; alias tests for any new multi-match reads.
- [ ] P8.3 Env wiring test auto-covers new vars.
- [ ] P8.4 Mobile layout tests for new panels.
- [ ] P8.5 Re-injection verification for any guard that is "looks right but
      wrong" (per AGENTS.md lesson).

### Phase 9 — Migration, docs, rollout

- [ ] P9.1 Backfill: no data migration needed (grants are new). Ensure old
      vaults unaffected.
- [x] P9.2 Backup/restore: include `audit.db` in savepoint as an additional
      file in the savepoint directory. Update `backup.py`.
- [x] P9.3 Update AGENTS.md with sharing section and new env vars.
- [ ] P9.4 Update README with user-facing sharing docs.
- [ ] P9.5 Staged rollout: feature flag `MEM_SHARING_ENABLED` (default 0)
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
