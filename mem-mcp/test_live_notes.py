"""Tests for the dependency-light Live notes helpers.

`live_notes.py` imports nothing from the app, so unlike most suites here these
tests **call** the shipping functions instead of lifting source segments. That
matters because every property worth guarding is behavioural: what the prompt
contains, which ids survive validation, and whether the generation counter
actually preempts a job are all invisible in the shape of the code.
"""

import types
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

    def test_the_prompt_tells_the_note_to_ignore_off_topic_speech(self):
        # Reported symptom: the note carried the rambling. The old prompt said
        # "incorporate every new section", which mandates exactly that -- an
        # instruction to summarise by topic has to say what to leave out.
        prompt = ln.LIVE_SYSTEM_PROMPT
        self.assertIn("meeting's topic", prompt)
        self.assertIn("small talk", prompt)
        self.assertIn("Summarise; do not transcribe", prompt)
        self.assertIn("If a new section adds nothing, return the note unchanged",
                      prompt)

    def test_the_candidate_list_is_not_a_checklist(self):
        # Reported symptom: a note linked its whole project scope. The list is
        # the scope; the prompt has to say that being in it is not evidence.
        prompt = ln.LIVE_SYSTEM_PROMPT
        self.assertIn("context, not a checklist", prompt)
        self.assertIn("not relevant merely because it belongs to the same "
                      "client or project", prompt)
        self.assertIn("most sections need no reference at all", prompt)

    def test_the_answer_replaces_the_reference_list(self):
        # Without this sentence the model cannot know that pruning is allowed,
        # and the code would be dropping ids it expected to be unioned in.
        self.assertIn("replaces the current list", ln.LIVE_SYSTEM_PROMPT)
        self.assertIn("return the ones to keep", ln.BLOCK_REFERENCES)

    def test_the_model_is_told_the_input_is_a_transcript(self):
        # Reported symptom: a note carried "Dan Pyre is identified as
        # suspectable" -- a misheard name and a nonsense word presented as
        # findings. The model was never told the text is speech recognition, so
        # it had no licence to read through a homophone and no instruction to
        # drop what it could not resolve.
        prompt = ln.LIVE_SYSTEM_PROMPT
        self.assertIn("machine speech recognition", prompt)
        self.assertIn("mishears words", prompt)
        self.assertIn("sounds like", prompt,
                      msg="the licence to normalise is the point; without it the "
                          "model either keeps the garble or invents a meaning")
        self.assertIn("not inventing a fact", prompt,
                      msg="the permission has to be bounded, or it licenses "
                          "invention instead of correction")
        self.assertIn("leave it out", prompt)

    def test_every_sentence_must_carry_something(self):
        # The same note restated its own numbers three times and closed with a
        # hedged guess. Relevance at sentence granularity is a separate
        # instruction from relevance at topic granularity.
        prompt = ln.LIVE_SYSTEM_PROMPT
        self.assertIn("must carry something a reader would act on", prompt)
        self.assertIn("only restates another", prompt)
        self.assertIn("hedged commentary", prompt)

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

    def test_the_answer_replaces_the_reference_list(self):
        # The old behaviour unioned existing ids in front, so a note that once
        # cited its whole project scope could never shed a link: the list only
        # ever grew. The model is told the field replaces the list, and the
        # facts it cites today are in the prompt, so dropping one is a choice
        # it made rather than an oversight.
        result = self.parse('{"title":"T","text":"Body","referencedFactIds":["f2"]}',
                            existing=["f1", "old"])
        self.assertEqual(result["referencedFactIds"], ["f2"])

    def test_an_empty_list_clears_the_references(self):
        result = self.parse('{"title":"T","text":"Body","referencedFactIds":[]}',
                            existing=["f1", "f2"])
        self.assertEqual(result["referencedFactIds"], [])

    def test_an_answer_without_the_key_keeps_the_existing_references(self):
        # "If a new section adds nothing, return the note unchanged" -- an
        # unchanged answer must not silently unlink everything.
        result = self.parse('{"title":"T","text":"Body"}', existing=["f1"])
        self.assertEqual(result["referencedFactIds"], ["f1"])

    def test_the_reference_count_is_capped(self):
        allowed = {f"f{i}" for i in range(20)}
        result = ln.parse_note_json(
            '{"title":"T","text":"Body","referencedFactIds":[%s]}'
            % ",".join('"f%d"' % i for i in range(20)),
            allowed, (), max_chars=1000,
        )
        self.assertEqual(len(result["referencedFactIds"]),
                         ln.LIVE_REFERENCE_LIMIT,
                         msg="the candidate list is the client scope; a note "
                             "citing all of it describes the project, not the meeting")

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


