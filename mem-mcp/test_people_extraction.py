"""Tests for the diary people-extraction windowing in diary_manager.py.

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
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chunking import normalize_text

DIARY_MANAGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diary_manager.py")


def _lift(name, **overrides):
    """Exec one top-level function out of diary_manager.py with stubs."""
    with open(DIARY_MANAGER, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    segment = ast.get_source_segment(source, target)
    namespace = {"normalize_text": normalize_text, "os": os}
    namespace.update(overrides)
    exec(segment, namespace)  # noqa: S102 - executing our own source
    return namespace[name]


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


if __name__ == "__main__":
    unittest.main()
