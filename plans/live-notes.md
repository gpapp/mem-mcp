# Epic: Live notes — transcription to enriched notes

**Status:** planned, implementation not started
**Branch:** `feature/live-notes`
**Task list:** [`tasks.md`](../tasks.md) → section **Epic: Live notes**
**Related:** `../asr-mcp` (live ASR), `AGENTS.md` (architecture and invariants)

---

## 1. Goal

Add a **Live notes** tab to the dashboard that turns live speech into one growing,
enriched note per session:

1. The browser captures microphone audio and streams it through mem-mcp to the
   existing `asr-mcp` service.
2. Transcript segments are stored individually against a transcription and shown
   in the **Transcription** sub-tab, where they can be edited or deleted.
3. When the user pauses, the unconsumed segments are sent to the local LLM
   (`MEM_EXTRACT_MODEL`, i.e. granite) together with the existing note and the
   titles of facts already referenced. The model returns the **full updated
   note**, which is saved immediately as an ordinary `Fact` in category `Notes`.
4. The note is linked to the transcription and to every referenced fact, and is
   searchable, visible in Memories/Graph, and included in backups.

The note is the durable artefact. The transcript is evidence that can be
corrected without rewriting what has already been distilled.

---

## 2. Decisions already made

| Topic | Decision |
|---|---|
| ASR transport | Browser ⇄ mem-mcp WebSocket; mem-mcp ⇄ `asr-mcp` WebSocket. The ASR key never reaches the browser. |
| ASR credentials | **Per-user token**, entered under Setup. If unset, the Live notes tab shows instructions and the record controls are disabled. |
| Pause semantics | One **running note** per transcription, revised at every pause. Not one note per pause. |
| Note persistence | **Save immediately** on summarise. The note is a normal Fact and can be edited or deleted afterwards. |
| Deleting a summarised segment | **Allowed with a warning.** The note keeps its text snapshot; deletion only changes what a future reprocess sees. |
| LLM | Existing local granite (`MEM_EXTRACT_MODEL`), via `get_llm_response(model=...)`. |
| Client scope | Referenced-fact candidates come from the transcription's identified client only. |

### Hard priority rule

> Transcription has priority over processing. If the user continues taking notes
> while processing has already started, voice processing MUST take priority.

Consequences that are not optional:

- **No LLM or embedding call runs while a recording socket is open.** Recording
  start preempts a live-note job before its commit phase.
- The WebSocket receive loop never awaits Neo4j or the LLM. Audio forwarding is
  the first await on every frame.
- A preempted job leaves its remaining segments unconsumed, and the next pause
  continues from there.

---

## 3. Non-goals (v1)

- Sharing transcriptions or notes through the sharing feature.
- Diarisation correction, speaker renaming across a whole transcription, or
  voiceprint enrolment.
- Editing the transcript through the LLM (whisper-style cleanup).
- Multiple concurrent recordings per user, or multiple notes per transcription.
- Indexing raw transcript text in Qdrant. Notes are indexed because they are
  Facts; transcripts are Neo4j-only.
- Retaining audio. Only text segments are stored.

---

## 4. Architecture

```
browser mic
  └─ AudioWorklet (16 kHz int16, 64 ms blocks)
       └─ WS /api/live/stream/{transcriptionId}      (mem-mcp, session cookie)
            ├─ forward raw PCM frame ─────────────► asr-mcp /api/asr/ws/stream
            │                                         (X-API-Key: user token)
            ├─ ASR JSON transcript ──────────────────┘
            ├─ forward to browser first
            └─ enqueue segment persistence (writer task)

pause:
  browser {"type":"pause"}
    └─ close upstream → asr flushes tail + stats
         └─ drain writer queue
              └─ {"type":"paused"} → POST /summarize (background job)

summarise job:
  unconsumed segments + running note + referenced fact titles
    + client-scoped candidate fact titles
      └─ get_llm_response(MEM_LIVE_MODEL) → {title, text, referencedFactIds}
           └─ validate → db_update_memory(note) → REFERENCES / consumption
```

### Components

