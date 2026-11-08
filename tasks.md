# Open Tasks

Only open work lives here. Completed tasks move to `task_history.md` (see
`AGENTS.md` → Task Tracking).

---

## Feature plan: what a technology consultant actually needs

Written 2026-11-08 against `1f9c375`. Ordered by leverage, not by size. Each
item names the surface that already exists and would host it, so nothing here
starts from a blank file.

The motivating observation: the vault models **what is known** (facts,
categories, clients, projects, people) and **what happened** (diary, live
notes). It does not model **what is owed and by when**. A consultant's dominant
failure mode is a dropped thread, and nothing in the product can currently
answer "what did I promise Deutsche Bank, and is it late?"

`## Actions` and `## Open Questions` already exist as diary Markdown headings
(`skills/process-transcription.md`, `mcp_skills.py:43`) and Live Notes already
tells the model to keep "owners, numbers, dates, risks and open questions"
(`live_notes.py:68`). Both are prose. Nothing extracts, stores, reconciles or
surfaces them — which is why the first task below is the headline.

---

### 1. Open commitments register (`:ActionItem`) — the headline gap

Every meeting produces commitments; they die in a Markdown bullet.

**Acceptance criteria**
- `:ActionItem {id, userId, text, owner, dueAt, status, sourceLabel, sourceId,
  createdAt, updatedAt}` in Neo4j, scoped by the same `FOR_CLIENT` /
  `IN_CONTEXT` edges facts use, plus `RAISED_IN` → the diary entry or note.
- Extraction runs on diary save/update and on live-note commit. **Deterministic
  first**: parse `## Actions` / `## Open Questions` bullets for an owner and a
  date; only fall back to the LLM for free prose, through
  `get_extraction_response(..., is_good=...)` on `MEM_EXTRACT_MODEL` — the same
  route the keyword and people extractors already use.
- Re-extraction reconciles rather than appends: match on normalised text within
  the same client, close an item whose bullet has gone, and never reopen a
  `done` item.
- `status ∈ {open, done, dropped}`. A due date the source text never named is
  stored `null`, never inferred (the `context_named_in_text` rule).
- UI: "Open items" in the existing client/project panel (`client_manager._scope_items`,
  line 701) and a cross-client **My actions** list grouped overdue / this week /
  later.
- MCP: `list_action_items`, `save_action_item`, `update_action_item`.
- Tests: extraction is pure and unit-testable in `matching_utils.py` (no DB), the
  reconciliation is driven against fakes, and the call site is pinned — a helper
  test is not a test of its call site.

**Decisions already made:** an action item is its own node, not a `:Fact`. A
fact is a statement about the world; an action is an obligation with a state
machine, and folding it into `:Fact` would put `status` on a node type that has
no concept of one. Deterministic parsing before the LLM because a bullet that
reads `- [ ] Tim: send the architecture doc by Fri` needs no model, and every
model call spent on it is latency plus a hallucination risk on the one field
that must be exact (the date).

**Effort:** L.

---

### 2. Meeting-prep brief

One click (and one MCP tool) that assembles a one-page brief for a client or
project: roster with roles, open actions, decisions since a date, recent diary
headlines, and the highest-signal linked facts — then a single LLM pass to write
it up.

**Acceptance criteria**
- `GET /api/clients/{id}/brief` and a `client_brief` MCP tool.
- Composed from existing reads (`db_list_diary`, `_scope_items`, action items,
  `db_list_clients`) — no new storage.
- Output is Markdown, cacheable per client until the vault changes
  (`vault_fingerprint` already exists for exactly this shape of question).
- Degrades to the raw sections rather than a failed request when the model is
  down, the way every other LLM surface here does.

**Decisions already made:** this is composition, not a new pipeline. It is also
the cheapest way to make items 1, 3 and 7 pay off — the brief is where a
consultant reads them.

**Effort:** S once #1 exists; M before it.

---

### 3. Stakeholder roster with staleness

