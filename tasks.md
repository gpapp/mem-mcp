# Open Tasks

Only open work lives here. Completed tasks move to `task_history.md` (see
`AGENTS.md` → Task Tracking).

---

# Epic: Live notes — transcription to enriched notes

**Plan:** [`plans/live-notes.md`](plans/live-notes.md)
**Branch:** `feature/live-notes`
**Status:** in progress

Build a **Live notes** tab that streams microphone audio through mem-mcp to
`asr-mcp`, stores transcript segments individually, and turns each pause into an
update of one running note enriched with client-scoped facts.

**Hard rule for every task:** while a recording socket is open, no LLM or
embedding call may run. Recording start preempts a running note job before its
commit. Audio forwarding never waits on processing.

---

## L4. Neo4j model, CRUD, indexes, note helpers

- [ ] Add indexes in `common.get_neo4j()` for `:Transcription(userId)`,
      `:TranscriptionSegment(userId)` and `:TranscriptionSegment(transcriptionId)`.
- [ ] Create `mem-mcp/live_manager.py` and re-export its public functions through
      `memory.py`.
- [ ] Implement transcription CRUD: create, list, get, rename/update scope,
      delete, all scoped by `userId`.
- [ ] Implement segment append (server-side), edit text/speaker, delete, and
      list ordered by `seq`.
- [ ] Store one running note: `FROM_TRANSCRIPTION`, `REFERENCES` one-way,
      `Transcription.noteId`, and `consumedBy`/`consumedAt` on segments.
- [ ] Clear note links and consumption when the note Fact is deleted (hook in
      `db_delete_memory`).
- [ ] Stamp `updatedAt` on both endpoints of every new edge, and on every
      create/update/delete, so the backup fingerprint sees the change.
- [ ] Return `404`-shaped results for another user's ids; never leak existence.

**Acceptance:** `test_cypher_safety.py` lints every new query (list parameters,
`FOREACH` self-reference, aliases). Editing or deleting a segment leaves the note
text untouched. Deleting the note releases its segments.

---

## L5. WebSocket proxy to asr-mcp

- [ ] Add `WS /api/live/stream/{transcriptionId}` in `gui.py`.
- [ ] Authenticate from `websocket.scope["session"]`; `auth_guard` does not cover
      WebSocket scope. Verify ownership before connecting upstream.
- [ ] Enforce `Origin` against `BASE_URL`; close with `4403` on mismatch.
- [ ] Resolve the caller's ASR token from `sessions.get_asr_token`; close with
      `4409` and an actionable `{"type":"error"}` message if unset.
- [ ] Connect to `MEM_ASR_WS_URL` with `X-API-Key` and `?language=`, passing the
      legacy 8-byte PCM frames through unchanged.
- [ ] Forward every ASR JSON message to the browser **before** persisting; use a
      dedicated writer task and an `asyncio.Queue` so the receive loop never
      awaits Neo4j.
- [ ] Assign `seq` from the stored maximum and add the connection time offset to
      `start`/`end`.
- [ ] Pause protocol: close upstream, persist tail/`stats`, drain the queue, send
      `{"type":"paused"}`. Resume opens a new upstream connection at
      `offset = max(endSec)`.
- [ ] Call `begin_recording` before the upstream connect and `end_recording` on
      pause/stop/error.
- [ ] With `MEM_LIVE_UNLOAD_ON_RECORD=1`, unload the live model after recording
      starts; never before the socket is accepted.
- [ ] Map failures to close codes `4401`, `4403`, `4409`, `4502`, `1008` and log
      the reason.

**Acceptance:** tokenless and cross-user connects are refused before any upstream
connection. A fake ASR server receives audio, returns `transcript`/`stats`, and
the proxy persists ordered segments with correct offsets. A recording opened
while a fake LLM job is running cancels that job before commit.

---

## L6. Browser capture client and worklet

- [ ] Vendor `live-worklet.js` unchanged from
      `/data/opt/asr-mcp/asr_mcp/static/live-worklet.js`.
- [ ] Serve it from an authenticated `GET /api/live/worklet.js` (there is no
      `StaticFiles` mount).
- [ ] Implement the page-side capture state machine (~250 lines, not a copy of
      asr-mcp's 1586-line `live.js`): `getUserMedia` → `AudioContext` → worklet →
      pack `struct.pack("<II", 0, sequence)` + PCM → `ws.send`.
- [ ] Start, pause, resume, stop; clear timers and audio nodes on stop; surface
      permission errors and a closed socket as visible messages.
- [ ] Do not start capture while a note job is running without first triggering
      the priority gate; the server is authoritative, the client just reconnects.

**Acceptance:** the worklet loads from the authenticated route, microphone denial
is reported in the UI, and the packed frames match the asr-mcp legacy contract.

---

## L7. Summarise and reprocess jobs

- [ ] Implement the background job in `live_manager.py`: one per user, status
      `queued | running | deferred | error | done`.
- [ ] Refuse to start while `LivePriorityGate.assert_idle` fails, or while
      another job for the same user is running.
- [ ] Load only segments with `consumedBy` null; load the running note title/text
      only.
- [ ] Build candidates from the transcription's **primary client** (`FOR_CLIENT`;
      `IN_CONTEXT` when a project is selected), excluding `Notes`, never another
      client's `RELEVANT_TO` rows; pass titles/ids only.
