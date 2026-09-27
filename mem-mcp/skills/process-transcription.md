---
name: process-transcription
description: Structured workflow for processing meeting transcriptions into the knowledge graph. Covers timestamp resolution keyed on meeting identity (reprocessing overwrites by design), transcript format recognition, smalltalk suppression with full retention of professional content, digest-based delegation for long transcripts, and human checkpoints before any write.
---

## When to Use This Skill

Use this skill when you have:
- Meeting transcription files (typically `.txt` files with timestamps)
- Need to extract structured information (people, projects, decisions, action items)
- Want to build or update a knowledge graph with the transcription content
- Need to store extracted information in memory systems for future reference
- Reprocessing an existing meeting (same source, better transcript) — overwrite the existing diary entry at its slot

---

Consider the original role defined in AGENTS.md to ensure the summarization is relevant to the role.

## Delegation Contract (token discipline)

Raw transcripts must never accumulate in the main context.

1. **Phase 1 runs in a dedicated subagent** (task tool, `subagent_type="general"`, one per file). It reads the transcript locally and returns only a **digest** — the raw text dies with the job.
2. **Long transcripts fan out inside that subagent** — extraction jobs per window of ~8–12k characters, run **up to 3–4 concurrently**, each returning only entity candidates; the subagent merges them.
3. **The summarization subagent receives the digest, never the transcript.**
4. **One consolidated `question` call per file** for all human verification — never one call per name. When driven by `process-directory`, its batch gate supersedes this rule (one call for the whole directory).
5. Digest schema (fixed):

```
file, slot, slot_source, title, client, context,
participants[]  (name, role, company — tentative),
decisions[]     (what, rationale, tentative owner),
actions[]       (task, tentative owner, deadline, project),
open_questions[],
projects[], technologies[], challenges[], principles[],
notes[], keywords[],
coverage { segments_processed: "N/N", turns_total, turns_professional }
```

Smalltalk does not appear in the digest at all.

---

## Phase 1 — Extract (read-only, no writes)

### 1. Metadata & Timestamp Resolution

Resolve the meeting timestamp with this ladder (first hit wins, record the source):

1. **Filename**: `YYYY-MM-DD[ _-]hh-mm[-ss]` (e.g. `2026-05-15 10-00-00 Standup.txt`)
2. **Date supplied for the batch/run** by the user (e.g. "these are from 2026-05-15")
3. **Source media mtime** — the `.mp4`/`.mp3` the transcript came from, if available
4. **Ask** (batch with all other questions) — never guess

> ⚠️ The asr-mcp client header line `Date:` is the **transcription run time, not the meeting time**. Using it as the meeting timestamp means reprocessing the same meeting later lands on a *different* slot and silently creates a duplicate entry. Only use it if the user explicitly accepts it as a processing date, and say so.

**Slot invariant:** the same meeting must always resolve to the same 15-minute slot. Round to the nearest 15 minutes (:00, :15, :30, :45) — meetings are always longer than 15 minutes, so one meeting occupies one slot and overwrite at that slot *is* the reprocessing mechanism. Never change a meeting's timestamp after its entry exists.

Record: `Slot: YYYY-MM-DDTHH:MM:00 (source: filename | batch | mtime | asked)`

Other metadata:
- Identify meeting name, topic, and context (internal/client/etc.)
- Capture the **original file identity** — pass through all phases for diary metadata and the local save filename
- **Cross-machine rule:** the memory server never opens local files. `metadata.original_file` must be a **stable id, not an absolute path**: `<basename> (<bytes> bytes)` — or a content hash if the file may move between machines.
- Apply stored corrections: `search_facts("correction")` → fix recurring misspellings of names and terms before proceeding.

### 2. Transcript Format Recognition

Recognize the input format; do not assume a single layout.

**asr-mcp client export** (most common):
```
Audio: <filename>
Date: <transcription run time — NOT the meeting time>
Speakers: N
Duration: NNN.Ns

[Speaker 1] 12.3s - 45.6s: text continues
    on indented continuation lines
```
Speakers may be known names (matched to voiceprints) or `Speaker N` (1-indexed diarization labels).