| File | Responsibility |
|---|---|
| `mem-mcp/live_notes.py` | **Stdlib-only** pure helpers: prompt assembly, candidate ranking, note validation, output budget, time-offset maths, origin check, priority gate. Importable without DB drivers, like `matching_utils.py`. |
| `mem-mcp/live_manager.py` | Neo4j CRUD for transcriptions/segments, note linking, summarise/reprocess orchestration, job status. Imports `common`, re-exported through `memory.py`. |
| `mem-mcp/gui.py` | REST routes, ASR-token settings routes, worklet route, WebSocket proxy. |
| `mem-mcp/sessions.py` | Per-user ASR token table in `sessions.db`. Stdlib-only, stays importable. |
| `mem-mcp/templates/dashboard.html` | Live notes tab, Setup token card, capture client. |
| `mem-mcp/common.py` | Neo4j indexes for the new labels. |
| `mem-mcp/fact_manager.py` | Note-deletion cleanup hook; candidate query may live here or in `live_manager.py`. |

---

## 5. Data model

All nodes carry `userId` and `updatedAt`, because the backup fingerprint is built
from those stamps. No Qdrant points exist for transcriptions or segments.

### `:Transcription`

```
id, userId, name, status, language,
createdAt, updatedAt,
clientId?, clientName?, contextId?, contextName?,
noteId?
```

`status`: `idle | recording | paused | processing | deferred | error`.
`noteId` is the running note Fact id, set on first summarise.

### `:TranscriptionSegment`

```
id, userId, transcriptionId, seq,
startSec, endSec, speaker, text,
createdAt, updatedAt,
editedAt?, consumedBy?, consumedAt?
```

Edges:

```
(:Transcription)-[:HAS_SEGMENT]->(:TranscriptionSegment)
(:Fact:Notes)-[:FROM_TRANSCRIPTION]->(:Transcription)
(:Fact:Notes)-[:REFERENCES]->(:Fact)          // one-way
```

- `REFERENCES` is written **one-way** from the note to the fact. Do not use
  `db_link_facts`, which creates Fact↔Fact edges in both directions.
- `db_unlink_facts` already matches a Fact↔Fact relationship in both directions,
  so the existing note UI can remove a reference.
- `consumedBy` names the note Fact and `consumedAt` is its timestamp. A segment
  is reprocessed only when `consumedBy` is null.
- Editing a segment clears nothing. It changes what a future reprocess reads; the
  note is a snapshot, not a projection.
- Deleting the note clears `consumedBy`/`consumedAt` on its segments and
  `Transcription.noteId`. The segments become eligible again.

### Note Fact

Created through `db_add_memory`, updated through `db_update_memory`, so it gets
embeddings, chunking, search and backup for free:

```
category = "Notes"
name     = note title
text     = note markdown
metadata = {source: "live_notes", transcriptionId, segmentIds: [...], live: true}
```

Scope is written through the normal `clientId`/`contextId` arguments. The note is
an ordinary Fact after creation; editing or deleting it from Memories is allowed.

---

## 6. Summarise contract

### Input

Exactly these four blocks, and nothing else:

1. **Existing note** — title and markdown, from the Fact. No other fact bodies.
2. **New segments** — only segments whose `consumedBy` is null, formatted as
   `[mm:ss] Speaker: text`.
3. **Referenced facts** — id + title of facts already linked by `REFERENCES`.
4. **Candidate facts** — id + title only, restricted to the transcription's
   **primary client** (`FOR_CLIENT`; `IN_CONTEXT` when a project is selected).
   Never another client's `RELEVANT_TO` rows. Category `Notes` is excluded.
   Ranked by title-token overlap with the new segments, capped by
   `MEM_LIVE_FACT_LIMIT`, with already-referenced facts always included.

Plus the client/project names, so the model can name them in the note.

### Output

The model returns **the full updated note**, not a patch:

```json
{"title": "...", "text": "...", "referencedFactIds": ["..."]}
```

Validation, all in `live_notes.py`:

- JSON object only; markdown fences stripped.
- `text` non-empty and within `MEM_LIVE_NOTE_MAX_CHARS`; title bounded.
- **Every referenced id must be in the allowed candidate set.** Unknown ids are
  dropped, never created. The model cannot mint facts or scope names.
- Existing references are unioned with the validated new ones; a reference cannot
  be silently dropped by an update.
- Empty `text` is a failed update, not a reason to consume segments.

### Budget and long pauses

- `num_predict` is computed as `context_tokens - estimated prompt tokens`, with a
  floor of `MEM_LIVE_MIN_NUM_PREDICT`. A budget below the floor refuses the update
  with a 400 naming the segment count and the estimate. It is never a fixed
  constant that silently truncates the note.
- New segments longer than `MEM_LIVE_WINDOW` are split with `text_windows()` and
  applied **sequentially**, so each pass sees the previous pass's note.
