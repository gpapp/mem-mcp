# Task History

Completed work, newest first. Open work lives in `tasks.md`; the convention is
recorded in `AGENTS.md` (Task Tracking).

---

# Live notes — a pause hung up on the last utterance, and the widget reached the Live rail (2026-10-27)

- **The report was "it seems like the voice is cut off at pause", and the pause
  path was closing the socket instead of ending the stream.** Two mechanisms in
  asr-mcp conspired, and neither is visible from our side: `TurnDetector` ends a
  turn on **320 ms of sub-threshold audio**, never on a wall clock, so stopping
  the microphone leaves the last utterance open until disconnect; and the flush
  that disconnect triggers produces transcripts that are then written to a
  socket `websockets` has already closed — asr-mcp swallows its own send
  failure, so both ends were silent about it.
- **The fix is silence, then an explicit flush, then wait for `stats`, then
  close.** `pause_flush_frames()` in `live_notes.py` builds 480 ms
  (`MEM_LIVE_TAIL_SILENCE_MS`) of digital silence as legacy mic frames followed
  by the 24-byte `MSG_FLUSH` control frame. The order is load-bearing: the
  silence is what closes the detector's turn, and `MSG_FLUSH` makes asr-mcp
  **skip its own `detector.flush()`** — so flushing first discards the utterance
  instead of saving it. `stats` is the only end-of-stream ack the protocol has
  (sent after `queue.join()`), so the proxy now awaits it via a `drained` event
  before closing, and `asr_reader` sets that event in a `finally` as well as on
  `stats` — otherwise a stream that ended without one would hold every pause for
  the full `_LIVE_TAIL_DRAIN_SECONDS` (20 s) timeout.
- **The event is re-created per connection.** A reused `asyncio.Event` is
  already set from the previous stream, so the next pause would return before
  its own tail had been transcribed — a cut-off that looks exactly like the one
  being fixed.
- **`PauseTailTests` (7) splits where the property lives**: the builder is
  *called* (frame order, 24-byte shape, channel 0, all-zero PCM, the
  320 ms…2 s silence window, `silence_ms=0`), and the ordering is read from the
  single call site because a swapped pair of `await`s is invisible in a diff.
  Both halves were mutation-checked — closing before the flush, and deleting
  `drained.set()` from the `stats` branch alone — and both bit.
- **The status widget is now on the Live notes rail too.** It is one markup
  line: `renderServerStatus` has always used `querySelectorAll('.srv-status')`,
  so a fourth copy needed no JS. The layout test could not have seen it —
  `_rail_markup()` matched `<div class="…">` only and the Live rail is an
  `<aside>` — so the regex is now `<(div|aside) …>` closed by a backreference,
  `_RAILS` names four rails, and the "no sibling strip" test covers
  `.live-layout` as well. The Live rail already had
  `display:flex; flex-direction:column; overflow:hidden`, so no CSS changed.
  Mutation-checked by removing the widget line: the failure names
  `.live-sidebar` rather than reporting the rail missing.

1289 tests OK, `node --check` clean on the extracted dashboard script,
`py_compile` clean, `git diff --check` clean.

# Live notes — the microphone produced no audio at all (2026-10-27)

- **The report was "recording starts but no transcription is happening", and the
  cause was one swapped `await` in the browser.** `LiveCapture._ensureMic()`
  called `getUserMedia` (a permission prompt) and only then constructed the
  `AudioContext`. Chrome grants one *user activation* per click and any `await`
  spends it, so the context was created without activation, started `suspended`,
  and a suspended context never runs the audio graph — `process()` was never
  called, the worklet posted nothing, and no byte ever reached the socket. The
  recorder looked live, the socket was open, asr-mcp was healthy, and nothing
  logged an error anywhere.
- **asr-mcp's own client already knew this**, which is where the answer came
  from rather than from another round of protocol reading:
  `asr_mcp/static/live.js::ensureAudio()` carries a docstring describing exactly
  this failure and throws when the context is not `running`. Our plan had read
  the capture code for the *frame format* and copied the worklet, and skipped
  the one function whose whole subject is the context's lifecycle.
- **The fix is order plus a refusal.** `_ensureAudio()` now runs first (create,
  `resume()` if suspended, **throw** naming the audio permission if the state is
  still not `running`, then `addModule`), and only then the microphone and the
  socket. A browser that blocks audio now says so instead of recording silence.
- **Silence is counted at both ends, because "no transcript yet" is also what a
  quiet room looks like.** `blocksSent` + `_watchForSilence()` toast after 12 s
  in the browser; `audio_blocks` + `audio_watchdog` WARNING after 20 s in
  `gui.py`, and `audioBlocks=` in the `recording stopped` line. This is the same
  shape as asr-mcp's `packets: 0` diagnostic, and it is what would have made this
  bug a one-line log read.
- **Two guards, both mutation-checked.** `LiveCaptureAudioOrderTests` runs
  `start()` against a fake audio graph through the `_NODE_HARNESS` borrowed from
  `test_share_endpoints` and asserts the context precedes the microphone and the
  socket (swapping the calls back fails with `1 not less than 0`), and that a
  stuck context rejects before any socket exists (disabling the state check
  fails with "a suspended context started anyway"). `AudioWatchdogTests` pins
  the counter to the forwarded path and the watchdog's create/cancel pair.
- 1282 tests pass; `node --check` on the extracted dashboard script, `py_compile`
  and `git diff --check` are clean.

---

# Live notes — the session middleware was inside the gate it had to wrap (2026-10-27)

- **The deploy after the path fix answered every request with a 500:**
  `AssertionError: SessionMiddleware must be installed to access request.session`
  out of `auth_guard`. Not a new bug — `web_app.add_middleware` inserts at
  position 0, so the middleware registered **last** is the outermost one, and
  `VaultSessionMiddleware` was registered *above* the two
  `@web_app.middleware("http")` gates. It therefore ran inside them, and the gate
  read `request.session` before anything had set `scope["session"]`.
