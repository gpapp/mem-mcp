# Open Tasks

Only open work lives here. Completed tasks move to `task_history.md` (see
`AGENTS.md` → Task Tracking).

---

## Consultant-facing work, in execution order

Written 2026-11-08 against `0d372d1`. The queue is ordered by leverage, not by
size, and each task names the surface that already exists and would host it so
nothing starts from a blank file.

The motivating observation: the vault models **what is known** (facts,
categories, clients, projects, people) and **what happened** (diary, live
notes). It does not model **what is owed and by when**. A consultant's dominant
failure mode is a dropped thread, and nothing in the product can currently
answer "what did I promise Deutsche Bank, and is it late?"

`## Actions` and `## Open Questions` already exist as diary Markdown headings
(`skills/process-transcription.md`, `mcp_skills.py:43`) and Live Notes already
tells the model to keep "owners, numbers, dates, risks and open questions"
(`live_notes.py:68`). Both are prose. Nothing extracts, stores, reconciles or
surfaces them — which is why task 5 exists.

| # | task | size |
|---|---|---|
| 5 | Open commitments register (`:ActionItem`) | L |
| 6 | Meeting-prep brief + skill | M |
| 7 | Stakeholder roster with staleness | M |
| 8 | Decision log per client | M |
| 9 | Engagement timeline | M |
| 10 | Remaining skills (`hygiene`, `orientation`, `weekly-review`) | S |
| 11 | Deliverables register | M |

Tasks 1, 2, 3 and 4 are shipped (see `task_history.md`). They came first because the
skill surface is what a client meets before it meets the vault: a discovery call
that answers with three opaque filenames, and no skill that says how to read the
vault, makes every later session worse.

---

## 5. Open commitments register (`:ActionItem`)

Every meeting produces commitments; they die in a Markdown bullet. This is the
feature that decides whether the product is useful to a consultant.

- [ ] `:ActionItem {id, userId, text, owner, dueAt, status, sourceLabel,
      sourceId, createdAt, updatedAt}` in Neo4j, scoped by the same
      `FOR_CLIENT` / `IN_CONTEXT` edges facts use, plus `RAISED_IN` → the diary
      entry or note.
- [ ] Extraction runs on diary save/update and on live-note commit.
      **Deterministic first**: parse `## Actions` / `## Open Questions` bullets
      for an owner and a date.
- [ ] LLM fallback only for free prose, through
      `get_extraction_response(..., is_good=...)` on `MEM_EXTRACT_MODEL` — the
      same route the keyword and people extractors already use.
- [ ] Re-extraction reconciles rather than appends: match on normalised text
      within the same client, close an item whose bullet has gone, and never
      reopen a `done` item.
- [ ] `status ∈ {open, done, dropped}`.
- [ ] A due date the source text never named is stored `null`, never inferred
      (the `context_named_in_text` rule).
- [ ] UI: "Open items" in the existing client/project panel
      (`client_manager._scope_items`, line 701).
- [ ] UI: cross-client **My actions** list grouped overdue / this week / later.
- [ ] MCP: `list_action_items`, `save_action_item`, `update_action_item`.
- [ ] Tests: extraction is pure and unit-testable in `matching_utils.py` (no DB);
      reconciliation is driven against fakes; the call sites are pinned — a
      helper test is not a test of its call site.

**Decisions already made:** an action item is its own node, not a `:Fact`. A fact
is a statement about the world; an action is an obligation with a state machine,
and folding it into `:Fact` would put `status` on a node type that has no concept
of one. Deterministic parsing before the LLM because a bullet reading
`- [ ] Tim: send the architecture doc by Fri` needs no model, and every model call
spent on it is latency plus a hallucination risk on the one field that must be
exact.

## 6. Meeting-prep brief, and its skill

One click (and one MCP tool) that assembles a one-page brief for a client or
project: roster with roles, open actions, decisions since a date, recent diary
headlines, and the highest-signal linked facts — then a single LLM pass to write
it up.