- If the note approaches `MEM_LIVE_NOTE_MAX_CHARS`, a compaction pass preserves
  headings and bullets. Prefix slicing is forbidden.
- No `text[:N]` anywhere on this path.

### Failure behaviour

| Failure | Result |
|---|---|
| LLM timeout / unreadable JSON | Nothing consumed; job status `error`; segments remain eligible. |
| Valid but unchanged note | Segments consumed; UI reports "no change". |
| Recording starts during the LLM phase | Job cancelled; nothing consumed; status `deferred`. |
| Recording starts during commit | Commit is shielded and finishes; audio was never blocked. |
| Ollama unreachable | 503 with the reason, not a silent no-op. |

### Reprocess

`POST /api/live/transcriptions/{id}/reprocess` with `{mode: "unconsumed" | "all"}`.

- `unconsumed` behaves exactly like a pause summarise.
- `all` rebuilds the running note **in place**, window by window, keeping the
  same Fact id. It resets the text first and recomputes references. The UI warns
  that manual edits to the note will be lost.
- Both are background jobs with status polling, and both answer `409` while a
  recording is open.

---

## 7. Priority gate

`LivePriorityGate` in `live_notes.py`, one per user:

```
begin_recording(user_id)   # recording = True, generation += 1,
                           # cancel the current job only if phase == "llm"
end_recording(user_id)     # recording = False
current_generation(user_id)
assert_idle(user_id)       # raises LiveRecordingActive -> 409
```

- The WS handler calls `begin_recording` **before** connecting upstream, and
  `end_recording` on pause/stop/error. The upstream connect does not wait for a
  job to finish.
- A job captures `generation` after it starts. Immediately before its commit it
  re-checks; a changed generation aborts with nothing consumed.
- The commit phase runs under `asyncio.shield`, preserving the
  Qdrant-before-Neo4j write order. It is short and never includes an LLM call.
- A job cancelled mid-LLM records `deferred` and keeps every previously completed
  window consumed.
- `summarize` and `reprocess` answer `409` when a recording is open. A queued job
  that finds a recording when it starts refuses to begin.
- Recording uses a per-user live-notes lock, **not** `claim_maintenance`:
  note-taking must not be blocked by a backup, and a backup must not block audio.
- With `MEM_LIVE_UNLOAD_ON_RECORD=1`, recording start unloads the live model from
  VRAM. asr-mcp and Ollama share one 4 GB card; the next pause pays a cold load.

---

## 8. ASR proxy and token

### Token storage

- Table `asr_tokens(user_id TEXT PRIMARY KEY, token TEXT NOT NULL, updated_at TEXT NOT NULL)`
  in `sessions.db`.
- `sessions.py` gains `set_asr_token`, `get_asr_token`, `clear_asr_token`,
  `asr_token_hint` (last four characters only).
- The token is stored in plaintext because the proxy must replay it to asr-mcp.
  The database is inside the `MEM_SESSION_DIR` bind mount. No API ever returns
  the token; only the hint and `set: true|false`.
- There is no server-wide fallback key: a user with no token cannot record.

### Endpoints

```
GET    /api/settings/asr-token          -> {set, hint}
PUT    /api/settings/asr-token          -> {set, hint}
DELETE /api/settings/asr-token
POST   /api/settings/asr-token/test     -> accepted | rejected | unreachable
```

The test opens a throwaway WebSocket to asr-mcp with the stored token and reports
which failure occurred. It distinguishes "ASR rejected the token" from "ASR is
unreachable", because those have different fixes.

### WebSocket

`WS /api/live/stream/{transcriptionId}`

- Authenticate from `websocket.scope["session"]` (set by
  `VaultSessionMiddleware`); `auth_guard` is HTTP-only and does not run here.
- Verify the transcription belongs to the session user before any upstream
  connect.
- Check `Origin` against `BASE_URL` to prevent cross-site WebSocket hijacking.
  An absent or foreign origin closes with `4403`.
- Close codes: `4401` no session, `4403` wrong owner/origin, `4409` no ASR token,
  `4502` upstream unreachable, `1008` upstream rejected the token.
- Upstream URL: `MEM_ASR_WS_URL`, default
  `ws://host.docker.internal:8087/api/asr/ws/stream`. asr-mcp publishes 8087 on
  the host and lives on its own compose network, so mem-mcp needs
  `extra_hosts: ["host.docker.internal:host-gateway"]`.
