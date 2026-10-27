"""Tests for the dependency-light Live notes helpers.

`live_notes.py` imports nothing from the app, so unlike most suites here these
tests **call** the shipping functions instead of lifting source segments. That
matters because every property worth guarding is behavioural: what the prompt
contains, which ids survive validation, and whether the generation counter
actually preempts a job are all invisible in the shape of the code.
"""

import unittest

import live_notes as ln


def seg(start, text, speaker="Ana", end=None):
    return {
        "startSec": start,
        "endSec": start + 5 if end is None else end,
        "speaker": speaker,
        "text": text,
    }


def gui_functions(names, namespace):
    """Exec selected top-level functions out of gui.py into `namespace`.

    gui.py cannot be imported here (no fastapi, no database drivers), so the
    shipping functions are lifted with `ast` and **called**. A source assertion
    would not be able to tell a probe that checks the cheap endpoint from one
    that opens a socket, and that difference is the whole property below.
    """
    import ast
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    wanted = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name in names]
    missing = set(names) - {n.name for n in wanted}
    if missing:
        raise AssertionError(f"gui.py has no top-level {sorted(missing)}")
    module = ast.Module(body=wanted, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, path, "exec"), namespace)
    return namespace


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.segments = [seg(0, "We should migrate the PPC reports first."),
                         seg(65, "Jane Smith will own the cutover.")]
        self.referenced = [{"id": "f1", "name": "PPC migration", "text": "BODY-SECRET-1"}]
        self.candidates = [{"id": "f2", "name": "Jane Smith", "text": "BODY-SECRET-2"},
                           {"id": "f3", "name": "Cutover checklist", "text": "BODY-SECRET-3"}]
        self.note = {"name": "Kickoff", "text": "## Decisions\n- Migrate in May"}

    def prompt(self, **kwargs):
        values = {
            "note": self.note,
            "segments": self.segments,
            "referenced": self.referenced,
            "candidates": self.candidates,
            "client": "EPAM",
            "context": "PPC",
        }
        values.update(kwargs)
        return ln.build_live_prompt(**values)

    def test_prompt_has_exactly_the_four_content_blocks(self):
        prompt = self.prompt()
        for heading in (ln.BLOCK_NOTE, ln.BLOCK_TRANSCRIPT,
                        ln.BLOCK_REFERENCES, ln.BLOCK_CANDIDATES):
            self.assertEqual(prompt.count(f"### {heading}"), 1)
        # A fifth heading is how an unreviewed block gets added.
        self.assertEqual(prompt.count("### "), 4)

    def test_fact_bodies_never_reach_the_prompt(self):
        prompt = self.prompt()
        self.assertNotIn("BODY-SECRET-1", prompt)
        self.assertNotIn("BODY-SECRET-2", prompt)
        self.assertNotIn("BODY-SECRET-3", prompt)

    def test_candidates_are_id_and_title_only(self):
        prompt = self.prompt()
        self.assertIn("- f2: Jane Smith", prompt)
        self.assertIn("- PPC migration", prompt)
        self.assertNotIn("- f1: PPC migration", prompt)

    def test_existing_note_title_and_text_are_included(self):
        prompt = self.prompt()
        self.assertIn("Kickoff", prompt)
        self.assertIn("Migrate in May", prompt)

    def test_client_and_project_are_named(self):
        prompt = self.prompt()
        self.assertIn("Client: EPAM", prompt)
        self.assertIn("Project: PPC", prompt)

    def test_missing_note_and_scope_have_explicit_placeholders(self):
        prompt = self.prompt(note=None, client="", context="")
        self.assertIn("(no note yet)", prompt)
        self.assertIn("(none identified)", prompt)

    def test_only_the_segments_passed_are_rendered(self):
        prompt = self.prompt(segments=[self.segments[1]])
        self.assertIn("Jane Smith will own", prompt)
        self.assertNotIn("migrate the PPC reports", prompt)

    def test_blank_segments_are_not_rendered_as_speech(self):
        prompt = self.prompt(segments=[seg(0, "  "), seg(10, "Real speech")])
        self.assertIn("Real speech", prompt)
        self.assertNotIn("[00:00] Ana:  \n", prompt)

    def test_timestamps_are_minutes_and_seconds(self):
        self.assertEqual(ln.format_timestamp(0), "00:00")
        self.assertEqual(ln.format_timestamp(65), "01:05")
        self.assertEqual(ln.format_timestamp(3661), "1:01:01")
        self.assertEqual(ln.format_timestamp(None), "00:00")


