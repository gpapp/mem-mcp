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
| 11 | Deliverables register | M |

Tasks 1, 2, 3, 4, 5, 6, 7, 8, 9 and 10 are shipped (see `task_history.md`). They came first because the skill surface is what a client meets before it meets the vault: a discovery call
that answers with three opaque filenames, and no skill that says how to read the
vault, makes every later session worse.

---

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
