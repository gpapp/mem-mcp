# Open Tasks

Only open work lives here. Completed tasks move to `task_history.md` (see
`AGENTS.md` → Task Tracking).

---

## Nothing open

The consultant-facing queue written 2026-11-08 against `0d372d1` is fully
shipped — tasks 1 to 11, each named in `task_history.md` with the commit that
delivered it. It covered skill discovery, the read-side skill, date- and
person-bounded search, the commitments register, the meeting-prep brief, the
stakeholder roster, the decision log, the engagement timeline, three orientation
skills, and the deliverables register.

The dashboard UX pass that followed it is shipped too: the client panel's
segmented switcher, the active-scope control and its one-click clear, the
Setup page's skills directory card, the empty-register hints, and the live level
meter. That entry is in `task_history.md` as well.

The diary/fact Markdown editor and the actions-to-source links shipped as well
(see `task_history.md`).

The next piece of work should start from a problem rather than from this list.
Add it here with its acceptance criteria before starting, so the criteria are
not written to match the implementation.

---

## ASR mishear corrections, scoped per client

asr-mcp mishears the same terms over and over — `Deutche Bank` for `Deutsche
Bank`, `1dp` for `OneDP`, `Southern TIO` for `TO`, `NJX` for `EngX`. The wrong
text is stored verbatim, the running note is generated from it, and a diary
entry created from the transcription inherits it.

**The reported problem is the inbound path, not live capture.** The
`process-transcription` skill (MS Teams + ASR transcripts) saves already-misheard
text as a diary entry, so the correction has to run *before* that save — and be
available as a retroactive pass over what is already there. Live microphone
segments are the same class of mistake and get the same fix, but they are
secondary: the inbound saves are what the user hit first.

**Decisions (open to challenge before implementation):**

- A rule is `wrong phrase -> right phrase`, scoped to `(user_id, client_id)`.
  Matching is case-insensitive on word boundaries, longest rule first, one
  non-overlapping pass, and never chains. A rule whose right side contains its
  wrong side is refused.
- Storage is a `:TermCorrection` node (`userId`, `clientId`, `wrong`, `right`,
  timestamps) with a boot index; the transcription row already carries
  `clientId`/`clientName`, which is the join key.
- **New diary entries (priority)**: applied inside `db_save_diary` /
  `db_update_diary` when `client_id` is set — one hook covers the GUI, the MCP
  tool and the `process-transcription` skill, which is the reported path.
- **New live segments (secondary)**: corrections loaded once per WebSocket
  connection and applied to the segment text before `db_append_segment`, so the
  stored transcript, the note and every downstream read are correct.
- **Retroactive** is per client, dry-run first, under the per-user maintenance
  lock. It rewrites `:TranscriptionSegment.text` (cheap, no re-embed) and
  `:DiaryEntry.content` (through the existing update path, so the vector and
  keywords stay consistent). Notes are regenerated with **Rebuild note**, not
  rewritten in place; Facts are out of scope (a Fact is a statement about the
  world and rewriting it is a bigger judgement call than fixing a mishear).

### 1. Pure correction engine + tests

- New stdlib-only module `mem-mcp/asr_corrections.py`: `normalise_rule`,
  `compile_rules` (dedupe, longest-first, word-boundary, case-insensitive),
  `apply_corrections(text, rules) -> (text, changes)` and `find_corrections`.
- No `common`, no logger, no DB driver — importable by a suite on a bare box.
- `test_asr_corrections.py`: case-insensitive match; whole-word boundaries
  (`Tim` must not touch `timeout`); longest-first (`Southern TIO` before `TIO`);
  non-overlapping single pass and no chaining; empty rules; a rule whose right
  contains its wrong is refused; `find_corrections` counts without changing.

### 2. Storage and CRUD

- `live_manager.db_list_corrections(user_id, client_id)`,
  `db_create_correction(user_id, client_id, wrong, right)`,
  `db_update_correction(correction_id, user_id, wrong, right)`,
  `db_delete_correction(correction_id, user_id)`.
- Refuse an unknown client and a duplicate `(client, wrong)`.
- Boot index in `common.py` next to the transcription/segment indexes.
- Tests: lift and drive against a fake driver (list/create/update/delete,
  unknown client, duplicate).

### 3. Apply on diary save (the inbound path)

- Inside `diary_manager.db_save_diary` and `db_update_diary`, when `client_id`
  is set, apply the client's rules to `content` and `name` before the write.
  Skip when `content` is `None` (metadata-only).
- One hook covers `gui.py:2327`, `gui.py:1310`, `mcp_tools.py:539` and the
  `process-transcription` skill.
- Pin both call sites; drive the hook against a fake driver.

### 4. Retroactive preview and apply

- `live_manager.db_preview_corrections(user_id, client_id)` returns counts and
  samples; `db_apply_corrections_to_client(user_id, client_id)` rewrites the
  changed `:TranscriptionSegment.text` rows and the `:DiaryEntry.content` rows
  (the latter through the normal diary update path so the vector is re-embedded
  and keywords regenerated).
- GUI: `POST /api/live/corrections/preview` and `.../apply`; apply takes the
  per-user maintenance lock and answers 409 with a reason when it cannot.
- Tests: drive both against a fake driver; assert a dry run writes nothing.

### 5. Apply on new live segments (secondary)

- In the WebSocket proxy, load the rules for the transcription's `clientId`
  once at route start and apply them to each segment's text before it is
  persisted (`asr_reader` builds the item; correct `item["text"]` there).
- Pin the call site; drive the applier against fakes.
- `db_update_segment` (manual edit) is left alone — a manual edit is
  intentional.

### 6. UI, client-scoped

- A **📖 Corrections** control scoped to the selected client: list rules,
  add/edit/delete, **Preview changes**, **Apply to existing records** with the
  dry-run counts shown first.
- Location is a decision for implementation: the Live tab toolbar (it already
  has `#live-client`) or the client panel, since that is where the inbound path
  lives. Both are client-scoped; pick the one that is open when a user notices
  a mishear.
- Routes in `gui.py` behind `_require_user`; `GET/POST/PATCH/DELETE
  /api/live/corrections`.
- `test_mobile_layout.py` if the control affects the responsive layout;
  dashboard wiring tests for the fetch calls.

### 7. Docs, task move, commit

- `AGENTS.md`: a "Mishear corrections" subsection (storage, the hook points,
  the retroactive pass, the matching rules) and the test paragraph.
- Move this section from `tasks.md` to `task_history.md` with the commit.
- Run the full dependency-light suite, `node --check` the dashboard script,
  `git diff --check`, commit and push.
