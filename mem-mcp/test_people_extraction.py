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
import datetime
import json
import os
import re
import sys
import typing
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chunking import normalize_text
from matching_utils import parse_people_name_array, text_windows

MCP_TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_tools.py")
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
        # The function passes this explicitly now that extraction has its own
        # model. The stub LLM asserts on `model`, so a NameError here would be
        # indistinguishable from a broken extractor.
        "EXTRACT_MODEL": "stub-extract-model",
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

    def test_window_count_at_and_just_past_the_boundary(self):
        # One fact, three inputs: how many calls an entry of a given length
        # costs, and what those windows contain. Each case carries its own
        # expected window list, so "a short entry" and "exactly at the window"
        # cannot be satisfied by the same answer.
        cases = (
            ("short entry", "Alice called Bob.", ["Alice called Bob."]),
            ("exactly at the window", "x" * 6000, ["x" * 6000]),
            ("one char past the window", "x" * 6001, ["x" * 6000, "x" * 601]),
        )
        for label, content, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.windows(content), expected)

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
        # Scoped to the assignment statement, not the whole file: `assertIn("6000",
        # source)` over 2500 lines of diary_manager passes on any 6000 anywhere,
        # so it could not tell this constant's default from a neighbour's. The
        # statement is found by AST so the default and the clamp are checked on
        # the same line that binds the name.
        with open(DIARY_MANAGER, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        for name, default in (
            ("PEOPLE_EXTRACT_WINDOW", "6000"),
            ("PEOPLE_EXTRACT_OVERLAP", "600"),
        ):
            with self.subTest(constant=name):
                target = next(
                    node for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name
                            for t in node.targets)
                )
                stmt = ast.get_source_segment(source, target)
                self.assertTrue(stmt.startswith(f"{name} = max("),
                                msg=f"{name} must be clamped at the lower bound, got: {stmt}")
                self.assertIn(f'"{default}"', stmt,
                              msg=f"{name} must default to {default}, got: {stmt}")


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
        self.assertFalse(
            "content[:2000]" in self.body, "the silent 2000-char truncation is back"
        )
        # The literal check above is one spelling of the defect; the AST walk is
        # the rule. `assertNotIn("content[:2000]", body)` alone passes on a
        # rewrite to `body[:2000]`, which is the same truncation under another
        # name -- and a substring guard is also what a docstring quoting the
        # slice trips, so neither half is sufficient alone.
        for node in ast.walk(ast.parse(self.body)):
            if not isinstance(node, ast.Slice) or node.lower is not None:
                continue
            upper = node.upper
            if isinstance(upper, ast.Constant) and isinstance(upper.value, int):
                self.fail(f"a numeric prefix slice is back in _extract_people_names: "
                          f"{ast.get_source_segment(self.body, node)}")

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

    def test_the_limit_applies_only_when_given_and_takes_from_the_front(self):
        # One fact about `limit`: absent means no truncation, and a limit is a
        # ceiling on a first-seen list rather than a set-ordering. Each case
        # carries its own expected list.
        cases = (
            ("no limit", 0, list("abcdefghij")),
            ("limit of 0 is also no limit", 0, list("abcdefghij")),
            ("limit above the length", 20, list("abcdefghij")),
            ("limit below the length keeps the front", 3, ["a", "b", "c"]),
            ("limit of one", 1, ["a"]),
        )
        for label, limit, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.clean(list("abcdefghij"), limit=limit), expected)


