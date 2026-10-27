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


if __name__ == "__main__":
    unittest.main()
