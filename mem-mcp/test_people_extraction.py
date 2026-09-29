"""Tests for the diary LLM-extraction windowing in diary_manager.py.

Covers both extractors that read a diary entry: person *names* and search
*keywords*. They had the same defect in the same shape -- a prefix slice --
and were fixed the same way, so they are guarded together here.

diary_manager cannot be imported without the DB drivers, so the function under
test is lifted out of the source with ``ast.get_source_segment`` and exec'd
against stubs — the same technique test_embedding_reliability.py uses, and the
same reason: it tests the shipping code rather than a transcription of it.

The defect these guard against was silent. The old code sent ``content[:2000]``
to the extractor, so on a long entry every person named after character 2000
was simply never linked. Nothing errored, and the only visible symptom was a
missing MENTIONS edge that nobody was looking for.
"""

import ast
import asyncio
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chunking import normalize_text
from matching_utils import parse_people_name_array, text_windows

DIARY_MANAGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diary_manager.py")


def _segment(name):
    """The source of one top-level function in diary_manager.py."""
    with open(DIARY_MANAGER, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    return source, tree, target, ast.get_source_segment(source, target)


class _Recorder:
    """Collects everything the lifted code would have logged."""

    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, message, *a, **k):
        self.warnings.append(str(message))

    def debug(self, message, *a, **k):
        self.debugs.append(str(message))

    def __getattr__(self, _name):  # info/error/... must not explode
        return lambda *a, **k: None


def _lift(name, **overrides):
    """Exec one top-level function out of diary_manager.py with stubs."""
    _source, _tree, _target, segment = _segment(name)
    namespace = {"normalize_text": normalize_text, "os": os, "re": re, "json": json}
    namespace.update(overrides)
    exec(segment, namespace)  # noqa: S102 - executing our own source
    return namespace[name]


def _lift_keyword_extractor(llm, recorder=None, **overrides):
    """Lift ``extract_diary_keywords`` with everything it touches in scope.

    Behavioural rather than source-level on purpose. The defect it replaced was
    a slice, and a slice is invisible to a call-count assertion: the old code
    also made exactly one call, it just sent a different argument. Only a test
    that inspects what was actually sent can tell the two apart.
    """
    recorder = recorder or _Recorder()
    clean = _lift("_clean_keywords")
    namespace = {
        "text_windows": text_windows,
        "KEYWORD_EXTRACT_WINDOW": 6000,
        "KEYWORD_EXTRACT_OVERLAP": 600,
        "KEYWORD_EXTRACT_WARN_WINDOWS": 6,
        "KEYWORD_LIMIT": 20,
        "logger": recorder,
        "get_llm_response": llm,
        "_clean_keywords": clean,
        "_KEYWORD_EXTRACT_SYSTEM": "stub system prompt",
        "re": re,
        "json": json,
    }
    namespace.update(overrides)
    return _lift("extract_diary_keywords", **namespace)