class CandidateTests(unittest.TestCase):
    def test_title_overlap_ranks_first(self):
        candidates = [{"id": "a", "name": "Annual budget"},
                      {"id": "b", "name": "Jane Smith"},
                      {"id": "c", "name": "Office move"}]
        ranked = ln.rank_fact_candidates(
            candidates, [seg(0, "Jane Smith raised the cutover risk.")], limit=3)
        self.assertEqual(ranked[0]["id"], "b")

    def test_verbatim_title_beats_token_overlap(self):
        candidates = [{"id": "a", "name": "Jane Smith"},
                      {"id": "b", "name": "Jane Smith cutover"}]
        ranked = ln.rank_fact_candidates(
            candidates, [seg(0, "We agreed Jane Smith cutover plan")], limit=2)
        self.assertEqual(ranked[0]["id"], "b")

    def test_referenced_ids_are_excluded_not_repeated(self):
        ranked = ln.rank_fact_candidates(
            [{"id": "a", "name": "Jane Smith"}], [], referenced_ids=["a"])
        self.assertEqual(ranked, [])

    def test_cap_is_applied_and_order_is_deterministic(self):
        candidates = [{"id": str(i), "name": f"Topic {i}"} for i in range(5)]
        ranked = ln.rank_fact_candidates(candidates, [], limit=2)
        self.assertEqual([item["id"] for item in ranked], ["0", "1"])

    def test_duplicate_ids_are_dropped(self):
        ranked = ln.rank_fact_candidates(
            [{"id": "a", "name": "One"}, {"id": "a", "name": "One again"}], [], limit=10)
        self.assertEqual(len(ranked), 1)

    def test_ranking_returns_titles_only(self):
        ranked = ln.rank_fact_candidates(
            [{"id": "a", "name": "Jane", "text": "SECRET"}], [], limit=10)
        self.assertEqual(ranked, [{"id": "a", "name": "Jane"}])

    def test_blank_candidates_are_ignored(self):
        self.assertEqual(ln.rank_fact_candidates([{"id": "", "name": ""}], []), [])


class ParseNoteTests(unittest.TestCase):
    ALLOWED = {"f1", "f2"}

    def parse(self, raw, existing=(), max_chars=1000):
        return ln.parse_note_json(raw, self.ALLOWED, existing, max_chars=max_chars)

    def test_fenced_json_is_accepted(self):
        result = self.parse('```json\n{"title":"T","text":"Body","referencedFactIds":["f1"]}\n```')
        self.assertEqual(result, {"title": "T", "text": "Body", "referencedFactIds": ["f1"]})

    def test_prose_around_the_object_is_ignored(self):
        result = self.parse('Here it is:\n{"title":"T","text":"Body"}\nThanks')
        self.assertEqual(result["text"], "Body")

    def test_unknown_fact_ids_are_dropped(self):
        result = self.parse('{"title":"T","text":"Body","referencedFactIds":["f1","nope"]}')
        self.assertEqual(result["referencedFactIds"], ["f1"])

    def test_existing_references_are_unioned_in_front(self):
        result = self.parse('{"title":"T","text":"Body","referencedFactIds":["f2","f1"]}',
                            existing=["f1", "old"])
        self.assertEqual(result["referencedFactIds"], ["f1", "f2"])

    def test_missing_title_defaults(self):
        self.assertEqual(self.parse('{"text":"Body"}')["title"], "Live notes")

    def test_long_title_is_truncated_but_text_is_not(self):
        title = "x" * 500
        result = self.parse('{"title":"%s","text":"Body"}' % title)
        self.assertEqual(len(result["title"]), ln.NOTE_TITLE_MAX_CHARS)

    def test_empty_text_is_refused(self):
        with self.assertRaises(ln.LiveNoteError):
            self.parse('{"title":"T","text":"   "}')

    def test_over_long_text_is_refused_rather_than_sliced(self):
        with self.assertRaises(ln.LiveNoteTooLong):
            self.parse('{"title":"T","text":"%s"}' % ("y" * 50), max_chars=20)

    def test_missing_json_is_refused(self):
        with self.assertRaises(ln.LiveNoteError):
            self.parse("I could not do that.")

    def test_invalid_json_is_refused(self):
        with self.assertRaises(ln.LiveNoteError):
            self.parse('{"title": "T", "text": }')

    def test_non_object_json_is_refused(self):
        with self.assertRaises(ln.LiveNoteError):
            self.parse('["not", "a", "note"]')

    def test_string_reference_is_accepted_as_one_id(self):
        result = self.parse('{"title":"T","text":"Body","referencedFactIds":"f1"}')
        self.assertEqual(result["referencedFactIds"], ["f1"])

    def test_non_list_references_are_ignored(self):
        result = self.parse('{"title":"T","text":"Body","referencedFactIds":{"a":1}}')
        self.assertEqual(result["referencedFactIds"], [])

    def test_duplicate_references_are_collapsed(self):
        result = self.parse('{"title":"T","text":"Body","referencedFactIds":["f1","f1"]}')
        self.assertEqual(result["referencedFactIds"], ["f1"])