People are `:Fact` rows in category `People` with free-text bodies, and the
people extractor already writes `MENTIONS` edges from diary entries to them. The
relationship graph is therefore already built and entirely unread.

**Acceptance criteria**
- Per-client roster: name, role (parsed from the fact body), last mentioned,
  mention count, open actions they own.
- "Last seen" derived from the newest `MENTIONS` edge — read-only derivation, no
  new writes, no migration.
- Flag a named stakeholder with no mention in `MEM_ROSTER_STALE_DAYS` (60).
- Rendered as a section of the client panel, next to open items.

**Decisions already made:** no new `:Person` node. The People facts and their
edges already carry the data; a parallel node type would need a migration and
would immediately disagree with the facts it mirrors.

**Effort:** M.

---

### 4. Decision log per client

`## Decisions` is prose today. "Why did we choose X, and who signed off?" is the
question a consultant gets asked most often after the fact, and a client that
changes its mind is the hardest thing to reconstruct.

**Acceptance criteria**
- Reuse the `:ActionItem` extraction machinery with a `:Decision {decidedAt,
  decider, status, supersededBy}` shape.
- Per-client chronological list, newest first, with the source diary entry
  linked.
- `supersededBy` is explicit rather than inferred — a decision that quietly
  stops being followed is worse than one that was never logged.

**Effort:** M, almost entirely shared with #1.

---

### 5. Engagement timeline

Chronological narrative of one client or project, for "where did this get to?"
and for handing an engagement to a colleague.

**Acceptance criteria**
- `GET /api/clients/{id}/timeline`: diary entries with headline, participants and
  decisions; actions closed; facts added — one merged, date-sorted stream.
- Grouped by month, each row linking to its source.
- Scope read through the edges (`FOR_CLIENT` / `IN_CONTEXT`), never a
  `clientId` property — that key is Qdrant-only and reading it answers `None`
  for every node.

**Effort:** M.

---

### 6. Time-bounded and person-bounded search

"What did Thomas say about the PPC migration last quarter" is a request the
index cannot express today.

**Acceptance criteria**
- `search_facts` and `diary_search_entries` accept `after` / `before` and
  `person`.
- Date filtering on the record's own timestamp, applied after the vector query
  and before the result cap, so a narrow window widens `fetch_limit` rather than
  returning nothing.
- `person` resolves through the existing `people_match_allowed` gate — a name
  filter must not reintroduce the confident-stranger bug that gate exists to
  prevent.
- Time content in the query is no longer treated as text to embed: an explicit
  `after`/`before` wins, and the rewriter must not invent a date range.

**Effort:** S. Highest value per line of code in this list.

---

### 7. Deliverables register

Consultants ship artifacts. `original_file` metadata on a diary entry is the
only trace of one today.

**Acceptance criteria**
- `:Deliverable {name, kind, status, deliveredAt}` linked to client/context and
  to the diary entry it came from.
- `kind ∈ {assessment, roadmap, deck, report, other}`,
  `status ∈ {draft, in-review, sent, approved}`.
- Per-client list, and a badge on the diary entry it came from.

**Effort:** M.

---

### Suggested sequence

| order | item | why here |
|---|---|---|
| 1 | #6 time/person search | small, and every later item is easier to verify with it |
| 2 | #1 commitments register | the gap that defines the product's usefulness |
| 3 | #2 meeting-prep brief | turns #1 into something read daily |
| 4 | #3 stakeholder roster | reuses edges already written, no migration |
| 5 | #4 decision log | shares #1's extraction machinery |
| 6 | #5 engagement timeline | needs #1 and #4 to have something to show |
| 7 | #7 deliverables | lowest urgency, no dependency |

Explicitly out of scope for now: calendaring integration (the vault should not
become a calendar), per-user task assignment across accounts (that is sharing
territory, `MEM_SHARING_ENABLED` is 0 by default), and any automatic write from
an extracted date into an external system.