- **Two defects, one hiding the other.** The ordering had been wrong for as long
  as the middleware existed, but the same gate's `startswith("/gui")` /
  `startswith("/api")` tests were false in the documented deployment
  (`root_path`), so it never ran far enough to read the session. Repairing the
  path is precisely what armed the second one. The general lesson is recorded in
  `AGENTS.md`: when a guard that has never actually fired is repaired, the code
  behind it has never been exercised either.
- **The registration moved below `log_gui_requests`**, so the stack is
  `CORS -> session -> log_gui_requests -> auth_guard -> routes`, and
  `assert_middleware_order(web_app)` now runs in `server.py` after the CORS
  middleware is added — a reorder refuses to start instead of serving a 500 per
  request. **That check belongs in `server.py` and not in `gui.py`:** only after
  `server.py` has added its middleware is the stack complete, and a check placed
  beside the registration would not see a gate registered further down the file.
- **Guards:** `test_auth_guard.MiddlewareOrderTests` asserts the session
  registration line is below both gate decorators (a source-order property, since
  the functions are indistinguishable once FastAPI has built the stack), and
  `MiddlewareOrderGuardTests` **calls** `assert_middleware_order` with the good
  order, the bad order (accepts CORS outside, refuses the session inside), no
  gates, and no session middleware. Both were re-injected: restoring the old
  registration fails the first, inverting the index comparison in the guard fails
  the second.
- Full suite: **1277 tests**, all passing. `py_compile` clean on `gui.py`,
  `server.py`, `test_auth_guard.py`.

---

# Live notes — recording start, the proxy Upgrade, and a cheap ASR-key probe (2026-10-27)

- **"Starting the recording failed" was nginx, not the app.** The handshake
  arrived at uvicorn as a plain HTTP `GET` — `Upgrade`/`Connection` are
  hop-by-hop headers, and the GUI location named only `Connection ""` — so no
  `WebSocketRoute` matched, the request fell through to the catch-all mount and
  the browser got **`McpAuthGuard`'s 401** about an access key it was never
  meant to send. `nginx_snippet.conf` now has a dedicated
  `location /mem-mcp/api/live/stream/` with `proxy_http_version 1.1`, the
  `Upgrade`/`Connection "upgrade"` pair, `proxy_buffering off` and a 24h read
  timeout. No `map` block: the snippet is included inside `server {}`.
- **The app now diagnoses that misconfiguration itself.** A `GET` on the same
  path answers **426 Upgrade Required** with a detail naming the two headers,
  because a 401 sends an operator looking at the wrong credential.
- **Reading the log is what found it, and one line was lying.** `[GUI] GET`
  looked like proof the browser sent a plain GET; it is `log_gui_requests`
  printing `request.url.path`, which includes `root_path`. The real defect is
  that `auth_guard` compared the *same* value against `/gui` and `/api` — with
  `root_path="/mem-mcp"` both prefixes are false, so the documented outer gate
  was answering every request with a redirect and each handler's `_require_user`
  was doing all the work. `_request_path()` strips the prefix and both
  middlewares use it; `ApiAuthTests` drives the guard with `root_path` set.
- **The key test no longer loads the ASR model.** `PUT /api/settings/asr-token`
  proved a key by opening a throwaway transcription socket, which asr-mcp
  answers by loading the model onto the 4 GB card — the exact contention the
  priority gate exists to prevent. It now calls
  `GET /api/asr/languages` (static list, no model), derived from
  `MEM_ASR_WS_URL` by `asr_languages_url()` in `live_notes.py`, falling back to
  the handshake only on `404` so an older asr-mcp still accepts a valid key.
- **Both new guards were re-injected to check they bite.** Reverting
  `auth_guard` to `request.url.path` fails the root-path test; deleting the
  `Upgrade` directive fails the wiring test. Full suite: **1272 tests**, all
  passing (170 in the two touched files, 11 of them new). `py_compile` clean on
  every touched module; `git diff --check` clean with `cr-at-eol`.

# Live notes — documentation, verification and epic close (2026-10-27)


Shipped in `40c2330` on `feature/live-notes`.

- **L11 — the invariants are written down where the next edit will read them.**
  `AGENTS.md` now has a **Live Notes** section covering the Neo4j-only transcript
  and Qdrant note split, the one-way `REFERENCES` edge (and why `db_link_facts`
  is wrong for it), the priority gate's pause/resume generation semantics, the
  four-block prompt, candidate scoping and ranking-before-cap, the output budget
  and compaction, the in-memory job registry, the write-only per-user ASR token,
  the proxy close codes, deployment (`extra_hosts`, `MEM_ASR_WS_URL`) and the
  stdlib-only rule for `live_notes.py`. `test_live_notes.py` is in the documented
  regression command.
- **Two final guards were added.** The pause branch now has a test that the gate
  is released **before** the `paused` acknowledgement (the browser starts the
  job from that message), and the resume branch a test that `begin_recording`
  precedes `open_upstream`. These are the two halves of the bug that made every
  pause-triggered summarise answer 409.
- **Two races were closed.** Changing a transcription's client/project while a
  note job is running now answers 409, because the job is choosing candidates
  from the old scope. Deleting a transcription is refused for the same interval.
  The UI disables those controls while a job is active, rather than offering an
  action the server refuses.
- **Verification.** Full suite: **1259 tests**, all passing, including
  `test_live_notes.py` (88), `test_mobile_layout.py` (95), `test_sessions.py`
  and `test_env_wiring.py`. `py_compile` clean on every touched Python module;
  `node --check` clean on the extracted dashboard script; `git diff --check`
  clean with `cr-at-eol`.
- `plans/live-notes.md` is marked shipped and `tasks.md` is empty again — the
  epic's completion lives here, not as a list of ticked boxes.

# Live notes — Live notes tab and mobile layout (2026-10-27)

Shipped in `fca4fbf` on `feature/live-notes`.