class KeywordWindowTests(unittest.TestCase):
    """Keywords must come from the whole entry, not its first 1500 characters.

    The old code sent ``text[:1500]``, so on a 40k transcription the keywords
    described its opening. A query about anything in the last thirty pages then
    scored as though the entry had never mentioned it -- the boost simply was not
    there, with no error anywhere to explain the missing relevance.
    """

    def setUp(self):
        self.prompts = []
        self.models = []
        self.recorder = _Recorder()
        # 12k chars of non-repetitive content -> three 6k windows.
        self.body = "".join(f"paragraph {i:06d}. " for i in range(800))
        self.marker = "HEDRON-ATLAS-PILOT"

    def _extractor(self, responder, **overrides):
        async def llm(prompt, system=None, num_predict=None, model=None, timeout=None):
            # The call site now names the extraction model explicitly, so the
            # stub must accept it. It also asserts below that the model it is
            # handed is the extraction one and not a leftover default.
            self.prompts.append(prompt)
            self.models.append(model)
            return responder(prompt, len(self.prompts))

        return _lift_keyword_extractor(llm, self.recorder, **overrides)

    @staticmethod
    def _reply(*keywords):
        return json.dumps({"keywords": list(keywords)})

    def test_one_model_call_per_window_at_both_ends_of_the_range(self):
        # The property is one call per window, so both ends of the range belong
        # in one test: a regression back to a single call still passes on the
        # short entry alone. Per-case expected call count and expected result.
        cases = (
            ("short entry", "Alice called Bob about the migration.",
             lambda n: self._reply("atlas"), 1, ["atlas"]),
            ("12k entry, three windows", self.body,
             lambda n: self._reply(), 3, []),
        )
        for label, body, reply, expected_calls, expected_out in cases:
            with self.subTest(label):
                self.prompts = []
                self.models = []
                extract = self._extractor(lambda p, n: reply(n))
                out = asyncio.run(extract("Tuesday", body))
                self.assertEqual(len(self.prompts), expected_calls,
                                 msg=f"got {len(self.prompts)} prompts")
                self.assertEqual(out, expected_out)

    def test_the_extraction_model_is_the_one_actually_called(self):
        """Behavioural, not a source check: the model is read off the call.

        The stub records the `model=` it was handed, so this asserts the real
        value rather than that the source mentions the constant. Keyword
        extraction is the role that moved onto MEM_EXTRACT_MODEL; if the call
        site silently reverts to inheriting LLM_QUERY_MODEL the extraction still
        works and still returns keywords, just with the slower, more inventive
        model -- and nothing else in the suite would notice.
        """
        extract = self._extractor(lambda p, n: self._reply("atlas"))
        asyncio.run(extract("Tuesday", "Alice called Bob about the migration."))
        self.assertEqual(self.models, ["stub-extract-model"])

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

    def test_the_window_results_are_unioned_and_deduped(self):
        # One claim about combining the per-window lists: their union, deduped,
        # in first-seen order. The duplicated case is the same operation on a
        # different input, not a second property.
        cases = (
            ("distinct keyword per window", lambda n: self._reply(f"kw{n}"),
             ["kw1", "kw2", "kw3"]),
            ("the same keyword in every window", lambda n: self._reply("atlas", "atlas"),
             ["atlas"]),
        )
        for label, reply, expected in cases:
            with self.subTest(label):
                self.prompts = []
                extract = self._extractor(lambda p, n: reply(n))
                self.assertEqual(asyncio.run(extract("Tuesday", self.body)), expected)

    def test_the_entry_name_reaches_every_window(self):
        """A window from the middle has no other way to know which entry it is."""
        extract = self._extractor(lambda p, n: self._reply())
        asyncio.run(extract("Weekly retro", self.body))
        self.assertEqual(len(self.prompts), 3)
        for prompt in self.prompts:
            self.assertIn("Weekly retro", prompt)

    def test_a_failing_window_is_skipped_and_the_others_survive(self):
        # Two ways a window fails to produce keywords -- it raises, or it
        # answers with something that is not a keyword object -- and one claim
        # either way: the windows that worked still apply. Each case names the
        # window it fails and its own expected union.
        def raises_on(index):
            def responder(_p, n):
                if n == index:
                    raise RuntimeError("ollama timeout")
                return self._reply(f"kw{n}")
            return responder

        def unparseable_first(_p, n):
            return "sorry, I cannot help" if n == 1 else self._reply("kw2")

        cases = (
            ("middle window raises", raises_on(2), ["kw1", "kw3"]),
            ("first window is unparseable", unparseable_first, ["kw2"]),
        )
        for label, responder, expected in cases:
            with self.subTest(label):
                self.prompts = []
                extract = self._extractor(responder)
                self.assertEqual(asyncio.run(extract("Tuesday", self.body)), expected)

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

    def test_the_window_helper_is_actually_used(self):
        # assertTrue rather than assertIn: the haystack is a whole function and
        # assertIn echoes both operands into the report.
        self.assertTrue(
            "text_windows(content, KEYWORD_EXTRACT_WINDOW, KEYWORD_EXTRACT_OVERLAP)" in self.body,
            "extract_diary_keywords no longer windows the content",
        )

    def test_neither_the_text_nor_the_window_list_is_sliced(self):
        # Two absences, one claim: this function takes no prefix of the entry
        # and no prefix of the window list. Each is checked on its own needle,
        # and the presence assertion runs first so an empty or unparseable
        # segment cannot make both negatives vacuous.
        self.assertTrue(
            "text_windows(content, KEYWORD_EXTRACT_WINDOW, KEYWORD_EXTRACT_OVERLAP)" in self.body,
            "refusing to slice is only meaningful while the content is windowed",
        )
        with self.subTest("the old 1500-char prefix slice"):
            self.assertFalse("text[:1500]" in self.body,
                             "the silent 1500-char truncation is back")
        with self.subTest("no cap on the number of windows"):
            self.assertNotRegex(
                self.body, r"windows\[:\s*\d+\s*\]", "windows must not be sliced to a cap"
            )
        # The two needles above are spellings of the defect; the walk is the
        # rule, so a rewrite to `body[:1500]` under another name is caught too.
        # A docstring quoting the old slice is an ast.Constant and cannot trip it.
        for node in ast.walk(ast.parse(self.body)):
            if not isinstance(node, ast.Slice) or node.lower is not None:
                continue
            upper = node.upper
            if isinstance(upper, ast.Constant) and isinstance(upper.value, int):
                self.fail("a numeric prefix slice is back in extract_diary_keywords: "
                          f"{ast.get_source_segment(self.body, node)}")