- [ ] `GET /api/clients/{id}/brief` and a `client_brief` MCP tool.
- [ ] Composed from existing reads (`db_list_diary`, `_scope_items`, action
      items, `db_list_clients`) — no new storage.
- [ ] Markdown output, cacheable per client until the vault changes
      (`vault_fingerprint` already answers exactly that).
- [ ] Degrades to the raw sections rather than a failed request when the model is
      down, the way every other LLM surface here does.
- [ ] `meeting-prep` skill walks the same steps as a manual workflow over
      existing tools, so it is useful before the endpoint lands and becomes that
      endpoint's spec.

**Decisions already made:** this is composition, not a new pipeline, and it is
the cheapest way to make tasks 5, 7 and 8 pay off — the brief is where a
consultant reads them.

## 7. Stakeholder roster with staleness

People are `:Fact` rows in category `People` with free-text bodies, and the
people extractor already writes `MENTIONS` edges from diary entries to them. The
relationship graph is therefore already built and entirely unread.

- [ ] Per-client roster: name, role (parsed from the fact body), last mentioned,
      mention count, open actions they own.
- [ ] "Last seen" derived from the newest `MENTIONS` edge — read-only, no new
      writes, no migration.
- [ ] Flag a named stakeholder with no mention in `MEM_ROSTER_STALE_DAYS` (60).
- [ ] Rendered as a section of the client panel, next to open items.

**Decisions already made:** no new `:Person` node. The People facts and their
edges already carry the data; a parallel node type would need a migration and
would immediately disagree with the facts it mirrors.

## 8. Decision log per client

`## Decisions` is prose today. "Why did we choose X, and who signed off?" is the
question a consultant is asked most often after the fact, and a client that
changes its mind is the hardest thing to reconstruct.

- [ ] Reuse the task 5 extraction machinery with a `:Decision {decidedAt,
      decider, status, supersededBy}` shape.
- [ ] Per-client chronological list, newest first, with the source diary entry
      linked.
- [ ] `supersededBy` is explicit rather than inferred — a decision that quietly
      stops being followed is worse than one that was never logged.

## 9. Engagement timeline

Chronological narrative of one client or project, for "where did this get to?"
and for handing an engagement to a colleague.

- [ ] `GET /api/clients/{id}/timeline`: diary entries with headline, participants
      and decisions; actions closed; facts added — one merged, date-sorted
      stream.
- [ ] Grouped by month, each row linking to its source.
- [ ] Scope read through the edges (`FOR_CLIENT` / `IN_CONTEXT`), never a
      `clientId` property — that key is Qdrant-only and reading it answers `None`
      for every node.

## 10. Remaining skills

Cheap, and better once the data they describe exists.

- [ ] `client-context-hygiene` — review order: list clients → find unclassified
      records → reclassify → verify. States that a null scope is **permanent**
      (`scopeCheckedSig`), that `scopeManual` protects a hand-set scope from a
      bulk reclassify but not from a single-item one, and that creating a client
      on a near-miss is the wrong fix because it mints a duplicate the classifier
      will never link to.
- [ ] `vault-orientation` — the first-hour skill: facts vs diary vs live notes,
      clients vs contexts, categories are global and carry no owner, how scope is
      attached, and the three or four questions a new user should try.
- [ ] `weekly-review` — "what happened across all clients this week, what is
      still open, who have I not spoken to". Composes `list_diary_entries`,
      action items and roster staleness; before those exist it is a diary digest.

## 11. Deliverables register

Consultants ship artifacts. `original_file` metadata on a diary entry is the only
trace of one today.

- [ ] `:Deliverable {name, kind, status, deliveredAt}` linked to client/context
      and to the diary entry it came from.
- [ ] `kind ∈ {assessment, roadmap, deck, report, other}`,
      `status ∈ {draft, in-review, sent, approved}`.
- [ ] Per-client list, and a badge on the diary entry it came from.

---

Explicitly out of scope for now: calendaring integration (the vault should not
become a calendar), per-user task assignment across accounts (that is sharing
territory, `MEM_SHARING_ENABLED` is 0 by default), and any automatic write from
an extracted date into an external system.
