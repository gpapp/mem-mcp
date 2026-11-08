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
| 1 | `find_skills` returns descriptions | S |
| 2 | `search-and-recall` skill | S |
| 3 | Time- and person-bounded search | S |
| 4 | Every skill resource-addressable | S |
| 5 | Open commitments register (`:ActionItem`) | L |
| 6 | Meeting-prep brief + skill | M |
| 7 | Stakeholder roster with staleness | M |
| 8 | Decision log per client | M |
| 9 | Engagement timeline | M |
| 10 | Remaining skills (`hygiene`, `orientation`, `weekly-review`) | S |
| 11 | Deliverables register | M |

Tasks 1-4 together are roughly a day, and they improve every session after them
— including the sessions spent building task 5 — which is why they come first.

---

## 1. `find_skills` returns descriptions, not bare filenames

`find_skills` (`mcp_tools.py:866`) is
`[f[:-3] for f in os.listdir(skills_dir) if f.endswith(".md")]`. Discovery
returns three opaque strings, so an agent must load all three to learn that none
answers its question — the one tool whose purpose is *not* loading things forces
loading everything.

- [ ] Parse the YAML front matter (`name`, `description`) two of the three skills
      already carry, and return `[{name, description}]`.
- [ ] Fall back to the filename when a file has no front matter.
- [ ] Deterministic only — no model call on a discovery path.
- [ ] `memory-deduplication.md` gains the front matter the other two have.
- [ ] Test: every `.md` in `skills/` yields a non-empty description.

## 2. `search-and-recall` skill

The highest-value skill in this list and the cheapest: pure Markdown over
behaviour that already exists but is nowhere documented for a client.
`search_facts`'s docstring does not mention that `score` is a ranking number that
must never be thresholded, that `confidence` is the gate, that `scopeStrength` is
a third axis, or that ordering is `(scopeStrength, score, confidence)`.

The skill must state, in the order a reader needs them:

- [ ] Ask for `client` and `context` whenever they are known — scope
      **prioritises and never excludes**, so a filter cannot cause an empty
      result.
- [ ] `score` ranks, `confidence` gates, `evidence` explains. Never threshold
      `score`.
- [ ] A weak-identity name query is deliberate: follow it with
      `get_fact_neighborhood` rather than assuming a miss.
- [ ] Long records are chunked, and a hit returns the whole record, so a
      fragment is never the answer.
- [ ] `find_patterns` is for "what recurs", not for "what is true of X".
- [ ] When the vault disagrees with the user, say so rather than smoothing it
      over.
- [ ] Registered as a `skill://` resource (see task 4).

## 3. Time- and person-bounded search

"What did Thomas say about the PPC migration last quarter" is a request the index
cannot express today.

- [ ] `search_facts` and `diary_search_entries` accept `after` / `before` and
      `person`.
- [ ] Date filtering uses the record's own timestamp, applied after the vector
      query and before the result cap, so a narrow window widens `fetch_limit`
      rather than returning nothing.
- [ ] `person` resolves through the existing `people_match_allowed` gate — a name
      filter must not reintroduce the confident-stranger bug that gate exists to
      prevent.
- [ ] An explicit `after`/`before` wins over time phrasing in the query, and the
      rewriter must not invent a date range.
- [ ] Tests: the window widening is driven against fakes; the rewriter's
      no-invented-range rule is pinned at its call site.

## 4. Every skill must be resource-addressable, including `process-directory`

`register_skills` hand-registers `skill://process-transcription` and
`skill://memory-deduplication`. **`process-directory` has neither a resource nor
a prompt**, yet the `process-transcription` prompt tells the reader to "load
skill 'process-directory' instead" — so the recommended path is the one path a
client cannot address by URI. The documented promise that adding a `.md` file
needs "no server restart" is also true only for `get_skill_workflow`.

- [ ] Resources registered by iterating `skills/*.md`, not one decorator per
      skill (bind the name with a default argument so the closure is not
      late-bound).
- [ ] A prompt for `process-directory` alongside the existing two.
- [ ] Test asserting every `.md` in `skills/` has a matching `skill://` URI — the
      asymmetry is invisible until someone adds the fourth skill.

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