class PeopleWindowTests(unittest.TestCase):
    def setUp(self):
        self.windows = _lift(
            "people_extract_windows",
            PEOPLE_EXTRACT_WINDOW=6000,
            PEOPLE_EXTRACT_OVERLAP=600,
        )

    def test_blank_content_yields_no_calls(self):
        self.assertEqual(self.windows(""), [])
        self.assertEqual(self.windows("   \n\t "), [])
        self.assertEqual(self.windows(None), [])

    def test_short_entry_is_a_single_window(self):
        """A short entry must cost exactly what it always did: one call."""
        self.assertEqual(self.windows("Alice called Bob."), ["Alice called Bob."])

    def test_entry_exactly_at_the_window_is_still_one_call(self):
        self.assertEqual(len(self.windows("x" * 6000)), 1)
        self.assertEqual(len(self.windows("x" * 6001)), 2)

    def test_every_character_is_seen_by_some_window(self):
        """The defect: content past the first window was never looked at.

        Asserted by walking the source offsets rather than by searching for
        substrings — a name-shaped needle would match at every offset in
        repetitive text, which is how a coverage test can pass while the code
        skips the middle.
        """
        body = "".join(f"{i:06d}" for i in range(6000))
        windows = self.windows(body)
        self.assertGreater(len(windows), 1, "36k chars must not be one window")
        covered = [False] * len(body)
        offset = 0
        for window in windows:
            # Each window is a slice of the body. The body is non-repetitive so
            # index() lands on the true offset rather than an earlier match.
            index = body.index(window, max(0, offset - 700))
            for pos in range(index, index + len(window)):
                covered[pos] = True
            offset = index
        self.assertTrue(all(covered), "some characters were in no window")

    def test_windows_overlap_so_a_name_is_not_cut_in_half(self):
        window, overlap = 1000, 200
        windows = _lift(
            "people_extract_windows",
            PEOPLE_EXTRACT_WINDOW=window,
            PEOPLE_EXTRACT_OVERLAP=overlap,
        )("x" * 2500)
        self.assertGreater(len(windows), 1)
        for earlier, later in zip(windows, windows[1:]):
            self.assertTrue(
                earlier.endswith(later[:overlap]),
                "consecutive windows must share a boundary region",
            )

    def test_a_name_across_a_boundary_lands_whole_in_at_least_one_window(self):
        window, overlap = 500, 100
        body = ("filler " * 200) + "ALICE SMITH" + (" filler" * 200)
        windows = _lift(
            "people_extract_windows",
            PEOPLE_EXTRACT_WINDOW=window,
            PEOPLE_EXTRACT_OVERLAP=overlap,
        )(body)
        self.assertTrue(
            any("ALICE SMITH" in w for w in windows),
            "the name was split across every window boundary",
        )

    def test_crlf_is_normalised(self):
        self.assertEqual(self.windows("a\r\nb"), ["a\nb"])

    def test_explicit_overrides_win_over_the_module_constants(self):
        windows = _lift(
            "people_extract_windows",
            PEOPLE_EXTRACT_WINDOW=6000,
            PEOPLE_EXTRACT_OVERLAP=600,
        )("y" * 500, window=100, overlap=10)
        # step = 100 - 10 = 90, so range(0, 500, 90) yields 6 windows.
        self.assertEqual(len(windows), len(range(0, 500, 90)))
        self.assertTrue(all(len(w) <= 100 for w in windows))

    def test_overlap_cannot_reach_the_window_size(self):
        """A degenerate overlap would make the step zero and never terminate."""
        windows = _lift(
            "people_extract_windows",
            PEOPLE_EXTRACT_WINDOW=100,
            PEOPLE_EXTRACT_OVERLAP=100,
        )("z" * 1000)
        self.assertGreater(len(windows), 1)
        self.assertTrue(all(w for w in windows))

    def test_configured_constants_read_the_environment(self):
        with open(DIARY_MANAGER, "r", encoding="utf-8") as handle:
            source = handle.read()
        for name, default in (
            ("PEOPLE_EXTRACT_WINDOW", "6000"),
            ("PEOPLE_EXTRACT_OVERLAP", "600"),
        ):
            self.assertIn(
                f'{name} = max(', source, f"{name} must be clamped at the lower bound"
            )
            self.assertIn(default, source)


class ExtractLoopTests(unittest.TestCase):
    """The caller must union every window rather than keep the last result."""

    def setUp(self):
        with open(DIARY_MANAGER, "r", encoding="utf-8") as handle:
            self.source = handle.read()
        tree = ast.parse(self.source)
        self.fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_extract_people_names"
        )
        self.body = ast.get_source_segment(self.source, self.fn)

    def test_it_loops_over_windows(self):
        self.assertIn("for index, window in enumerate(windows):", self.body)
        self.assertIn("get_llm_response(window", self.body)

    def test_it_no_longer_slices_the_content_to_2000_chars(self):
        self.assertNotIn(
            "content[:2000]", self.body, "the silent 2000-char truncation is back"
        )

    def test_one_failing_window_does_not_discard_the_others(self):
        """The loop needs a try inside the loop, not around the whole thing."""
        loop_start = self.body.index("for index, window in enumerate(windows):")
        loop_body = self.body[loop_start:]
        self.assertIn("except Exception", loop_body)
        # ...and the except must be inside the for, i.e. the accumulate line
        # has to precede the handler in the same block.
        self.assertLess(
            loop_body.index("found.extend"),
            loop_body.index("except Exception"),
            "accumulation must happen inside the try",
        )

    def test_results_are_deduped_across_windows(self):
        self.assertIn("clean_extracted_people_names(found)", self.body)

    def test_a_very_long_entry_warns_instead_of_truncating(self):
        """A cap would silently drop the tail — the exact bug being fixed."""
        self.assertIn("PEOPLE_EXTRACT_WARN_WINDOWS", self.body)
        self.assertIn("logger.warning", self.body)
        self.assertNotRegex(
            self.body, r"windows\[:\s*\d+\s*\]", "windows must not be sliced to a cap"
        )

    def test_the_old_whole_entry_debug_line_records_the_window_count(self):
        self.assertRegex(self.body, r"windows=\{len\(windows\)\}")