- [ ] Call `get_llm_response(model=MEM_LIVE_MODEL, num_predict=<computed budget>)`;
      do not pass a constant budget.
- [ ] Create the note on first update with `db_add_memory`; update it in place
      with `db_update_memory`, keeping the Fact id.
- [ ] Union validated references, rewrite `REFERENCES`, mark segments consumed.
- [ ] Re-check the gate generation immediately before commit; shield the commit
      so Qdrant-before-Neo4j ordering cannot be torn apart.
- [ ] Long pauses: sequential `text_windows` passes; note compaction above
      `MEM_LIVE_NOTE_MAX_CHARS`.
- [ ] `reprocess` modes `unconsumed` and `all`; `all` rebuilds in place.
- [ ] LLM/JSON failure consumes nothing and reports `error`; a valid unchanged
      note consumes and reports "no change".
- [ ] Changing the transcription's client/project re-scopes the note with
      `db_set_fact_scope`.

**Acceptance:** with a fake LLM, the prompt contains only the four allowed blocks;
an unknown referenced id is dropped; a timeout leaves every segment eligible;
recording start during the LLM phase leaves status `deferred` with nothing
consumed; commit continues if recording starts after it began; one note is reused
across pauses.

---

## L8. REST routes and token endpoints

- [ ] Add the routes listed in the plan section 9, all `_require_user` and
      `userId`-scoped.
- [ ] Add token routes:
      `GET/PUT/DELETE /api/settings/asr-token` and
      `POST /api/settings/asr-token/test`.
- [ ] `PUT` validates the token by opening a short asr-mcp handshake before
      storing it; `test` distinguishes accepted / rejected / unreachable.
- [ ] Never return the token; return only `{set, hint}`.
- [ ] `summarize` and `reprocess` answer `409` with a human-readable reason while
      a recording is open.
- [ ] Route JSON errors through the existing `_conflict` / `_service_unavailable`
      helpers so the reason reaches the client body.
- [ ] Add the Pydantic request models next to the existing ones; no `BaseModel`
      with unbounded fields.

**Acceptance:** cross-user ids answer 404; token endpoints never echo plaintext;
`summarize` during a recording answers 409; a fake asr handshake pins accepted,
rejected and unreachable outcomes.

---

## L9. Setup ASR-token card

- [ ] Add a **Live notes — ASR access token** card to the Setup page.
- [ ] Password input, Save, Test, Clear; show the stored hint, never the token.
- [ ] Explain that the token is the user's own asr-mcp API key and that recording
      is unavailable without it.
- [ ] Reuse the existing toast/error handling; no new inline styles on elements
      that mobile CSS must override.

**Acceptance:** saving, testing and clearing work from the page; the token field
is never populated from the server; `node --check` passes.

---

## L10. Live notes tab and mobile layout

- [ ] Add the top-level **🎙️ Live notes** tab (`data-tab="live"`, `#page-live`).
- [ ] Three-pane layout: session rail, toolbar with client/project/language and
      record controls, then the two sub-tabs **Transcription** and **Notes**.
- [ ] Token-unset empty state: numbered instructions and a button that switches
      to Setup; Start disabled.
- [ ] Segment list: time, speaker, text, inline edit and delete with the warning
      that the note keeps its snapshot.
- [ ] Notes pane: rendered markdown, referenced-fact badges that navigate to the
      fact, **Rebuild note**, and the job status (`updating`, `deferred`,
      `error`).
- [ ] Poll `/status` only while a job is in flight; stop on a terminal state.
- [ ] Persist `activeLiveTranscriptionId` and `liveTab` in
      `mem_vault_session_state`; keep `switchTab`'s safe fallback.
- [ ] Update `switchTab` lazy loading and `init()`; do not dereference a missing
      page.
- [ ] Mobile: stack rail and panes below 900px, release zero flex bases, one
      scroll region per pane, no inline `display`/`width` that beats the media
      query.
- [ ] Wire capture controls to the L6 client and send `{"type":"pause"}` before
      starting summarise.

**Acceptance:** `node --check` passes on the template script; `test_mobile_layout.py`
pins the new selectors; starting a recording while a note update is in flight
shows "paused — recording resumed" rather than blocking the microphone.

---

## L11. Documentation, full verification, commit and push

- [ ] Add a **Live notes** section to `AGENTS.md`: architecture, data model,
      priority invariant, token handling, ASR networking, prompt contract, and
      the `live_notes.py` stdlib-only rule.
- [ ] Add `test_live_notes.py` to the documented regression command list.
- [ ] Note the deployment steps: pull/build, `extra_hosts`, asr-mcp reachability,
      and the per-user token.
- [ ] Run `py_compile` on every touched module, `node --check` on the dashboard
      script, `git -c core.whitespace=cr-at-eol diff --check`, and the full test
      suite command.
- [ ] Commit in logical slices and push `feature/live-notes`.
- [ ] Move this epic's completed tasks into `task_history.md` with the commit
      that shipped each one; leave nothing ticked here.

**Acceptance:** the full suite is green, the branch is pushed, and the epic in
`plans/live-notes.md` is marked shipped.