class DegenerateArrayParseTests(unittest.TestCase):
    """A model that never closes its array must not cost us the names in it.

    Measured on a real 30k-char diary entry: one 6000-char window enumerated
    ten correct names and then repeated the same name two hundred times
    without ever emitting the closing bracket. The old `re.search(r"\\[.*\\]")`
    found nothing, the window was skipped, and ten good names were lost with
    no error logged. Raising num_predict does not fix it -- the repetition is
    the fault, it just runs longer.
    """

    def test_a_well_formed_array_of_names_parses_exactly(self):
        # One property -- well-formed input is returned verbatim -- over the
        # three shapes that used to need different handling. Each case carries
        # the raw text and the exact names it must yield.
        cases = (
            ("plain array", '["Alice Smith", "Bob Jones"]',
             ["Alice Smith", "Bob Jones"]),
            ("fenced in a json block", '```json\n["Alice"]\n```', ["Alice"]),
            ("escaped quotes inside a name", '["Ann \\"Annie\\" Lee"]',
             ['Ann "Annie" Lee']),
        )
        for label, raw, expected in cases:
            with self.subTest(label):
                self.assertEqual(parse_people_name_array(raw), expected)

    def test_input_that_is_not_a_name_array_yields_nothing(self):
        # One property -- nothing that is not an array of names returns names --
        # and refusing is the safe direction, because an invented name becomes a
        # MENTIONS edge. Prose, an empty answer and a JSON *object* all fall here.
        cases = (
            ("a sentence that found nobody", "I could not find any people."),
            ("an empty reply", ""),
            ("prose with no bracket", "no bracket here"),
            ("a json object, not an array", '{"names": ["Alice"]}'),
        )
        for label, raw in cases:
            with self.subTest(label):
                self.assertIsNone(parse_people_name_array(raw))

    def test_a_repeating_unterminated_array_yields_the_good_prefix(self):
        raw = '["Priyanka", "Siarhei Bahdanau", "Tim Lohmann"' + ', "Siarhei Bahdanau"' * 300
        names = parse_people_name_array(raw)
        self.assertIsNotNone(names, msg="an unterminated array must not be discarded")
        self.assertEqual(names[:3], ["Priyanka", "Siarhei Bahdanau", "Tim Lohmann"],
                         msg="names emitted before the degenerate tail are as "
                             "trustworthy as any other extraction")


