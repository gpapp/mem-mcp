---
name: process-transcription
description: Structured workflow for processing meeting transcriptions into the knowledge graph. Includes interactive human checkpoints for people resolution and fact-person assignment before writing anything to memory.
context: fork
---

## When to Use This Skill

Use this skill when you have:
- Meeting transcription files (typically .txt files with timestamps)
- Need to extract structured information (people, projects, decisions, action items)
- Want to build or update a knowledge graph with the transcription content

---

## Phase 1 — Extract (read-only, no writes)

### 1. File and Metadata

- Extract date/time from filename (pattern: `YYYY-MM-DD HH-MM-SS`)
- Identify meeting title, topic, and context (internal/client/etc.)
- Apply stored corrections: `search_facts("correction")` → fix recurring misspellings of names and terms before proceeding.

### 2. Participant List

Scan for speaker labels and voice profile headers (e.g., `[00:00:20] Speaker A:`).

Voice profiles are **guidance only** — verify speaker identity from content:
- Topic expertise: who would naturally discuss this subject
- Questions asked vs. answers given
- Demonstrated knowledge, perspective, or role
- Facilitator vs. participant behaviour

Build a raw candidate list: `Raw participants: [Tim, Rafael, Kate, ...]`

Mark all speaker-to-name assignments as **tentative** at this stage.

### 3. Entity Extraction

Extract all entities from the full transcription text:

| Category | What to capture | What NOT to put here |
|---|---|---|
| **People** | Name, role, seniority, company/team, domain expertise, interests | What they said in this meeting, decisions they made, actions assigned |
| **Projects** | Name, purpose, current status, tech stack, open questions | Who attended which meeting about it, what was said, meeting outcomes |
| **Technologies** | Tool name, purpose, version, integration context | Which meeting it was discussed in |
| **Decisions** | What was decided, rationale, tentative owner, date | — |
| **Action Items** | Task description, tentative owner, deadline, linked project | — |
| **Challenges/Risks** | Problem description, impact, mitigation strategy | — |
| **Principles** | Methodology or guideline, application context | — |

> **Separation rule:** People and Project facts describe the *entity itself* — what it is, what it does, what it knows. Meeting outcomes, assignments, and discussions belong in Decisions, Action Items, and the Diary — linked back to the people/projects involved.

**Mark all ownership as tentative — do not assume.** Human confirms in Phase 2.

---

## Phase 2 — Human Verification (STOP before writing)

**Do not write anything to memory until this phase is fully complete.**

### 4. People Resolution

For **each** name from step 2, search memory with multiple queries before asking:

```
search_facts("<Full Name>", category="People")
search_facts("<First Name>", category="People")
search_facts("<role> <company>")          ← if known from transcript
```

Use the **`question` tool** to ask for each person's identity. One question per ambiguous name; batch unambiguous names into a single question with multiple options.

Example question shape:
```
header: "Who is 'Tim'?"
question: "Tim appears in the transcript. Who is this person?"
options:
  - label: "Tim Lohman — Lead Engineer, SAP"   [existing]
  - label: "Tim Berners-Lee — CTO, Client X"   [existing]
  - label: "New person — create as new record"
  - label: "Not important — skip"
multiple: false
custom: false
```

Rules:
- **Confident single match** (same name + role/company aligns): use the `question` tool with a Y/N confirmation — "I matched 'Kate' to Kate Müller, PM at Deutsche Bank. Is that correct?"
- **2+ plausible matches**: always ask via `question` tool, never auto-select.
- **No match**: use the `question` tool to propose creating a new record — never create without confirmation.
- **Never create a People record without explicit human confirmation.**
- Batch unambiguous names into one `question` call using `multiple: true`.

### 5. Fact–Person Assignment

After all people are resolved, use the **`question` tool** once to assign all decisions, actions, and project involvements in a single interaction:

```
header: "Fact–Person Assignment"
question: "Who is involved in each of the following? Reply with the letter(s) for each item."
options:
  - label: "A) Rafael Papp"
  - label: "B) Tim Lohman"
  - label: "C) Kate Müller"
  - label: "D) Someone not listed — I'll name them"
  - label: "E) Unassigned / unknown"
multiple: true
```

Present the full list of items in the question body:
```
1. Decision: Proceed with Concur LEC on SAP ISP
2. Action:   Complete schema mapping by end of month
3. Action:   Schedule DB AI workshop for week of Mar 16
4. Project:  Deutsche Bank AI Adoption (involvement — multi ok)
```

- Accept multiple letters per item (e.g. `A,C`).
- If the human answers `D`, ask for the name before proceeding.
- If `E`, store the fact unlinked and note it as unassigned.
- **Do not proceed to Phase 3 until the human has replied to this message.**

---

## Phase 3 — Store (after human confirmation only)

Execute all writes in one batched response — do not pause between individual calls.

### 6. People — update or create

**Existing person** → `update_fact`: append only genuinely new *stable* information (new role, new team, new area of expertise). Do not add event details — those belong in the Diary and linked facts.

**New person** → `add_fact`:
```
Title: Tim Lohman
Category: People
Text:
  **Role:** Lead Engineer
  **Company/Team:** SAP
  **Domain:** [area of expertise or responsibility]
  **Interests/Focus:** [recurring topics, known priorities]
  **Aliases:** Tim Lohman (100%), Tim Loman (90%), Tim (70%)

metadata: {"tags": ["person"], "aliases": {"Tim Lohman": 1.0, "Tim Loman": 0.9, "Tim": 0.7}}
```

**Do not include:** meeting dates, what they said, decisions they made, tasks assigned to them. Those go in Decision/Action Item facts and the Diary, linked to the person.

Title = name only. Role and company go in the description body.

### 7. Facts, Decisions, Action Items

Store all other extracted entities with rich Markdown context. Use confirmed ownership from Phase 2.

**Tools:**
- `add_fact(title, text, category)` — new facts
- `update_fact(memoryId, text, category)` — update existing (new info only)
- `link_facts(sourceFactId, targetFactId, relationshipType)` — build the graph

**Reprocessing guard:** before creating any fact, run `search_facts` on its title. If it already exists, update instead of creating a duplicate.

**Content separation rules by category:**

*Project facts* describe the project itself — not meeting outcomes:
```
Title: Deutsche Bank AI Adoption
Category: Project
Text:
  **Purpose:** [what the project is for]
  **Status:** [current state]
  **Tech stack:** [technologies used]
  **Open questions:** [unresolved design/direction questions]
```
Do not add: who attended a meeting about it, what was decided in a meeting. Link the project to Decision and Action Item facts instead.

*Decision facts* describe a specific decision with full event context:
```
Title: Decision: Proceed with Concur LEC on SAP ISP — YYYY-MM-DD
Category: Decision
Text:
  **Decided:** YYYY-MM-DD  **Meeting:** [title]
  **Owner:** [confirmed person]
  **Decision:** [what was decided]
  **Rationale:** [why]
  **Impact:** [affected projects/systems]
```

*Action Item facts* carry the task with all context needed to act on it:
```
Title: Action: Complete schema mapping by end of month — YYYY-MM-DD
Category: Action
Text:
  **Assigned to:** [confirmed person]
  **Due:** [date]
  **From meeting:** [title] — YYYY-MM-DD
  **Task:** [full description]
  **Linked project:** [project name]
```

**Link immediately after creating each fact:**

| Relationship | Type |
|---|---|
| Person → Project | `WORKS_ON` |
| Person → Action Item | `ASSIGNED_TO` |
| Person → Decision | `DECIDED` |
| Decision → Project | `DECIDED_FOR` |
| Action Item → Project | `PART_OF` |
| Person → Client/Org | `WORKS_FOR` |
| Meeting Summary → Person | `ATTENDED_BY` |
| Meeting Summary → Decision | `CONTAINS` |
| Meeting Summary → Action Item | `CONTAINS` |
| Diary Entry → Meeting Summary | `REFERENCES` |
| Diary Entry → Person | `MENTIONS` |
| Diary Entry → Project | `MENTIONS` |

