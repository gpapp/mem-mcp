---
name: process-transcription
description: Structured workflow for processing meeting transcriptions into the knowledge graph. Includes interactive human checkpoints for people resolution and fact-person assignment before writing anything to memory.
---

## When to Use This Skill

Use this skill when you have:
- Meeting transcription files (typically .txt files with timestamps)
- Need to extract structured information (people, projects, decisions, action items)
- Want to build or update a knowledge graph with the transcription content
- Need to store extracted information in memory systems for future reference

---
Consider the original role defined in AGENTS.md to ensure the summarization is relevant to the role.

Use @general subagent to execute the processing in it's own space.

## Phase 1 — Extract (read-only, no writes)

### 1. File and Metadata

- Extract date/time from filename (pattern: `YYYY-MM-DD hh-mm-ss`) round it to the nearest 15 minutes.
- Identify meeting name, topic, and context (internal/client/etc.)
- Capture the **original file path** — pass it through all phases for use in diary metadata and local save filename.
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

For **each** name from step 2, search memory. Use **liberal matching** — the vector search supports partial/name-only queries, so always query with **just the person's name** (no role, company, or context):

```
search_facts("<Full Name>", category="People", top_p=0.4)
search_facts("<First Name>", category="People", top_p=0.4)
```

If the person's first+last name doesn't match, try querying by their first or last name individually before falling back to broader terms.

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

---

## Phase 3 — Store (after human confirmation only)

Execute all writes in one batched response — do not pause between individual calls.

### 6. People — update or create

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

Use `add_fact` for storing facts and `link_facts` for creating relationships between facts.

- `link_facts(sourceFactId, targetFactId, relationshipType)` — build the graph

**Link immediately after creating each fact:**

| Relationship | Type |
|---|---|
| Person → Project | `WORKS_ON` |
| Person → Client/Org | `WORKS_FOR` |
| Diary Entry → Person | `MENTIONS` |
| Diary Entry → Project | `MENTIONS` |

### 8. Summarization via Subagent

Before writing anything to memory, spawn a **dedicated subagent** (using the `task` tool with `subagent_type="general"`) to produce the structured summary. Pass it the following as the prompt:

```
You are a summarization agent. Based on the extracted data below, produce:

{INTEREST FROM THE AGENTS.MD FILE}

1. A diary entry in the exact format below.
2. A local save file in the exact same format.
3. An extracted list of 5-12 keyword tags representing the key technologies, projects, topics, and decisions discussed.

TIMESTAMP (rounded to 15 min): {ISO timestamp}
TITLE: {Meeting Title}
ORIGINAL FILE: {path to original transcription file, if any}

EXTRACTED PARTICIPANTS:
- {Name} ({role/context})
- ...

EXTRACTED DECISIONS:
- ...

EXTRACTED ACTIONS:
- ...

EXTRACTED PROJECTS / TECHNOLOGIES / NOTES:
- ...

===

DIARY FORMAT (use this exactly):

## Participants
- **{Name}** ({role/context})

## Context
2-3 sentences: what meeting, why, who led.

## Description
Detailed description of the meeting subject.
If processes were described, make sure all steps, responsibles are listed. Mention all architecture components, caveats that were mentioned.
Make sure to list all relevant information for the role you are working in. 
Ignore personal stories/nicities.

## Decisions
- {numbered list of every decision made}

## Actions
- [ ] {Owner}: {action description}

## Notes
- challenges, risks, dependencies, context not captured above

## Keywords
{comma-separated list of 5-12 keywords}

===

LOCAL SAVE FILE:
Save the exact same content (including the ## headers and ## Keywords) to a local file named:
{YYYY-MM-DD hh-mm-ss Title.md}
(use the rounded timestamp and the meeting title).

Return ONLY the rendered diary content as your output — nothing else.
```

Capture the subagent's output as the rendered diary content.

### 9. Local File Save

Write the subagent output to a file named `YYYY-MM-DD hh-mm-ss Title.md` in the working directory, using the **rounded timestamp** (15-minute boundary) and the meeting title as the filename.

Use the `write` tool or equivalent to create the file.

### 10. Diary Logging

**Before writing, find existing entries for the meeting timeframe:**
```
list_diary_entries(fromTs="YYYY-MM-DDT00:00:00", toTs="YYYY-MM-DDT23:59:59")
```
Returns `[(id, timestamp, name), ...]` for entries on that date.

**Then search for related content:**
```
diary_search_entries("<meeting topic> <date>")
```
- **No match:** write a fresh entry with the subagent-rendered content.
- **Match found, new info:** call `diary_save_entry` again with the **same `timestamp`** and the **full updated content** (merge old + new). The server replaces the existing entry for that timestamp.
- **Match found, nothing new:** skip entirely.

Call `diary_save_entry` with:
- `content` = the subagent-rendered diary content (exact format above)
- `name` = meeting title
- `timestamp` = ISO-8601 with time **rounded to the nearest 15 minutes** (:00, :15, :30, :45), e.g. `2026-05-15T10:00:00`
- `metadata` = `{"original_file": "<path to original transcription file>", "meeting_date": "<date>", "topic": "<topic>", "keywords": "<comma-separated list of extracted keywords>"}` — always include metadata and keywords when processing transcripts; it enables cross-referencing, source tracing, keyword searching/filtering, and automatic UI rendering.

**After saving the diary entry, link it to every fact it references:**

```
link_facts(diary_entry_id, person_id, "MENTIONS")      ← for each participant
link_facts(diary_entry_id, project_id, "MENTIONS")     ← for each project discussed
link_facts(diary_entry_id, decision_id, "RECORDS")     ← for each decision
link_facts(diary_entry_id, action_id, "RECORDS")       ← for each action item
```

These links make the diary navigable from any fact and vice versa.


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
8. **Summarize via subagent** — use a dedicated `task` subagent (`subagent_type="general"`) for the structured summary.
9. **Save locally** — write `YYYY-MM-DD hh-mm-ss Title.md` with the rounded timestamp.
10. **Diary entries use exact format** — `## Participants`, `## Context`, `## Description`, `## Decisions`, `## Actions`, `## Notes`.
11. **Original file is metadata** — pass `{"original_file": "..."}` as `metadata` to `diary_save_entry`.
12. **Diary entries are linked** — every diary entry must be linked to all mentioned people, projects, decisions, and action items.
