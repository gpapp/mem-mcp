---
name: cleanup-transcription
description: Refines raw transcription into a speaker-labeled, cleaned version with a summary. Focuses on accurate speaker identification for labels and summaries without updating the knowledge graph.
context: fork
---

## When to Use This Skill

Use this skill when you have:
- A raw, unrefined transcription (often with labels like "[00:00:20] Shreekes Srinivas:")
- A need for a readable, clean version of the transcript.
- A requirement to accurately identify who said what.
- A need for a concise summary of the discussion.

### Example Header (Unreliable Profiles)
The transcription often starts with metadata that may be inaccurate. Use this as a starting point but verify against the actual dialogue content:
The identified speaker profiles are mostly WRONG, rely on facts and content.

```text
============================================================
SPEAKER VOICE PROFILES
============================================================
  Alexandr Kirov: pitch=134Hz (±84Hz)  energy=0.0855  speech=38s
  Alok Kumar: pitch=136Hz (±104Hz)  energy=0.0538  speech=3s
  Daniel Zehr: pitch=130Hz (±75Hz)  energy=0.0610  speech=4s
  Gergely Papp: pitch=110Hz (±73Hz)  energy=0.0052  speech=34s
  Kateryna Bukhtoiarova: pitch=205Hz (±60Hz)  energy=0.0698  speech=50s
  Marco Burkhart: pitch=130Hz (±84Hz)  energy=0.0833  speech=478s
  Mark Falkheim: pitch=129Hz (±68Hz)  energy=0.0656  speech=450s
  Pierluigi Casale: pitch=128Hz (±100Hz)  energy=0.0758  speech=6s
  SPEAKER1: pitch=105Hz (±60Hz)  energy=0.0036  speech=3s
  SPEAKER12: pitch=131Hz (±58Hz)  energy=0.0445  speech=4s
  ...
============================================================
```

## Skill Workflow

### 1. Context and Entity Discovery
- **Identify Metadata**: Extract date, project context, and potential clients from the filename or header.
- **Scan for speaker labels and voice profile headers (e.g., "[00:00:20] Shreekes Srinivas:")**: Voice profiles are guidance only — use discussion content to verify speaker identity:
    - **Topic expertise (who would discuss this subject)**
    - **Questions asked vs. answers given**
    - **Demonstrated knowledge/perspective**
- **Search Memory**: Use `search_facts` to find:
    - **Projects/Clients**: Facts related to the meeting topic.
    - **Potential Participants**: Search for people linked to the identified projects or clients.
    - **Corrections**: Look for facts in the 'Corrections' category to fix spelling or technical terms.

### 2. Participant Verification
- **Ask for Participants**: If the list of participants is not provided by the user or clear from memory, **STOP and ask the user** for a list of who was in the meeting.
- **Note**: Not all participants necessarily spoke during the meeting.

### 3. Speaker Identification and Turn Correction
- **Identify Speakers**: Map generic labels (e.g., "SPEAKER_01") to actual participant names.
- **Direct Addressing**: Look for instances where speakers address each other by name (e.g., "John, what do you think?").
- **Deductive Guessing**: If addressing is not present, use the participants list and the content of the speech (roles, topics mentioned) to guess the speaker.
- **Accuracy**: Maintain consistent speaker labeling throughout the transcript.
- **Speaker correction option:** If voice profile doesn't match context, offer to reassign the label (e.g., "Speaker B appears to be X based on context").


### 4. Transcription Cleanup
- **Tool**: Call `transcription_cleanup` with the transcription text and the identified participants list.
- The tool returns the raw text with metadata and cleanup instructions — **you** perform the actual cleanup using the instructions provided.
- Apply any "Stored Corrections" from memory (step 1) to fix recurring misspellings.
- Produce the cleaned transcript in `[Speaker Name]: <text>` format, removing filler words and correcting errors.

---

## Phase 2 — Human Verification (STOP — interactive)

**Use this phase to ensure the summary and transcript labels are accurate. Do not call any memory write tools (add_fact, update_fact, etc.).**

### 4. People Resolution — fuzzy search + multi-choice

For **each** name identified in the transcript or participants list:

1. Search memory with **name-only queries** (no role, company, or description text — search_facts handles partial name matching via vector search automatically):
   - Full name: `search_facts("Tim Lohman", category="People")`
   - First name only: `search_facts("Tim", category="People")`
   - Purpose: To find the correct spelling and role for the final output.

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

## Phase 3 — Final Output

#### A. Cleaned Transcript
```markdown
[00:00:10] **John Doe**: Welcome everyone. Today we are discussing...
[00:00:45] **Jane Smith**: I have an update on the frontend progress.
...
```

#### B. Main Points Summary
- **Key Decisions**: Summarize explicit decisions made.
- **Action Items**: List tasks with their owners and deadlines.
- **Project/Client Links**: Explicitly link the summary to the relevant [[Project]] and [[Client]].

## Implementation Guidelines

- **Entity Linking**: Ensure all mentioned people and projects are linked using [[Name]] syntax.
- **Ambiguity**: If you are unsure about a speaker identification, flag it (e.g., "[00:12:00] **Unidentified (possibly John?)**").

## Efficiency: Multi-Tool Execution
You are encouraged to call multiple tools in a single response. For example, you can call `transcription_cleanup` and `search_facts` in parallel, then process all results together.