### 8. Diary Logging

**Before writing, search first:**
```
diary_search_entries("<meeting topic> <date>")
```
- **New topic:** write a fresh entry.
- **Existing entry, new info:** write a new entry labelled `[Updated] <topic>`.
- **Unchanged:** skip entirely.

Each entry covers one meeting. Append-only — never overwrite.

Content focus: **what the user did** — outcomes, decisions made, tasks assigned to the user. Not a processing log.

**Uniform diary entry format** (always use this exact structure):

```markdown
## [Meeting Title] — YYYY-MM-DD

**Participants:** [Name (Role), Name (Role), ...]
**Context:** [one-line summary of what this meeting was about]

### Decisions
- [Decision made, with brief rationale]

### Actions
- [ ] [Task description] — Owner: [Name] — Due: [date or "TBD"]

### Notes
[Any important context, open questions, or next steps not captured above]
```

**After saving the diary entry, link it to every fact it references:**

```
link_facts(diary_entry_id, meeting_summary_id, "REFERENCES")
link_facts(diary_entry_id, person_id, "MENTIONS")      ← for each participant
link_facts(diary_entry_id, project_id, "MENTIONS")     ← for each project discussed
link_facts(diary_entry_id, decision_id, "RECORDS")     ← for each decision
link_facts(diary_entry_id, action_id, "RECORDS")       ← for each action item
```

These links make the diary navigable from any fact and vice versa.

### 9. Meeting Summary Fact

Store one summary fact per meeting:

```
Title: Meeting Summary: [Topic] — YYYY-MM-DD
Category: Event
Text:
  # [Topic]
  **Date:** YYYY-MM-DD  **Participants:** [names]

  ## Decisions
  - ...

  ## Action Items
  - [ ] Task — Owner: Name — Due: date

  ## Links
  - [Fact title] (created/updated)
```

Link the summary fact to each participant: `summary → person: ATTENDED_BY`.

---

## Format Rules for Human Questions

- **Always use the `question` tool** for all Phase 2 interactions — never rely on free-text replies.
- **Lettered options** (A, B, C…) — never ask open-ended questions when choices are known; use `custom: false` to restrict to the provided options, `custom: true` only when "other" input is genuinely needed (e.g. spelling of an unknown name).
- **`multiple: true`** for fact–person assignment (one person can be involved in many items); **`multiple: false`** for single-identity questions.
- **Batch questions** — one `question` call per logical group; avoid calling `question` separately for each individual item.
- **One confirm per person** — don't ask again for each additional mention of the same name.
- **Show context** — include role and company in every option label so the human can answer in one glance.
- After the human responds, **confirm your interpretation in one line** before writing: "Got it — Tim → Tim Lohman (existing), Kate → new record."

## Key Rules (summary)

1. **Phase 1 is read-only** — no memory writes at all.
2. **Phase 2 is a hard stop** — wait for human replies before any writes.
3. **Search before creating** — always check memory first to prevent duplicates.
4. **Title = name only** — role/company/context go in the description body.
5. **Batch writes** — after Phase 2, fire all `add_fact`, `update_fact`, and `link_facts` calls in a single response without pausing.
6. **Never create a People record without human confirmation.**
7. **Facts describe entities, not events** — People and Project facts contain stable identity information (role, expertise, purpose, status). Meeting outcomes, discussions, and assignments go in Decision/Action Item facts and the Diary.
8. **Diary entries are uniform** — always use the standard format: heading, participants, context, decisions, actions, notes.
9. **Diary entries are linked** — every diary entry must be linked to the meeting summary, all mentioned people, projects, decisions, and action items.