class AudioWatchdogTests(unittest.TestCase):
    """An open recorder that sends nothing must leave a line in the log.

    This is the diagnostic for a suspended `AudioContext`: the socket is up,
    the UI says "recording", and the browser is producing no audio at all. Every
    other symptom of that is "no transcript yet", which is also what a quiet
    room looks like, so the absence of audio has to be counted and reported
    rather than inferred.
    """

    _route_source = AsrProxyTests._route_source

    def test_forwarded_audio_is_counted_after_it_is_sent(self):
        route = self._route_source()
        send = route.index('await upstream.send(message["bytes"])')
        count = route.index('audio_blocks["value"] += 1')
        self.assertLess(send, count,
                        msg="the block counter must account for audio that was "
                            "actually forwarded, not for one that raised")

    def test_a_silent_recording_warns_once_and_names_the_cause(self):
        route = self._route_source()
        self.assertIn("async def audio_watchdog()", route)
        self.assertIn('if stopping or audio_blocks["value"]:', route,
                      msg="the watchdog must not warn after audio arrived")
        self.assertIn("mem.logger.warning(", route)
        self.assertIn("AudioContext", route,
                      msg="the warning has to name the cause, or the operator has "
                          "the same puzzle with a log line attached")

    def test_the_watchdog_is_started_and_cancelled(self):
        route = self._route_source()
        self.assertIn("audio_task = asyncio.create_task(audio_watchdog())", route)
        self.assertIn("audio_task.cancel()", route,
                      msg="an uncancelled watchdog leaks one task per recording")
        self.assertIn("audioBlocks=", route,
                      msg="the stopped line reports the count, so a completed "
                          "recording with audioBlocks=0 explains itself")