- **L10 — the tab exists, and the microphone is the primary control.** A new
  top-level **🎙️ Live notes** tab owns a session rail plus a toolbar for name,
  client, project and language, with Start / Pause & summarise / Resume / Stop.
  Pause sends `{"type":"pause"}` and only starts the note job from the server's
  `paused` acknowledgement, after the segment writer has drained and the
  priority gate has been released. Resume is a recording start, not a
  reconnect, so the gate bumps its generation and defers a queued note job.
- **The token is write-only and Start knows it.** The page asks Setup's
  `GET /api/settings/asr-token`; when no key is saved it renders numbered
  asr-mcp instructions and disables Start rather than letting a socket fail
  with 4409. A 4409 close also clears the local flag so the state is visible
  immediately.
- **Two panes, one poller.** **Transcription** lists time, speaker and text
  with inline edit, delete-with-snapshot warning, and live placeholder segments
  while audio arrives. **Notes** renders markdown, referenced-fact badges that
  navigate through `showFactInMemories`, the job banner (`updating`,
  `deferred`, `error`) and **Rebuild note** using `reprocess {mode:"all"}`.
  `/status` is requested once when a job starts and then only while it is
  non-terminal.
- **Session state.** `activeLiveTranscriptionId` and `liveTab` are persisted in
  `mem_vault_session_state` and validated on load; `switchTab` still resolves
  the page element before dereferencing it.
- **Mobile.** Below 900px the rail stacks above the workspace; `.live-panel`,
  `.live-workspace` and `.live-panel-scroll` are released from their zero flex
  bases with both halves (`flex: 0 0 auto` and `overflow-y: visible`), the
  toolbar wraps, and controls get 40px targets. `LiveNotesLayoutTests` pins the
  full-bleed page, the shared 100vh budget, the absence of inline display on a
  layout container, and the stacked rules.

# Live notes — Setup ASR-token card (2026-10-27)

Shipped in `ef7d90a` on `feature/live-notes`.

- **L9 — the key is write-only.** Setup now carries **🎙️ Live transcription
  key**: a password input, Save, Test saved key and Clear, with the stored
  `hint` shown and the input deliberately never populated from the server. Save
  validates against asr-mcp first (`PUT /api/settings/asr-token` probes before
  storing), so "saved" means accepted rather than merely written. The card says
  the key is the user's own asr-mcp API key, stored per account, and that
  recording is unavailable without one; a banner under the form repeats the
  consequence while the key is unset. No new inline `display`/`width` that the
  mobile block has to override.

# Live notes — capture client, note jobs and REST surface (2026-10-27)

Shipped in `c053471` on `feature/live-notes`.

- **L6 — browser capture.** `templates/live-worklet.js` is the 16 kHz mono
  AudioWorklet, vendored unchanged from asr-mcp, and it is served by an
  authenticated `GET /api/live/worklet.js`; there is no `StaticFiles` mount to
  expose it by accident. `LiveCapture` in `dashboard.html` owns
  `getUserMedia` → `AudioContext` → worklet → packed frames: an 8-byte
  little-endian `(channel, sequence)` header followed by int16 LE PCM in one
  binary message. It exposes `start(id, handlers)`, `pause()`, `resume()` and
  `stop()`, reports permission and socket errors through the same handler
  surface, and tears down every node and timer on stop. The worklet declares
  `numberOfOutputs: 0` and is connected only to `node`, never to
  `destination`; that is what keeps it a tap rather than a feedback path.

- **L7 — one running note, updated per pause.** The prompt is exactly four
  blocks: the existing note, the unconsumed windows, the titles this note
  already references, and the candidate titles it may reference. Bodies are
  never sent. `db_note_fact_candidates` filters to the transcription's primary
  client (`FOR_CLIENT`, plus `IN_CONTEXT` when a project is selected), never
  `RELEVANT_TO`, excludes `Notes` and the note itself, and ranks **before**
  capping — ranking after the cap would just reorder whatever the graph
  happened to return first.

  A window's answer is validated against the allowed id set, unknown ids are
  dropped, existing references are unioned, and an empty note is refused. An
  answer over `MEM_LIVE_NOTE_MAX_CHARS` reaches one compaction pass with a
  numeric target (70% of the limit); a note is never sliced. Long pauses run
  `segment_windows` **sequentially**, so each window sees the previous note.

  `num_predict` is `live_output_budget(prompt_chars)` — the context window
  minus the estimated prompt — with `MEM_LIVE_MIN_NUM_PREDICT` as a refusal
  floor, not a budget. The first window is preflighted before the job is
  queued, so an over-budget selection is a 400 rather than a model failure.

- **L7 — priority and commit.** `start_note_job` refuses with
  `RecordingActiveError` while the gate is held. The job captures the gate
  generation, re-checks it immediately before the first write, and commits
  under `asyncio.shield`; the commit does no LLM work and keeps
  Qdrant-before-Neo4j ordering. A recording that starts during the LLM phase
  cancels the call, leaves the current window unconsumed, keeps earlier
  windows committed, and reports `deferred`. `all` releases the consumption
  markers first and rebuilds the **same** Fact id; a failed rebuild leaves the
  unprocessed segments eligible.

  Jobs are per transcription with a per-user `asyncio.Lock`, deliberately not
  `claim_maintenance`: a reclassify is vault-wide, and a note update is not a
  reason to block one. The registry is in memory and capped at 50 terminal
  jobs; the graph's consumption markers are the durable truth across a restart.

- **L7 — the pause/resume gate bug.** The proxy called `end_recording` only in
  its `finally`, so a paused socket held the gate and every pause-triggered
  summary answered 409. Pause now releases it after the writer queue drains and
  before `{"type":"paused"}` is sent; resume calls `begin_recording` before
  reconnecting, so a job queued by the pause is deferred rather than writing
  while audio flows again.

