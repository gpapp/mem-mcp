---
name: process-directory
description: Batch workflow that drives process-transcription over a directory of meeting transcripts. Dry-run inventory first, slot-based idempotency ledger (re-runs are safe), per-file error isolation, one consolidated human question round, anchor-first writes, and a final status report.
---

## When to Use This Skill

Use this skill when you have:
- A directory (or explicit list) of meeting transcript files — typically `.txt` exports from asr-mcp
- Many meetings to process, and re-runs must be safe (no duplicates, no partial-state surprises)
- A need for failures to be isolated per file, with a report at the end

Single file → use `process-transcription` directly. This skill orchestrates it per file and adds batch-level concerns: inventory, ledger, consolidated questions, isolation, reporting.

---

Consider the original role defined in AGENTS.md to ensure the summarization is relevant to the role.

## Core model

**Slot = meeting identity.** Every file resolves to one 15-minute slot (resolution ladder in `process-transcription`). Meetings are always longer than 15 minutes, so one meeting occupies one slot; saving at an occupied slot **overwrites by design** — that is how reprocessing works. Consequently:

- Existing entry at the slot → **reprocess** (update in place), not a duplicate
- Two *files* resolving to the same slot → they are the **same meeting** (split recording / duplicate export) → **CONFLICT**: process the superset file or ask; never let one silently replace the other
- A re-run of the whole directory must produce **zero new entries** for already-processed files

**Idempotency ledger = the diary itself.** There is no sidecar manifest. The ledger is built from `list_diary_entries` using `metadata.original_file`.

**Token discipline.** The coordinator never holds raw transcripts — one subagent per file returns a digest; the summarizer gets the digest; one `question` call for the whole batch.

---

## Phase 0 — Inventory (read-only, no questions, no writes)

### 1. Enumerate

List candidate files in the directory (or the explicit list). Accept transcript-shaped files: asr-mcp client exports (`Audio:` header + `[Speaker] NN.Ns - NN.Ns:` labels), GUI exports (`=== file ===` + `[Speaker] HH:MM:SS - HH:MM:SS`), or any text file with speaker labels. Skip non-transcripts with a note.

### 2. Resolve slots

For each file, run the **timestamp ladder** from `process-transcription` (filename `YYYY-MM-DD[ _-]hh-mm[-ss]` → batch-supplied date → source media mtime → **ask**). Record `slot` + `slot_source`. Files that reach "ask" are marked `UNRESOLVED` — they collect into the consolidated question round.

> ⚠️ Never resolve slots from the asr-mcp `Date:` header (transcription run time) — all files in a batch would collide.

### 3. Build the slot ledger

**One** call, with an explicit wide range — never rely on the default 30-day window, or older meetings become invisible and get re-created as duplicates:

```
list_diary_entries(fromTs="<earliest batch date - safety margin>", toTs="<now + 1d>")
```

Return shape: `(id, timestamp, name, original_file)`. Build `{slot → (id, original_file)}`.

### 4. Classify and print the inventory

| Status | Meaning |
|---|---|
| `NEW` | no entry at the slot |
| `REPROCESS` | entry at the slot; `original_file` matches or no original recorded → update in place |
| `CONFLICT` | entry at the slot from a *different* file, or two batch files share a slot |
| `UNRESOLVED` | timestamp ladder exhausted → needs a human answer |

Print the table (file → slot → status) **before any writes**. If `CONFLICT` or `UNRESOLVED` exist, fold them into Phase 2 questions; otherwise proceed.

---

## Phase 1 — Per-file extraction (delegated, no writes)

Process files **one at a time**. For each file:

1. Dispatch **one subagent** (`subagent_type="general"`). It reads the file locally, runs the full Phase 1 extraction from `process-transcription` (long files fan out internally into ~8–12k-char window jobs run **up to 3–4 concurrently**), applies smalltalk suppression and the professional-content capture checklist, and returns only the **fixed digest schema** — raw text dies with the job.
2. On subagent failure or malformed digest: record `{file, stage, error}` and **continue with the next file**. Never abort the batch; never blind-retry a write.
3. The coordinator accumulates **digests only**.

No memory writes happen in this phase.

---

## Phase 2 — One consolidated human gate (still no writes)

Across **all** digests:

1. Resolve people and client/context against memory (`search_facts` by name-only, `list_clients`). Pass the tentatively identified `client` (and `context`) to `search_facts` when there is one — it prioritises that scope rather than filtering, so the same-first-name/different-client candidates still surface for the human gate.
2. **Auto-approve** confident single matches (name + role/company aligns) — log the decision, do not ask.
3. Collect every genuine ambiguity from every file: unknown names, no-match people, client/context for ambiguous meetings, plus all `UNRESOLVED` timestamps and `CONFLICT` resolutions from Phase 0.
4. Ask **one** consolidated `question` call (multiple questions, lettered options, `custom: false`) covering all of them.
5. Apply the answers to every affected digest.

**No writes occur before the human answers.** After the answers, confirm the interpretation in one line.

---

## Phase 3 — Anchor-first write (per file)

Run `process-transcription` steps 8–14 for each digest, in that order:

1. **Render** the summary from the digest (step 8) and run the **pre-save checklist** against the digest (step 9).
2. **Local `.md` save** (step 10).
3. **`diary_save_entry` first among server writes** — it is the anchor and the ledger record (step 11). Apply the reprocess rules from `process-transcription` step 7: reuse `entryId` on reprocess, **re-send the complete metadata set** (save replaces metadata wholesale), omit `linked_facts`.
4. **Facts** (step 12): batch all `add_fact`/`update_fact` in one response. On reprocess, retrieve prior-run facts via `diary_search_entries("<title> <date>")` → the entry's `mentions` field, then `update_fact` — never re-create.
5. **Links** (step 13): `link_facts` with `MENTIONS` once all ids exist (MERGE-idempotent — a resumed run can safely re-issue).
6. **Post-save verification** (step 14): slot present, `name`, `original_file` match.

Any error in steps 3–6 → record `{file, stage, error}`, continue. The next run of this skill is safe: the entry already exists at the slot, so it resumes as a reprocess.

**Reprocess safety:** never change a timestamp; never pass an `entryId` whose id differs from the slot's id (delete-before-save path).

---

## Phase 4 — Report

Print a summary table:

```
file → slot → status: processed | reprocessed | skipped (already done) | failed | needs-review
```

Plus counts (files, new, reprocessed, failed, questions asked) and the full failure list with `{file, stage, error}` for a targeted re-run.

**Targeted re-run:** invoke this skill again with the failed files — completed files show as `REPROCESS`/already-done and are reprocessed idempotently (or pass them through `list_diary_entries` and skip if the user prefers no overwrite).

---

## Key Rules (summary)

1. **Inventory before anything** — print file → slot → status; no writes in Phase 0/1.
2. **Ledger = `list_diary_entries` with an explicit wide `fromTs`** — the 30-day default hides older meetings.
3. **Slot = meeting identity; overwrite = reprocess.** Two files on one slot = one meeting → superset or ask.
4. **Digest-only context** — raw transcripts never enter the coordinator's context; one subagent per file; summarizer gets the digest.
5. **One consolidated `question` call per batch**, after auto-approving confident matches.
6. **Anchor-first writes** — diary entry, then facts, then links; a crash resumes as reprocess.
7. **Per-file isolation** — record `{file, stage, error}` and continue; never halt the batch on one file.
8. **Complete metadata on every save** (replaced, not merged) with a stable `original_file` id.
9. **Verify after saving** — re-query the slot; retry only same-timestamp (safe upsert).
10. **End with the report** — every file accounted for, failures listed for a targeted re-run.