**asr-mcp GUI export:**
```
=== <file> ===
[Speaker 1] 00:12 - 00:45
text on the next line, blank line after
```

**Other / unknown layouts:** parse whatever speaker + timestamp convention is present; if none exists, extract without speaker attribution and note it in the digest.

Voice profiles are **guidance only** — verify speaker identity from content:
- Topic expertise: who would naturally discuss this subject
- Questions asked vs. answers given
- Demonstrated knowledge, perspective, or role
- Facilitator vs. participant behaviour

Build a raw candidate list: `Raw participants: [Tim, Rafael, Kate, ...]`

Mark all speaker-to-name assignments as **tentative** at this stage.

### 3. Client Detection

Infer which **client** this meeting belongs to from participants' companies, meeting title, and topic:

```
list_clients()
```

- If a participant's company matches a known client → that client is the leading candidate.
- If the meeting is internal (no external participants, no client topic) → client is **none**.
- If ambiguous (multiple client companies present, or unknown company) → mark tentative, confirm in Phase 2.
- Also propose a **context** within the client (e.g. project or workstream name like "SAP Implementation") when the topic clearly maps to one; otherwise leave context empty.

Record: `Tentative client: [name | none] · Tentative context: [name | none]`

### 4. Entity Extraction & Content Discipline

Extract all entities from the **full** transcription text (windowed if long — see coverage):

| Category | What to capture | What NOT to put here |
|---|---|---|
| **People** | Name, role, seniority, company/team, domain expertise, interests | What they said in this meeting, decisions they made, actions assigned |
| **Projects** | Name, purpose, current status, tech stack, open questions | Who attended which meeting about it, what was said, meeting outcomes |
| **Technologies** | Tool name, purpose, version, integration context | Which meeting it was discussed in |
| **Decisions** | What was decided, rationale, tentative owner, date | — |
| **Action Items** | Task description, tentative owner, deadline, linked project | — |
| **Open Questions** | What is unresolved and must be answered by a follow-up | Topics that already have a decision |
| **Challenges/Risks** | Problem description, impact, mitigation strategy | — |
| **Principles** | Methodology or guideline, application context | — |

> **Separation rule:** People and Project facts describe the *entity itself* — what it is, what it does, what it knows. Meeting outcomes, assignments, and discussions belong in Decisions, Action Items, and the Diary — linked back to the people/projects involved.

**Smalltalk suppression (mandatory, no trace):** greetings, farewells, weather, family/personal news, jokes, "how are you", room/dial-in logistics, tech checks, side conversations, filler acknowledgements, and ASR artifacts (stutters, mis-heard names, repetition) are **fully omitted** — they must not appear in the digest, the diary, the local file, or metadata. A stretch with no professional content contributes nothing.

**Professional content completeness (mandatory):** the meeting's substance must survive intact.
- **One distinct topic = one idea covered.** Never merge unrelated topics into a single paragraph to save space.
- Capture checklist per professional stretch:
  - decisions + rationale
  - commitments → owner, artifact, due date
  - open questions / deferred items
  - risks, dependencies, constraints, explicit non-decisions ("budget is fixed")
  - technical specifics: system names, versions, configs, numbers, dates
- **Opinions and potentialities are not decisions.** "We could maybe look at X" → Open Questions or Notes, never Decisions.
- Prefer lossless paraphrase over compression. Drop smalltalk; do not drop substance.

**Mark all ownership as tentative — do not assume.** Human confirms in Phase 2.

Record coverage: `segments_processed: N/N` (every window accounted for; N = planned windows).

---

## Phase 2 — Human Verification (STOP before writing)

**Do not write anything to memory until this phase is fully complete.**

### 5. People Resolution

For **each** name from step 2, search memory. Use **liberal matching** — the vector search supports partial/name-only queries, so always query with **just the person's name** (no role, company, or context):

```
search_facts("<Full Name>", category="People", top_p=0.4)
search_facts("<First Name>", category="People", top_p=0.4)
```

If the person's first+last name doesn't match, try querying by their first or last name individually before falling back to broader terms.

