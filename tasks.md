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