# ---------------------------------------------------------------------------
# Diary keyword extraction
# ---------------------------------------------------------------------------
class KeywordCleanTests(unittest.TestCase):
    """The dedupe/ordering helper, called directly.

    Order is load-bearing: each window's prompt asks for the *most important*
    keywords first, so truncating the union to the limit keeps the best ones
    rather than an arbitrary slice of a set.
    """

    def setUp(self):
        self.clean = _lift("_clean_keywords")

    def test_it_lowercases_and_strips(self):
        self.assertEqual(
            self.clean(["  Atlas Migration ", "HEDRON", "acme corp"]),
            ["atlas migration", "hedron", "acme corp"],
        )

    def test_dedupe_is_case_insensitive(self):
        self.assertEqual(self.clean(["Atlas", "atlas", "ATLAS", " atlas "]), ["atlas"])

    def test_order_is_first_seen(self):
        self.assertEqual(self.clean(["c", "a", "b", "a"]), ["c", "a", "b"])

    def test_blanks_and_non_strings_are_dropped(self):
        self.assertEqual(self.clean(["a", "", "   ", None, 7, [], {}, "b"]), ["a", "b"])

    def test_none_and_empty_input_yield_nothing(self):
        self.assertEqual(self.clean(None), [])
        self.assertEqual(self.clean([]), [])

    def test_the_limit_keeps_the_front_of_the_list(self):
        self.assertEqual(self.clean(list("abcdefghij"), limit=3), ["a", "b", "c"])

    def test_no_limit_means_no_truncation(self):
        self.assertEqual(len(self.clean([f"k{i}" for i in range(50)])), 50)


class KeywordWindowTests(unittest.TestCase):
    """Keywords must come from the whole entry, not its first 1500 characters.

    The old code sent ``text[:1500]``, so on a 40k transcription the keywords
    described its opening. A query about anything in the last thirty pages then
    scored as though the entry had never mentioned it -- the boost simply was not
    there, with no error anywhere to explain the missing relevance.
    """

    def setUp(self):
        self.prompts = []
        self.recorder = _Recorder()
        # 12k chars of non-repetitive content -> three 6k windows.
        self.body = "".join(f"paragraph {i:06d}. " for i in range(800))
        self.marker = "HEDRON-ATLAS-PILOT"

    def _extractor(self, responder, **overrides):
        async def llm(prompt, system=None, num_predict=None):
            self.prompts.append(prompt)
            return responder(prompt, len(self.prompts))

        return _lift_keyword_extractor(llm, self.recorder, **overrides)

    @staticmethod
    def _reply(*keywords):
        return json.dumps({"keywords": list(keywords)})

    def test_a_short_entry_still_costs_exactly_one_call(self):
        extract = self._extractor(lambda p, n: self._reply("atlas"))
        out = asyncio.run(extract("Tuesday", "Alice called Bob about the migration."))
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(out, ["atlas"])

    def test_every_window_is_sent_to_the_model(self):
        extract = self._extractor(lambda p, n: self._reply())
        asyncio.run(extract("Tuesday", self.body))
        self.assertEqual(len(self.prompts), 3, f"got {len(self.prompts)} prompts")

    def test_the_prompt_carries_text_past_the_old_1500_char_cut(self):
        """The defect itself, stated as an assertion."""
        body = ("padding. " * 200) + self.marker + " " + ("tail. " * 400)

        def responder(prompt, _n):
            return self._reply("atlas-pilot") if self.marker in prompt else self._reply()

        extract = self._extractor(responder)
        out = asyncio.run(extract("Tuesday", body))
        self.assertIn(
            "atlas-pilot", out, "a keyword visible only late in the entry was lost"
        )

    def test_keywords_from_several_windows_are_unioned(self):
        def responder(_p, n):
            return self._reply(f"kw{n}")

        extract = self._extractor(responder)
        out = asyncio.run(extract("Tuesday", self.body))
        self.assertEqual(out, ["kw1", "kw2", "kw3"])

    def test_a_keyword_found_in_two_windows_appears_once(self):
        extract = self._extractor(lambda p, n: self._reply("atlas", "atlas"))
        out = asyncio.run(extract("Tuesday", self.body))
        self.assertEqual(out, ["atlas"])

    def test_the_entry_name_reaches_every_window(self):
        """A window from the middle has no other way to know which entry it is."""
        extract = self._extractor(lambda p, n: self._reply())
        asyncio.run(extract("Weekly retro", self.body))
        self.assertEqual(len(self.prompts), 3)
        for prompt in self.prompts:
            self.assertIn("Weekly retro", prompt)

    def test_one_failing_window_does_not_discard_the_others(self):
        def responder(_p, n):
            if n == 2:
                raise RuntimeError("ollama timeout")
            return self._reply(f"kw{n}")

        extract = self._extractor(responder)
        out = asyncio.run(extract("Tuesday", self.body))
        self.assertEqual(out, ["kw1", "kw3"])

    def test_an_unparseable_window_is_skipped(self):
        def responder(_p, n):
            return "sorry, I cannot help" if n == 1 else self._reply("kw2")

        extract = self._extractor(responder)
        self.assertEqual(asyncio.run(extract("Tuesday", self.body)), ["kw2"])

    def test_a_fenced_reply_is_still_parsed(self):
        extract = self._extractor(lambda p, n: "```json\n" + self._reply("atlas") + "\n```")
        self.assertEqual(asyncio.run(extract("Tuesday", "short entry")), ["atlas"])

    def test_the_union_is_capped_but_keeps_the_front(self):
        extract = self._extractor(lambda p, n: self._reply("a", "b", "c"), KEYWORD_LIMIT=2)
        self.assertEqual(asyncio.run(extract("Tuesday", "short")), ["a", "b"])

    def test_a_very_long_entry_warns_instead_of_truncating(self):
        """A cap on the window count would restore the silent tail-drop."""
        body = "word " * 60000
        extract = self._extractor(lambda p, n: self._reply("kw"))
        asyncio.run(extract("Tuesday", body))
        self.assertTrue(
            any("MEM_KEYWORD_WINDOW" in w for w in self.recorder.warnings),
            f"expected a cost warning, got {self.recorder.warnings}",
        )

    def test_no_error_leaves_a_save_blocked(self):
        """The contract: keyword extraction is best-effort and returns []."""

        async def llm(prompt, system=None, num_predict=None):
            raise RuntimeError("ollama down")

        extract = _lift_keyword_extractor(llm, self.recorder)
        self.assertEqual(asyncio.run(extract("Tuesday", "some content")), [])