class BudgetTests(unittest.TestCase):
    def test_budget_is_context_minus_prompt(self):
        result = ln.live_output_budget(27000, context_tokens=10000, min_predict=1000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["prompt_tokens"], 9000)
        self.assertEqual(result["budget"], 1000)

    def test_floor_is_inclusive(self):
        self.assertTrue(ln.live_output_budget(
            27000, context_tokens=10000, min_predict=1000)["ok"])

    def test_one_token_below_the_floor_is_refused(self):
        result = ln.live_output_budget(27003, context_tokens=10000, min_predict=1000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["budget"], 999)
        self.assertIn("MEM_LIVE_CONTEXT_TOKENS", result["reason"])

    def test_prompt_larger_than_context_is_refused_without_a_negative_budget(self):
        result = ln.live_output_budget(60000, context_tokens=10000, min_predict=1000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["budget"], 0)

    def test_prompt_estimate_rounds_up(self):
        self.assertEqual(ln.estimate_prompt_tokens(1), 1)
        self.assertEqual(ln.estimate_prompt_tokens(0), 0)
        self.assertEqual(ln.estimate_prompt_tokens(4), 2)


class WindowTests(unittest.TestCase):
    def test_blank_segments_produce_no_windows(self):
        self.assertEqual(ln.segment_windows([]), [])
        self.assertEqual(ln.segment_windows([seg(0, "   ")]), [])

    def test_short_input_is_one_window(self):
        segments = [seg(0, "one"), seg(10, "two")]
        self.assertEqual(ln.segment_windows(segments, window=1000), [segments])

    def test_every_segment_appears_exactly_once_in_order(self):
        segments = [seg(i, "word " * 20) for i in range(8)]
        windows = ln.segment_windows(segments, window=100)
        flat = [item for window in windows for item in window]
        self.assertEqual(flat, segments)

    def test_no_segment_is_split_across_windows(self):
        segments = [seg(0, "x" * 200), seg(10, "y" * 50)]
        windows = ln.segment_windows(segments, window=100)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0], [segments[0]])

    def test_oversize_segment_gets_its_own_window_instead_of_being_dropped(self):
        windows = ln.segment_windows([seg(0, "x" * 5000)], window=100)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][0]["text"], "x" * 5000)


class OffsetTests(unittest.TestCase):
    def test_offset_is_added_to_both_ends(self):
        result = ln.with_time_offset(seg(10, "hello", end=14), 100)
        self.assertEqual((result["startSec"], result["endSec"]), (110, 114))

    def test_negative_offset_is_clamped(self):
        result = ln.with_time_offset(seg(10, "hello"), -5)
        self.assertEqual(result["startSec"], 10)

    def test_original_is_not_mutated(self):
        original = seg(10, "hello")
        ln.with_time_offset(original, 100)
        self.assertEqual(original["startSec"], 10)

    def test_offset_list_preserves_order(self):
        result = ln.offset_segments([seg(0, "a"), seg(5, "b")], 60)
        self.assertEqual([item["startSec"] for item in result], [60, 65])


class OriginTests(unittest.TestCase):
    BASE = "https://mcp.example.test/mem-mcp"

    def test_same_origin_is_allowed_despite_the_mount_path(self):
        self.assertTrue(ln.origin_allowed("https://mcp.example.test", self.BASE))

    def test_case_and_trailing_slash_are_normalised(self):
        self.assertTrue(ln.origin_allowed("HTTPS://MCP.EXAMPLE.TEST/", self.BASE))

    def test_other_scheme_is_refused(self):
        self.assertFalse(ln.origin_allowed("http://mcp.example.test", self.BASE))

    def test_other_host_and_port_are_refused(self):
        self.assertFalse(ln.origin_allowed("https://evil.test", self.BASE))
        self.assertFalse(ln.origin_allowed("https://mcp.example.test:8443", self.BASE))

    def test_absent_origin_is_allowed_for_non_browser_clients(self):
        self.assertTrue(ln.origin_allowed("", self.BASE))
        self.assertTrue(ln.origin_allowed(None, self.BASE))

    def test_null_origin_is_refused(self):
        self.assertFalse(ln.origin_allowed("null", self.BASE))

    def test_relative_base_fails_closed_for_a_supplied_origin(self):
        self.assertFalse(ln.origin_allowed("https://evil.test", "/mem-mcp"))

    def test_request_origin_can_stand_in_for_a_relative_base(self):
        self.assertTrue(ln.origin_allowed(
            "https://mcp.example.test", "/mem-mcp", request_origin=self.BASE))

    def test_no_base_at_all_still_allows_an_absent_origin(self):
        self.assertTrue(ln.origin_allowed("", ""))
        self.assertFalse(ln.origin_allowed("https://evil.test", ""))


class PriorityGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = ln.LivePriorityGate()

    def test_begin_marks_recording_and_increments_generation(self):
        first = self.gate.begin_recording("alice")
        second = self.gate.begin_recording("alice")
        self.assertTrue(self.gate.is_recording("alice"))
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(self.gate.current_generation("alice"), 2)

    def test_assert_idle_raises_while_recording(self):
        self.gate.begin_recording("alice")
        with self.assertRaises(ln.RecordingActiveError):
            self.gate.assert_idle("alice")

    def test_end_recording_allows_work_again(self):
        self.gate.begin_recording("alice")
        self.gate.end_recording("alice")
        self.assertFalse(self.gate.is_recording("alice"))
        self.gate.assert_idle("alice")

    def test_begin_invokes_and_clears_the_llm_cancel_hook(self):
        calls = []
        self.assertTrue(self.gate.register_llm_cancel("alice", lambda: calls.append(1)))
        self.gate.begin_recording("alice")
        self.assertEqual(calls, [1])
        self.gate.end_recording("alice")
        self.gate.begin_recording("alice")
        self.assertEqual(calls, [1])

    def test_cancel_cannot_be_registered_while_recording(self):
        self.gate.begin_recording("alice")
        self.assertFalse(self.gate.register_llm_cancel("alice", lambda: None))

    def test_clear_only_removes_the_matching_hook(self):
        calls = []
        first = lambda: calls.append("first")
        second = lambda: calls.append("second")
        self.gate.register_llm_cancel("alice", first)
        self.gate.clear_llm_cancel("alice", second)
        self.gate.begin_recording("alice")
        self.assertEqual(calls, ["first"])

    def test_a_failing_hook_cannot_block_recording(self):
        def boom():
            raise RuntimeError("job bug")
        self.gate.register_llm_cancel("alice", boom)
        self.gate.begin_recording("alice")
        self.assertTrue(self.gate.is_recording("alice"))

    def test_users_are_isolated(self):
        self.gate.begin_recording("alice")
        self.assertFalse(self.gate.is_recording("bob"))
        self.gate.assert_idle("bob")

    def test_empty_user_is_refused(self):
        with self.assertRaises(ValueError):
            self.gate.begin_recording("")


