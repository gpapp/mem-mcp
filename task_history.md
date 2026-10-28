# Task History

Completed work, newest first. Open work lives in `tasks.md`; the convention is
recorded in `AGENTS.md` (Task Tracking).

---

# The GPU reservation moved into the base compose file (2026-10-28)

- **Reported as** "Please merge docker-compose.gpu.yml and add documentation to
  support those without CUDA (it should fall back to CPU anyway)".
- **Why the split existed, and why it stopped paying**: the reservation lived in
  `docker-compose.gpu.yml` merged with `-f`, and the failure that caused was a
  bare `docker compose up -d` recreating ollama without it. It started healthy,
  answered every request, and produced entirely plausible latencies -- on the
  CPU. That happened twice (2026-09-29 while reducing `OLLAMA_CONTEXT_LENGTH`,
  and the 2026-10-27 note rewrite) and neither left a line in the log saying so.
- **What changed**: the `deploy` reservation, the `/dev/dri` + `/dev/nvidia*`
  nodes and the Vulkan/`NVIDIA_DRIVER_CAPABILITIES` env now sit in
  `docker-compose.yml`, so the GPU host cannot lose the card by forgetting a
  flag. `docker-compose.gpu.yml` is deleted.
- **The CPU direction inverted, and that is the part worth remembering**: a
  host with no NVIDIA driver now fails at *create* time
  (`could not select device driver`, or `error gathering device information ...
  /dev/nvidia0`), which is the safe direction for the mistake to happen in.
  **Ollama's own CPU fallback does not rescue it** -- Ollama picks the CPU only
  once its container exists, and compose cannot omit a field conditionally --
  so the CPU host carries the overlay instead:
  `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d`, where
  `docker-compose.cpu.yml` `!reset`s `deploy` and `devices` (Compose >= 2.24),
  plus `MEM_GPU_EXPECTED=0` so the status strip stops reporting the intended
  state as a fallback.
- **What a future reader would otherwise rediscover**: compose merges by
  appending and overriding, never by removing, so an overlay that merely *omits*
  `devices` inherits it. The `!reset` tag is the whole mechanism, and a test that
  asserts the key is present passes on an overlay that does nothing -- hence
  `ComposeGpuDeploymentTests` asserts the tag itself, and that exactly one
  compose file declares a reservation at all.
- **Also** `GPU_WATCHDOG_EXPECT_GPU` (default 1): the host watchdog would
  otherwise report a deliberately CPU-only host as degraded every 15 minutes,
  which is the same failure as no watchdog.
- **Verified**: `Ran 1373 tests ... OK`; `bash -n ops/gpu-watchdog.sh`;
  both compose files parse with a `!reset`-aware loader; `git diff --check`
  clean; both new guards mutation-checked (dropping the `!reset` tag, and a
  second file declaring a reservation, each fail the class they belong to).

Shipped in `dd25b2f` on `feature/live-notes`.

---

# Granite was not unresponsive, it was looping and nobody was told (2026-10-28)

- **Reported as** "Granite seems to be unresponsive on name extraction".
- **What the log actually said**: three distinct failures on
  `granite3.3:2b`, all of them a *degenerate decode* rather than a stuck
  model. `content: ""` with `done_reason: "stop"` after `eval_count: 1`; a
  named-entity window answered `" A A A A A A ..."` to `done_reason:
  "length"` (59 repetitions of one token); and two
  `{"error":"prediction aborted, token repeat limit reached"}` HTTP 500s.
  The model answered every time, in 15-70 s, so "unresponsive" was the one
  thing it was not.
- **Why it looked dead instead**: `_extract_people_names` caught the 500 and
  logged `logger.debug`, and an unparseable answer was `continue`d with
  another DEBUG line. Production runs at `LOG_LEVEL=WARNING`, so a window
  that produced nothing was indistinguishable from a window with no people
  in it. A third of that day's extractor calls failed this way.