- **L8 — REST surface.** Transcriptions (`GET`/`POST`, `GET`/`PATCH`/`DELETE`
  by id), segment edit/delete, `summarize`, `reprocess {mode}`, and `status`.
  Every handler is `_require_user` and every foreign id is 404. `summarize`
  calls `start_note_job` on the event-loop thread because it creates a task —
  running it in `to_thread` would fail with no running loop. A scope change
  re-scopes an existing note through `db_set_fact_scope`; deletion is refused
  while any recording is open.

  `PUT /api/settings/asr-token` validates the token with the same throwaway
  handshake as `POST .../test` before storing it: a key saved unchecked fails
  later as a recording that will not start, with nothing linking it to the
  value. Rejected is a 400 naming asr-mcp's refusal; unreachable is a 503.
  `GET` returns only `{set, hint}` (last four characters) and no endpoint ever
  returns the token.

- **Verification.** `py_compile` clean for every touched Python module; the
  dashboard's last script block passes `node --check` with Jinja expressions
  replaced by a bare token; `test_live_notes.py` (86 tests),
  `test_sessions.py` and `test_env_wiring.py` pass. New compaction and job
  contract tests pin the numeric target, the absence of slicing, the
  generation check before the first write, the shield, sequential windows, and
  `deferred` on cancellation.

---

---

# Live notes — ASR WebSocket proxy (2026-10-27)

Shipped in `5e36c71` on `feature/live-notes`.

- **L5 — the proxy.** `WS /api/live/stream/{transcriptionId}` in `gui.py` accepts
  the browser socket, resolves the user from `websocket.scope["session"]`
  (`auth_guard` does not run for a WebSocket scope), checks `Origin` through
  `live_notes.origin_allowed` with `BASE_URL` and the request origin as
  fallbacks, verifies ownership, reads the caller's token from
  `sessions.get_asr_token`, and only then calls `LIVE_GATE.begin_recording`.
  Every refusal accepts first, sends an actionable `{"type":"error"}` and closes
  with `4401` / `1008` / `4403` / `4409` / `4502`; refusing before `accept()`
  collapses all of them into an opaque 403.

- **Nothing is opened for a failed check.** The tests pin the order in the route
  source: ownership, token and `begin_recording` all precede `_ws_connect`.
  The upstream URL gets `X-API-Key` (never the browser) and `?language=` merged
  into an existing query. The websockets client is imported with
  `additional_headers` for 14+ and `extra_headers` for older versions.

- **Audio first, storage second.** `asr_reader` forwards each ASR message to the
  browser *before* parsing it, then `live_notes.asr_segment` maps a non-empty
  `transcript` to `{text, speaker, startSec, endSec}` with the connection offset
  added, and a single writer task drains an `asyncio.Queue`. `seq` is assigned
  from the stored maximum by `db_append_segment`, so a reconnect needs no client
  state. The tests pin the forward-before-queue order and the offset maths.

- **Pause/resume.** Pause waits the ASR detector's 0.8 s hangover before closing
  upstream, drains the reader (which lets a flushed tail and `stats` through),
  waits `queue.join()`, sets status `paused`, and only then sends
  `{"type":"paused"}`. Resume opens a new upstream connection at
  `offset = max(endSec)`, because asr-mcp timestamps restart at zero per
  connection. A close we initiated suppresses the "service disconnected" error
  via a per-connection quiet flag; an unexpected drop still reports it.

- **Priority.** `LIVE_GATE` is one process-wide gate in `live_manager.py`.
  `begin_recording` runs before the upstream connect and `end_recording` is in
  the `finally` block, so a browser crash cannot leave the vault permanently
  "recording". When `MEM_LIVE_UNLOAD_ON_RECORD` is on, a background task evicts
  the live model after the socket is accepted; it never delays the microphone,
  and it only calls `unload_ollama_model` when the snapshot says the model is
  actually resident.

The lesson to carry: a pause is not a disconnect. Closing the upstream must
first let the detector's hangover and post-roll finish, and the reader must be
allowed to drain its final messages before it is cancelled — otherwise the last
sentence of every section is silently missing from a recording that otherwise
looks complete.

---
# Live notes — transcription storage and note links (2026-10-27)

Shipped in `8d43536` on `feature/live-notes`.

- **L4 — graph model and CRUD.** New `live_manager.py` stores
  `(:Transcription)-[:HAS_SEGMENT]->(:TranscriptionSegment)` in Neo4j only; raw
  transcripts are deliberately not embedded. A note is an ordinary `:Fact` in
  category `Notes`, linked one-way with `(:Fact)-[:FROM_TRANSCRIPTION]->(:Transcription)`
  and `(:Fact)-[:REFERENCES]->(:Fact)`; `REFERENCES` is written directly rather
  than through `db_link_facts`, which would create the reverse edge and put the
  note on every referenced fact's card. `Transcription.noteId` and the segment
  `consumedBy`/`consumedAt` markers are the state that stops a pause
  re-summarising the whole transcript. Every create/update/delete and both ends
  of every link stamp `updatedAt`, because the backup fingerprint reads only
  that property and a count.

- **Deletion is a two-way hook.** `fact_manager.db_delete_memory` checks
  `f.source == "live_notes"` before calling `release_note_segments`, so an
  ordinary fact deletion pays two queries for a note it never was. Deleting the
  note clears every `consumedBy`/`consumedAt` and `Transcription.noteId`, which
  makes those segments eligible again; without that, deleting a note would look
  like losing the transcript. Deleting a transcription keeps the note by default
  and removes its `FROM_TRANSCRIPTION` edge first, so the survivor is an ordinary
  fact rather than a dangling live one.

- **Candidates are the identified client only.** `db_note_fact_candidates`
  returns `{id, name}` for facts with `FOR_CLIENT` to the session's client, plus
  facts under the selected project, excluding `Notes`. It never returns a body
  and never widens through another client's `RELEVANT_TO` edge. A session with
  no client yields no candidates at all rather than the whole vault.