class KeywordSourceTests(unittest.TestCase):
    """Two assertions about the call site rather than the behaviour.

    The behavioural tests above would all still pass if the old truncation were
    reintroduced *alongside* the window loop, so pin the shape too.
    """

    def setUp(self):
        _source, _tree, _target, self.body = _segment("extract_diary_keywords")

    def test_the_1500_char_slice_is_gone(self):
        self.assertFalse("text[:1500]" in self.body, "the silent 1500-char truncation is back")

    def test_windows_are_not_sliced_to_a_cap(self):
        self.assertNotRegex(
            self.body, r"windows\[:\s*\d+\s*\]", "windows must not be sliced to a cap"
        )

    def test_the_window_helper_is_actually_used(self):
        # assertTrue rather than assertIn: the haystack is a whole function and
        # assertIn echoes both operands into the report.
        self.assertTrue(
            "text_windows(content, KEYWORD_EXTRACT_WINDOW, KEYWORD_EXTRACT_OVERLAP)" in self.body,
            "extract_diary_keywords no longer windows the content",
        )


class DegenerateArrayParseTests(unittest.TestCase):
    """A model that never closes its array must not cost us the names in it.

    Measured on a real 30k-char diary entry: one 6000-char window enumerated
    ten correct names and then repeated the same name two hundred times
    without ever emitting the closing bracket. The old `re.search(r"\\[.*\\]")`
    found nothing, the window was skipped, and ten good names were lost with
    no error logged. Raising num_predict does not fix it -- the repetition is
    the fault, it just runs longer.
    """

    def test_a_closed_array_is_unchanged(self):
        self.assertEqual(parse_people_name_array('["Alice Smith", "Bob Jones"]'),
                         ["Alice Smith", "Bob Jones"])

    def test_fenced_json_is_still_parsed(self):
        self.assertEqual(parse_people_name_array('```json\n["Alice"]\n```'), ["Alice"])

    def test_a_repeating_unterminated_array_yields_the_good_prefix(self):
        raw = '["Priyanka", "Siarhei Bahdanau", "Tim Lohmann"' + ', "Siarhei Bahdanau"' * 300
        names = parse_people_name_array(raw)
        self.assertIsNotNone(names, msg="an unterminated array must not be discarded")
        self.assertEqual(names[:3], ["Priyanka", "Siarhei Bahdanau", "Tim Lohmann"],
                         msg="names emitted before the degenerate tail are as "
                             "trustworthy as any other extraction")

    def test_prose_with_no_array_is_none(self):
        for raw in ("I could not find any people.", "", "no bracket here"):
            self.assertIsNone(parse_people_name_array(raw))

    def test_a_json_object_is_not_mistaken_for_a_name_array(self):
        self.assertIsNone(parse_people_name_array('{"names": ["Alice"]}'),
                          msg="an object is not an array of names")

    def test_escaped_quotes_inside_a_name_survive(self):
        self.assertEqual(parse_people_name_array('["Ann \\"Annie\\" Lee"]'),
                         ['Ann "Annie" Lee'])


if __name__ == "__main__":
    unittest.main()