Use the **`question` tool** for names that need a human decision. One question per ambiguous name; batch unambiguous names into a single question with multiple options.

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
- **Confident single match** (same name + role/company aligns): **auto-approve and log the decision** — "Auto: 'Kate' → Kate Müller, PM at Deutsche Bank (existing)". Do not ask.
- **2+ plausible matches**: always ask via `question` tool, never auto-select.
- **No match**: use the `question` tool to propose creating a new record — never create without confirmation.
- **Never create a People record without explicit human confirmation.**
- Batch unambiguous names into one `question` call using `multiple: true`.

### 6. Client Confirmation

Confirm the tentative client/context from step 3 via the `question` tool (batch with people questions when possible):

```
header: "Which client?"
question: "This meeting appears to belong to which client?"
options:
  - label: "Deutsche Bank — SAP Implementation"   [inferred]
  - label: "Internal — no client"
  - label: "New client — create from participant company"
multiple: false
```

Rules:
- **Confident single match** (participant company = known client): Y/N confirmation is enough.
- **Internal meeting** (no external participants): confirm "no client" — facts and diary will be stored without client scope.
- **New client company**: propose creating it — `create_client` runs in Phase 3 after confirmation.
- After the human responds, record: `Confirmed client: [name | none] · Confirmed context: [name | none]` — pass both through Phase 3.

---

## Phase 3 — Store (after human confirmation only)

**Write order per meeting — follow it exactly:**

```
7. new-vs-reprocess check (read-only)
8. render summary from digest  →  9. pre-save checklist  →  10. local .md save
11. diary_save_entry (the anchor)  →  verify (14)
12. facts (add/update)  →  13. link_facts (once target ids exist)
```

Batch the fact writes (step 12) in a single response; issue `link_facts` only after their targets return ids. Anchor-first means a crash at any later step leaves the diary entry in place, and the next run resumes as a reprocess instead of orphaning facts.

### 7. Determine New vs Reprocess (before any write)

```
list_diary_entries(fromTs="<slot date 00:00:00>", toTs="<slot date 23:59:59>")
```

- **Entry exists at the slot** → this is a **reprocess** (overwrite by design). Record its `id`. Reprocess rules:
  - Reuse the `entryId` when saving (same timestamp → same id → pure in-place replace).
  - **Re-send the complete metadata set** — save *replaces* metadata wholesale; omitting `original_file`/`meeting_date`/`topic` silently drops them.
  - **Omit `linked_facts`** from the save call (omitting preserves existing MENTIONS; passing `[]` clears them).
  - Prior-run facts are evidence, not garbage: retrieve them with `diary_search_entries("<title> <date>")` → the entry's `mentions` field (`id`, `text`), then use **`update_fact`** to append genuinely new stable info — never re-create the same fact. Only remove a Decision/Action fact if it is clearly an extraction error (absent from *both* the old and new transcript).
  - Never change the entry's timestamp, and never pass an `entryId` whose id differs from the slot's id (that path deletes the old entry *before* saving — a failed save destroys it permanently).
- **No entry at the slot** → fresh entry; `entryId` omitted.

### 8. Summarization via Subagent

Spawn a **dedicated subagent** (task tool, `subagent_type="general"`) to render the structured summary. Pass it **the digest — never the raw transcript**:

```
You are a summarization agent. Based on the digest below, produce:

{INTEREST FROM THE AGENTS.MD FILE}

1. A diary entry in the exact format below.
2. A local save file in the exact same format.
3. The keyword tags (5-12) from the digest.

TIMESTAMP (already resolved): {ISO slot timestamp}
TITLE: {Meeting Title}
ORIGINAL FILE: {stable id: basename (bytes)}

EXTRACTED PARTICIPANTS: ...
EXTRACTED DECISIONS: ...
EXTRACTED ACTIONS: ...
EXTRACTED OPEN QUESTIONS: ...
EXTRACTED PROJECTS / TECHNOLOGIES / NOTES: ...

DIGEST:
{the fixed-schema digest from Phase 1}

===

DIARY FORMAT (use this exactly, in this order):

## Participants
- **{Name}** ({role/context})

## Context
2-3 sentences: what meeting, why, who led.

## Description
One paragraph per distinct topic from the digest — never merge unrelated topics.
If processes were described, make sure all steps, responsibles are listed. Mention all
architecture components, caveats, numbers, versions and dates that were mentioned.
Make sure to list all relevant information for the role you are working in.
No smalltalk, greetings, weather, personal stories or logistics.

## Decisions
- {numbered list of every decision made, with rationale}

## Actions
- [ ] {Owner}: {action description} ({artifact}, due {date})

## Open Questions
- {what is unresolved and what a follow-up must answer}

## Notes
- challenges, risks, dependencies, constraints, context not captured above

## Keywords
{comma-separated list of 5-12 keywords}

===

Return ONLY the rendered diary content as your output — nothing else.
```