- **Tests.** `test_live_notes.py` grew to 70 tests. The new L4 tests lift the
  real functions out of `live_manager.py` (it cannot be imported without
  `httpx`) and drive them against a fake Neo4j session, pinning that a segment
  edit names only `seg`, a segment delete is `DETACH DELETE seg` with no Fact,
  note deletion releases segments and clears `noteId`, and references are
  written in one direction. `test_cypher_safety` adds its automatic lint to
  every new query. The focused set (`test_live_notes`, `test_cypher_safety`,
  `test_sessions`, `test_env_wiring`, `test_matching_regressions`) is 392 tests
  green.

The lesson to carry: a query that deletes two variables from an `OPTIONAL MATCH`
(`DETACH DELETE seg, t`) can try to delete `null` when the optional side is
empty. Split it into an unconditional segment delete and a separate
transcription delete; the empty match simply deletes nothing.

---

# Live notes — configuration, ASR token store, pure helpers (2026-10-27)

Shipped in `06bd3f0` on `feature/live-notes`.

- **L1 — configuration.** `websockets` is a direct dependency because uvicorn is
  not installed with `[standard]`; it serves the browser socket and is the
  outbound asr-mcp client. The mem-mcp service gained
  `extra_hosts: host.docker.internal:host-gateway` (asr-mcp is on its own compose
  network and publishes 8087 on the host), and all eight `MEM_LIVE_*` /
  `MEM_ASR_WS_URL` knobs are both documented in `.env.example` and forwarded in
  the compose `environment:` block. `MEM_LIVE_CONTEXT_TOKENS` must track
  `OLLAMA_CONTEXT_LENGTH` (`16332`). `test_env_wiring` passes.

- **L2 — per-user ASR token store.** `asr_tokens(user_id PK, token, updated_at)`
  in `sessions.db`, with `set_asr_token` / `get_asr_token` / `clear_asr_token` /
  `asr_token_hint`. The token is plaintext at rest because the proxy replays it
  in `X-API-Key`; the database sits in the `MEM_SESSION_DIR` bind mount. `set`
  returns only `{set, hint, updated_at}` and never the value; the hint is the
  last four characters and cannot authenticate. `test_sessions` covers
  round-trip, isolation, clear, empty/oversized refusal and a fresh-process read
  (123 tests green).

- **L3 — pure helpers and priority gate.** New `live_notes.py` is stdlib-only
  and **called** by `test_live_notes.py` (62 tests). It owns the four-block
  prompt (existing note, unconsumed segments, referenced titles, candidate
  titles; no fact bodies), candidate ranking by title overlap, strict note-JSON
  validation (unknown ids dropped, existing references unioned, over-long note
  refused rather than sliced), the context-minus-prompt output budget with an
  inclusive floor, segment batching that never splits a segment, ASR time-offset
  maths, the `BASE_URL` origin check (absent Origin allowed, `null` refused),
  and `LivePriorityGate`. The gate increments a per-user generation on
  `begin_recording`, invokes and clears the **LLM-phase** cancel hook outside
  its lock, and `assert_idle` raises `RecordingActiveError` for a 409.

The lesson to carry: the prompt's four blocks are asserted as a **heading
count**, not by looking for expected text. A block added later is invisible to
`assertIn` over the block it did not touch, and the prompt is the one place a
fact body can leak into the context window.

---
# Backup skip: timestamps instead of a content digest (2026-10-27)

Superseded the content-digest fingerprint from `be36b29` per the operator's
correction: *"that's excessive. add update timestamps, ensure it is updated on
each write and link (both sides), use that for checking changes. drop hashing."*

`be36b29`'s fingerprint paginated the whole graph twice per scheduled run and
sha256'd every node and relationship body. The replacement reads one aggregate
per label — `MATCH (n) RETURN labels(n), count(*), toString(max(n.updatedAt))` —
plus `MATCH ()-[r]->() RETURN count(r)`, so a scheduled check is two small reads
instead of a full scan.

The write side is where the work was, and it is the part a future reader would
otherwise rediscover: **every create, update and link now stamps
`updatedAt = datetime()`**, and every link stamps **both** endpoints. That is
what makes a reclassify visible — it rewrites `FOR_CLIENT` / `IN_CONTEXT` edges
without touching any content property — and stamping both ends means moving a
same number of edges still moves a stamp. Touched: `fact_manager.py`
(`db_add_memory` CREATE, `db_update_memory`, both add/remove relevant,
`db_link_facts` / `db_unlink_facts` in all four branches),
`diary_manager.py` (`db_save_diary`, `db_update_diary`,
`db_update_diary_metadata`, `_auto_link_people`, `sync_diary_mentions`, link /
unlink mention, add / remove relevant), `client_manager.py` (client and context
create, rename, activate, cross-client, `_stamp_manual_scope`, the four
`link_*_to_*` helpers, and the clear statements in `db_set_fact_scope` /
`db_set_diary_scope`), and `migrate_client_context.py` (`_link_diary_relevant_to`,
`clear_scope_links_batch`).

Two things are load-bearing and easy to undo by accident. A **delete stamps
nothing**, so the per-label count is what catches it — which is why the count
lives in the same entry as the stamp. And the clear queries in
`db_set_*_scope` had to become `SET x.updatedAt = datetime() WITH x MATCH …`:
a `SET` after a `MATCH` that rebinds the variable does not apply to the rows the
delete matched, and `WITH` is what carries it.

`backup.py` lost `import hashlib`, `_digest_line()` and `_vault_digest()`;
`_vault_stamp()` replaced them. `fingerprint_matches()`,
`_collection_point_count()`, `latest_fingerprint()` and the scheduler-only skip
are unchanged, and so are their reasons: an unreadable store still yields an
empty fingerprint that never matches, so a failure takes the savepoint instead
of silently skipping it.

Tests: `test_backup_compression.py`'s `FingerprintTests` now pins the stamp shape
(count plus newest write per label, a null stamp as `""`, a label with no records
absent from both sides), a later write, a delete through the count, a same-count
edge move through the node stamps, an unreadable fingerprint, the failed
savepoint baseline and a pre-feature savepoint reading as unknown.
`_FakeSession` dispatches on the aggregate's `labels(n)` SELECT list rather than
on `"MATCH (n)"`, because the edge query is `MATCH ()-[r]->()` and a looser match
would answer it with node rows and pass on a wrong query.