class ReclassifyIsScopeOnlyTests(unittest.TestCase):
    """A reclassify must not re-run participant extraction.

    ``_classify_and_link_diary`` used to call a *private* copy of people
    extraction before doing anything else. That copy lived in
    migrate_client_context.py and resolved to ``SCOPE_MODEL``, while the real
    one -- ``diary_manager._auto_link_people``, called on save, on update, and
    by the UI's "Extract participants" -- resolves to ``EXTRACT_MODEL``. So the
    same task existed twice on two different models, and a user clicking
    "Reclassify scope" silently rewrote MENTIONS edges using the slower one.

    This is behavioural, not a source check. ``assertNotIn("_extract_people_
    names", body)`` would pass against the defect the first time it was written
    and then rot with the refactor, and worse: a twin defined in another module
    is invisible to a substring guard on this function altogether. The names are
    bound as stubs that raise if called, so the only way this passes is if the
    function genuinely does not reach them.
    """

    MODULE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "migrate_client_context.py")

    def _lift(self, **overrides):
        with open(self.MODULE, "r", encoding="utf-8") as handle:
            source = handle.read()
        target = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_classify_and_link_diary"
        )
        segment = ast.get_source_segment(source, target)

        def _forbidden(*_a, **_k):
            raise AssertionError(
                "reclassify called people extraction; it must classify scope only"
            )

        namespace = {
            # The lifted body is an async def, so it closes over asyncio.
            "asyncio": asyncio,
            "_extract_people_names": _forbidden,
            "_link_missing_people": _forbidden,
            "_auto_link_people": _forbidden,
            "db_find_people_matches": _forbidden,
            "resolve_people_candidates": _forbidden,
        }
        namespace.update(overrides)
        exec(segment, namespace)  # noqa: S102 - executing our own source
        return namespace["_classify_and_link_diary"]

    def test_a_reclassify_never_reaches_a_people_extractor(self):
        # Every collaborator the function needs for the *scope* verdict is
        # stubbed so the body runs to completion without a database. The
        # unanimous-MENTIONS fast path is the interesting one: it READS the
        # edges people extraction writes, so a stubbed match on it proves the
        # read survives the write's removal.
        linked = {"value": False}

        class _Sem:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        def _fast(*_a, **_k):
            return ("EPAM", None)

        async def _stamp(*_a, **_k):
            return None

        def _write(*_a, **_k):
            # _write_related_links is sync (migrate_client_context.py:501).
            # An async stub here is awaited-never, and the resulting
            # RuntimeWarning is the only thing that says so.
            return None

        async def _classify_scope_full(_body, _clients):
            return ("EPAM", None, [], True)

        def _resolve(name, *rest):
            # db_resolve_client / db_resolve_context are sync; making the stub
            # async would fail on a subscript rather than on the thing under
            # test, which hides the real assertion.
            return {"id": "c1", "name": name}

        async def _link_diary(*_a, **_k):
            return None

        async def _link_ctx(*_a, **_k):
            return None

        def _enriched(*_a, **_k):
            return "entry text"

        fn = self._lift(
            sem=_Sem, _fast_diary_scope=_fast,
            _enriched_diary_text=_enriched,
            classify_scope_full=_classify_scope_full,
            _stamp_scope_checked=_stamp,
            _write_related_links=_write,
            db_resolve_client=_resolve,
            db_resolve_context=_resolve,
            link_diary_to_client=_link_diary,
            link_diary_to_context=_link_ctx,
            _related_clients_for=lambda *_a, **_k: [],
            logger=_Recorder(),
        )

        item = {"id": "d1", "content": "Alice Smith and Bob Jones met.",
                "name": "entry", "keywords": []}
        linked["value"] = asyncio.run(fn(item, [{"name": "EPAM"}], "u1", _Sem(), None, None, None))
        self.assertTrue(linked["value"],
                        msg="the stubbed scope path stopped linking, so this test "
                            "is no longer exercising the reclassify body")

    def test_the_duplicate_extractors_are_gone_from_the_module(self):
        """The twin itself, not just its call site.

        Both copies were removed rather than one of them. Leaving a dead
        private copy of an extractor that routes to the wrong model is an
        invitation to wire it back up, and the model routing would be the
        subtle part -- it would still pass every behavioural test, because
        nobody would be calling it.
        """
        with open(self.MODULE, "r", encoding="utf-8") as handle:
            source = handle.read()
        for name in ("_PEOPLE_SYSTEM", "_extract_people_names",
                     "_link_missing_people", "_existing_auto_people",
                     "_diary_has_mentions"):
            needle = f"def {name}"
            self.assertFalse(needle in source, f"{name} is still defined here")
            needle = f"{name} ="
            self.assertFalse(needle in source, f"{name} is still assigned here")

    def test_the_real_extractor_is_still_reachable_from_the_save_path(self):
        """The other half of the fix: extraction must not have been removed.

        Deleting the twin is only correct while diary_manager still owns the
        job. A future edit that removes both leaves an entry whose MENTIONS
        edges are never written, and no test in this file would notice -- the
        windowing tests lift diary_manager's own function and would keep
        passing on a code path nothing calls.
        """
        with open(DIARY_MANAGER, "r", encoding="utf-8") as handle:
            diary = handle.read()
        self.assertTrue("async def _auto_link_people(" in diary,
                        msg="diary_manager no longer owns people extraction")
        for caller, path in (("_auto_link_people(doc_id", diary),
                             ("_auto_link_people(entry_id", diary)):
            self.assertTrue(caller in path,
                            msg=f"the save/update path no longer calls {caller}")

    def test_reclassify_still_reads_mentions_as_scope_evidence(self):
        """Scope classification depends on MENTIONS, so it must keep reading them.

        This is why deleting the write is safe rather than a regression: the
        unanimous-MENTIONS fast path in _fast_diary_scope is the strongest
        signal the classifier gets, and clear_scope_links_batch deletes only
        FOR_CLIENT and IN_CONTEXT. The evidence a reclassify reads is written
        by the save path and survives the clear.
        """
        with open(self.MODULE, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        fast = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_fast_diary_scope"
        )
        segment = ast.get_source_segment(source, fast)
        self.assertTrue("MENTIONS" in segment,
                        msg="_fast_diary_scope no longer reads MENTIONS edges")

        clear = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "clear_scope_links_batch"
        )
        clear_src = ast.get_source_segment(source, clear)
        self.assertTrue("FOR_CLIENT|IN_CONTEXT" in clear_src,
                        msg="the scope clear no longer names both edge types")
        self.assertFalse("MENTIONS" in clear_src,
                         msg="the scope clear must not delete MENTIONS -- that "
                             "would destroy the evidence the fast path reads")




def _lift_tool(node_name, namespace):
    """Lift a tool out of mcp_tools.py.

    mcp_tools cannot be imported here (no fastmcp, no httpx), so the routing
    decision is exec'd from the real source rather than transcribed. A copied
    version would keep passing after the shipping tool was broken, which is the
    whole reason for lifting rather than rewriting.
    """
    with open(MCP_TOOLS, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    target = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == node_name
    )
    exec(compile(ast.get_source_segment(source, target),
                 "mcp_tools.py:" + node_name, "exec"), namespace)
    return namespace[node_name]


