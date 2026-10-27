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
