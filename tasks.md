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
| 8 | Decision log per client | M |
| 9 | Engagement timeline | M |
| 10 | Remaining skills (`hygiene`, `orientation`, `weekly-review`) | S |
| 11 | Deliverables register | M |

Tasks 1, 2, 3, 4, 5, 6 and 7 are shipped (see `task_history.md`). They came first because the
skill surface is what a client meets before it meets the vault: a discovery call
that answers with three opaque filenames, and no skill that says how to read the
vault, makes every later session worse.

---

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