- **The chronic part is not the GPU.** 138 `token repeat limit` aborts in the
  log, the first granite one on 2026-10-08, with a 38-failure spike on
  2026-10-17 -- three weeks before the card was wedged. Greedy decoding at
  the app's hard `temperature: 0.0` default is the property; the CPU fallback
  only made each retry cost 15-70 s and turned a quiet recall loss into a
  visible stall.
- **What shipped**: `get_extraction_response` in `common.py` -- a repeat
  penalty at temperature 0, exactly one retry with stronger, narrower
  sampling, `is_degenerate_answer` covering both the empty answer and the
  token run, `is_repeat_abort` retrying that 500 and nothing else, and a
  WARNING when a window yields nothing. Both hops take it: the extractor per
  window, and the binding pass through `_bind_people_llm`, which was calling
  `get_llm_response` bare and silently inheriting the judgement model.
- **What a future reader would otherwise rediscover**: the guard returns the
  last answer rather than raising. Raising would trade a recall loss for an
  outage, because every caller already has a deliberate "nothing here" path.
  And the `is_good` predicate is what keeps a real `[]` from being retried --
  "no people in this window" is an answer, not a failure.
- **Two deployment configs disagree with the documented split**, found in the
  same `.env` while reading the log: `MEM_SCOPE_MODEL` and `MEM_LIVE_MODEL`
  are both on `granite3.3:2b`, where AGENTS.md measured granite as the worst
  choice for the judgement role (2 permanent scope nulls against nemotron's
  0) and as the model that loses the thread on a 2.4k-token note rewrite.
- **Verified**: 1370 tests OK across the twenty suites, `py_compile` clean on
  the five touched files, `git diff --check` clean.
- Shipped in `94251f3` on `feature/live-notes`.

---

# A 100% CPU label that was not the dropped overlay (2026-10-28)

- **Reported as** "The service reports 100% CPU in ollama, when VULKAN is turned
  off. Is that right?" — with the ollama log attached. It was right, and the
  label was the one true thing in the picture.
- **What the log actually said**: `updated VRAM based on existing loaded models
  gpu=0 library=CUDA total="3.6 GiB" available="3.6 GiB"` — so the reservation
  *was* applied and the scheduler saw the card through NVML — followed by
  `ggml_cuda_init: failed to initialize CUDA: unknown error`, a `device_info`
  carrying only the `CPU : Intel(R) Core(TM) i5-7600` row, a memory breakdown
  with only `Host` and `CPU_REPACK`, and a llama-server command line with no
  `--n-gpu-layers`. The subprocess never got a CUDA device, so the model loaded
  on the CPU and `/api/ps` reported `size_vram = 0`. `OLLAMA_IGPU_ENABLE=0`
  drops Vulkan0, so there was no second backend to fall back on either.
- **The trap**: AGENTS.md's only reliable GPU check is "`ollama ps` must say
  `100% GPU`", and its documented remedy for `100% CPU` is "the overlay was
  dropped". Here the overlay was present and re-adding it would have changed
  nothing. `cuInit` fails before any allocation, so over-commit is not the cause
  either — `nvidia-smi` still answers, which is what makes this look like a
  configuration problem rather than a wedged card.
- **What shipped, app side**: `cpu_fallback_warnings()` in `status_monitor.py`,
  called from `build_status`, appending one note per resident model with
  `sizeVram == 0` that names the model, `docker exec ollama ollama ps` and the
  `ggml_cuda_init` / `device lost` lines. Excluded on purpose: a split
  (`49%/51%`) is a slow card, a cold model has no bytes in use, and an unknown
  `size` is not evidence. `MEM_GPU_EXPECTED` (default 1) turns it off on a host
  that genuinely has no GPU, because a warning that every CPU-only install sees
  is noise. It is part of the snapshot and therefore of `signature()`, so it
  pushes once rather than on every poll.