class _FakeResult:
    def __init__(self, rows=None, single=None):
        self._rows = rows or []
        self._single = single

    def single(self):
        return self._single

    def data(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.queries.append((query, params))
        return self.handler(query, params)


class _FakeDriver:
    def __init__(self, handler):
        self.handler = handler
        self.sessions = []

    def session(self):
        session = _FakeSession(self.handler)
        self.sessions.append(session)
        return session

    @property
    def queries(self):
        return [query for session in self.sessions for query, _ in session.queries]


def _lift_manager_function(name, **extra_globals):
    """Exec one function out of `live_manager.py` with stubs for its imports.

    `live_manager` imports `common`, which imports `httpx`, so it cannot be
    imported on a machine with no web dependencies. Lifting the single function
    keeps the real body under test rather than a copy of it.
    """
    import ast
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "live_manager.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"__builtins__": __builtins__}
    namespace.update(extra_globals)
    exec(compile(module, path, "exec"), namespace)
    return namespace[name]


class SegmentStorageTests(unittest.TestCase):
    """The L4 invariants that are about *not* writing somewhere.

    A segment edit must not rewrite the note, and deleting a segment must not
    delete the note. Both are properties of which nodes a query names, so the
    tests inspect the queries the real function issued.
    """

    def _node(self):
        return {"id": "seg-1", "transcriptionId": "tr-1", "seq": 1,
                "startSec": 0.0, "endSec": 4.0, "speaker": "Ana",
                "text": "We should migrate PPC first."}

    def test_a_segment_edit_never_touches_a_note_fact(self):
        node = self._node()
        driver = _FakeDriver(lambda query, params: _FakeResult(single={"seg": node}))
        update = _lift_manager_function(
            "db_update_segment",
            _require_driver=lambda: driver,
            _segment_dict=lambda value: value,
            Optional=object,
        )
        result = update("tr-1", "seg-1", "alice",
                        text="We should migrate PPC reports first.")
        self.assertEqual(result["text"], "We should migrate PPC first.")
        joined = "\n".join(driver.queries)
        self.assertIn("seg.text = coalesce", joined)
        self.assertIn("seg.editedAt", joined)
        self.assertNotIn(":Fact", joined)
        self.assertNotIn("REFERENCES", joined)
        self.assertNotIn("consumedBy", joined)

    def test_deleting_a_segment_cannot_delete_the_note(self):
        driver = _FakeDriver(
            lambda query, params: _FakeResult(single={"n": 1})
            if "RETURN count(seg)" in query else _FakeResult()
        )
        delete = _lift_manager_function(
            "db_delete_segment",
            _require_driver=lambda: driver,
            Optional=object,
        )
        self.assertTrue(delete("tr-1", "seg-1", "alice"))
        joined = "\n".join(driver.queries)
        self.assertIn("DETACH DELETE seg", joined)
        self.assertNotIn(":Fact", joined)
        self.assertNotIn("REFERENCES", joined)

    def test_a_missing_segment_is_a_false_not_a_delete(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(single={"n": 0}))
        delete = _lift_manager_function(
            "db_delete_segment",
            _require_driver=lambda: driver,
            Optional=object,
        )
        self.assertFalse(delete("tr-1", "missing", "alice"))
        self.assertFalse(any("DETACH DELETE" in query for query in driver.queries))


class AsrProxyTests(unittest.TestCase):
    """The parts of the proxy that can be exercised without a socket."""

    def _route_source(self):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        start = source.index('@web_app.websocket("/api/live/stream')
        end = source.index('@web_app.get("/api/events")', start)
        return source[start:end]

    def test_url_helper_replaces_language_and_keeps_other_parameters(self):
        import ast
        import os
        import urllib.parse
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_asr_url_with_language")
        module = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"__builtins__": __builtins__, "urllib": urllib}
        exec(compile(module, path, "exec"), namespace)
        build = namespace["_asr_url_with_language"]
        self.assertEqual(
            build("ws://asr:8080/api/asr/ws/stream?foo=1&language=de", "auto"),
            "ws://asr:8080/api/asr/ws/stream?foo=1&language=auto",
        )
        self.assertEqual(
            build("wss://asr/api/asr/ws/stream", "hu"),
            "wss://asr/api/asr/ws/stream?language=hu",
        )

    def test_identity_and_ownership_come_before_the_upstream_connection(self):
        route = self._route_source()
        token = route.index("get_asr_token")
        owner = route.index("db_get_transcription")
        begin = route.index("begin_recording")
        connect = route.index("_ws_connect(")
        self.assertLess(owner, begin)
        self.assertLess(token, begin)
        self.assertLess(begin, connect)
        self.assertIn("X-API-Key", route)

    def test_transcript_is_forwarded_before_it_is_queued_for_writing(self):
        route = self._route_source()
        forwarded = route.index("await websocket.send_text(raw)")
        queued = route.index("queue.put_nowait")
        self.assertLess(forwarded, queued)
        self.assertIn("asyncio.to_thread(\n                    mem.db_append_segment", route)

    def test_the_gate_is_released_on_every_exit_path(self):
        route = self._route_source()
        self.assertGreaterEqual(route.count("end_recording"), 1)
        self.assertIn("finally:", route)
        self.assertIn("LIVE_GATE.end_recording(user_id)", route)

    def test_pause_releases_the_gate_before_it_acknowledges(self):
        """A paused microphone must allow the summarise it just triggered.

        The gate used to stay set for the whole pause, so every
        pause-triggered job answered 409 while the socket looked closed. The
        release has to be before the `paused` message: the browser starts the
        job from that message.
        """
        route = self._route_source()
        pause = route.index('if kind == "pause":')
        resume = route.index('elif kind == "resume":', pause)
        branch = route[pause:resume]
        self.assertIn("LIVE_GATE.end_recording(user_id)", branch)
        self.assertLess(branch.index("LIVE_GATE.end_recording(user_id)"),
                        branch.index('{"type": "paused"}'),
                        "the paused acknowledgement would race the gate release")

    def test_resume_acquires_the_gate_before_opening_upstream(self):
        route = self._route_source()
        resume = route.index('elif kind == "resume":')
        branch = route[resume:resume + 1200]
        self.assertIn("LIVE_GATE.begin_recording(user_id)", branch)
        self.assertLess(branch.index("LIVE_GATE.begin_recording(user_id)"),
                        branch.index("open_upstream("),
                        "resume must preempt a queued note job before audio flows")


    def test_a_plain_http_handshake_is_answered_with_the_reason(self):
        """A dropped Upgrade must not be reported as a bad access key.

        A WebSocketRoute cannot match an http scope, so before this route existed
        the handshake fell through to the catch-all MCP mount and was refused
        with a 401 naming an access key. The realistic cause is a proxy that
        stripped `Upgrade`/`Connection`, so the answer names the directive and
        the status is the one that means "use the upgrade protocol".
        """
        import asyncio
        import types

        class JSON:
            def __init__(self, content, status_code=200, headers=None):
                self.content = content
                self.status_code = status_code
                self.headers = headers or {}

        messages = []
        namespace = {
            "JSONResponse": JSON,
            "Request": object,      # only ever an annotation
            "mem": types.SimpleNamespace(
                logger=types.SimpleNamespace(warning=messages.append)),
            "web_app": types.SimpleNamespace(
                get=lambda *args, **kwargs: (lambda fn: fn)),
        }
        gui_functions({"live_stream_needs_upgrade"}, namespace)
        response = asyncio.run(namespace["live_stream_needs_upgrade"](
            "t1", types.SimpleNamespace(method="GET")))
        self.assertEqual(response.status_code, 426)
        self.assertEqual(response.headers.get("Upgrade"), "websocket")
        self.assertIn("Upgrade", response.content["detail"])
        self.assertIn("proxy_http_version 1.1", response.content["detail"])
        # Logged: the log is the only place an operator sees why no socket
        # opened, and this request never reaches the WebSocket route.
        self.assertTrue(messages)
        self.assertIn("t1", messages[0])