Capture the subagent's output as the rendered diary content.

### 9. Pre-Save Checklist (run against the digest, not the transcript)

Before any `diary_save_entry` call, verify the rendered content against the digest:

1. Every `projects[]`, `technologies[]`, `challenges[]`, `principles[]` and `notes[]` entry in the digest → covered by a `## Description` paragraph or `## Notes` line
2. Every commitment in `actions[]` → a `- [ ]` line in `## Actions` with owner
3. Every `decisions[]` item → numbered line in `## Decisions`
4. No `open_questions[]` item leaked into `## Decisions`
5. Every `participants[]` entry → a `## Participants` line
6. `coverage.segments_processed == N/N` (all windows accounted for)
7. No smalltalk anywhere in the rendered content

A failed check → fix the content **before** saving. Never save and fix later.

### 10. Local File Save

Write the rendered diary content to a file named `YYYY-MM-DD hh-mm-ss Title.md` in the working directory, using the **rounded slot timestamp** and the meeting title as the filename.

Use the `write` tool or equivalent to create the file.

### 11. Diary Logging

Call `diary_save_entry` with:
- `content` = the rendered diary content (exact format above)
- `name` = meeting title
- `timestamp` = the resolved slot, ISO-8601 with time **rounded to the nearest 15 minutes** (:00, :15, :30, :45), e.g. `2026-05-15T10:00:00`
- `entryId` = the existing entry's id **only on reprocess** (step 7)
- `metadata` = **always the complete set** (save replaces metadata wholesale):
  `{"original_file": "<stable id>", "meeting_date": "<date>", "topic": "<topic>", "keywords": "<comma-separated keywords>", "segments_processed": "N/N"}`
  — it enables cross-referencing, source tracing, keyword searching/filtering, and automatic UI rendering.
- `client` = confirmed client name (omit for internal meetings with no client)
- `context` = confirmed context name within the client (omit when none)
- `linked_facts` = **omit entirely** (preserves existing MENTIONS on reprocess)

This entry is the **anchor**: everything after it is resumable.

### 12. Facts — update or create

**Existing person** → `update_fact`: append only genuinely new *stable* information (new role, new team, new area of expertise). Preserve the Markdown structure — if existing text uses bold field labels, continue using that format. Do not add event details — those belong in the Diary and linked facts.

**New person** → `add_fact`:
```
Title: Tim Lohman
Category: People
Text:
  **Role:** Lead Engineer
  **Company/Team:** SAP
  **Domain:** [area of expertise or responsibility]
  **Notes:** [any other relevant stable information]
```

**Do not include:** meeting dates, what they said, decisions they made, tasks assigned to them. Those go in Decision/Action Item facts and the Diary, linked to the person.

Title = name only. Role and company go in the description body.

Examples of correct vs incorrect:
- ✅ Correct: name="Gergely Papp" (People)
- ❌ Incorrect: name="Gergely Papp - Enterprise Architect"
- ✅ Correct: name="LeanIX" (Technology)
- ❌ Incorrect: name="LeanIX — EA Tool"

General work-related: use `People` (for personnel), `Project`/`Projects` (for initiatives), `Technology` (for tools), `Concepts` (for principles).

Use `add_fact` for storing facts and `update_fact` for extending existing ones. Batch every `add_fact`/`update_fact` for this meeting in **one response** — then capture the returned ids for linking.