- **What shipped, host side**: `ops/gpu-watchdog.sh` plus a `.service` and
  `.timer` (15 min, `OnBootSec=5min`) for the ops team to install. Deliberately
  **not** inside the container: there is no Docker socket and no `nvidia-smi`
  there, and the fault is driver/card state, so a container-side reset is aimed
  at the wrong layer. The probe is behavioural rather than NVML-based —
  `size_vram` of a forced load — because on 2026-10-28 NVML answered happily
  while `cuInit` failed. It re-classifies after `RECHECK_SECONDS` so a cold load
  cannot fire it, defaults to `ACTION=alert`, and gates `restart`/`reboot`
  behind strike counting and a minimum interval.
- **What a future reader would otherwise rediscover**: `nvidia-smi --gpu-reset`
  is generally refused on a GeForce (it is a data-centre feature) and would not
  fix a `cuInit` failure anyway, so the escalation ladder is `ollama stop` →
  `docker restart ollama` → host reboot. The cheap discriminator in the
  meantime is to turn Vulkan back on: if a Vulkan model runs, the card is alive
  and this is a CUDA/driver-layer fault; if it also dies, the device is wedged.
- **Verified**: 1350 tests OK across all twenty suites, `py_compile` clean on
  `status_monitor.py` / `common.py` / `test_status_monitor.py`, `bash -n` clean
  on the watchdog, `diff --check` clean.

Shipped in `0cfb57b` on `feature/live-notes`.

# Live notes — the model was summarising captions (2026-10-27)

The note read like it had been written from a list of captions, because it had.
asr-mcp ends a turn on 320 ms of sub-threshold audio rather than on punctuation,
so one spoken sentence arrives as several segments with no full stop anywhere,
and `format_segments` rendered one line per segment: `[00:01] Speaker 1: over
300`. The model joined those fragments itself and produced run-on sentences with
the same number stated three times and no sentence boundary to hang a decision
on.

`segment_paragraphs()` in `live_notes.py` groups consecutive same-speaker turns
into one paragraph, timestamp and speaker once per run, and closes each piece:
collapsed whitespace, a full stop after stripping a trailing `,;:—–-` (so
`over 300,` does not become `300,.`), trailing quotes stepped over when deciding
whether a sentence is already closed, and the first letter capitalised unless it
is a digit. An `empty` turn is skipped rather than splitting the run — silence is
not a change of speaker.

Two things about the shape of the fix are worth keeping. It is a **rendering**:
segments, ids and `consumedBy` markers are untouched, so deleting a summarised
segment still addresses the words the note was built from. And it is used by
both readers — `format_segments` for the prompt and `renderLiveSegments` for the
pane, where a continuation turn drops its repeated speaker and time (the time
cell stays empty so the column does not shift) while keeping its own ✏️ / 🗑.

`ParagraphTests` (7) calls the helpers rather than reading them, and
`build_live_prompt` is asserted to emit one line per run. Re-injecting the
per-segment render fails four of them. 1342 tests, `node --check` clean,
`py_compile` clean, `diff --check` clean.

# The note described the garble instead of the meeting (2026-10-27)

- **Reported as** a produced note whose own numbers appeared three times and
  which closed on *"Dan Pyre is identified as suspectable, indicating potential
  issues with the process"*, with the instruction to tell the model it works from
  a transcription and is **allowed** to assume phonetically similar content.
- **Two gaps, not one.** "Leave out small talk" was already there and does not
  reach either: the note restated facts *within* the topic, and it turned speech
  the model could not parse into an apparent conclusion rather than dropping it.
- `LIVE_SYSTEM_PROMPT` (`live_notes.py`) gained two paragraphs. The first makes
  relevance per-sentence — every sentence must carry something a reader would act
  on, a sentence that only restates another is deleted, hedged commentary about
  speech that was not understood is forbidden. The second states that the input
  is **machine speech recognition** and grants the licence explicitly: a garbled
  phrase may be read as the thing it sounds like, preferring a spelling already
  present in the header or the candidate list over a homophone, and a mangled
  number or acronym may be reconstructed from sounds and context.