class AsrLanguagesUrlTests(unittest.TestCase):
    """The token probe URL is derived from the socket URL, not configured twice.

    asr-mcp's socket and its HTTP surface are one server. Two knobs would drift,
    and the wrong one would be the one nobody edits.
    """

    def test_the_default_socket_url_maps_to_the_languages_route(self):
        self.assertEqual(
            ln.asr_languages_url(
                "ws://host.docker.internal:8087/api/asr/ws/stream"),
            "http://host.docker.internal:8087/api/asr/languages",
        )

    def test_a_secure_socket_maps_to_https(self):
        self.assertEqual(
            ln.asr_languages_url("wss://asr.example/api/asr/ws/stream"),
            "https://asr.example/api/asr/languages",
        )

    def test_a_trailing_slash_does_not_produce_a_double_slash(self):
        self.assertEqual(
            ln.asr_languages_url("ws://asr:8087/api/asr/ws/stream/"),
            "http://asr:8087/api/asr/languages",
        )

    def test_an_unconfigured_url_is_empty_so_the_caller_can_say_so(self):
        self.assertEqual(ln.asr_languages_url(""), "")


class _ProbeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _Handshake(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class AsrTokenProbeTests(unittest.TestCase):
    """Checking a key must not load a model.

    asr-mcp loads the recogniser when a socket opens, and the probe used to open
    one -- on a 4 GB card Ollama is also using. These tests drive the shipping
    probe, so the assertion is which URL it asked and whether a socket was
    opened at all; neither is visible in the shape of the code.
    """

    TOKEN = "asr_secret_key"

    def make(self, status=200, error=None, url="ws://asr:8087/api/asr/ws/stream",
             install_ws=True):
        import asyncio
        import types
        calls = []
        connections = []

        class Client:
            def __init__(self, timeout=None):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, target, headers=None):
                calls.append((target, dict(headers or {}), self.timeout))
                if error is not None:
                    raise error
                return _ProbeResponse(status)

        class Connection:
            async def close(self):
                pass

        async def connect(target, **kwargs):
            connections.append((target, kwargs))
            return Connection()

        logger = types.SimpleNamespace(
            info=lambda message: None,
            warning=lambda message: None,
        )
        namespace = {
            "asyncio": asyncio,
            "httpx": types.SimpleNamespace(AsyncClient=Client),
            "Optional": __import__("typing").Optional,
            "mem": types.SimpleNamespace(
                ASR_WS_URL=url,
                # The real helper: the URL the probe asks is part of the
                # property, and stubbing it would assume the answer.
                asr_languages_url=ln.asr_languages_url,
                logger=logger,
            ),
            "_ws_connect": connect if install_ws else None,
            "_WS_HEADER_KWARG": "extra_headers",
            "_asr_url_with_language": lambda base, language: base,
        }
        gui_functions({"_probe_asr_token", "_probe_asr_websocket"}, namespace)
        return namespace["_probe_asr_token"], calls, connections

    def run_probe(self, **kwargs):
        import asyncio
        probe, calls, connections = self.make(**kwargs)
        return asyncio.run(probe(self.TOKEN, "alice")), calls, connections

    def test_it_asks_the_cheap_authenticated_route_and_opens_no_socket(self):
        result, calls, connections = self.run_probe(status=200)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(calls), 1)
        url, headers, timeout = calls[0]
        self.assertEqual(url, "http://asr:8087/api/asr/languages")
        self.assertEqual(headers, {"X-API-Key": self.TOKEN})
        self.assertIsNotNone(timeout, "the probe needs its own timeout")
        self.assertEqual(connections, [],
                         "validating a key must not load the recogniser")

    def test_a_refusal_is_rejected_rather_than_unreachable(self):
        result, _, connections = self.run_probe(status=401)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("401", result["detail"])
        self.assertEqual(connections, [])

    def test_an_unreachable_server_is_not_reported_as_a_bad_key(self):
        result, _, _ = self.run_probe(error=ConnectionError("no route to host"))
        self.assertEqual(result["status"], "unreachable")
        self.assertNotEqual(result["status"], "rejected")

    def test_an_older_asr_mcp_falls_back_to_the_socket_handshake(self):
        result, _, connections = self.run_probe(status=404)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(connections), 1,
                         "a server without the languages route must still be able "
                         "to have a working key saved")

    def test_a_missing_websockets_client_does_not_block_the_http_probe(self):
        result, calls, _ = self.run_probe(status=200, install_ws=False)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(calls), 1)

    def test_an_unconfigured_server_is_named_as_such(self):
        result, calls, _ = self.run_probe(url="")
        self.assertEqual(result["status"], "unreachable")
        self.assertIn("MEM_ASR_WS_URL", result["detail"])
        self.assertEqual(calls, [])


