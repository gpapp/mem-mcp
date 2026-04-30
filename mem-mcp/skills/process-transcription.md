---
name: process-transcription
description: Structured workflow for processing meeting transcriptions into the knowledge graph. Includes interactive human checkpoints for people resolution and fact-person assignment before writing anything to memory.
---

## When to Use This Skill

Use this skill when you have:
- Meeting transcription files (typically .txt files with timestamps)
- Need to extract structured information (people, projects, decisions, action items)
- Want to build or update a knowledge graph with the transcription content

---

## Phase 1 — Extract (automated, no writes yet)

### 1. File and Metadata
- Extract date/time from filename (pattern: `YYYY-MM-DD HH-MM-SS`)
- Identify meeting title, topic, and context (internal/client/etc.)
- Apply stored corrections: `search_facts("correction")` → fix recurring misspellings of names and terms before proceeding.

### 2. Participant Extraction
Scan for all speaker labels and name mentions. Build a raw list:
```
Raw participants: [Tim, Rafael, Kate, ...]
```
Do NOT resolve to memory yet — that happens in Phase 2.

### 3. Content Analysis
Extract all entities from the transcription:

| Category | What to capture |
|---|---|
| **People** | Names, roles, affiliations, what they said/did |
| **Projects** | Name, goals, status, timeline, stakeholders |
| **Technologies** | Tool name, purpose, version, pros/cons |
| **Decisions** | What was decided, by whom (tentative), context |
| **Action Items** | Task description, tentative owner, deadline |
| **Challenges/Risks** | Problem, impact, mitigation |
| **Principles** | Methodologies, guidelines, frameworks |

At this stage, mark ownership as **tentative** — do not assume. The human confirms in Phase 2.

---

## Phase 2 — Human Verification (STOP — interactive)

**Do not write anything to memory until Phase 2 is complete.**

### 4. People Resolution — fuzzy search + multi-choice

For **each** name extracted in step 2:

1. Search memory with multiple fuzzy queries:
   - Full name: `search_facts("Tim Lohman", category="People")`
   - First name only: `search_facts("Tim", category="People")`
   - Role + company if known: `search_facts("engineer SAP")`

2. Present the results as a **compact multi-choice question**:

```
❓ Who is "Tim" from the transcript?

  A) Tim Lohman — Lead Engineer, SAP  [existing]
  B) Tim Berners-Lee — CTO, Client X  [existing]
  C) New person — create "Tim" as new record
  D) Not important — skip

Reply: _
```

3. Rules:
   - If memory returns a confident single match (same name + role/company): auto-select and inform the user ("I matched 'Kate' to Kate Müller, PM at Deutsche Bank — confirm? Y/N").
   - If 2+ plausible matches: always ask.
   - If no match: propose creating a new record, but still ask.
   - **Never create a new People record without human confirmation.**

4. Batch unambiguous names together in one message; ask about ambiguous ones individually.

### 5. Fact–Person Assignment — multi-choice involvement

After all people are resolved, present each **decision** and **action item** as a short question with the confirmed participant list as options.

Use **one message per batch** — group related items together, not one question per item:

```
❓ Who is involved in the following? (reply with participant letter(s) for each)

Participants this meeting:
  A) Rafael Papp       B) Tim Lohman
  C) Kate Müller       D) Someone not listed
  E) Unassigned / unknown

1. Decision: Proceed with Concur LEC on SAP ISP  →  _
2. Action: Complete schema mapping by end of month  →  _
3. Action: Schedule DB AI workshop for week of Mar 16  →  _
4. Project involvement: Deutsche Bank AI Adoption  →  _ (multi ok, e.g. A,B)
```

- Accept multiple letters per item (e.g. "A,C").
- If the human answers "D", ask for the name.
- If "E", store the fact unlinked and note it as unassigned.

---

## Phase 3 — Store (after human confirmation)

### 6. People — resolve or create

For each person (now confirmed by the human):

**Existing person:** `update_fact` — append only the genuinely new information. Do NOT re-state what is already in the record.

**New person:** `add_fact` with:
```markdown
### Person: Tim Lohman
**Role:** Lead Engineer
**Company/Team:** SAP
**Met in:** [Meeting Title] — YYYY-MM-DD
...
**Aliases:** Tim Lohman (100%), Tim Loman (90%), Tim (70%)
```
Include `metadata={"tags": ["person"], "aliases": {"Tim Lohman": 1.0, "Tim Loman": 0.9, "Tim": 0.7}}`.

### 7. Facts, Decisions, Action Items

Store all other entities with rich Markdown. Use confirmed ownership from Phase 2.

**Tool usage:**
- `add_fact(title, text, category)` — new facts
- `update_fact(memoryId, text, category)` — update existing (new info only)
- `link_facts(sourceFactId, targetFactId, relationshipType)` — build the graph

**Linking strategy** (execute immediately after creating each fact):
- Person → Project: `WORKS_ON`
- Person → Action Item: `ASSIGNED_TO`
- Decision → Project: `DECIDED_FOR`
- Person → Client: `WORKS_FOR`

**Reprocessing guard:** Before creating any fact, run `search_facts` on the title/topic. If it already exists, update it instead.

### 8. Diary Logging

**Before writing:** `diary_search_entries("<meeting topic> <date>")` to find existing entries.
- **New topic:** write a fresh entry.
- **Updated topic:** write a new entry with the complete updated picture, label it `[Updated] <topic>`.
- **Unchanged topic:** skip.

Each entry = one focused topic (decisions, action items, etc.). Append-only — never call it twice for the same topic to "fix" it.

Content focus: WHAT THE USER DID — outcomes, decisions, assigned tasks. Not what was processed.

### 9. Summary

Store a meeting summary fact (`category="Project"` or `"Event"`):
```
# Meeting Summary: [Topic] — YYYY-MM-DD
...links to created/updated facts...
```

---

## Format Rules for Human Questions

- **Always use lettered options** (A, B, C…) — never ask open-ended questions when choices are known.
- **Batch questions** — group as many related items into one message as possible.
- **One confirm per ambiguous person** — don't ask separately for each mention of the same name.
- **Show context** — include role/company next to each person option so the human can answer quickly.
- After the human responds, **confirm your interpretation** in one line before proceeding: "Got it — linking Tim → Tim Lohman, Kate → new record."

## Efficiency

After human checkpoints are resolved, batch all `add_fact`, `update_fact`, and `link_facts` calls in a single response. Don't wait for confirmation between individual writes.