- Send `X-API-Key: <user token>` and `?language=<...>`.
- Browser frames are the legacy raw PCM form: 8-byte header
  `struct.pack("<II", 0, sequence)` + int16 LE PCM 16 kHz mono, as produced by the
  vendored `live-worklet.js`.
- On `transcript`: forward to the browser **first**, then enqueue persistence.
  The writer assigns `seq`, adds the current connection's time offset, and writes
  the segment. The receive loop never awaits Neo4j.
- Pause: close upstream, persist the tail and `stats`, drain the queue, then send
  `{"type":"paused"}`. Resume opens a **new** upstream connection with
  `offset = max(endSec)` because ASR timestamps restart per connection.

### Capture client

Vendor `live-worklet.js` (198 lines) unchanged, served from an authenticated
`GET /api/live/worklet.js` (the app has no `StaticFiles` mount). The page-side
client is a new, small state machine (~250 lines), not a copy of asr-mcp's
1586-line `live.js`: `getUserMedia` → `AudioContext` → worklet → pack → `ws.send`,
plus start/pause/resume/stop and reconnect messaging.

---

## 9. REST surface

All routes are `_require_user` and scoped by `userId`; a record owned by someone
else answers **404**, not 403.

```
GET    /api/live/transcriptions
POST   /api/live/transcriptions                 {name?, clientId?, contextId?, language?}
GET    /api/live/transcriptions/{id}            transcription + segments + note + job status
PATCH  /api/live/transcriptions/{id}            {name?, clientId?, contextId?, language?}
DELETE /api/live/transcriptions/{id}?delete_note=0|1
PATCH  /api/live/transcriptions/{id}/segments/{sid}   {text?, speaker?}
DELETE /api/live/transcriptions/{id}/segments/{sid}
POST   /api/live/transcriptions/{id}/summarize
POST   /api/live/transcriptions/{id}/reprocess  {mode}
GET    /api/live/transcriptions/{id}/status     job + transcription status
```

Notes are also readable through the existing `/api/memories` routes. Changing the
transcription's client or project re-scopes the running note with
`db_set_fact_scope`.

---

## 10. UI

New top-level tab **🎙️ Live notes** (`data-tab="live"`, `#page-live`), full width
like Memories/Diary.

```
┌ sessions rail ─┬ toolbar: name · client · project · language · status ──────┐
│ ＋ New session │ 🎙 Start  ⏸ Pause & summarise  ▶ Resume  ⏹ Stop            │
│ session A      ├────────────────────────────────────────────────────────────┤
│ session B      │ [Transcription] [Notes]                                    │
│                │ segments with time, speaker, inline ✏️ / 🗑️, "summarised"  │
│                │ badge; or rendered note with referenced-fact badges         │
└────────────────┴────────────────────────────────────────────────────────────┘
```

- Sub-tabs use the same class contract as the Service page (`.live-panel`,
  `.live-panel.active`), never inline `display`.
- If the ASR token is unset: a numbered instruction panel and a button that
  switches to Setup. Start is disabled; the token form is not duplicated here.
- Segment edit is inline; delete warns that the note keeps its snapshot and only
  reprocessing changes.
- Notes pane shows the referenced-fact badges, a **Rebuild note** button, and the
  job state (`updating`, `deferred — recording resumed`, `error`).
- Poll `/status` only while a job is in flight, then stop. No idle polling.
- Persist `activeLiveTranscriptionId` and `liveTab` in
  `mem_vault_session_state`; `switchTab` already falls back safely if the page is
  missing.
- Mobile: stack the rail above the panes at <900px, release zero flex bases, and
  keep one scroll region per pane. Extend `test_mobile_layout.py`.
- Validate the template's inline JS with `node --check` after every edit.

---

## 11. Configuration

Add every knob to **both** `.env.example` and the compose `environment:` list, or
it is unreachable (`test_env_wiring`).

| Variable | Default | Purpose |
|---|---|---|
| `MEM_ASR_WS_URL` | `ws://host.docker.internal:8087/api/asr/ws/stream` | Upstream ASR WebSocket. |
| `MEM_LIVE_MODEL` | `MEM_EXTRACT_MODEL` | LLM for note updates. |
| `MEM_LIVE_FACT_LIMIT` | `100` | Candidate fact titles per update. |
| `MEM_LIVE_CONTEXT_TOKENS` | `16332` | Must track `OLLAMA_CONTEXT_LENGTH`. |
| `MEM_LIVE_MIN_NUM_PREDICT` | `1500` | Refusal floor for the output budget. |
| `MEM_LIVE_NOTE_MAX_CHARS` | `12000` | Bounds the note and the output budget. |
| `MEM_LIVE_WINDOW` | `6000` | Chars per sequential update window. |
| `MEM_LIVE_UNLOAD_ON_RECORD` | `1` | Free VRAM for ASR before recording. |