class AsrSegmentTests(unittest.TestCase):
    def test_a_transcript_becomes_a_segment_with_the_connection_offset(self):
        item = ln.asr_segment({
            "type": "transcript", "speaker": "Speaker 1", "text": " Next point. ",
            "start": 1.5, "end": 4.0,
        }, offset=90.0)
        self.assertEqual(item, {
            "text": "Next point.", "speaker": "Speaker 1",
            "startSec": 91.5, "endSec": 94.0,
        })

    def test_duration_is_the_end_fallback(self):
        item = ln.asr_segment({
            "type": "transcript", "text": "Hello", "start": 2.0, "duration": 3.5,
        })
        self.assertEqual((item["startSec"], item["endSec"]), (2.0, 5.5))

    def test_non_transcript_and_blank_messages_are_not_stored(self):
        self.assertIsNone(ln.asr_segment({"type": "stats"}))
        self.assertIsNone(ln.asr_segment({"type": "transcript", "text": "  "}))
        self.assertIsNone(ln.asr_segment(None))

    def test_unparseable_timings_are_refused_rather_than_raising(self):
        item = ln.asr_segment({
            "type": "transcript", "text": "Still useful", "start": "nope",
        })
        self.assertIsNone(item)

    def test_an_inverted_end_is_clamped_to_start(self):
        item = ln.asr_segment({
            "type": "transcript", "text": "Boundary", "start": 10.0, "end": 9.0,
        })
        self.assertEqual((item["startSec"], item["endSec"]), (10.0, 10.0))


class NoteStorageTests(unittest.TestCase):
    def test_deleting_a_note_releases_its_segments_and_clears_the_link(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(single={"released": 3}))
        release = _lift_manager_function(
            "release_note_segments",
            _require_driver=lambda: driver,
        )
        self.assertEqual(release("note-1", "alice"), 3)
        joined = "\n".join(driver.queries)
        self.assertIn("REMOVE seg.consumedBy, seg.consumedAt", joined)
        self.assertIn("REMOVE t.noteId", joined)
        self.assertIn("consumedBy: $noteId", joined)
        self.assertNotIn(":Fact", joined)

    def test_references_are_written_in_one_direction_only(self):
        def handler(query, params):
            if "RETURN count(n)" in query:
                return _FakeResult(single={"n": 1})
            if "RETURN referenced" in query:
                return _FakeResult(single={"referenced": 2})
            return _FakeResult()

        driver = _FakeDriver(handler)
        set_refs = _lift_manager_function(
            "db_set_note_references",
            _require_driver=lambda: driver,
        )
        self.assertEqual(set_refs("note-1", ["f1", "f2"], "alice"), 2)
        joined = "\n".join(driver.queries)
        self.assertIn("MERGE (n)-[:REFERENCES]->(f)", joined)
        self.assertNotIn("(f)-[:REFERENCES]->(n)", joined)
        self.assertIn("WHERE f.id <> n.id", joined)
        # The old outgoing set is replaced, so a reference the user removed
        # does not survive a later update.
        self.assertIn("-[r:REFERENCES]->() DELETE r", joined)

    def test_a_fact_delete_hook_only_releases_live_note_segments(self):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fact_manager.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('note_source == "live_notes"', source)
        self.assertIn("release_note_segments(memory_id, user_id)", source)