- **The licence is bounded in the same paragraph** ("correcting a misheard word
  is not inventing a fact", and an unresolvable passage is left out). The
  pre-existing "never invent names, decisions, numbers or facts" would otherwise
  contradict the permission, and an unbounded version is simply a licence to
  invent — which is worse than the garble, because the garble is visibly wrong
  and an invented name is not.
- `PromptTests` pins the four sentences that carry it (`machine speech
  recognition`, `mishears words`, `sounds like`, `not inventing a fact`,
  `leave it out`) and the per-sentence rules. Both mutations were re-injected and
  bit: softening "sounds like" to "literally", and dropping the
  "hedged commentary" clause.
- 1335 tests OK; `py_compile` and `git diff --check` clean. Prompt text only —
  no schema, no route, no template.

---


# Ollama lost the Vulkan device and its CPU fallback dumped core (2026-10-27)

- **Reported as** `ggml_vulkan: device lost on Vulkan0` during a `/api/chat`
  that ran **1m55s** over ~2422 tokens (the note rewrite), followed by
  `GGML_ASSERT(batch.slot_batched || batch.size() == 0) failed` on the CPU
  runner and `llama-server terminated … core dumped` — so the 500 was a dead
  runner, and every later call failed with it.
- **Cause**: VRAM over-commitment on a 4 GB card shared with ASR. Ollama's
  defaults are `OLLAMA_MAX_LOADED_MODELS = 3 × GPU count` and
  `OLLAMA_NUM_PARALLEL = auto`, so granite (2.34 GB) + nemotron (2.63 GB) + the
  embedder could be resident at once against a 16332-token context. Not an app
  bug, and not fixable in the app — a lost device is the driver's answer.
- **Shipped**: `docker-compose.yml` pins `OLLAMA_MAX_LOADED_MODELS=2` and
  `OLLAMA_NUM_PARALLEL=1` with the incident in the comment;
  `common._ollama_failure_hint()` maps the device-lost/terminated body to
  "restart ollama, then `docker ps`/`ollama ps` and the two knobs", appended to
  the `HTTP 5xx` `RuntimeError`. `test_llm_reliability.OllamaFailureHintTests`
  (3) pins the hint and, just as importantly, that an ordinary error gets none.
- **What a future reader would otherwise rediscover**: the note rewrite is the
  single largest prompt in the app, so it is the request that finds an
  over-committed card first; a `500` here is not a prompt problem, and no retry
  policy helps because the runner process is gone. `_ollama_timing` exists for
  the same class of "the number is the diagnosis".
- **Verified**: 1333 tests OK, `py_compile` clean on `common.py`, `diff --check`
  clean.

# Live notes — the panel polled a job the event bus never carried (2026-10-27)

- **Reported as** a question, not a bug: *"why use status queries, when all other
  calls use SSE?"* The honest answer was that `/api/events` was populated only by
  `gui.py` route handlers, so a note job — which runs in `live_manager` below the
  HTTP layer — published nothing at all. Three consequences, one of them a real
  defect: the panel had to poll `GET /status` every 1.5 s, `settleLiveGate` polled
  it every 700 ms, and **a committed note was invisible to Memories and Diary**,
  because no `memory_changed` event was ever emitted for it.
- **Shipped**: `live_manager._announce()` publishes a `live_note_job` snapshot
  from `_set_job` and from `start_note_job` before its task starts (fire-and-forget,
  and a no-op without a running loop so the sync `_set_job` stays callable);
  `_commit_note_window` publishes `memory_changed` after the write and after the
  consumption markers; `gui._announce_gate_release()` publishes
  `live_gate_released` after each of the three `LIVE_GATE.end_recording` calls.
  The dashboard subscribes to both in `initSSE`, `scheduleLivePoll` returns early
  while `sseOpen()` (re-armed by `onerror`, disarmed by `onopen`), and
  `settleLiveGate` waits on `waitForLiveGate()` — a resolver list the gate event
  wakes — before falling back to `/status`.
- **What a future reader would otherwise rediscover**: `renderLiveControls(null)`
  throws (`null.segmentCount`) because it labels the state from the record. An SSE
  event can arrive before a transcription is open, so both handlers guard the
  render. The node harness surfaced that as a thrown `null.segmentCount`; in the
  browser the listener's `try/catch` would have swallowed it.
- **Guards**: `test_live_notes.LiveJobEventTests` (6 — `_announce` **called**
  against a recording bus, plus the two call-site orderings and the
  release-per-exit count) and `LivePanelPushTests` (5 — listener registration,
  the `sseOpen()` guard, and two handlers driven through the fake DOM: a gate
  event clears the flag *and* wakes a waiter, another recording's event is
  ignored). Mutations bit: deleting `_announce(job)` from `_set_job`, and deleting
  the `sseOpen()` block.
- **Verified**: 1330 tests OK (was 1319), `node --check` on the dashboard script,
  `py_compile` on `live_manager.py`/`gui.py`/`test_live_notes.py`, `diff --check`
  clean.

# Live notes — the model answered with word salad (2026-10-27)

- **Reported as** `content:"Pro Ad\n Access\n At S An Ac A U Ar Under Of…"` —
  62 tokens of vocabulary noise for a JSON rewrite, `done_reason:"stop"`,
  `eval_count:62`. Not an empty answer, so the empty-answer retry from the
  previous fix did not fire: `parse_note_json` rejected it *after* the call
  returned, and the job died with `the model answer contained no JSON object`.
- **The retry now wraps the parse.** `_call_live_llm(..., parse=…)` applies a
  callable to the answer inside the retry loop and retries a `LiveNoteError`
  once with `LIVE_SAMPLING_RETRY`. An unusable non-empty answer and an empty one
  were the same fault treated two ways — one got a second attempt, the other a
  dead end.
- **`LiveNoteTooLong` is re-raised before the parse handler and never retried.**
  It is a good answer that is merely too long, and the caller's compaction pass
  is what handles it; retrying would ask the same model for the same length
  again. Handler order is the same trap as `MergeDraftTooLarge`.
- **Both call sites parse through the call now** — the window rewrite with the
  permissive `max_chars=max(LIVE_NOTE_MAX_CHARS, len(text) + 1)` so compaction
  still receives an over-long answer, and the compaction pass with the hard
  limit.
- **The word salad is also evidence about the model.** The same run shows prompt
  evaluation at ~12,500 tok/s with 2448 of 2449 tokens cached, so the GPU is
  healthy here — this is `granite3.3:2b` losing the thread on a 2.4k-token
  Markdown rewrite, not a slow host. It is excellent at the short-prompt
  extraction it is configured for and is the wrong model for this call:
  `MEM_LIVE_MODEL` (defaulting to `MEM_EXTRACT_MODEL`) is the knob, and
  `nemotron-3-nano:4b` is already resident as the judgement model.
- Guards: two new `NoteCallRetryTests` drive garbage-then-good (retried, retry
  set used) and too-long (one call only), plus two-garbage naming the model and
  `MEM_LIVE_MODEL`. The scripted fake now raises a named `AssertionError` when
  its replies run out, so removing the retry reports "the retry never happened"
  rather than an `IndexError` from the harness.
- Mutations that bit: `pass` instead of `raise` for `LiveNoteTooLong`, and
  dropping the retry from the parse branch.
- **1319 tests OK**; `py_compile`/`diff --check` clean.

---


# Live notes — an empty answer at 29 tok/s was CPU inference (2026-10-27)

- **The report was a "Rebuild note" with every model already unloaded**, which
  retires the contention theory the previous message carried: nothing was
  competing for the card, so `2449 tokens in 85 s` (~29 tok/s) is **CPU
  inference** — the documented consequence of a deploy that dropped
  `docker-compose.gpu.yml`. The load was fast (2.7 s) and the answer was one
  token, so the model was resident; it was just being evaluated on the processor.
- **`_ollama_timing()` in `common.py` turns Ollama's nanosecond fields into one
  readable clause** on the empty-answer ERROR line:
  `prompt=2449 tok in 85.1s (29 tok/s), answer=1 tok, load=2.7s,
  done_reason=stop`. It appends the GPU check **only under 100 tok/s**, because a
  *fast* empty answer is a different fault and must not send every operator
  hunting the overlay. Unparseable bodies return `""` rather than raising.
- **The old wording was actively wrong**: *"check `docker logs ollama` for memory
  pressure"* names a cause this deployment did not have. The log now states the
  measurement and the one check that distinguishes the faults.
- **`_call_live_llm`'s failure message was wrong in the same way** — it claimed
  the model was "struggling for memory next to the transcriber". It now points at
  the timing lines and at `docker exec ollama ollama ps` saying `100% GPU`, with
  `MEM_LIVE_MODEL` as the lever.
- `EmptyAnswerTimingTests` (4) calls the helper with the production numbers,
  pins `answer=1 tok` / `done_reason=stop` / `load=2.7s`, drives the logged ERROR
  line end to end, and asserts a 4898 tok/s prompt is **not** blamed on the CPU.
  Both directions of the rate guard were mutation-checked, as were the
  empty-answer retry's `if True: break` and a retry set equal to the first.
- **1317 tests OK**; `py_compile`/`diff --check` clean.

---


# Live notes — the model answered with nothing after 85 s (2026-10-27)

- **Reported as an empty 200**: `content:""`, `done_reason:"stop"`,
  `eval_count:1`, `prompt_eval_count:2449` with `prompt_eval_duration` 85 s, then
  `LLM returned no content` and `note job failed ... the model returned an empty
  answer`. The model loaded, read the prompt and emitted one token.
- **At temperature 0 an empty answer is a fixed point.** The same request
  produces the same nothing, so the previous retry policy (repeat aborts only)
  left this failure with no remedy but a manual retry. The empty answer is the
  same diagnosis as the repeat abort one step further: the decoder, not the
  prompt.
- **`_call_live_llm` now retries an empty answer once with
  `LIVE_SAMPLING_RETRY`** (temperature 0.35, penalty 1.4 over 64 tokens,
  `top_p: 0.9`) and raises `LiveNoteError` after the second one. A
  whitespace-only answer counts as empty — `answer or ""` used to let it through
  to `parse_note_json`, which then failed with a message about the note body.
- **The failure message now names the residency check.** 2449 prompt tokens in
  85 s is ~29 tok/s, i.e. a card shared with the transcriber rather than a prompt
  problem, so it says `docker exec ollama ollama ps` and `MEM_LIVE_MODEL` instead
  of "Ollama returned HTTP 500". Nothing is consumed on a failed window.
- Guards: `NoteCallRetryTests` **drives** the real `_call_live_llm` against a
  scripted `get_llm_response` (lifted with `ast`, so `live_manager.py` itself
  stays unimportable here) — empty-then-good retries with the retry set,
  whitespace counts as empty, one abort retries, and the **call count** is
  asserted for a timeout and a missing model, since "it raised" reads identically
  whether it was retried first.
- Mutations that bit: `if True: break` on the empty answer (two tests),
  `(LIVE_SAMPLING, LIVE_SAMPLING)` for the retry set, and the earlier
  retry-temperature/policy pair.
- **1313 tests OK**; `py_compile`/`diff --check` clean.

---


# Live notes — the model looped until Ollama aborted the note (2026-10-27)

- **Reported as an error, not a symptom**:
  `{"error":"prediction aborted, token repeat limit reached"}`, HTTP 500, then
  `live notes: note job failed ... Ollama returned HTTP 500`. That is Ollama's
  own guard stopping a degenerate prediction, and the app's default sampling
  invites it: every chat call here passes `temperature: 0.0`, which is right for
  short JSON answers and wrong for the one call that rewrites a long Markdown
  document — greedy decoding re-emits a heading or a bullet until the guard fires.
- **`get_llm_response` gained an `options` dict** merged over the default
  `{"temperature": 0.0}`, caller keys winning *including* temperature. Without
  that override a retry would be the same decode under a different label. Every
  existing caller is unchanged.
- **`LIVE_SAMPLING`** sends `repeat_penalty: 1.15` / `repeat_last_n: 256` and
  keeps temperature at 0, so the normal path stays reproducible.
  **`LIVE_SAMPLING_RETRY`** is stronger and narrower (penalty 1.4 over 64 tokens,
  temperature 0.35, `top_p: 0.9`): a retry at the same settings is not a retry.
- **Exactly one retry, and only for this abort** — `if attempt or not
  is_repeat_abort(exc): raise` is the whole policy, because a timeout or a
  missing model cannot improve by being asked twice. `is_repeat_abort()` is in
  `live_notes.py` (stdlib) so the suite calls it: the remedy is unique, since
  waiting does nothing and only different sampling has a chance.
- **A second abort raises `LiveNoteError` naming `MEM_LIVE_NOTE_MAX_CHARS` and
  `MEM_LIVE_MODEL`** instead of surfacing the raw 500, and nothing is consumed on
  a failed window — the segments are still there for the next pause or **Rebuild
  note**.
- Guards: `RepeatAbortTests` (the predicate against the real string plus three
  that must not match; the two sampling sets as a pair; the call site's
  one-call-in-a-loop shape) and two `test_llm_reliability.HappyPathTests` driving
  the `options` merge through the recording fake client. Mutations that bit:
  `if True: raise`, and a retry temperature of 0.0.
- **1308 tests OK**; `py_compile`/`diff --check` clean.

---



---

# Live notes — the note kept the rambling and linked the whole project (2026-10-27)

- **Two complaints, and the first was self-inflicted by the prompt.** *"The notes
  should ignore non-relevant ramblings, the note should be about the topic"* —
  the system prompt said **"incorporate every new section"**, which is an
  instruction to keep the rambling. It now states the topic (the client and
  project in the header), what belongs to it, what does not (small talk, side
  conversations, unrelated work), and `Summarise; do not transcribe`.
- **"Only the note-relevant links should be created"** had a code cause as well
  as a prose one. `parse_note_json` **unioned** `referencedFactIds` into the
  note's existing references, so the list could only grow: one early over-link
  was permanent, and the model had no visible way to prune. The answer now
  **replaces** the list, the prompt says so, and `BLOCK_REFERENCES` reads "FACTS
  THIS NOTE REFERENCES TODAY (return the ones to keep)".
- **Absent is not empty.** An answer that omits the key keeps the existing
  references (the prompt's "return the note unchanged" case must not unlink
  everything); an explicit `[]` clears them. Both are pinned.
- **The candidate list was the whole client scope and read as a checklist.** The
  prompt now says it is *context, not a checklist*, that most sections need no
  reference at all, and that a fact is not relevant merely because it shares the
  client or project.
- **`MEM_LIVE_REFERENCE_LIMIT` (default 12) is a backstop, not a target** — a
  note citing more than a dozen records is describing the project. Wired in
  `.env.example` and compose; `MEM_LIVE_FACT_LIMIT` (100) still bounds the
  candidate titles, ranked before the cap, and lowering it is the next lever.
- Guards: three `PromptTests` pinning the sentences, and `ParseNoteTests`
  covering replace / omit / empty / cap. Three mutations bit — restoring the
  union (three failures), dropping the cap, and softening "not a checklist".
- **1303 tests OK**; `py_compile`/`diff --check` clean. The template was not
  touched, so no `node --check`.

---



# Live notes — the pause answered its own summarise with 409 (2026-10-27)

- **Reported as two 409s**, `/summarize` and `/reprocess`, from clicks in the
  Live pane. `RecordingActiveError` is the only 409 those routes can raise, so
  the server still considered the microphone open at a moment the pane said
  "Paused".
- **That window is by design and the UI was guessing.** A pause acks only after
  asr-mcp finishes the last sentence (the tail drain, up to 20 s), and the stop
  path's `finally` sits behind a 5 s `queue.join()`, so the gate outlives the
  browser's own flags. `pauseLiveRecording` set `livePaused = true`
  optimistically, which re-enabled "Update now" during the drain — a click that
  could only ever 409.
- **Fix: a third flag, `liveGatePending`, set when the pause or stop is asked
  for and cleared by the server.** The `paused` ack clears it (the server calls
  `end_recording` before sending it) and `settleLiveGate()` polls `GET
  /api/live/transcriptions/{id}/status` for `recording === false` after a stop.
  Update / Rebuild / Delete are disabled while it is set and the state line says
  "Finishing the last seconds…".
- **A 409 that still slips through is a wait, not a failure.** Both actions set
  the flag, await the gate and ask for a second press. Reporting a transient,
  self-clearing refusal as an error is a message the user cannot act on.
- **`assert_idle`'s message was wrong in the same direction.** It said "Pause the
  microphone before updating notes" to a user who had just paused. It now allows
  for the tail ("its last seconds are still being transcribed… try again"), and
  both routes raise it through `_conflict` so the reason is logged — a bare
  `HTTPException(409)` formats the reason into a response body and drops it.
- Guards: `LiveGateRaceTests` (6) — the message, `_conflict` at both call sites,
  the pending flag set/cleared around the pause ack, the disabled actions, and
  that the gate is read from `/status` rather than assumed. Three mutations bit
  (dropping `liveGatePending` from the update disable; reverting a route to a
  bare 409; restoring the old message), then were restored.
- **1297 tests OK**; `node --check` clean on the extracted dashboard script,
  `py_compile`/`diff --check` clean.

---



- **Reported as a console line**: `[DOM] Password field is not contained in a
  form … <input id="asr-token-input" type="password">`. The Setup card's field
  sat in a `<div id="asr-token-form">` — the id said form and the element was
  not one — so Chrome warned on every visit and a password manager could offer
  to save the asr-mcp key against this site instead of leaving it alone.
- **It is now a real `<form>`** whose `onsubmit` is `saveAsrToken(); return
  false;`. The wrapping is functional in both directions: the warning goes away,
  and Enter in the field now saves, which it previously did nothing at all.
- **The trap in the fix is `<button>` defaulting to `type="submit"`.** Save
  became `type="submit"` and lost its `onclick` (an `onclick` plus the form
  handler would save twice), while Test and Clear needed an explicit
  `type="button"` — without it, Clear submits first, so `saveAsrToken()` runs
  against an empty field and toasts "Paste an asr-mcp API key first" before
  anything is cleared.
- **No JS changed.** `renderAsrToken` only touches the state line, hint, the two
  buttons' `disabled` and the input placeholder; nothing read
  `#asr-token-form` as an element, so the tag change was safe.
- Guards: `SetupPageAuthGuidanceTests.test_the_asr_key_field_is_inside_a_real_form`
  (the marker is the opening `<form …>` tag, so a regression to a `<div>` fails
  as a missing marker) and `test_only_the_save_button_submits_the_asr_key_form`.
  Both mutation-checked (form→div fails both; dropping `type="button"` from
  Clear fails the second with the button named), then restored.
- **1291 tests OK**, `node --check` clean on the extracted dashboard script,
  `py_compile`/`diff --check` clean.

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