**Client scoping (applies to every write in this phase):**
- Pass the confirmed `client` (and `context` when set) to **every** `add_fact` call — client-specific people, projects, decisions, and actions all get scoped.
- Shared/generic facts (public technologies, general principles with no client relevance) → omit `client` so they stay global.
- If the confirmed client is new → call `create_client("<name>")` first, then use the name in all subsequent writes (auto-create handles the rest, but explicit creation confirms intent).
- When searching for existing records in this phase, pass `client` to `search_facts` so matches prefer the meeting's client scope.

### 13. Link

Once the diary id (from step 11) and all fact ids (from step 12) exist, fire the links together:

| Relationship | Type |
|---|---|
| Person → Project | `WORKS_ON` |
| Person → Client/Org | `WORKS_FOR` |
| Diary Entry → Person | `MENTIONS` |
| Diary Entry → Project | `MENTIONS` |
| Diary Entry → Decision | `MENTIONS` |
| Diary Entry → Action | `MENTIONS` |

```
link_facts(diary_entry_id, person_id, "MENTIONS")     ← for each participant
link_facts(diary_entry_id, project_id, "MENTIONS")    ← for each project discussed
link_facts(diary_entry_id, decision_id, "MENTIONS")   ← for each decision
link_facts(diary_entry_id, action_id, "MENTIONS")     ← for each action item
```

Note: Diary↔Fact links are stored as `MENTIONS` regardless of the type string passed — the `RECORDS` label does not exist in storage, so use `MENTIONS` to keep intent honest. `link_facts` is MERGE-idempotent — safe to re-issue on a resumed run.

These links make the diary navigable from any fact and vice versa.

### 14. Post-Save Verification (mandatory)

```
list_diary_entries(fromTs="<slot date 00:00:00>", toTs="<slot date 23:59:59>")
```

Confirm: the slot is present, `name` matches, `original_file` matches the stable id. Also confirm the local `.md` file was written.

- **Mismatch/failure** → report it. Retry only with the **same timestamp** (that is a safe in-place upsert). Never change a timestamp to "fix" a save — never pass an `entryId` that differs from the slot's id.

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

1. **Phase 1 is read-only** — no memory writes at all; it runs in a subagent and returns only the digest.
2. **Phase 2 is a hard stop** — wait for human replies before any writes; one consolidated `question` call (per file, or per batch under `process-directory`).
3. **Search before creating** — always check memory first to prevent duplicates.
4. **Title = name only** — role/company/context go in the description body.
5. **Anchor-first, then batch** — render → checklist → local save → diary entry → batch all fact writes in one response → links once ids exist.
6. **Never create a People record without human confirmation.** Confident single matches are auto-approved and logged; 2+ matches and unknowns are asked.
7. **Facts describe entities, not events** — People and Project facts contain stable identity information. Meeting outcomes, discussions, and assignments go in Decision/Action Item facts and the Diary.
8. **Summarize via subagent from the digest** — never feed the raw transcript to the summarizer.
9. **Save locally** — write `YYYY-MM-DD hh-mm-ss Title.md` with the slot timestamp.
10. **Diary entries use the exact format** — `## Participants`, `## Context`, `## Description`, `## Decisions`, `## Actions`, `## Open Questions`, `## Notes`, `## Keywords`.
11. **Original file is metadata** — complete metadata set on every save (it is replaced, not merged); stable id as `original_file`.
12. **Diary entries are linked** — every diary entry must be linked to all mentioned people, projects, decisions, and action items (`MENTIONS`).
13. **Client scope on every write** — detect client in Phase 1, confirm in Phase 2, pass `client`/`context` to all Phase 3 writes (facts + diary). Shared knowledge stays global.
14. **Slot = meeting identity** — same meeting → same slot; overwrite at that slot is reprocessing. Never move an entry's timestamp.
15. **Smalltalk is fully omitted**; professional content is retained losslessly — one paragraph per topic, commitments with owners/dates, opinions never become decisions.
16. **Checklists run before saving, against the digest**; post-save verification runs after, against `list_diary_entries`.
