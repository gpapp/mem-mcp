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

## Skills: improve discovery, then add the ones that are missing

The skills system is the right home for every one of the features above — a
skill is Markdown, costs no runtime, and is addressable by an MCP client that
has never seen this repo. But it currently only covers **getting data in**.

**Current inventory** (`mem-mcp/skills/`): `process-transcription.md` (452
lines), `process-directory.md` (136), `memory-deduplication.md` (87). All three
are ingestion or maintenance. Nothing covers *using* the vault, which is what a
consultant does all day.

### 8. `find_skills` must return descriptions, not bare filenames

`find_skills` (mcp_tools.py:866) is
`[f[:-3] for f in os.listdir(skills_dir) if f.endswith(".md")]`. Discovery
returns three opaque strings, so an agent must load all three to learn that none
of them answers its question — the one tool whose purpose is *not* loading
things forces loading everything.

**Acceptance criteria**
- Two of the three skills already carry YAML front matter (`name`,
  `description`); parse it and return `[{name, description}]`, falling back to
  the filename when a file has none.
- Deterministic only — no model call on a discovery path.
- `memory-deduplication.md` gains the front matter the other two have.

**Effort:** S. It is on the first call every new client makes.

### 9. Every skill must be resource-addressable, including `process-directory`

`register_skills` hand-registers `skill://process-transcription` and
`skill://memory-deduplication`. **`process-directory` has neither a resource nor
a prompt**, yet the `process-transcription` prompt tells the reader to "load
skill 'process-directory' instead" — so the recommended path is the one path a
client cannot address by URI. The documented promise that adding a `.md` file
needs "no server restart" is also true only for `get_skill_workflow`.

**Acceptance criteria**
- Resources registered by iterating `skills/*.md`, not one decorator per skill
  (bind the name with a default argument so the closure is not late-bound).
- A prompt for `process-directory` alongside the existing two.
- A test asserting every `.md` in `skills/` has a matching `skill://` URI — the
  asymmetry is invisible until someone adds the fourth skill.

**Effort:** S.

### 10. `search-and-recall` — how to query this vault, not how to fill it

The highest-value skill in this list, and the cheapest: it is pure Markdown over
behaviour that already exists but is nowhere documented for a client.
`search_facts`'s docstring does not mention that `score` is a ranking number
that must never be thresholded, that `confidence` is the gate, that
`scopeStrength` is a third axis, or that ordering is `(scopeStrength, score,
confidence)`.

**Acceptance criteria** — the skill states, in the order a reader needs them:
- Ask for `client` and `context` whenever you know them; scope **prioritises and
  never excludes**, so a filter cannot cause an empty result.
- `score` ranks, `confidence` gates, `evidence` explains. Never threshold
  `score`.
- A weak-identity name query is deliberate: follow with `get_fact_neighborhood`
  rather than assuming a miss.
- Long records are chunked; a hit returns the whole record, so a fragment is
  never the answer.
- `find_patterns` is for "what recurs", not for "what is true of X".
- When the vault disagrees with the user, say so rather than smoothing it over.

**Effort:** S. Pairs with feature #6.

### 11. `meeting-prep` — the read-side skill for feature #2

Walks a client or project and produces the brief: roster, open actions,
decisions since a date, recent diary headlines, highest-signal linked facts.
Written now as a *manual* workflow over existing tools, so it is useful before
feature #2 lands and becomes the spec for that endpoint.

**Effort:** S to write, and it makes features #1/#3/#4 usable on the day they
ship.

### 12. `client-context-hygiene` — the scope maintenance workflow

The reclassify machinery is subtle and its rules live in `AGENTS.md`, aimed at
developers: a null scope is **permanent** (`scopeCheckedSig`), `scopeManual`
protects a hand-set scope from a bulk reclassify but not from a single-item one,
a context the text never names is dropped in code, an unresolvable name is a
real null rather than a retry. An agent asked to "tidy up my clients" will
otherwise guess.

**Acceptance criteria**
- Review order: list clients → find unclassified records → reclassify → verify,
  with the manual-scope rule stated at the point it matters.
- Explicit that creating a client on a near-miss is the wrong fix (it mints a
  duplicate the classifier will never link to).
- Never instruct a bulk reclassify over a manually scoped item.

**Effort:** S.

### 13. `vault-orientation` — the first-hour skill

What is in here, what the shapes mean (facts vs diary vs live notes, clients vs
contexts, categories are global and carry no owner), how scope is attached, and
the three or four questions a new user should try. Turns an empty-looking vault
into a legible one.

**Effort:** S.

### 14. `weekly-review` — the cadence surface

"What happened across all clients this week, what is still open, who have I not
spoken to." Composes `list_diary_entries`, action items (#1) and roster
staleness (#3); before those exist it is a diary digest.

**Effort:** S once #1/#3 exist.

---

### Suggested sequence

| order | item | why here |
|---|---|---|
| 1 | #8 `find_skills` descriptions | 15 lines, and every client's first call sees it |
| 2 | #10 `search-and-recall` skill | pure Markdown, changes answer quality immediately |
| 3 | #6 time/person search | small, and every later item is easier to verify with it |
| 4 | #9 skill resource loop | fixes `process-directory` and makes "add a file" true |
| 5 | #1 commitments register | the gap that defines the product's usefulness |
| 6 | #2/#11 prep brief | turns #1 into something read daily |
| 7 | #3 stakeholder roster | reuses edges already written, no migration |
| 8 | #4 decision log | shares #1's extraction machinery |
| 9 | #5 engagement timeline | needs #1 and #4 to have something to show |
| 10 | #12/#13/#14 remaining skills | cheap, and better once the data they describe exists |
| 11 | #7 deliverables | lowest urgency, no dependency |

Items 1, 2 and 4 together are roughly a day and they improve every session after
them, which is why they come before the headline feature rather than with it.

Explicitly out of scope for now: calendaring integration (the vault should not
become a calendar), per-user task assignment across accounts (that is sharing
territory, `MEM_SHARING_ENABLED` is 0 by default), and any automatic write from
an extracted date into an external system.