Full 19-suite battery green; documented in `AGENTS.md` → Backup & Restore,
including the consequence for future write paths: a write that does not stamp
`updatedAt` is invisible to the check, so the vault can go unsaved with nothing in
the log to say so.

---

# Project/Context Sharing — Execution Plan

Status: **planning only**. No code changes yet. Created: 2026-10-18.

## 0. Goal and scope

Allow a project/context **owner** to share a single client or context (project)
with other signed-in users, via a unique link, with two permission levels:

- **read** — recipient can view the records scoped to that client/context.
- **read-write** — recipient can also create, edit, delete and re-scope records
  under that client/context.

The owner manages the set of recipients and their permissions from a settings
surface. Every access and modification performed through a share is written to
an audit log in the database.

Out of scope for v1 (explicit):

- Sharing an entire vault or arbitrary fact sets.
- Transitive sharing (a recipient cannot re-share).
- Anonymous (unauthenticated) recipients — a share link requires sign-in.
- Per-record sharing; sharing is always at client/context granularity.
- Real-time collaboration / concurrent edit conflict resolution (last write wins,
  audited).

## 1. Open decisions (resolve before Phase 1)

- [x] **Where do records created under a shared context live?** Proposed:
      stored with the **owner's `userId`** (shared workspace semantics) with a
      `createdBy` attribute naming the acting user. Alternative: stored under the
      acting user's `userId`, which fragments the shared view. Decision affects
      every read query.
- [x] **Read access to the owner's other clients.** A shared context must not
      leak sibling clients/contexts or unscoped facts. Scope filters are already
      edge-based; confirm no code path returns the owner's full client list to a
      grantee.