class _Row(dict):
    """A Neo4j record: attribute access, because the query aliases camelCase."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


class _FakeCypherSession:
    """Records the query and returns the rows the test chose.

    Cypher cannot be executed here, so these tests pin the query's *shape*
    separately from the mapping of a result row onto the returned dict. The
    shape is where both of the documented diary bugs live.
    """

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def run(self, query, **params):
        self.queries.append((query, params))
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeCypherDriver:
    def __init__(self, rows):
        self._session = _FakeCypherSession(rows)

    def session(self):
        return self._session


def _list_entries_runner(rows):
    driver = _FakeCypherDriver(rows)
    # diary_manager does `from datetime import datetime, timezone, timedelta`,
    # so the lifted code needs the *class*, not the module.
    fn = _lift("db_list_diary_entries",
               get_neo4j=lambda: driver,
               json=json,
               datetime=datetime.datetime,
               timezone=datetime.timezone,
               timedelta=datetime.timedelta,
               Optional=typing.Optional)

    def run(from_ts=None, to_ts=None):
        return fn("alice", from_ts, to_ts)

    run.driver = driver
    run.session = driver._session
    return run


class ListDiaryEntriesTests(unittest.TestCase):
    """list_diary_entries must report scope, and must report it once.

    Two documented defects shaped this. Diary scope lives on the FOR_CLIENT /
    IN_CONTEXT *edges* -- no DiaryEntry node carries a clientId property at
    all, that key is Qdrant-only -- so a property read answers None for every
    entry and the whole vault looks unclassified. And a chain of OPTIONAL
    MATCHes returns the product of the rows each produces, so an entry with one
    client and one project comes back twice unless every pattern is closed by
    an aggregating WITH, the trailing one included.
    """

    SCOPED = [_Row({
        "id": "e1", "timestamp": "2026-05-15T14:30:00", "name": "Handover",
        "metadata": '{"original_file": "handover.md"}',
        "clientName": "Deutsche Bank (DB)",
        "contextName": "DB AI Adoption",
    })]

    def test_it_returns_one_dict_per_entry_with_the_documented_keys(self):
        entries = _list_entries_runner(self.SCOPED)()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        for key in ("id", "timestamp", "name", "original_file", "client",
                    "context"):
            self.assertIn(key, entry, f"{key} is documented in the tool "
                                      "description and must be present")
        # Names only. The ids are deliberately absent: diary_save_entry takes
        # client/context by name, so an id here would not round-trip, and
        # returning both invites a caller to pick the wrong one.
        self.assertNotIn("clientId", entry)
        self.assertNotIn("contextId", entry)
        self.assertIsInstance(entry, dict,
                              "the tool description documents named fields; a "
                              "tuple makes that description unreadable")

    def test_scope_comes_back_as_the_stored_spelling(self):
        entry = _list_entries_runner(self.SCOPED)()[0]
        self.assertEqual(entry["client"], "Deutsche Bank (DB)")
        self.assertEqual(entry["context"], "DB AI Adoption")

    def test_the_original_file_is_reported(self):
        self.assertEqual(_list_entries_runner(self.SCOPED)()[0]["original_file"],
                         "handover.md")

    def test_the_longer_metadata_spelling_is_read_too(self):
        row = _Row(dict(self.SCOPED[0],
                        metadata='{"original_filename": "long-form.md"}'))
        self.assertEqual(_list_entries_runner([row])()[0]["original_file"],
                         "long-form.md")

    def test_an_unscoped_entry_reports_null_rather_than_a_guess(self):
        row = _Row({"id": "e2", "timestamp": "2026-05-15T10:00:00",
                    "name": "Standup", "metadata": None,
                    "clientName": None, "contextName": None})
        entry = _list_entries_runner([row])()[0]
        self.assertIsNone(entry["client"])
        self.assertIsNone(entry["context"])
        self.assertEqual(entry["original_file"], "")

    def test_a_nameless_entry_is_labelled_rather_than_null(self):
        row = _Row({"id": "e3", "timestamp": None, "name": None,
                    "metadata": None, "clientName": None, "contextName": None})
        entry = _list_entries_runner([row])()[0]
        self.assertEqual(entry["name"], "Unnamed")
        self.assertIsNone(entry["timestamp"])

    def test_metadata_stored_as_a_map_is_not_raised_on(self):
        row = _Row(dict(self.SCOPED[0], metadata={"original_file": "map.md"}))
        self.assertEqual(_list_entries_runner([row])()[0]["original_file"],
                         "map.md")

    def test_the_query_reads_scope_from_the_edges_not_from_properties(self):
        run = _list_entries_runner(self.SCOPED)
        run()
        query = run.session.queries[0][0]
        self.assertIn("FOR_CLIENT", query)
        self.assertIn("IN_CONTEXT", query)
        # The trap this guards: a DiaryEntry has no clientId property to read,
        # so a property read is silently None for every entry in the vault.
        self.assertNotRegex(query, r"\bd\.clientId\b")
        self.assertNotRegex(query, r"\bd\.contextId\b")

    def test_every_optional_match_is_closed_by_an_aggregating_with(self):
        run = _list_entries_runner(self.SCOPED)
        run()
        query = run.session.queries[0][0]

        # Split the query at each OPTIONAL MATCH. Every segment from one match
        # to the next (or to RETURN) must contain a `collect(` -- that is the
        # aggregation that collapses the pattern's rows back to the entry's own.
        # The trailing segment is the one that gets forgotten: collapsing every
        # pattern *except* the last turns a mentions x relevant product into a
        # single relevant multiplier, which is a bug this repo shipped once.
        segments = re.split(r"OPTIONAL\s+MATCH", query)
        self.assertEqual(len(segments) - 1, 2,
                         "precondition: both scope edges are read")
        for index, segment in enumerate(segments[1:], start=1):
            # Only the *next* pattern or the RETURN ends the span. A WITH is
            # what is supposed to close it, so cutting on it would skip the
            # very collect() being looked for.
            tail = re.split(r"\b(?:OPTIONAL\s+MATCH|RETURN)\b", segment, 1)[0]
            self.assertIn("collect(", tail,
                          f"OPTIONAL MATCH #{index} is not closed by an "
                          "aggregating WITH before the next clause, so the "
                          "rows each pattern produces multiply together")

    def test_the_defaults_are_the_last_thirty_days(self):
        run = _list_entries_runner(self.SCOPED)
        run()
        params = run.session.queries[0][1]
        self.assertEqual(params["userId"], "alice")
        self.assertTrue(params["fromTs"] < params["toTs"])
        span = (datetime.datetime.fromisoformat(params["toTs"])
                - datetime.datetime.fromisoformat(params["fromTs"]))
        self.assertEqual(span.days, 30)

    def test_an_explicit_range_is_passed_through(self):
        run = _list_entries_runner(self.SCOPED)
        run("2026-01-01T00:00:00", "2026-02-01T00:00:00")
        params = run.session.queries[0][1]
        self.assertEqual(params["fromTs"], "2026-01-01T00:00:00")
        self.assertEqual(params["toTs"], "2026-02-01T00:00:00")


class _Result(list):
    def single(self):
        return self[0] if self else None


class _WriteSession:
    """Records every statement; returns `rows` from the read."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def run(self, query, **params):
        self.statements.append((query, params))
        return _Result(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _WriteDriver:
    def __init__(self, rows):
        self._session = _WriteSession(rows)

    def session(self):
        return self._session


class _FakeQdrant:
    def __init__(self, fail=False):
        self.patches = []
        self._fail = fail

    async def set_payload(self, collection_name=None, payload=None, points=None):
        if self._fail:
            raise RuntimeError("qdrant is down")
        self.patches.append({"collection": collection_name, "payload": payload,
                             "points": list(points or [])})


class _MetaRecorder:
    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, message, *a, **k):
        self.warnings.append(str(message))

    def debug(self, message, *a, **k):
        self.debugs.append(str(message))

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Result(list):
    def single(self):
        return self[0] if self else None