class LiveCaptureAudioOrderTests(unittest.TestCase):
    """The audio context must be running before anything else awaits.

    Chrome hands out a user activation that any `await` spends. A context
    created after the permission prompt -- or after the WebSocket connect --
    therefore starts `suspended`, a suspended context never runs the audio
    graph, `process()` is never called, the worklet posts nothing and **not one
    byte reaches the socket**. There is no exception and no console error
    anywhere: the recorder simply looks live and silent. That is why `start()`
    is driven here against a fake audio graph instead of being read.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed; the JS is not executable here")
        import test_share_endpoints
        cls._share = test_share_endpoints

    def _run(self, cases):
        import json
        import os
        import re
        import subprocess
        import tempfile

        share = self._share
        source = share._read("mem-mcp/templates/dashboard.html")
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", source, re.S)
        payload = "".join(
            f"  try {{ out[{name!r}] = ({expr}); }} "
            f"catch (e) {{ out[{name!r}] = 'THREW: ' + e.message; }}\n"
            for name, expr in cases
        )
        harness = share._NODE_HARNESS.replace(
            "__APP_JS__", "\n".join(blocks[2:])
        ).replace("__CASES__", payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(harness)
            proc = subprocess.run([self.node, path], capture_output=True,
                                  text=True, timeout=120)
        if proc.returncode != 0:
            self.fail("the dashboard script did not load under the fake DOM: "
                      + proc.stderr.strip()[:400])
        return json.loads(proc.stdout)

    # A fake graph that records the order in which it is touched. `stuck`
    # models the autoplay policy: the context exists but never leaves
    # `suspended`, which is exactly what the old code got.
    _FAKES = r"""
      const order = [];
      const stuck = __STUCK__;
      class FakeCtx {
        constructor() {
          this.state = stuck ? 'suspended' : 'running';
          this.sampleRate = 48000;
          this.audioWorklet = { addModule: async () => { order.push('module'); } };
          order.push('ctx');
        }
        async resume() { order.push('resume'); if (!stuck) this.state = 'running'; }
        createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
        close() { this.state = 'closed'; }
      }
      window.AudioContext = FakeCtx;
      navigator.mediaDevices = {
        getUserMedia: async () => {
          order.push('mic');
          return { getTracks: () => [{ stop() {} }] };
        },
      };
      window.AudioWorkletNode = class {
        constructor() { this.port = {}; order.push('worklet'); }
        connect() {} disconnect() {}
      };
      class FakeWS {
        constructor() {
          order.push('ws');
          this.readyState = 0;
          setTimeout(() => {
            this.readyState = 1;
            if (this.onopen) this.onopen();
          }, 0);
        }
        send() {}
        close() {}
      }
      FakeWS.OPEN = 1;
      window.WebSocket = FakeWS;
    """

    def _fakes(self, stuck):
        return self._FAKES.replace("__STUCK__", "true" if stuck else "false")

    def test_the_context_is_created_before_the_microphone_and_the_socket(self):
        expr = ("(async () => { %s\n"
                "  await LiveCapture.start('t1', {});\n"
                "  LiveCapture._clearSilenceWatch();\n"
                "  const out = { order: order, blocks: LiveCapture.blocksSent };\n"
                "  await LiveCapture.stop(true);\n"
                "  return out; })()" % self._fakes(False))
        result = self._run([("started", expr)])["started"]
        self.assertNotIsInstance(result, str, msg=result)
        order = result["order"]
        self.assertLess(order.index("ctx"), order.index("mic"),
                        msg="the context was created after the permission prompt, "
                            "which spends the click's activation and leaves it "
                            "suspended in Chrome: nothing is ever captured")
        self.assertLess(order.index("ctx"), order.index("ws"),
                        msg="the context was created after the WebSocket connect")
        self.assertEqual(result["blocks"], 0,
                         msg="no worklet block has been seen yet, so the count "
                             "must start at zero for the watchdog to mean anything")

    def test_a_suspended_context_refuses_instead_of_recording_silence(self):
        expr = ("(async () => { %s\n"
                "  try { await LiveCapture.start('t2', {}); return {ok: true, order: order}; }\n"
                "  catch (e) { return {ok: false, msg: e.message, order: order}; } })()"
                % self._fakes(True))
        result = self._run([("stuck", expr)])["stuck"]
        self.assertNotIsInstance(result, str, msg=result)
        self.assertFalse(result["ok"], msg="a suspended context started anyway")
        self.assertIn("blocked audio", result["msg"])
        self.assertIn("suspended", result["msg"])
        self.assertNotIn("ws", result["order"],
                         msg="the socket was opened for a recorder that can never "
                             "send anything -- an open, silent connection is the "
                             "symptom this check exists to prevent")


class PauseTailTests(unittest.TestCase):
    """A pause must end the stream, not merely hang up on it.

    asr-mcp ends a turn on 320 ms of sub-threshold *audio* and then holds the
    closed turn for a wall-clock second before decoding. A client that stops
    sending therefore loses the last utterance twice over: the detector's open
    turn is only flushed at disconnect, and whatever that flush produces is
    written to a socket the client has already closed. Both halves are pinned
    here -- the frames by calling the builder, the order by reading the one
    call site, because a swapped pair of awaits is invisible in a diff.
    """

    _route_source = AsrProxyTests._route_source

    def test_the_tail_is_silence_then_the_end_of_stream_frame(self):
        frames = ln.pause_flush_frames()
        self.assertGreater(len(frames), 1,
                           msg="the flush frame alone drops the open utterance: "
                               "asr-mcp skips its own detector flush once it sees one")
        self.assertEqual(frames[-1], ln.LIVE_FLUSH_FRAME,
                         msg="the end-of-stream frame must come last, after the "
                             "silence that closes the turn")

    def test_the_end_of_stream_frame_is_the_shape_asr_mcp_parses(self):
        import struct
        magic, version, msg_type, channel, start, samples, sequence = struct.unpack(
            "<4sBBHQII", ln.LIVE_FLUSH_FRAME
        )
        self.assertEqual(magic, b"LVT1")
        self.assertEqual(version, 1)
        self.assertEqual(msg_type, 2, msg="2 is MSG_FLUSH; 1 would be an empty turn")
        self.assertEqual((channel, start, samples, sequence), (0, 0, 0, 0))
        self.assertEqual(len(ln.LIVE_FLUSH_FRAME), 24)

    def test_the_silence_is_longer_than_the_hangover_and_short_enough_to_wait_for(self):
        frames = ln.pause_flush_frames()[:-1]
        samples = sum(len(frame) - 8 for frame in frames) // 2
        ms = samples * 1000.0 / ln.LIVE_ASR_SAMPLE_RATE
        self.assertGreaterEqual(ms, 320,
                                msg="the detector needs 10 frames of 32 ms below "
                                    "the end threshold before it closes a turn")
        self.assertLess(ms, 2000,
                        msg="a pause must not feel like a stall")

    def test_the_silence_frames_are_legacy_mic_frames_of_zeros(self):
        import struct
        for index, frame in enumerate(ln.pause_flush_frames()[:-1]):
            with self.subTest(frame=index):
                channel, _sequence = struct.unpack("<II", frame[:8])
                self.assertEqual(channel, 0, msg="channel 0 is the microphone; any "
                                                 "other channel is discarded by asr-mcp")
                self.assertFalse(any(frame[8:]),
                                 msg="the tail has to be silence, not noise")
                self.assertEqual((len(frame) - 8) % 2, 0)

    def test_no_silence_still_releases_what_the_coalescer_holds(self):
        self.assertEqual(ln.pause_flush_frames(silence_ms=0), [ln.LIVE_FLUSH_FRAME])

    def test_the_proxy_sends_the_tail_before_it_closes(self):
        route = self._route_source()
        flush = route.index("for frame in mem.pause_flush_frames():")
        close = route.index("await connection.close()")
        self.assertLess(flush, close,
                        msg="closing first is the bug: asr-mcp's flushed transcripts "
                            "are then written to a socket nobody is reading")
        drain = route.index("drained.wait()")
        self.assertLess(flush, drain)
        self.assertLess(drain, close,
                        msg="the ack has to be awaited before the local close, or "
                            "the frames that carry the tail are thrown away")

    def test_the_reader_marks_the_drain_on_the_stats_ack(self):
        route = self._route_source()
        stats = route.index('payload.get("type") == "stats"')
        self.assertIn("drained.set()", route[stats:stats + 400].split("continue", 1)[0],
                      msg="`stats` is the only end-of-stream ack asr-mcp sends, so "
                          "that branch is where the pause stops waiting")
        self.assertIn("asr_reader(upstream, offset, asr_quiet, asr_drained", route,
                      msg="the reader and the closer must share one event")
        self.assertIn('asr_drained["event"] = asyncio.Event()', route,
                      msg="a reused event would let the next pause return before "
                          "its own tail had been transcribed")


class LiveGateRaceTests(unittest.TestCase):
    """The gate the server holds outlives the browser's own pause/stop.

    Reported as a pair of 409s from the Live pane. The pause now ends the stream
    properly, which means the server keeps the audio gate closed while asr-mcp
    finishes the last sentence -- seconds after the browser has already decided
    it is paused. The client was acting on its own optimistic flag, so the
    buttons were enabled throughout that window and every click in it answered
    409.
    """

    @classmethod
    def setUpClass(cls):
        import test_share_endpoints
        import test_auth_guard
        cls._share = test_share_endpoints
        cls._js = staticmethod(test_auth_guard._js_function_source)
        cls.dashboard = test_share_endpoints._read("mem-mcp/templates/dashboard.html")

    def _between(self, start, end):
        i = self.dashboard.find(start)
        self.assertTrue(i >= 0, msg=f"missing marker: {start!r}")
        j = self.dashboard.find(end, i)
        self.assertTrue(j > i, msg=f"missing {end!r} after {start!r}")
        return self.dashboard[i:j]

    # -- the server half ---------------------------------------------------

    def test_the_gate_says_the_pause_may_still_be_finishing(self):
        # The old message said "pause the microphone", which is exactly what a
        # user who just paused has already done. A 409 that accuses the reader
        # of the thing they did sends them looking for a bug that is not there.
        gate = ln.LivePriorityGate()
        gate.begin_recording("alice")
        with self.assertRaises(ln.RecordingActiveError) as caught:
            gate.assert_idle("alice")
        message = str(caught.exception).lower()
        self.assertIn("still being transcribed", message,
                      msg="the reason must allow for a pause that has not finished")
        self.assertIn("try again", message,
                      msg="a transient refusal must name the remedy")

    def test_both_note_routes_log_their_409_through_conflict(self):
        # A bare HTTPException(status_code=409, ...) is formatted into the
        # response and then discarded: the access log records the status and
        # nothing records which of the two conditions produced it.
        namespace = {
            "web_app": types.SimpleNamespace(
                post=lambda *a, **k: (lambda fn: fn)),
            "_conflict": lambda exc: exc,
            "mem": types.SimpleNamespace(RecordingActiveError=ln.RecordingActiveError),
            "HTTPException": type("HTTPException", (Exception,), {}),
            "JSONResponse": type("JSONResponse", (), {}),
        }
        gui_functions({"api_summarize_live_transcription",
                       "api_reprocess_live_transcription"}, namespace)
        for name in ("api_summarize_live_transcription",
                     "api_reprocess_live_transcription"):
            with self.subTest(route=name):
                source = self._share._read("mem-mcp/gui.py")
                tree = __import__("ast").parse(source)
                node = [n for n in tree.body
                        if isinstance(n, (__import__("ast").FunctionDef,
                                          __import__("ast").AsyncFunctionDef))
                        and n.name == name][0]
                body = __import__("ast").get_source_segment(source, node)
                self.assertIn("raise _conflict(exc)", body)
                self.assertNotIn("status_code=409", body,
                                 msg="the conflict must go through _conflict so it "
                                     "is logged")

    # -- the client half ---------------------------------------------------

    def test_a_pause_marks_the_gate_pending_until_the_server_answers(self):
        body = self._js("templates/dashboard.html", "pauseLiveRecording")
        self.assertIn("liveGatePending = true", body,
                      msg="the click that pauses must also disable the actions")
        ack = self._between("else if (payload.type === 'paused') {",
                            "else if (payload.type === 'resumed') {")
        self.assertIn("liveGatePending = false", ack,
                      msg="the pause ack is the only proof the gate is free")

    def test_the_write_actions_are_disabled_while_the_gate_is_held(self):
        body = self._js("templates/dashboard.html", "renderLiveControls")
        for line in ('if (update) update.disabled', 'if (rebuild) rebuild.disabled'):
            with self.subTest(control=line):
                start = body.index(line)
                self.assertIn("liveGatePending", body[start:body.index(";", start)],
                              msg=f"{line} ignores the pending gate")
        self.assertIn("Finishing the last seconds", body,
                      msg="the state line must say why the buttons are dead")

    def test_both_note_actions_wait_instead_of_reporting_the_race(self):
        for name in ("summarizeLive", "rebuildLiveNote"):
            with self.subTest(action=name):
                body = self._js("templates/dashboard.html", name)
                self.assertIn("liveGatePending", body,
                              msg="the optimistic guard is what produced the 409")
                self.assertIn("awaitLiveGate", body,
                              msg="a 409 here is a wait, not a failure the user "
                                  "can act on")
                self.assertIn("status === 409", body)

    def test_the_gate_is_read_from_the_server_not_assumed(self):
        body = self._js("templates/dashboard.html", "settleLiveGate")
        self.assertIn("data.recording === false", body,
                      msg="only the server knows when its writer and ASR tail are done")
        stopped = self._js("templates/dashboard.html", "onLiveCaptureStopped")
        self.assertIn("liveGatePending = true", stopped)
        self.assertIn("awaitLiveGate(id)", stopped)


class RepeatAbortTests(unittest.TestCase):
    """Ollama aborts a prediction that repeats itself; retry with other sampling.

    Reported as `ERROR live notes: note job failed ... Ollama returned HTTP 500
    for model 'granite3.3:2b': {"error":"prediction aborted, token repeat limit
    reached"}`. The prompt was fine and the model answered -- it just looped
    until Ollama's own guard stopped it, which is what greedy decoding of a long
    Markdown rewrite does to a 2b model.
    """

    @classmethod
    def setUpClass(cls):
        import test_share_endpoints
        cls.source = test_share_endpoints._read("mem-mcp/live_manager.py")

    def test_the_predicate_matches_the_abort_and_nothing_else(self):
        abort = ('Ollama returned HTTP 500 for model \'granite3.3:2b\': '
                 '{"error":"prediction aborted, token repeat limit reached"}')
        self.assertTrue(ln.is_repeat_abort(abort))
        # The predicate drives a retry, so a false positive costs a second call
        # on a failure that cannot improve -- a timeout, a missing model, a 404.
        for other in ("LLM call timed out after 300.0s",
                      "model 'nope' is not installed",
                      "Ollama returned HTTP 404"):
            with self.subTest(case=other):
                self.assertFalse(ln.is_repeat_abort(other))
        self.assertFalse(ln.is_repeat_abort(None))

    def test_the_first_attempt_stays_deterministic_and_the_retry_does_not(self):
        self.assertEqual(ln.LIVE_SAMPLING["temperature"], 0.0,
                         msg="the normal path must stay reproducible")
        self.assertGreater(ln.LIVE_SAMPLING["repeat_penalty"], 1.0,
                           msg="without a penalty a loop just re-runs")
        self.assertGreater(ln.LIVE_SAMPLING_RETRY["repeat_penalty"],
                           ln.LIVE_SAMPLING["repeat_penalty"])
        self.assertLess(ln.LIVE_SAMPLING_RETRY["repeat_last_n"],
                        ln.LIVE_SAMPLING["repeat_last_n"],
                        msg="the retry must act on the run that is looping")
        self.assertGreater(ln.LIVE_SAMPLING_RETRY["temperature"], 0.0,
                           msg="a retry at temperature 0 is the same decode")

    def test_the_note_call_passes_them_and_retries_only_the_abort(self):
        start = self.source.index("async def _call_live_llm")
        body = self.source[start:self.source.index("async def _compact_note")]
        self.assertIn("options=sampling", body,
                      msg="the sampling sets are useless unless they are sent")
        self.assertIn("for attempt, sampling in enumerate((LIVE_SAMPLING, "
                      "LIVE_SAMPLING_RETRY))", body)
        # Exactly one retry, and only when the abort is the repetition one:
        # `attempt or not is_repeat_abort(exc)` is the whole policy.
        self.assertIn("if attempt or not is_repeat_abort(exc):", body)
        self.assertEqual(body.count("get_llm_response("), 1,
                         msg="the call must happen once, inside the loop")
        # The second failure must name the remedy rather than the raw 500.
        self.assertIn("MEM_LIVE_MODEL", body)
        self.assertIn("ollama ps", body,
                      msg="the empty answer is a residency symptom; say how to "
                          "check it")


class NoteCallRetryTests(unittest.TestCase):
    """`_call_live_llm` drives one model call, and retries exactly once.

    Production reported `content:"" done_reason:"stop" eval_count:1` after 85 s
    of prompt evaluation -- the model loaded, read the prompt and emitted one
    token. At temperature 0 that is a fixed point: the same request produces the
    same nothing, so this is a case for different sampling rather than patience.
    Driven rather than read, because the retry decision is a control-flow
    property and the sources of a `continue` and a `break` are one character
    apart.
    """

    PATH = "mem-mcp/live_manager.py"

    def _lift(self, replies, warnings=None):
        """`_call_live_llm` with a scripted `get_llm_response`."""
        import ast
        import asyncio
        import test_share_endpoints

        source = test_share_endpoints._read(self.PATH)
        tree = ast.parse(source)
        node = next(n for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_call_live_llm")
        module = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(module)

        calls = []
        scripted = list(replies)

        async def fake_llm(prompt, system="", model="", num_predict=0,
                           timeout=0.0, options=None):
            calls.append(dict(options or {}))
            if not scripted:
                # A missing retry must read as the property under test, not as
                # an IndexError from the harness.
                raise AssertionError(
                    f"unexpected call {len(calls)}: the scripted replies are used up"
                )
            value = scripted.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        class NoteJobDeferred(RuntimeError):
            pass

        namespace = {
            "asyncio": asyncio,
            "LIVE_MODEL": "granite3.3:2b",
            "LIVE_SAMPLING": ln.LIVE_SAMPLING,
            "LIVE_SAMPLING_RETRY": ln.LIVE_SAMPLING_RETRY,
            "is_repeat_abort": ln.is_repeat_abort,
            "LiveNoteError": ln.LiveNoteError,
            "LiveNoteTooLong": ln.LiveNoteTooLong,
            "NoteJobDeferred": NoteJobDeferred,
            "get_llm_response": fake_llm,
            "LIVE_GATE": types.SimpleNamespace(
                register_llm_cancel=lambda user, cb: True,
                clear_llm_cancel=lambda user, cb=None: None,
            ),
            "logger": types.SimpleNamespace(
                warning=lambda *a, **k: (warnings if warnings is not None else []).append(a[0]),
                error=lambda *a, **k: None,
            ),
        }
        exec(compile(module, self.PATH, "exec"), namespace)
        return namespace["_call_live_llm"], calls

    def run_call(self, replies, parse=None):
        import asyncio
        warnings = []
        call, calls = self._lift(replies, warnings)
        return (asyncio.run(call({"userId": "alice"}, "prompt", "system", 900,
                                 parse=parse)),
                calls, warnings)

    def test_an_empty_answer_is_retried_with_different_sampling(self):
        answer, calls, _ = self.run_call(["", '{"title":"T","text":"Body"}'])
        self.assertEqual(answer, '{"title":"T","text":"Body"}')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], dict(ln.LIVE_SAMPLING),
                         msg="the first attempt must be the deterministic one")
        self.assertEqual(calls[1], dict(ln.LIVE_SAMPLING_RETRY),
                         msg="a retry at the same settings is the same decode")

    def test_whitespace_only_is_an_empty_answer(self):
        _, calls, _ = self.run_call(["   ", "ok"])
        self.assertEqual(len(calls), 2)

    def test_garbage_is_retried_and_a_too_long_answer_is_not(self):
        # A non-empty but unusable answer is the same class as an empty one: the
        # decoder, not the prompt. Production got 62 tokens of word salad where
        # a JSON note was asked for, and parsing *outside* the retry loop made
        # that a dead end while an empty answer got a second attempt.
        def parse(text):
            if text == "LONG":
                raise ln.LiveNoteTooLong("too long")
            if not text.startswith("{"):
                raise ln.LiveNoteError("the model answer contained no JSON object")
            return {"text": text}

        import asyncio
        import test_share_endpoints
        import ast

        source = test_share_endpoints._read(self.PATH)
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_call_live_llm")
        module = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(module)

        calls = []
        scripted = ["Pro Ad Access At S", '{"title":"T","text":"Body"}']

        async def fake_llm(prompt, system="", model="", num_predict=0,
                           timeout=0.0, options=None):
            calls.append(dict(options or {}))
            if not scripted:
                raise AssertionError(
                    f"unexpected call {len(calls)}: the scripted replies are used up"
                )
            return scripted.pop(0)

        namespace = {
            "asyncio": asyncio,
            "LIVE_MODEL": "granite3.3:2b",
            "LIVE_SAMPLING": ln.LIVE_SAMPLING,
            "LIVE_SAMPLING_RETRY": ln.LIVE_SAMPLING_RETRY,
            "is_repeat_abort": ln.is_repeat_abort,
            "LiveNoteError": ln.LiveNoteError,
            "LiveNoteTooLong": ln.LiveNoteTooLong,
            "NoteJobDeferred": type("NoteJobDeferred", (RuntimeError,), {}),
            "get_llm_response": fake_llm,
            "LIVE_GATE": types.SimpleNamespace(
                register_llm_cancel=lambda user, cb: True,
                clear_llm_cancel=lambda user, cb=None: None,
            ),
            "logger": types.SimpleNamespace(
                warning=lambda *a, **k: None, error=lambda *a, **k: None),
        }
        exec(compile(module, self.PATH, "exec"), namespace)
        call = namespace["_call_live_llm"]

        result = asyncio.run(call({"userId": "alice"}, "p", "s", 900, parse=parse))
        self.assertEqual(result, {"text": '{"title":"T","text":"Body"}'})
        self.assertEqual(len(calls), 2,
                         msg="an unparseable answer must get the retry too")
        self.assertEqual(calls[1], dict(ln.LIVE_SAMPLING_RETRY))

        # A too-long answer is a GOOD answer to a different problem: the caller
        # compacts it. Retrying would ask the same model for the same length.
        calls.clear()
        scripted[:] = ["LONG", "LONG"]
        with self.assertRaises(ln.LiveNoteTooLong):
            asyncio.run(call({"userId": "alice"}, "p", "s", 900, parse=parse))
        self.assertEqual(len(calls), 1)

    def test_two_unparseable_answers_name_the_model(self):
        def parse(text):
            raise ln.LiveNoteError("the model answer contained no JSON object")

        import asyncio

        with self.assertRaises(ln.LiveNoteError) as caught:
            self.run_call(["garbage", "garbage"], parse=parse)
        self.assertIn("granite3.3:2b", str(caught.exception))
        self.assertIn("MEM_LIVE_MODEL", str(caught.exception))

    def test_two_empty_answers_fail_with_the_model_and_the_check(self):
        with self.assertRaises(ln.LiveNoteError) as caught:
            self.run_call(["", ""])
        message = str(caught.exception)
        self.assertEqual(len(self.run_call(["ok"])[1]), 1,
                         msg="a usable answer costs exactly one call")
        self.assertIn("granite3.3:2b", message)
        self.assertIn("ollama ps", message,
                      msg="the operator needs the residency check, not a 500")

    def test_a_repeat_abort_is_retried(self):
        abort = RuntimeError(
            'Ollama returned HTTP 500 for model \'granite3.3:2b\': '
            '{"error":"prediction aborted, token repeat limit reached"}'
        )
        answer, calls, _ = self.run_call([abort, "ok"])
        self.assertEqual(answer, "ok")
        self.assertEqual(len(calls), 2)

    def test_any_other_failure_is_not_retried(self):
        # The count is the property: a retryable abort consumes both calls, and
        # anything else must fail on the first one. `expected_calls` is asserted
        # rather than "it raised", because raising looks the same either way.
        import asyncio
        for label, error, expected_calls in (
            ("timeout", RuntimeError("LLM call to 'granite3.3:2b' timed out"), 1),
            ("missing model", RuntimeError("model 'nope' is not installed"), 1),
            ("repeat abort", RuntimeError(
                '{"error":"prediction aborted, token repeat limit reached"}'), 2),
        ):
            with self.subTest(case=label):
                call, calls = self._lift([error, error])
                with self.assertRaises(RuntimeError):
                    asyncio.run(call({"userId": "alice"}, "p", "s", 900))
                self.assertEqual(len(calls), expected_calls)


class LiveJobEventTests(unittest.TestCase):
    """A note job ran below the HTTP layer, so it published nothing at all.

    Every other write in the app announces itself from its route handler
    (`publish_db_event` lives only in `gui.py`), which is why the Live panel had
    to poll `/status` every 1.5 s and why a note written by a background job was
    invisible to Memories and Diary. These pin the push: a job announces each
    state change, and the commit announces the fact like any other write.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        from pathlib import Path
        cls.source = Path(__file__).with_name("live_manager.py").read_text()
        cls.tree = ast.parse(cls.source)
        cls.gui = Path(__file__).with_name("gui.py").read_text()

    def _segment(self, name):
        import ast
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(self.source, node) or ""
        self.fail(f"{name} not found")

    def _announcer(self):
        """`_publish_job_event` and `_announce`, exec'd against a recording bus."""
        import ast
        import asyncio
        import types

        published = []

        async def publish_db_event(user_id, event_type, payload=None):
            published.append((user_id, event_type, payload))

        namespace = {
            "asyncio": asyncio,
            "logger": types.SimpleNamespace(
                warning=lambda *a, **k: None, info=lambda *a, **k: None,
                error=lambda *a, **k: None),
            "publish_db_event": publish_db_event,
        }
        for name in ("_job_public", "_publish_job_event", "_announce"):
            node = next(n for n in self.tree.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name == name)
            exec(compile(ast.get_source_segment(self.source, node), "live_manager.py", "exec"),
                 namespace)
        return namespace, published

    def test_announcing_a_job_publishes_the_whole_snapshot(self):
        # Called, not read: the payload is what the browser renders, and a
        # source assertion cannot see whether it carries a status.
        import asyncio
        namespace, published = self._announcer()

        async def run():
            namespace["_announce"]({
                "transcriptionId": "t1", "userId": "alice",
                "status": "running", "phase": "summarising",
                "mode": "unconsumed", "consumedSegments": 3,
            })
            await asyncio.sleep(0)  # let the fire-and-forget task run

        asyncio.run(run())
        self.assertEqual(len(published), 1)
        user_id, event_type, payload = published[0]
        self.assertEqual(user_id, "alice")
        self.assertEqual(event_type, "live_note_job")
        self.assertEqual(payload["transcriptionId"], "t1")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["consumedSegments"], 3)

    def test_announcing_without_a_running_loop_is_not_an_error(self):
        # `_set_job` is sync and is called from tools and tests as well as from
        # the job task; scheduling a task there would raise.
        namespace, published = self._announcer()
        namespace["_announce"]({"transcriptionId": "t1", "userId": "alice"})
        self.assertEqual(published, [])

    def test_every_state_change_announces(self):
        body = self._segment("_set_job")
        self.assertIn("_announce(job)", body,
                      msg="a status change nobody hears about leaves the panel "
                          "on the poll it exists to replace")

    def test_the_job_is_announced_before_its_task_starts(self):
        body = self._segment("start_note_job")
        self.assertLess(body.index("_announce(job)"),
                        body.index("create_task(_run_note_job"),
                        msg="a tab that opened between the POST and the first "
                            "state change would never learn the job exists")

    def test_the_commit_announces_the_note_like_any_other_write(self):
        # The one gap the poll was papering over: the note is an ordinary fact,
        # but it is written by a job rather than by a handler, so nothing told
        # Memories or Diary about it.
        body = self._segment("_commit_note_window")
        self.assertIn('"memory_changed"', body)
        self.assertIn('"source": "live_notes"', body)
        self.assertLess(body.index("db_mark_segments_consumed"),
                        body.index('"memory_changed"'),
                        msg="announcing before the write would tell open tabs to "
                            "refetch a note that is not there yet")
        self.assertIn('"add" if created else "update"', body)

    def test_the_gate_release_is_announced_at_every_exit(self):
        releases = self.gui.count("mem.LIVE_GATE.end_recording(user_id)")
        announced = self.gui.count(
            "_announce_gate_release(user_id, transcription_id)")
        self.assertGreaterEqual(releases, 3)
        self.assertEqual(
            announced, releases,
            msg="every path that opens the audio gate must say so, or the "
                "buttons wait for a poll that no longer runs")