class CandidateStorageTests(unittest.TestCase):
    def _lift_candidates(self, handler, limit=2):
        driver = _FakeDriver(handler)
        candidates = _lift_manager_function(
            "db_note_fact_candidates",
            _require_driver=lambda: driver,
            NOTE_CATEGORY="Notes",
            LIVE_FACT_LIMIT=limit,
            _CANDIDATE_SCAN_LIMIT=2000,
            Optional=object,
        )
        return candidates, driver

    def test_no_client_means_no_candidates_at_all(self):
        candidates, driver = self._lift_candidates(
            lambda query, params: _FakeResult(single={"clientId": None})
        )
        self.assertEqual(candidates("tr-1", "alice"), [])
        joined = "\n".join(driver.queries)
        self.assertNotIn("FOR_CLIENT", joined)
        self.assertNotIn("IN_CONTEXT", joined)

    def test_candidates_are_titles_only_and_capped(self):
        def handler(query, params):
            if "t.clientId AS clientId" in query:
                return _FakeResult(single={"clientId": "c1", "contextId": None,
                                           "noteId": None})
            return _FakeResult(rows=[
                {"id": "f1", "name": "PPC migration"},
                {"id": "f1", "name": "PPC migration"},
                {"id": "f2", "name": ""},
                {"id": "f3", "name": "Jane Smith"},
                {"id": "f4", "name": "Cutover checklist"},
            ])

        candidates, driver = self._lift_candidates(handler)
        result = candidates("tr-1", "alice")
        self.assertEqual([row["id"] for row in result], ["f1", "f3"])
        joined = "\n".join(driver.queries)
        self.assertIn("FOR_CLIENT", joined)
        self.assertIn("<> $noteCategory", joined)
        self.assertNotIn("f.text", joined)
        # No project selected: no context branch is issued at all.
        self.assertNotIn("IN_CONTEXT", joined)


class CompactionPromptTests(unittest.TestCase):
    """The compaction pass is a prompt, so its properties are text properties."""

    def test_the_target_is_a_number_below_the_hard_limit(self):
        prompt = ln.build_compaction_prompt({"title": "Weekly sync", "text": "a" * 5000},
                                         max_chars=10000)
        self.assertIn("at most 7000 characters", prompt)
        self.assertIn("hard limit is 10000", prompt)
        self.assertIn("### NOTE TITLE\nWeekly sync", prompt)
        self.assertIn("### NOTE TEXT\na", prompt)

    def test_it_never_slices_the_note(self):
        note = {"title": "T", "text": "b" * 9000}
        prompt = ln.build_compaction_prompt(note, max_chars=10000)
        # The whole body reaches the model. A compaction that receives a
        # prefix condenses a fragment and silently drops the tail.
        self.assertIn("b" * 9000, prompt)

    def test_a_missing_body_still_returns_a_prompt(self):
        prompt = ln.build_compaction_prompt({}, max_chars=100)
        self.assertIn("(untitled)", prompt)
        self.assertIn("(empty)", prompt)


class JobContractTests(unittest.TestCase):
    """Pins the source-level contract of the background note job."""

    @classmethod
    def setUpClass(cls):
        import ast
        from pathlib import Path
        source = Path(__file__).with_name("live_manager.py").read_text()
        cls.tree = ast.parse(source)
        cls.source = source

    def _segment(self, name):
        import ast
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(self.source, node) or ""
        self.fail(f"{name} not found")

    def test_commit_rechecks_the_generation_before_writing(self):
        body = self._segment("_commit_note_window")
        check = body.index("_still_current(job)")
        first_write = body.index("db_update_memory")
        self.assertLess(check, first_write,
                        "the generation check must precede the first write")

    def test_commit_is_shielded(self):
        body = self._segment("_refresh_note")
        self.assertIn("asyncio.shield(", body)

    def test_a_recording_job_is_deferred_not_consumed(self):
        body = self._segment("_run_note_job")
        self.assertIn("deferred", body)
        self.assertIn("RecordingActiveError", body)

    def test_windows_are_processed_sequentially(self):
        body = self._segment("_refresh_note")
        self.assertNotIn("gather(", body)
        self.assertIn("for window in", body)


if __name__ == "__main__":
    unittest.main()