class _WriteSession:
    """Records every statement; returns `rows` from the read."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def run(self, query, **params):
        self.statements.append((query, params))
        return _Result(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _WriteDriver:
    def __init__(self, rows):
        self._session = _WriteSession(rows)

    def session(self):
        return self._session


class _FakeQdrant:
    def __init__(self, fail=False):
        self.patches = []
        self._fail = fail

    async def set_payload(self, collection_name=None, payload=None, points=None):
        if self._fail:
            raise RuntimeError("qdrant is down")
        self.patches.append({"collection": collection_name, "payload": payload,
                             "points": list(points or [])})


class _MetaRecorder:
    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, message, *a, **k):
        self.warnings.append(str(message))

    def debug(self, message, *a, **k):
        self.debugs.append(str(message))

    def __getattr__(self, _name):
        return lambda *a, **k: None


class DiaryMetadataUpdateTests(unittest.TestCase):
    """Metadata has to be changeable without resending the body.

    ``db_save_diary`` writes ``SET d.content = $content`` unconditionally, so
    there is no way to attach a filename to an entry whose body the caller
    cannot afford to read back, embed and re-keyword. Sending an empty body is
    not a no-op there; it is the loss. This is that other path, and the
    properties worth pinning are the ones a caller cannot see: the merge, the
    absence of an embed, and the chunk family.
    """

    EXISTING = {"metadata": '{"original_file": "old.md", "keywords": "epam, sap"}',
                "timestamp": "2026-05-15T14:30:00"}

    def _run(self, rows=None, qdrant=None, fail_qdrant=False):
        rows = self.EXISTING if rows is None else rows
        driver = _WriteDriver([rows] if rows is not False else [])
        qdrant = qdrant if qdrant is not None else _FakeQdrant(fail=fail_qdrant)
        published = []
        recorder = _MetaRecorder()

        async def get_qdrant():
            return qdrant

        async def publish(user_id, kind, payload):
            published.append((user_id, kind, payload))

        async def fake_targets(qdrant_arg, record_id, collection):
            return ["e1", "e1-chunk1", "e1-chunk2"]

        stub = type(sys)("client_manager")
        stub._scope_targets = fake_targets
        saved = sys.modules.get("client_manager")
        sys.modules["client_manager"] = stub
        self.addCleanup(lambda: sys.modules.__setitem__("client_manager", saved)
                        if saved is not None
                        else sys.modules.pop("client_manager", None))

        fn = _lift("db_update_diary_metadata",
                   get_neo4j=lambda: driver,
                   get_qdrant=get_qdrant,
                   publish_db_event=publish,
                   DIARY_COLLECTION="ea_diary",
                   json=json,
                   logger=recorder,
                   Optional=typing.Optional)
        result = asyncio.run(fn("e1", "alice", {"original_file": "new.md"}))
        return result, driver._session, qdrant, recorder, published

    def test_the_new_value_wins_and_the_other_keys_survive(self):
        result, _, _, _, _ = self._run()
        self.assertEqual(result["original_file"], "new.md")
        self.assertEqual(result["keywords"], "epam, sap",
                         "sending one field must add it, not replace the rest — "
                         "a caller that cannot see the current metadata could "
                         "only assume a replace")

    def test_the_body_is_never_written(self):
        _, session, _, _, _ = self._run()
        writes = [q for q, _ in session.statements if " SET " in q.upper()]
        self.assertEqual(len(writes), 1, "precondition: one write was issued")
        for query in writes:
            self.assertNotIn("d.content", query,
                             "this path exists precisely so the body is left "
                             "alone; a SET on it would be the bug it replaces")
            self.assertNotIn("d.name", query)
            self.assertNotIn("d.timestamp", query)

    def test_no_embedding_and_no_llm_call_are_reachable(self):
        # The reason to have this path at all. Both are awaited inside
        # db_save_diary/db_update_diary; a re-embed of a 40k entry is the cost
        # this avoids, and a keyword regeneration is an LLM call to arrive at
        # the same string.
        _source, _tree, node, segment = _segment("db_update_diary_metadata")
        for name in ("get_embedding", "extract_diary_keywords", "get_llm_response",
                     "_upsert_diary_points", "_auto_link_people", "replace=True"):
            self.assertNotIn(name, segment,
                             f"{name} must not appear on the metadata-only "
                             "path; the entry's text is not changing")

    def test_the_whole_chunk_family_is_patched_not_one_point(self):
        _, _, qdrant, _, _ = self._run()
        self.assertEqual(len(qdrant.patches), 1)
        patch = qdrant.patches[0]
        self.assertEqual(patch["collection"], "ea_diary")
        # Chunk 0 keeps the record id, so addressing one point by it looks right
        # until the entry is long enough to chunk — then the other chunks keep
        # the previous metadata.
        self.assertEqual(patch["points"], ["e1", "e1-chunk1", "e1-chunk2"])
        self.assertEqual(patch["payload"]["metadata"]["original_file"], "new.md")

    def test_a_missing_entry_is_reported_rather_than_created(self):
        result, session, _, _, _ = self._run(rows=False)
        self.assertIsNone(result)
        self.assertEqual(len(session.statements), 1,
                         "no write may be issued for an entry that is not there")

    def test_a_patch_failure_does_not_fail_the_write(self):
        # Neo4j holds the canonical value and a stale payload is recoverable by
        # any later reconcile pass; the reverse order would lose the edit.
        result, session, _, recorder, _ = self._run(fail_qdrant=True)
        self.assertEqual(result["original_file"], "new.md")
        self.assertEqual(len(session.statements), 2, "the Neo4j write still ran")
        self.assertTrue(recorder.warnings, "the failure must be logged, not silent")

    def test_the_change_is_published(self):
        _, _, _, _, published = self._run()
        self.assertEqual(len(published), 1)
        user_id, kind, payload = published[0]
        self.assertEqual((user_id, kind), ("alice", "diary_changed"))
        self.assertEqual(payload["id"], "e1")
        self.assertEqual(payload["date"], "2026-05-15")

    def test_metadata_stored_as_a_map_is_merged_too(self):
        result, _, _, _, _ = self._run(
            rows={"metadata": {"original_file": "old.md", "tags": "x"},
                  "timestamp": "2026-05-15T14:30:00"})
        self.assertEqual(result["original_file"], "new.md")
        self.assertEqual(result["tags"], "x")

    def test_a_non_dict_is_refused(self):
        with self.assertRaises(ValueError):
            asyncio.run(self._lifted()("e1", "alice", "not-a-dict"))

    def _lifted(self):
        driver = _WriteDriver([self.EXISTING])

        async def get_qdrant():
            return None

        async def publish(*a, **k):
            return None

        return _lift("db_update_diary_metadata",
                     get_neo4j=lambda: driver,
                     get_qdrant=get_qdrant,
                     publish_db_event=publish,
                     DIARY_COLLECTION="ea_diary",
                     json=json,
                     logger=_MetaRecorder(),
                     Optional=typing.Optional)


class DiarySaveEntryRoutingTests(unittest.TestCase):
    """An empty body must not become an empty entry.

    `diary_save_entry` routes on one condition — an `entryId` with a blank
    `content` means "metadata only" — and both mistakes available there are
    destructive. Routing that condition the other way runs `db_save_diary`,
    whose `SET d.content = $content` replaces the body: the caller asked to
    attach a filename and destroyed a 40k transcription, with nothing in the
    return value to say so. And an empty body with no `entryId` must be
    refused rather than stored, since it is indistinguishable from a mistake.
    """

    def _lift(self, **stubs):
        recorded = {"saved": [], "metadata": [], "deleted": []}

        async def db_update_diary_metadata(entry_id, user, metadata):
            recorded["metadata"].append((entry_id, user, metadata))
            return {**metadata, "keywords": "epam"}

        async def db_delete_diary(entry_id, user):
            recorded["deleted"].append((entry_id, user))

        async def db_save_diary(content, user, timestamp, name, **kw):
            recorded["saved"].append({"content": content, "user": user,
                                      "timestamp": timestamp, "name": name, **kw})
            return timestamp

        async def db_resolve_client(name, user):
            return {"id": "c1"}

        async def db_resolve_context(name, client_id, user):
            return {"id": "x1"}

        async def db_create_client(name, user):
            return "c-new"

        async def db_create_context(name, client_id, user):
            return "x-new"

        mem = type(sys)("mem")
        mem._current_user_value = "alice"
        mem._diary_id = lambda user, ts: f"id-{ts}"
        mem.db_update_diary_metadata = db_update_diary_metadata
        mem.db_delete_diary = db_delete_diary
        mem.db_save_diary = db_save_diary
        mem.db_resolve_client = db_resolve_client
        mem.db_resolve_context = db_resolve_context
        mem.db_create_client = db_create_client
        mem.db_create_context = db_create_context
        for name, value in stubs.items():
            setattr(mem, name, value)

        fn = _lift_tool("diary_save_entry", {
            "mem": mem,
            "_current_user": lambda: "alice",
            "Optional": typing.Optional,
        })
        return fn, recorded

    def test_an_empty_body_with_an_id_updates_metadata_alone(self):
        fn, recorded = self._lift()
        result = asyncio.run(fn(content="", entryId="e1",
                                metadata={"original_file": "meeting.md"}))
        self.assertEqual(recorded["metadata"], [("e1", "alice",
                                                 {"original_file": "meeting.md"})])
        self.assertEqual(recorded["saved"], [],
                         "db_save_diary SETs d.content unconditionally; taking "
                         "that path here would wipe the entry's body")
        self.assertEqual(recorded["deleted"], [],
                         "the old entry must not be deleted either — this is "
                         "an update, not a move")
        self.assertTrue(result["metadataOnly"])
        self.assertEqual(result["id"], "e1")

    def test_the_merged_metadata_comes_back_so_the_caller_can_see_it(self):
        fn, _ = self._lift()
        result = asyncio.run(fn(content="", entryId="e1",
                                metadata={"original_file": "meeting.md"}))
        self.assertEqual(result["metadata"]["keywords"], "epam",
                         "the merged value is returned; without it the caller "
                         "cannot tell what the entry now carries")

    def test_whitespace_counts_as_empty(self):
        fn, recorded = self._lift()
        asyncio.run(fn(content="   \n\t ", entryId="e1", metadata={"a": 1}))
        self.assertEqual(len(recorded["metadata"]), 1)
        self.assertEqual(recorded["saved"], [])

    def test_an_empty_body_with_no_id_is_refused(self):
        fn, recorded = self._lift()
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(fn(content="", name="X", timestamp="2026-05-15T14:30:00",
                          metadata={"a": 1}))
        self.assertIn("entryId", str(ctx.exception),
                      "the message has to name the way out, not just the refusal")
        self.assertEqual(recorded["saved"], [])

    def test_an_empty_body_and_an_id_but_no_metadata_is_refused(self):
        # Otherwise the condition silently means "do nothing" and returns
        # success, which reads as an update that happened.
        fn, recorded = self._lift()
        with self.assertRaises(ValueError):
            asyncio.run(fn(content="", entryId="e1"))
        self.assertEqual(recorded["metadata"], [])
        self.assertEqual(recorded["saved"], [])

    def test_an_unknown_id_is_reported_rather_than_created(self):
        async def missing(entry_id, user, metadata):
            return None

        fn, recorded = self._lift(db_update_diary_metadata=missing)
        with self.assertRaises(ValueError):
            asyncio.run(fn(content="", entryId="nope", metadata={"a": 1}))
        self.assertEqual(recorded["saved"], [])

    def test_a_real_body_still_takes_the_save_path(self):
        fn, recorded = self._lift()
        result = asyncio.run(fn(content="the body", name="Handover",
                                timestamp="2026-05-15T14:30:00"))
        self.assertEqual(len(recorded["saved"]), 1)
        self.assertEqual(recorded["saved"][0]["content"], "the body")
        self.assertEqual(recorded["metadata"], [])
        self.assertFalse(result.get("metadataOnly"))
        self.assertEqual(result["timestamp"], "2026-05-15T14:30:00")

    def test_a_new_entry_still_requires_a_name_and_a_timestamp(self):
        fn, recorded = self._lift()
        for label, kwargs in (("no name", {"timestamp": "2026-05-15T14:30:00"}),
                              ("no timestamp", {"name": "X"}),
                              ("neither", {})):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    asyncio.run(fn(content="body", **kwargs))
        self.assertEqual(recorded["saved"], [])

    def test_metadata_alongside_a_real_body_is_still_written(self):
        fn, recorded = self._lift()
        asyncio.run(fn(content="body", name="X", timestamp="2026-05-15T14:30:00",
                       metadata={"original_file": "a.md"}))
        self.assertEqual(recorded["saved"][0]["metadata"],
                         {"original_file": "a.md"})
        self.assertEqual(recorded["metadata"], [],
                         "a non-empty body is a save; db_save_diary writes the "
                         "metadata itself")

if __name__ == "__main__":
    unittest.main()