class LivePanelPushTests(unittest.TestCase):
    """The Live panel reads the push, and polls only when the stream is down.

    `/api/events` is the app's write bus and the dashboard already subscribes to
    it. The Live panel was the one place that polled a *running job*, which is
    also why a note commit was invisible to every other tab.
    """

    @classmethod
    def setUpClass(cls):
        import re
        import shutil
        from pathlib import Path
        cls.node = shutil.which("node")
        cls.source = Path(__file__).with_name("templates").joinpath(
            "dashboard.html").read_text()
        cls.blocks = re.findall(r"<script[^>]*>(.*?)</script>", cls.source, re.S)

    def _body(self, name):
        """The JS function's source, by brace matching (the template is not Python)."""
        marker = f"function {name}("
        start = self.source.find(marker)
        self.assertTrue(start > 0, msg=f"{name} is not defined in dashboard.html")
        index = self.source.index("{", start)
        begin, depth = index, 0
        while index < len(self.source):
            if self.source[index] == "{":
                depth += 1
            elif self.source[index] == "}":
                depth -= 1
                if depth == 0:
                    return self.source[start:index + 1]
            index += 1
        self.fail(f"unbalanced braces in {name}")

    def _run(self, cases):
        import json
        import os
        import re
        import subprocess
        import tempfile
        import test_share_endpoints
        payload = "".join(
            f"  try {{ out[{name!r}] = ({expr}); }} "
            f"catch (e) {{ out[{name!r}] = 'THREW: ' + e.message; }}\n"
            for name, expr in cases
        )
        harness = test_share_endpoints._NODE_HARNESS.replace(
            "__APP_JS__", "\n".join(self.blocks[2:])
        ).replace("__CASES__", payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live-push.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(harness)
            proc = subprocess.run([self.node, path], capture_output=True,
                                  text=True, timeout=120)
        if proc.returncode != 0:
            self.fail("the dashboard script did not load under the fake DOM: "
                      + proc.stderr.strip()[:400])
        return json.loads(proc.stdout)

    def test_the_panel_subscribes_to_both_live_events(self):
        body = self._body("initSSE")
        self.assertIn("'live_note_job'", body,
                      msg="without it a job that finished is only visible to a "
                          "poll that no longer runs")
        self.assertIn("'live_gate_released'", body)

    def test_the_poll_is_a_fallback_and_asks_the_stream_first(self):
        body = self._body("scheduleLivePoll")
        self.assertIn("sseOpen()", body,
                      msg="a poll running while the push is alive is the request "
                          "loop this change exists to remove")
        self.assertLess(body.index("sseOpen()"), body.index("setTimeout("),
                        msg="the stream check must be before the timer is armed")

    def test_the_gate_wait_is_settled_by_the_push(self):
        body = self._body("settleLiveGate")
        self.assertLess(body.index("waitForLiveGate("),
                        body.index("data.recording === false"),
                        msg="the poll is the fallback; the push settles it")

    def test_a_gate_event_clears_the_flag_and_wakes_the_waiters(self):
        expr = ("(async () => {\n"
                "  liveGatePending = true;\n"
                "  liveDetail = null;\n"
                "  activeLiveTranscriptionId = 't1';\n"
                "  const waiting = waitForLiveGate(5000);\n"
                "  onLiveGateReleased({ transcriptionId: 't1' });\n"
                "  const woken = await waiting;\n"
                "  return { pending: liveGatePending, woken: woken };\n"
                "})()")
        result = self._run([("released", expr)])["released"]
        self.assertNotIsInstance(result, str, msg=result)
        self.assertFalse(result["pending"],
                         msg="the push must clear the flag the poll used to clear")
        self.assertTrue(result["woken"],
                         msg="a click queued behind the gate waits on the waiter "
                             "list; not waking it holds the toolbar for 1.5 s and "
                             "then starts a poll that has nothing to ask")

    def test_an_event_for_another_transcription_is_ignored(self):
        expr = ("(async () => {\n"
                "  activeLiveTranscriptionId = 't1';\n"
                "  liveJob = { transcriptionId: 't1', status: 'running' };\n"
                "  liveDetail = null;\n"
                "  liveGatePending = true;\n"
                "  onLiveGateReleased({ transcriptionId: 'other' });\n"
                "  return { pending: liveGatePending };\n"
                "})()")
        result = self._run([("ignored", expr)])["ignored"]
        self.assertNotIsInstance(result, str, msg=result)
        self.assertTrue(result["pending"],
                        msg="one recording's gate says nothing about another's")


if __name__ == "__main__":
    unittest.main()