Also add `websockets` to `requirements.txt` (browser server and outbound client;
uvicorn is not installed with `[standard]`).

---

## 12. Testing

| Suite | Additions |
|---|---|
| `test_live_notes.py` (new) | Calls pure helpers: prompt contents, candidate ranking and client filtering, id validation, offset maths, budget floor, origin check, gate preemption/commit semantics. |
| `test_sessions.py` | Token store round-trip, hint, isolation, clear, empty refusal. |
| `test_env_wiring.py` | New variables documented and forwarded. |
| `test_cypher_safety.py` | Lints new Cypher (list parameters, `scopeManual`-style coalesce, no `FOREACH` self-reference). |
| `test_mobile_layout.py` | New tab/layout selectors, stacked panes, inline-style traps. |
| `test_auth_guard.py` | WS auth is not covered by `auth_guard`; pin the origin/session checks and that no token route returns plaintext. |

Plus `py_compile` on every touched Python module, `node --check` on the extracted
dashboard script, and `git -c core.whitespace=cr-at-eol diff --check`.

---

## 13. Task index

The authoritative checklist lives in [`tasks.md`](../tasks.md).
This table is only a map.

| # | Task | Depends on |
|---|---|---|
| L1 | Branch, dependencies, configuration | — |
| L2 | Per-user ASR token store | L1 |
| L3 | Pure helpers + priority gate + tests | L1 |
| L4 | Neo4j model, CRUD, indexes, note helpers | L3 |
| L5 | WebSocket proxy to asr-mcp | L2, L3, L4 |
| L6 | Browser capture client and worklet | L1 |
| L7 | Summarise/reprocess jobs and LLM contract | L3, L4 |
| L8 | REST routes and token endpoints | L2, L4, L7 |
| L9 | Setup ASR-token card | L8 |
| L10 | Live notes tab UI and mobile layout | L5, L6, L8, L9 |
| L11 | Docs, full verification, commit/push | all |

---

## 14. Epic acceptance criteria

- [ ] A user with a valid ASR token can start, pause, resume and stop a recording
      without losing audio while processing is in flight.
- [ ] Pause produces an updated note, saved immediately as a Fact in category
      `Notes`, scoped to the selected client/project.
- [ ] Referenced facts are limited to the identified client's primary scope and
      linked one-way from the note.
- [ ] Only unconsumed segments and fact titles are sent to the LLM; no fact bodies
      and no already-consumed transcript go into the prompt.
- [ ] Segments can be edited and deleted, with the documented warning.
- [ ] A transcription can be reprocessed in `unconsumed` or `all` mode without
      changing the note's identity.
- [ ] Starting a recording preempts a running job before commit; no LLM or
      embedding call runs while the socket is open.
- [ ] The token never leaves the server, and a user without one sees actionable
      instructions.
- [ ] Transcriptions are private to their owner and excluded from sharing.
- [ ] The full regression suite passes and the template JS validates.

---

## 15. Risks and deployment notes

- **GPU contention** is the main operational risk: asr-mcp (CUDA) and Ollama
  share a 4 GB card. `MEM_LIVE_UNLOAD_ON_RECORD` exists for that; a pause then
  pays a cold model load (9–20 s).
- **Container networking:** mem-mcp and asr-mcp are on different compose
  networks. Without `host.docker.internal`, point `MEM_ASR_WS_URL` at the public
  `wss://…/asr-mcp/api/asr/ws/stream` path, which nginx already proxies with a
  WebSocket upgrade.
- **WebSocket auth is a new surface.** `auth_guard` does not run for WebSocket
  scopes; the handler must authenticate and authorize itself, including `Origin`.
- **Transcript provenance:** ASR timestamps restart on every reconnect (pause →
  resume), so the stored offset is part of the segment record. Getting this wrong
  silently interleaves the transcript.
- **No audio retention** means a mis-transcribed segment can only be corrected by
  hand. That is deliberate: it keeps the feature out of the backup-size problem.