- [x] **Expiry of share links.** Proposed default: 30 days, revocable anytime.
      Also allow "never expires" for long-lived links (owner's choice).
- [x] **Maximum recipients per shared target.** Proposed 50; revisit if needed.
- [x] **Audit log storage.** Proposed: a dedicated SQLite database
      `audit.db` next to the session store (stdlib only, like `sessions.py`) so
      it is importable/testable without DB drivers. Alternative: Neo4j nodes.
      SQLite keeps audit writes cheap and queryable.
- [x] **Audit log retention.** Proposed 90 days, configurable
      (`MEM_AUDIT_KEEP_DAYS`).
- [x] **Should a read-only recipient see the owner's name in the UI?** Yes
      (attribution), but never the owner's other data.
- [x] **MCP exposure.** Should an access key (mvk_) be able to act on shared
      targets on behalf of its owner? Proposed: yes, but only targets the key's
      owner has access to (owned or granted). No new credential type.

## 2. Architecture overview

The existing system partitions everything by `userId` (Neo4j `userId` property,
Qdrant payload `userId`, SQLite `psks.user_id`). Sharing introduces a second
axis: **access grants**. A grantee's effective readable set is:

```
owned:    userId = self
shared:   userId = owner  AND  target in granted_targets(self)
```

Every read path that is user-scoped must be widened to this union, and every
write path must check the grant's permission level. The owner's identity stays
the partition key of the stored records; grants are metadata on top.

New concepts:

- `ShareLink` — a revocable, expiring token minted by an owner for one target.
- `ShareGrant` — a (grantee, target, permission) record created when a link is
  accepted (or when an owner directly adds a user).
- `AuditLog` — append-only rows recording who did what, when, on which target.

## 3. Data model

### 3.1 Neo4j

Add to `:User` hub (already exists). New nodes/edges:

- `(:ShareLink {id, tokenHash, ownerUserId, targetType, targetId, permission,
  createdAt, expiresAt, revokedAt, useCount, maxUses})`
- `(:ShareGrant {id, ownerUserId, granteeUserId, targetType, targetId,
  permission, grantedAt, revokedAt, viaLinkId})`
- Edges: `(owner:User)-[:MINTED]->(link:ShareLink)`,
  `(grantee:User)-[:HAS_GRANT]->(grant:ShareGrant)`,
  `(grant)-[:FOR_TARGET]->(client:Client|context:Context)` (polymorphic by label).
- Indexes: `ShareLink(tokenHash)`, `ShareGrant(granteeUserId, targetId)`,
  `ShareGrant(ownerUserId, targetId)`.

`targetType` is `'client'` or `'context'`. A client share implicitly covers its
contexts unless the owner shares a context alone.

### 3.2 SQLite (audit + grants mirror)

New module `audit.py` (stdlib only, like `sessions.py`) operating on
`audit.db` next to the session store.

Tables:

```sql
CREATE TABLE share_links (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  token_prefix TEXT NOT NULL,          -- 10-char display prefix
  owner_user_id TEXT NOT NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('client','context')),
  target_id TEXT NOT NULL,
  permission TEXT NOT NULL CHECK (permission IN ('read','write')),
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  max_uses INTEGER
);

CREATE TABLE share_grants (
  id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL,
  grantee_user_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  permission TEXT NOT NULL CHECK (permission IN ('read','write')),
  granted_at TEXT NOT NULL,
  revoked_at TEXT,
  via_link_id TEXT,
  UNIQUE (owner_user_id, grantee_user_id, target_type, target_id)
);

CREATE TABLE audit_log (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  owner_user_id TEXT,                 -- null for own-vault actions
  target_type TEXT,                   -- 'client'|'context'|'fact'|'diary'|...
  target_id TEXT,
  action TEXT NOT NULL,               -- 'read','create','update','delete','link',...
  permission TEXT,                    -- effective permission used
  shared INTEGER NOT NULL DEFAULT 0,  -- 1 if action was on a shared target
  detail TEXT                         -- JSON, no secrets
);
CREATE INDEX audit_log_ts ON audit_log(ts);
CREATE INDEX audit_log_actor ON audit_log(actor_user_id, ts);
```

Grants are mirrored in both Neo4j (for graph queries/scope) and SQLite (for
fast permission checks and admin UI). Neo4j is authoritative for graph traversal;
SQLite is authoritative for the user-management list and audit.

### 3.3 Qdrant

No schema change. Access filtering happens at the application/Neo4j layer by
record id sets; Qdrant queries are widened with accessible owners. Performance
concern → Phase 3 task to benchmark.

## 4. Phases and tasks

### Phase 1 — Foundations (data model + core helpers)

- [x] P1.1 Create `audit.py` (stdlib only) with `share_links`, `share_grants`,
      `audit_log` tables, migration-on-open, and helpers:
      `mint_share_link`, `resolve_share_link`, `revoke_share_link`,
      `list_share_links`, `grant_access`, `revoke_grant`, `list_grants_for_target`,
      `list_grants_for_user`, `log_access`, `query_audit_log`.
- [x] P1.2 Add Neo4j schema helpers in `client_manager.py`:
      `db_mint_share_link`, `db_resolve_share_link`, `db_revoke_share_link`,
      `db_grant_access`, `db_revoke_grant`, `db_list_grants`, `db_has_access`,
      `db_accessible_client_ids`, `db_accessible_context_ids`,
      `db_accessible_fact_ids` (for a grantee).
- [x] P1.3 Add permission helpers (new `sharing.py` or extend `client_manager.py`):
      `can_read(user, target)`, `can_write(user, target)`,
      `is_owner(user, target)`. Must consult grants + ownership.
- [x] P1.4 Env wiring: `MEM_AUDIT_DB`, `MEM_AUDIT_KEEP_DAYS`,
      `MEM_SHARE_LINK_TTL_DAYS` (default 30), `MEM_SHARE_MAX_RECIPIENTS`
      (default 50), `MEM_SHARING_ENABLED` (default 0). Add to `.env.example`
      and `docker-compose.yml` environment block (per env-wiring rule).
- [x] P1.5 Document in `scoped_ids.py` docstring that shared targets reuse
      owner's ids (no new id derivation).

### Phase 2 — Share link lifecycle (backend)

- [x] P2.1 `POST /api/share/links` (owner only): body `{targetType, targetId,
      permission, expiresInDays?, maxUses?}` → returns `{token (once), prefix,
      url, expiresAt}`. Token is `secrets.token_urlsafe(32)`; store SHA-256 hash
      + 10-char prefix (mirrors PSK pattern).
- [x] P2.2 `GET /api/share/links?targetType=&targetId=` (owner only): list
      active/revoked links for a target with use counts.
- [x] P2.3 `DELETE /api/share/links/{id}` (owner only): revoke; idempotent,
      scoped by owner, returns one answer for not-found/not-yours/already-revoked
      (enumeration-safe, mirrors `revoke_psk`).
- [x] P2.4 Accept endpoint `POST /api/share/accept/{token}`: requires
      authenticated user; resolves link (hash compare, not expired, not revoked,
      under maxUses); creates grant (idempotent upsert of permission);
      increments use_count; logs audit. Returns `{targetType, targetId, owner,
      permission, alreadyHadAccess}`.
- [x] P2.5 Share landing page route `GET /share/{token}` (or
      `/gui/share/{token}`) that prompts sign-in if anonymous, then redirects to
      accept. Must not leak target existence to unauthenticated callers.
- [x] P2.6 Owner direct-add: `POST /api/share/grants` body `{targetType,
      targetId, username, permission}` — creates grant without a link (for
      users already known). Owner only.
- [x] P2.7 `PUT /api/share/grants/{id}` change permission; `DELETE` revoke.
      Owner only, scoped.
- [x] P2.8 `GET /api/share/grants?targetType=&targetId=` list recipients for
      owner management UI.
- [x] P2.9 `GET /api/share/mine` — targets shared with the current user
      (for "Shared with me" view).

### Phase 3 — Access control in read paths

- [x] P3.1 `db_list_clients` / `db_list_contexts`: include shared clients/
      contexts for the current user, flagged `shared: true`, `owner`,
      `permission`. Distinguish owned vs shared in the payload.
- [x] P3.2 Fact search (`db_search_memories`, `db_search_diary`): widen Qdrant
      filter to accessible owners; post-filter by accessible record ids.
      Benchmark; if too slow, add a `sharedOwnerIds` payload index or resolve
      accessible ids first.
- [x] P3.3 `db_get_graph`, `db_get_neighborhood`, `db_focus`: include shared
      records when their client/context is in accessible set. Respect category
      and scope filters.
- [x] P3.4 Diary list / calendar: include shared diary entries under shared
      contexts. Attribute owner.
- [x] P3.5 Memory list (`db_list_memories`): include shared facts.
- [x] P3.6 Client/context items endpoints: allow grantee read for shared
      targets; 404 otherwise (no existence leak).
- [x] P3.7 MCP tools (`mcp_tools.py`): every read tool resolves accessible
      targets through the same helpers; no separate code path.

### Phase 4 — Write paths and permission enforcement

- [x] P4.1 Central permission gate helper `_require_target_access(request,
      target_type, target_id, level='read'|'write')` used by all endpoints and
      MCP tools. Returns owner userId (for storage) or raises 403.
- [x] P4.2 Create fact/diary under a shared target: permission write; store
      with owner's userId (per decision 1); set `createdBy`/`updatedBy`.
- [x] P4.3 Update/delete fact/diary: permission write on the record's target;
      owner can always; grantee only if write grant exists and not revoked.
- [x] P4.4 Scope changes (`db_set_*_scope`) on shared records: write permission
      on both old and new target.
- [x] P4.5 Link/unlink between records: write permission on both endpoints'
      targets (or both owned by same user).
- [x] P4.6 Client/context metadata edits (rename, active toggle, crossClient):
      owner only. Grantees cannot rename shared targets.
- [x] P4.7 Delete client/context: owner only.
- [x] P4.8 MCP write tools: same gates; read-only grants yield a clear error
      message naming the permission.

### Phase 5 — Audit logging

- [x] P5.1 `log_access(actor, owner, target_type, target_id, action, permission,
      shared, detail)` called from every shared read and write. Own-vault actions
      optionally logged (config `MEM_AUDIT_LOG_OWN`, default false) to keep
      volume manageable.
- [x] P5.2 Audit writes are fire-and-forget (async, swallowed on failure, logged
      at WARNING) so they never break the request.
- [x] P5.3 Retention purge: `purge_audit_log(older_than_days)` in the
      scheduled backup loop or a separate daily task.
- [x] P5.4 `GET /api/audit?targetType=&targetId=&actor=&since=&limit=` owner
      only, paginated, for the settings UI.
- [x] P5.5 Ensure no secrets/tokens in `detail` (prefix only, never full token).

### Phase 6 — UI

- [x] P6.1 Projects page: badge shared clients/contexts ("shared by X"); filter
      chips for "Owned / Shared with me".
- [x] P6.2 Share dialog on a client/context (owner only): permission picker,
      expiry, max uses, "Copy link", list of existing links with revoke.
- [x] P6.3 Recipients management panel (owner only): list grants, change
      permission, revoke; direct-add by username.
- [x] P6.4 "Shared with me" section listing targets grouped by owner.
- [x] P6.5 Read-only indicator in record editors (disable save when no write).
- [x] P6.6 Audit log viewer in Settings/Service (owner sees own targets; admin
      sees all).
- [x] P6.7 Share acceptance landing page with sign-in redirect and success/
      error states.
- [x] P6.8 Mobile layout tests for new panels (follow existing
      `test_mobile_layout.py` pattern).

### Phase 7 — MCP and API surface

- [x] P7.1 Document new endpoints in README.
- [x] P7.2 MCP tools: `list_shared_with_me`, `accept_share_link` (if token
      passed), `list_share_grants` (owner), and permission-aware existing tools.
- [x] P7.3 OpenAPI disabled (already) — no schema leak.

### Phase 8 — Tests

- [x] P8.1 New `test_sharing.py` suite covering: link mint/hash/verify,
      expiry, revocation, max uses, enumeration-safe revoke, grant idempotency,
      permission checks (read vs write), cross-user access, audit writes.
- [x] P8.2 Cypher safety tests: new queries added to `test_cypher_safety.py`
      list-parameter lint; alias tests for any new multi-match reads.
- [x] P8.3 Env wiring test auto-covers new vars.
- [x] P8.4 Mobile layout tests for new panels.
- [x] P8.5 Re-injection verification for any guard that is "looks right but
      wrong" (per AGENTS.md lesson).

### Phase 9 — Migration, docs, rollout

- [x] P9.1 Backfill: no data migration needed (grants are new). Ensure old
      vaults unaffected.
- [x] P9.2 Backup/restore: include `audit.db` in savepoint as an additional
      file in the savepoint directory. Update `backup.py`.
- [x] P9.3 Update AGENTS.md with sharing section and new env vars.
- [x] P9.4 Update README with user-facing sharing docs.
- [x] P9.5 Staged rollout: feature flag `MEM_SHARING_ENABLED` (default 0)
      initially, like registration. Start with read-only sharing only, then add
      write in a follow-up.

## 5. Risk register

- **Performance**: read paths widened to union of owners may multiply Qdrant
  queries. Mitigate with accessible-id sets and benchmarking in Phase 3.
- **Data leakage**: a bug in access filter exposes owner's whole vault. Mitigate
  with tests that assert a grantee cannot see non-shared clients/facts.
- **Audit volume**: per-read logging can be large; default own-vault logging off
  and sample shared reads if needed.
- **Permission bypass via MCP**: ensure MCP tools use the same gates, not a
  parallel implementation.
- **Link leakage**: tokens in URLs hit browser history/proxies. Mitigate with
  short TTL, revocation, and never logging full token.

## 6. Suggested implementation order

Phase 1 → Phase 2 → Phase 5 (logging infra) → Phase 3 → Phase 4 → Phase 6 →
Phase 7 → Phase 8 (continuous) → Phase 9.

Estimated size: large (multi-week). Recommend shipping behind `MEM_SHARING_ENABLED`
and starting with read-only sharing only, then adding write in a follow-up.

---

# Backup skip when nothing changed — Task (added 2026-10-26)

Don't run a backup if there was no data change since the last backup.

- [x] Detect "no data change since the last savepoint": fingerprint the vault
      (record counts per store + newest updated_at timestamp in Neo4j, plus
      Qdrant point counts) before each scheduled run and compare against the
      fingerprint recorded in the last *complete* savepoint's `manifest.json`.
- [x] On no change, skip the savepoint (log it at INFO) and keep the existing
      savepoint as the newest — `prune_savepoints()` retention must not treat
      the skipped night as missing.
- [x] `POST /api/backup/run` (manual) ignores the fingerprint — an explicit
      request always takes a savepoint.
- [x] A restore or a failed/aborted backup resets the fingerprint (the stored
      fingerprint must describe the *current* data, not a stale one).
- [x] Tests: fingerprint stability (two polls, no writes → equal), change
      detection (a write bumps it), prune interplay, manual run unaffected.

**Implementation note (2026-10-26):** the fingerprint is a sha256 over the
*content* of every node and relationship (plus both collections' point counts),
not over timestamps. `Fact.updatedAt` is only written by fact updates and
`DiaryEntry` carries no update timestamp at all, so a time-based check would
have missed every diary edit and every edit of an older fact. Edges are hashed
too, because a reclassify rewrites scope links without touching a node. A
fingerprint that cannot be read is empty and never matches, so an unreadable
store takes the savepoint rather than silently skipping it. `run_backup` accepts
the fingerprint the scheduler already took, so the comparison and the snapshot
describe the same moment.
