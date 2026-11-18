"""Tests for the ASR mishear-correction engine.

``asr_corrections.py`` is stdlib-only, so this suite imports and **calls** it --
no lifting, no fake driver. That is the point of the module: the rules decide
what gets written into somebody's vault, so their behaviour has to be directly
observable rather than inferred from the shape of the code.

The defect class being guarded is silent rewriting. Every rule here fails in
the same direction -- a real mishear is missed, or a word the rule was never
about is eaten, or the pass keeps going until the text says something nobody
said. None of those raises, and none of them is visible without calling the
engine.
"""

import ast
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from asr_corrections import (
    CorrectionRuleError,
    apply_corrections,
    compile_rules,
    find_corrections,
    normalise_rule,
)


class NormaliseRuleTests(unittest.TestCase):
    def test_whitespace_is_stripped_and_collapsed(self):
        self.assertEqual(
            normalise_rule("  Deutche   Bank ", "Deutsche Bank"),
            ("Deutche Bank", "Deutsche Bank"),
        )

    def test_an_empty_side_is_refused(self):
        with self.assertRaises(CorrectionRuleError):
            normalise_rule("", "OneDP")
        with self.assertRaises(CorrectionRuleError):
            normalise_rule("1dp", "   ")

    def test_a_rule_whose_right_side_contains_its_wrong_side_is_refused(self):
        with self.assertRaises(CorrectionRuleError):
            normalise_rule("Bank", "Deutsche Bank")

    def test_a_substring_that_is_not_a_word_is_not_a_loop(self):
        # "TO" appears inside "Toronto" but not as a word, so this is a real
        # correction. A plain substring test would refuse it.
        self.assertEqual(normalise_rule("TO", "Toronto"), ("TO", "Toronto"))


class MatchingTests(unittest.TestCase):
    def test_a_rule_matches_regardless_of_case(self):
        text, changes = apply_corrections(
            "deutche bank", [("Deutche Bank", "Deutsche Bank")]
        )
        self.assertEqual(text, "Deutsche Bank")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["found"], "deutche bank")

    def test_boundaries_stop_a_short_rule_eating_a_longer_word(self):
        cases = [
            ("timeout", [("Tim", "Timothy")]),
            ("STATION", [("TIO", "TO")]),
            ("Towards", [("TO", "Toronto")]),
        ]
        for text, rules in cases:
            with self.subTest(text=text):
                self.assertEqual(apply_corrections(text, rules)[0], text)
        # ...and the standalone word still matches.
        self.assertEqual(apply_corrections("a TIO b", [("TIO", "TO")])[0], "a TO b")

    def test_the_longest_rule_wins_at_the_same_position(self):
        # If the short rule ran first the long one could never fire and the
        # result would be "Southern TO".
        text, _ = apply_corrections(
            "Southern TIO", [("TIO", "TO"), ("Southern TIO", "TO")]
        )
        self.assertEqual(text, "TO")

    def test_one_pass_does_not_overlap_a_consumed_span(self):
        # "New York" is consumed whole by the longer rule; the shorter "York"
        # rule must not then fire inside a span that was already replaced.
        # Word boundaries make two *adjacent* word matches impossible, so the
        # overlap this guards is between rules, not characters.
        text, changes = apply_corrections(
            "New York", [("York", "City"), ("New York", "NYC")]
        )
        self.assertEqual(text, "NYC")
        self.assertEqual(len(changes), 1)

    def test_replacements_do_not_chain(self):
        # "a" becomes "b"; the new "b" is never re-examined.
        text, _ = apply_corrections("a", [("a", "b"), ("b", "c")])
        self.assertEqual(text, "b")

    def test_whitespace_inside_a_phrase_is_flexible(self):
        self.assertEqual(
            apply_corrections("Deutche\n  Bank", [("Deutche Bank", "Deutsche Bank")])[
                0
            ],
            "Deutsche Bank",
        )

    def test_empty_rules_change_nothing(self):
        text = "leave me alone"
        self.assertEqual(apply_corrections(text, []), (text, []))

    def test_every_occurrence_is_reported_with_its_position(self):
        text, changes = apply_corrections("1dp and 1dp", [("1dp", "OneDP")])
        self.assertEqual(text, "OneDP and OneDP")
        self.assertEqual([c["start"] for c in changes], [0, 8])
        self.assertEqual([c["found"] for c in changes], ["1dp", "1dp"])

    def test_the_stored_right_side_is_emitted_verbatim(self):
        # The rule carries the canonical spelling; there is no case guessing.
        self.assertEqual(
            apply_corrections("deutche bank", [("Deutche Bank", "Deutsche Bank")])[0],
            "Deutsche Bank",
        )

    def test_row_dicts_are_accepted(self):
        rows = [{"wrong": "NJX", "right": "EngX"}]
        self.assertEqual(
            apply_corrections("the NJX platform", rows)[0], "the EngX platform"
        )

    def test_a_duplicate_wrong_keeps_the_first_rule(self):
        rules = [{"wrong": "NJX", "right": "EngX"}, {"wrong": "NJX", "right": "Other"}]
        self.assertEqual(apply_corrections("NJX", rules)[0], "EngX")


class FindCorrectionsTests(unittest.TestCase):
    RULES = [("Deutche Bank", "Deutsche Bank")]

    def test_it_counts_matches_without_changing_the_text(self):
        text = "Deutche Bank and Deutche Bank"
        found = find_corrections(text, self.RULES)
        self.assertEqual(len(found), 2)
        self.assertEqual(text, "Deutche Bank and Deutche Bank")

    def test_no_match_is_an_empty_list(self):
        self.assertEqual(find_corrections("nothing here", self.RULES), [])

    def test_apply_agrees_with_find(self):
        rules = [("1dp", "OneDP")]
        text = "1dp is 1dp"
        self.assertEqual(
            len(find_corrections(text, rules)), len(apply_corrections(text, rules)[1])
        )


class CompileRulesTests(unittest.TestCase):
    def test_rules_are_ordered_longest_first(self):
        compiled = compile_rules([("TIO", "TO"), ("Southern TIO", "TO")])
        self.assertEqual([rule[0] for rule in compiled], ["Southern TIO", "TIO"])

    def test_ordering_does_not_depend_on_the_order_handed_in(self):
        forward = compile_rules([("TIO", "TO"), ("Southern TIO", "TO")])
        backward = compile_rules([("Southern TIO", "TO"), ("TIO", "TO")])
        self.assertEqual(
            [(rule[0], rule[1]) for rule in forward],
            [(rule[0], rule[1]) for rule in backward],
        )

    def test_a_compiled_list_can_be_reused_by_both_entry_points(self):
        compiled = compile_rules([("NJX", "EngX")])
        self.assertEqual(apply_corrections("NJX", compiled)[0], "EngX")
        self.assertEqual(find_corrections("NJX", compiled)[0]["right"], "EngX")

    def test_an_invalid_rule_is_refused_at_compile_time(self):
        with self.assertRaises(CorrectionRuleError):
            compile_rules([("Bank", "Deutsche Bank")])


# ---------------------------------------------------------------------------
# Storage -- lifted out of live_manager.py
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows=None, single=None):
        self._rows = list(rows or [])
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


def _lift_manager(*names, **extra_globals):
    """Exec named functions out of ``live_manager.py`` with stub imports.

    The same constraint `test_live_notes.py` documents for its own lifts:
    ``live_manager`` imports ``common``, which imports httpx, so it cannot be
    imported on a machine with no web dependencies.
    """
    import ast
    import types

    from asr_corrections import normalise_rule

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_manager.py")
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    wanted = set(names)
    found = {}
    namespace = {
        "__builtins__": __builtins__,
        "uuid": types.SimpleNamespace(uuid4=lambda: "corr-1"),
        "Optional": object,
        "normalise_rule": normalise_rule,
        "_stamp": lambda value: None if value is None else str(value),
    }
    namespace.update(extra_globals)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            # One namespace for every requested name: `_correction_dict` is a
            # global to `db_create_correction`, not an argument to it. Async
            # functions are matched too — `db_apply_corrections_to_client` is
            # one, and a lift that silently skips it says "not found" about a
            # function that is right there.
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, path, "exec"), namespace)
            found[node.name] = namespace[node.name]
    missing = wanted - set(found)
    if missing:
        raise AssertionError(f"not found in live_manager.py: {sorted(missing)}")
    return found


def _known_client(client_id, user_id, name="Deutsche Bank (DB)"):
    return {"id": client_id, "name": name} if client_id else None


class CorrectionStorageTests(unittest.TestCase):
    """The CRUD half, driven against a fake driver.

    What is worth guarding here is the refusals, because every one of them is a
    query plus a branch and none is visible in the shape of the code: an
    unknown client, a duplicate trigger, an update that names nothing, and a
    delete scoped to the caller.
    """

    def _lift(self, *names, driver, client=_known_client):
        return _lift_manager(
            "_correction_dict", *names,
            _require_driver=lambda: driver,
            _resolve_client_by_id=client,
        )

    def test_create_stores_the_normalised_pair(self):
        def handler(query, params):
            if "RETURN c.wrong AS wrong" in query:
                return _FakeResult(rows=[])
            if "CREATE (c:TermCorrection" in query:
                node = {key: params[key] for key in
                        ("id", "userId", "clientId", "clientName",
                         "wrong", "right")}
                return _FakeResult(single={"c": node})
            return _FakeResult()

        driver = _FakeDriver(handler)
        funcs = self._lift("db_create_correction", driver=driver)
        stored = funcs["db_create_correction"](
            "alice", "c1", "  Deutche   Bank ", "Deutsche Bank")
        self.assertEqual(stored["wrong"], "Deutche Bank")
        self.assertEqual(stored["right"], "Deutsche Bank")
        self.assertEqual(stored["clientName"], "Deutsche Bank (DB)")
        self.assertEqual(stored["id"], "corr-1")
        self.assertIn(":TermCorrection", driver.queries[-1])

    def test_a_duplicate_trigger_is_refused_case_insensitively(self):
        def handler(query, params):
            if "RETURN c.wrong AS wrong" in query:
                return _FakeResult(rows=[{"wrong": "Deutche Bank"}])
            return _FakeResult()

        driver = _FakeDriver(handler)
        funcs = self._lift("db_create_correction", driver=driver)
        with self.assertRaisesRegex(ValueError, "already exists"):
            funcs["db_create_correction"](
                "alice", "c1", "deutche bank", "Deutsche Bank")
        self.assertFalse(
            any("CREATE (c:TermCorrection" in q for q in driver.queries))

    def test_an_unknown_client_is_refused_before_any_write(self):
        driver = _FakeDriver(lambda query, params: _FakeResult())
        funcs = self._lift("db_create_correction", driver=driver,
                           client=lambda client_id, user_id: None)
        with self.assertRaisesRegex(ValueError, "Unknown client 'c-nope'."):
            funcs["db_create_correction"]("alice", "c-nope", "NJX", "EngX")
        self.assertEqual(driver.queries, [])

    def test_a_correction_must_belong_to_a_client(self):
        driver = _FakeDriver(lambda query, params: _FakeResult())
        funcs = self._lift("db_create_correction", driver=driver)
        with self.assertRaisesRegex(ValueError, "belongs to a client"):
            funcs["db_create_correction"]("alice", "", "NJX", "EngX")
        self.assertEqual(driver.queries, [])

    def test_a_rule_that_would_match_its_own_output_is_refused(self):
        driver = _FakeDriver(lambda query, params: _FakeResult())
        funcs = self._lift("db_create_correction", driver=driver)
        with self.assertRaises(CorrectionRuleError):
            funcs["db_create_correction"]("alice", "c1", "Bank", "Deutsche Bank")
        self.assertFalse(
            any("CREATE (c:TermCorrection" in q for q in driver.queries))

    def test_list_returns_the_rules_for_that_client(self):
        first = {"id": "a", "userId": "alice", "clientId": "c1",
                 "clientName": "DB", "wrong": "Southern TIO", "right": "TO"}
        second = {"id": "b", "userId": "alice", "clientId": "c1",
                  "clientName": "DB", "wrong": "TIO", "right": "TO"}

        def handler(query, params):
            self.assertEqual(params["userId"], "alice")
            self.assertEqual(params["clientId"], "c1")
            return _FakeResult(rows=[{"c": first}, {"c": second}])

        driver = _FakeDriver(handler)
        funcs = self._lift("db_list_corrections", driver=driver)
        rules = funcs["db_list_corrections"]("alice", "c1")
        self.assertEqual([rule["id"] for rule in rules], ["a", "b"])
        self.assertIn("$userId", driver.queries[0])
        self.assertIn("$clientId", driver.queries[0])

    def test_list_refuses_an_unknown_or_missing_client(self):
        driver = _FakeDriver(lambda query, params: _FakeResult())
        funcs = self._lift("db_list_corrections", driver=driver,
                           client=lambda client_id, user_id: None)
        with self.assertRaisesRegex(ValueError, "Unknown client 'c-nope'."):
            funcs["db_list_corrections"]("alice", "c-nope")
        with self.assertRaisesRegex(ValueError, "belongs to a client"):
            funcs["db_list_corrections"]("alice", "")
        self.assertEqual(driver.queries, [])

    def _current(self, **over):
        node = {"id": "corr-1", "userId": "alice", "clientId": "c1",
                "clientName": "DB", "wrong": "1dp", "right": "OneDP"}
        node.update(over)
        return node

    def test_update_changes_one_side_and_leaves_the_other(self):
        def handler(query, params):
            if "SET c.wrong = $wrong" in query:
                node = dict(self._current(), wrong=params["wrong"],
                            right=params["right"])
                return _FakeResult(single={"c": node})
            if "WHERE c.id <> $id" in query:
                return _FakeResult(single={"n": 0})
            return _FakeResult(single={"c": self._current()})

        driver = _FakeDriver(handler)
        funcs = self._lift("db_update_correction", driver=driver)
        stored = funcs["db_update_correction"]("corr-1", "alice", wrong="One DP")
        self.assertEqual(stored["wrong"], "One DP")
        self.assertEqual(stored["right"], "OneDP")

    def test_update_scopes_every_query_to_the_caller(self):
        seen = []

        def handler(query, params):
            seen.append(params)
            if "SET c.wrong = $wrong" in query:
                return _FakeResult(single={"c": self._current()})
            if "WHERE c.id <> $id" in query:
                return _FakeResult(single={"n": 0})
            return _FakeResult(single={"c": self._current()})

        driver = _FakeDriver(handler)
        funcs = self._lift("db_update_correction", driver=driver)
        funcs["db_update_correction"]("corr-1", "alice", right="One DP")
        self.assertTrue(seen)
        for params in seen:
            self.assertEqual(params["userId"], "alice")

    def test_update_refuses_a_trigger_another_rule_already_owns(self):
        def handler(query, params):
            if "WHERE c.id <> $id" in query:
                return _FakeResult(single={"n": 1})
            return _FakeResult(single={"c": self._current()})

        driver = _FakeDriver(handler)
        funcs = self._lift("db_update_correction", driver=driver)
        with self.assertRaisesRegex(ValueError, "already exists"):
            funcs["db_update_correction"]("corr-1", "alice", wrong="TIO")
        self.assertFalse(any("SET c.wrong" in q for q in driver.queries))

    def test_updating_someone_elses_rule_answers_none(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(single=None))
        funcs = self._lift("db_update_correction", driver=driver)
        self.assertIsNone(funcs["db_update_correction"]("corr-x", "alice",
                                                       wrong="TIO"))
        self.assertEqual(len(driver.queries), 1)

    def test_naming_neither_side_is_refused(self):
        driver = _FakeDriver(lambda query, params: _FakeResult())
        funcs = self._lift("db_update_correction", driver=driver)
        with self.assertRaisesRegex(ValueError, "Nothing to update"):
            funcs["db_update_correction"]("corr-1", "alice")
        self.assertEqual(driver.queries, [])

    def test_delete_removes_only_the_callers_rule(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(single={"n": 1}))
        funcs = self._lift("db_delete_correction", driver=driver)
        self.assertTrue(funcs["db_delete_correction"]("corr-1", "alice"))
        query = driver.queries[0]
        self.assertIn("DELETE c", query)
        self.assertIn("$userId", query)

    def test_deleting_another_users_rule_is_false(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(single={"n": 0}))
        funcs = self._lift("db_delete_correction", driver=driver)
        self.assertFalse(funcs["db_delete_correction"]("corr-1", "bob"))


# ---------------------------------------------------------------------------
# The save-path read, and the pre-save hook that uses it
# ---------------------------------------------------------------------------

class _RecordingLogger:
    """Captures the level and text of every line, so a silent catch is visible."""

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("info", message))

    def warning(self, message):
        self.lines.append(("warning", message))

    def error(self, message):
        self.lines.append(("error", message))


def _diary_source():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diary_manager.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    return path, source, ast.parse(source)


def _lift_diary(*names, **extra_globals):
    """Exec named functions out of ``diary_manager.py`` with stub imports.

    Same constraint as ``_lift_manager``: ``diary_manager`` imports ``common``,
    which imports httpx, so it cannot be imported on a machine without the web
    dependencies. The namespace is shared across every requested name for the
    same reason it is over there — a sibling function is a global, not an
    argument.
    """
    path, _source, tree = _diary_source()
    wanted = set(names)
    found = {}
    namespace = {
        "__builtins__": __builtins__,
        "Optional": object,
        "apply_corrections": apply_corrections,
        "logger": _RecordingLogger(),
    }
    namespace.update(extra_globals)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, path, "exec"), namespace)
            found[node.name] = namespace[node.name]
    missing = wanted - set(found)
    if missing:
        raise AssertionError(f"not found in diary_manager.py: {sorted(missing)}")
    return found


class CorrectionRuleLoaderTests(unittest.TestCase):
    """``db_client_correction_rules`` is the save path's read, so it must not raise.

    ``db_list_corrections`` is the CRUD read and refuses an unknown client. This
    one deliberately does not, because it runs underneath a diary save: a share
    revoked in another tab, or a client deleted while the form was open, must not
    turn a working save into a refused one.
    """

    def _load(self, handler, log=None):
        driver = _FakeDriver(handler)
        funcs = _lift_manager("db_client_correction_rules",
                              _require_driver=lambda: driver,
                              logger=log or _RecordingLogger())
        return funcs["db_client_correction_rules"], driver

    def test_it_reads_the_rules_for_one_client_in_stored_order(self):
        def handler(query, params):
            self.assertIn(":TermCorrection", query)
            self.assertEqual(params, {"userId": "alice", "clientId": "c1"})
            # The ordering the engine relies on is stated in the query, so the
            # two cannot drift: longest trigger first, then alphabetical.
            self.assertIn("ORDER BY size(c.wrong) DESC", query)
            self.assertIn("toLower(c.wrong) ASC", query)
            return _FakeResult(rows=[
                {"wrong": "Deutche Bank", "right": "Deutsche Bank"},
                {"wrong": "1dp", "right": "OneDP"},
            ])

        load, _driver = self._load(handler)
        self.assertEqual(load("alice", "c1"), [
            {"wrong": "Deutche Bank", "right": "Deutsche Bank"},
            {"wrong": "1dp", "right": "OneDP"},
        ])

    def test_a_missing_client_reads_nothing(self):
        load, driver = self._load(lambda query, params: _FakeResult(rows=[]))
        self.assertEqual(load("alice", None), [])
        self.assertEqual(load("alice", ""), [])
        self.assertEqual(load("", "c1"), [])
        self.assertEqual(driver.queries, [])

    def test_an_unreachable_graph_answers_empty_rather_than_raising(self):
        def boom():
            raise RuntimeError("Neo4j not connected.")

        funcs = _lift_manager("db_client_correction_rules",
                              _require_driver=boom,
                              logger=_RecordingLogger())
        self.assertEqual(funcs["db_client_correction_rules"]("alice", "c1"), [])

    def test_a_failed_query_is_logged_and_swallowed(self):
        log = _RecordingLogger()

        def handler(query, params):
            raise RuntimeError("Neo4j went away")

        load, _driver = self._load(handler, log=log)
        self.assertEqual(load("alice", "c1"), [])
        self.assertEqual([level for level, _line in log.lines], ["warning"],
                         msg="a swallowed read must say so, or the mishear looks "
                             "like there is no rule for it")

    def test_a_half_written_rule_is_ignored_rather_than_applied(self):
        load, _driver = self._load(lambda query, params: _FakeResult(rows=[
            {"wrong": "1dp", "right": None},
            {"wrong": None, "right": "OneDP"},
            {"wrong": "NJX", "right": "EngX"},
        ]))
        self.assertEqual(load("alice", "c1"), [{"wrong": "NJX", "right": "EngX"}])


class InboundSaveHookTests(unittest.TestCase):
    """`_correct_inbound_text` is the pre-save hook the whole feature exists for."""

    def _hook(self, rules, log=None):
        calls = []

        def loader(user_id, client_id):
            calls.append((user_id, client_id))
            return rules

        funcs = _lift_diary("_correct_inbound_text",
                            db_client_correction_rules=loader,
                            logger=log or _RecordingLogger())
        return funcs["_correct_inbound_text"], calls

    def test_the_body_and_the_title_are_both_corrected(self):
        hook, _calls = self._hook([{"wrong": "Deutche Bank", "right": "Deutsche Bank"}])
        content, name, changes = hook(
            "Met Deutche Bank about the estate.",
            "Deutche Bank - architecture review",
            "alice", "c1",
        )
        self.assertEqual(content, "Met Deutsche Bank about the estate.")
        self.assertEqual(name, "Deutsche Bank - architecture review")
        self.assertEqual(len(changes), 2,
                         msg="the mishear is in the title as often as in the body")

    def test_the_example_mishears_from_the_report_are_applied(self):
        hook, _calls = self._hook([
            {"wrong": "Deutche Bank", "right": "Deutsche Bank"},
            {"wrong": "1dp", "right": "OneDP"},
            {"wrong": "NJX", "right": "EngX"},
        ])
        content, _name, _changes = hook(
            "Deutche Bank, the 1dp programme and NJX", "", "alice", "c1")
        self.assertEqual(content, "Deutsche Bank, the OneDP programme and EngX")

    def test_no_client_means_no_read_and_no_change(self):
        hook, calls = self._hook([{"wrong": "1dp", "right": "OneDP"}])
        self.assertEqual(hook("the 1dp team", "t", "alice", None),
                         ("the 1dp team", "t", []))
        self.assertEqual(calls, [], msg="an unscoped entry has no dictionary to consult")

    def test_no_rules_leaves_the_text_untouched(self):
        hook, calls = self._hook([])
        self.assertEqual(hook("the 1dp team", "t", "alice", "c1"),
                         ("the 1dp team", "t", []))
        self.assertEqual(calls, [("alice", "c1")],
                         msg="the rules are read for the client the entry is filed under")

    def test_a_missing_title_stays_missing(self):
        hook, _calls = self._hook([{"wrong": "1dp", "right": "OneDP"}])
        _content, name, _changes = hook("1dp", None, "alice", "c1")
        self.assertIsNone(name,
                          msg="a None title must not become an empty string on the way to Neo4j")

    def test_a_counted_change_is_logged(self):
        log = _RecordingLogger()
        hook, _calls = self._hook([{"wrong": "1dp", "right": "OneDP"}], log=log)
        hook("1dp", "", "alice", "c1")
        self.assertEqual([level for level, _line in log.lines], ["info"])


class InboundSaveCallSiteTests(unittest.TestCase):
    """A hook tested in isolation is not a test of its call site.

    `test_action_items` documents the incident this pins against: the helper was
    added and the call was never wired, and every helper test stayed green.
    """

    def _body(self, name):
        _path, source, tree = _diary_source()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(source, node)
        self.fail(f"not found in diary_manager.py: {name}")

    def test_both_write_paths_apply_the_corrections(self):
        for name in ("db_save_diary", "db_update_diary"):
            body = self._body(name)
            self.assertTrue("_correct_inbound_text(" in body,
                            msg=f"{name} no longer applies the client's corrections")

    def test_the_correction_runs_before_anything_derives_from_the_text(self):
        # The ordering is the property, not the call: keywords, participants,
        # commitments, decisions and the embedded vector all read this text, so
        # correcting afterwards stores the right words beside an index of the
        # wrong ones.
        for name in ("db_save_diary", "db_update_diary"):
            body = self._body(name)
            self.assertLess(
                body.index("_correct_inbound_text("),
                body.index("extract_diary_signals("),
                msg=f"{name} corrects after the extraction, so the keywords "
                    f"describe the mishear",
            )

    def test_the_save_path_uses_the_client_it_was_given(self):
        body = self._body("db_save_diary")
        self.assertIn("_correct_inbound_text(content, name, user_id, client_id)", body)

    def test_the_edit_path_uses_the_client_the_entry_is_filed_under(self):
        # An edit that does not repeat client_id still belongs to its client.
        # Reading only the argument would skip the correction for every edit
        # made from the entry view, which is where an inbound file is reviewed.
        body = self._body("db_update_diary")
        self.assertIn('scope.get("clientId")', body,
                      msg="db_update_diary must correct against the entry's own "
                          "client, not only the one the caller resent")


class RetroactiveCorrectionTests(unittest.TestCase):
    """The dry run and the sweep, driven against a fake graph.

    The failure this class exists for is a preview that writes. The button that
    shows the count and the button that rewrites the vault are one route apart,
    and a "dry run" that patched just the one row is the kind of defect nobody
    notices until the text has already changed.
    """

    RULES = [{"wrong": "1dp", "right": "OneDP"}]

    def _lift(self, segments, entries, writes=None, client=_known_client, log=None):
        def handler(query, params):
            if ":TermCorrection" in query:
                return _FakeResult(rows=list(self.RULES))
            if "HAS_SEGMENT" in query:
                return _FakeResult(rows=list(segments))
            if "FOR_CLIENT" in query:
                return _FakeResult(rows=list(entries))
            if "SET s.text" in query:
                if writes is not None:
                    writes.append((params["id"], params["text"]))
                return _FakeResult()
            raise AssertionError(f"unexpected query: {query}")

        funcs = _lift_manager(
            "db_client_correction_rules", "_correction_candidates",
            "_summarise_changes", "_correction_target",
            "db_preview_corrections", "db_apply_corrections_to_client",
            _require_driver=lambda: _FakeDriver(handler),
            _resolve_client_by_id=client,
            apply_corrections=apply_corrections,
            CORRECTION_SAMPLE_LIMIT=10,
            logger=log or _RecordingLogger(),
        )
        return funcs

    def test_the_dry_run_counts_everything_and_writes_nothing(self):
        writes = []
        funcs = self._lift(
            [{"id": "s1", "text": "the 1dp team"}, {"id": "s2", "text": "nothing here"}],
            [{"id": "d1", "content": "1dp update"}],
            writes,
        )
        report = funcs["db_preview_corrections"]("alice", "c1")

        self.assertEqual(report["ruleCount"], 1)
        self.assertEqual(report["clientName"], "Deutsche Bank (DB)")
        self.assertEqual(report["segments"]["scanned"], 2)
        self.assertEqual(report["segments"]["changed"], 1)
        self.assertEqual(report["segments"]["changes"], 1)
        self.assertEqual(report["segments"]["samples"],
                         [{"id": "s1", "wrong": "1dp", "right": "OneDP"}])
        self.assertEqual(report["entries"]["changed"], 1)
        self.assertEqual(writes, [],
                         msg="the dry run must not patch a segment")

    def test_the_dry_run_issues_no_write_statement(self):
        issued = []

        def handler(query, params):
            issued.append(query)
            if ":TermCorrection" in query:
                return _FakeResult(rows=list(self.RULES))
            if "HAS_SEGMENT" in query:
                return _FakeResult(rows=[{"id": "s1", "text": "1dp"}])
            if "FOR_CLIENT" in query:
                return _FakeResult(rows=[{"id": "d1", "content": "1dp"}])
            raise AssertionError(f"unexpected query: {query}")

        funcs = _lift_manager(
            "db_client_correction_rules", "_correction_candidates",
            "_summarise_changes", "_correction_target", "db_preview_corrections",
            _require_driver=lambda: _FakeDriver(handler),
            _resolve_client_by_id=_known_client,
            apply_corrections=apply_corrections,
            CORRECTION_SAMPLE_LIMIT=10,
            logger=_RecordingLogger(),
        )
        funcs["db_preview_corrections"]("alice", "c1")
        for query in issued:
            for verb in (" SET ", "CREATE ", "DELETE "):
                self.assertFalse(verb in query,
                                 msg=f"the dry run issued a write: {query}")

    def test_an_unknown_client_is_refused_before_anything_is_read(self):
        funcs = self._lift([], [], client=lambda *a, **k: None)
        with self.assertRaises(ValueError) as caught:
            funcs["db_preview_corrections"]("alice", "nope")
        self.assertIn("Unknown client", str(caught.exception))

    def test_the_sweep_corrects_segments_and_entries(self):
        writes, calls = [], []

        async def fake_update(entry_id, user_id, content=None):
            calls.append((entry_id, user_id, content))
            return True

        funcs = self._lift([{"id": "s1", "text": "the 1dp team"}],
                           [{"id": "d1", "content": "1dp update"}], writes)
        result = asyncio.run(funcs["db_apply_corrections_to_client"](
            "alice", "c1", update_diary=fake_update))

        self.assertEqual(writes, [("s1", "the OneDP team")])
        self.assertEqual(calls, [("d1", "alice", "OneDP update")],
                         msg="a diary entry must go through the ordinary update, "
                             "so its vector and keywords are rebuilt")
        self.assertEqual(result["segments"]["changed"], 1)
        self.assertEqual(result["entries"]["changed"], 1)

    def test_a_failed_row_is_counted_and_the_run_continues(self):
        writes = []

        def handler(query, params):
            if ":TermCorrection" in query:
                return _FakeResult(rows=list(self.RULES))
            if "HAS_SEGMENT" in query:
                return _FakeResult(rows=[{"id": "s1", "text": "1dp"},
                                         {"id": "s2", "text": "1dp"}])
            if "FOR_CLIENT" in query:
                return _FakeResult(rows=[])
            if "SET s.text" in query:
                if params["id"] == "s1":
                    raise RuntimeError("graph went away")
                writes.append((params["id"], params["text"]))
                return _FakeResult()
            raise AssertionError(f"unexpected query: {query}")

        async def fake_update(entry_id, user_id, content=None):
            return True

        funcs = _lift_manager(
            "db_client_correction_rules", "_correction_candidates",
            "_summarise_changes", "_correction_target",
            "db_apply_corrections_to_client", "db_preview_corrections",
            _require_driver=lambda: _FakeDriver(handler),
            _resolve_client_by_id=_known_client,
            apply_corrections=apply_corrections,
            CORRECTION_SAMPLE_LIMIT=10,
            logger=_RecordingLogger(),
        )
        result = asyncio.run(funcs["db_apply_corrections_to_client"](
            "alice", "c1", update_diary=fake_update))

        self.assertEqual(result["segments"]["errors"], 1)
        self.assertEqual(result["segments"]["changed"], 1)
        self.assertEqual(writes, [("s2", "OneDP")],
                         msg="one bad row must not abandon the rest")

    def test_no_rules_reads_no_text_and_writes_nothing(self):
        issued = []

        def handler(query, params):
            issued.append(query)
            return _FakeResult(rows=[])

        async def fake_update(entry_id, user_id, content=None):
            self.fail("no rule exists, so no entry should be rewritten")

        funcs = _lift_manager(
            "db_client_correction_rules", "_correction_candidates",
            "_summarise_changes", "_correction_target",
            "db_apply_corrections_to_client", "db_preview_corrections",
            _require_driver=lambda: _FakeDriver(handler),
            _resolve_client_by_id=_known_client,
            apply_corrections=apply_corrections,
            CORRECTION_SAMPLE_LIMIT=10,
            logger=_RecordingLogger(),
        )
        result = asyncio.run(funcs["db_apply_corrections_to_client"](
            "alice", "c1", update_diary=fake_update))

        self.assertEqual(result["ruleCount"], 0)
        self.assertEqual(result["segments"]["scanned"], 0)
        self.assertEqual(len(issued), 1, msg="only the rules read should run")

    def test_a_row_that_already_reads_correctly_is_scanned_not_changed(self):
        funcs = self._lift([{"id": "s1", "text": "the OneDP team"}], [])
        report = funcs["db_preview_corrections"]("alice", "c1")
        self.assertEqual(report["segments"]["scanned"], 1)
        self.assertEqual(report["segments"]["changed"], 0)


class CorrectionSurfaceTests(unittest.TestCase):
    """The wiring, which no helper test can see: routes, the lock, the hooks, the panel."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def _gui_function(self, name):
        path = os.path.join(self.HERE, "gui.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(source, node)
        self.fail(f"not found in gui.py: {name}")

    def _template(self):
        with open(os.path.join(self.HERE, "templates", "dashboard.html"),
                  "r", encoding="utf-8") as handle:
            return handle.read()

    def test_every_route_requires_a_session(self):
        for name in ("api_list_corrections", "api_create_correction",
                     "api_preview_corrections", "api_apply_corrections",
                     "api_update_correction", "api_delete_correction"):
            body = self._gui_function(name)
            self.assertIn("_require_user(request)", body,
                          msg=f"{name} is reachable without a session")

    def test_the_apply_route_takes_the_maintenance_lock_and_releases_it(self):
        body = self._gui_function("api_apply_corrections")
        self.assertIn("claim_maintenance", body)
        self.assertIn("release_maintenance", body)
        self.assertIn("finally:", body,
                      msg="a lock released only on success is a lock held for ever "
                          "after one failed sweep")
        self.assertIn("409", body,
                      msg="a busy vault must say why rather than 500")

    def test_the_dry_run_route_does_not_take_the_lock(self):
        # It writes nothing, so holding the lock for it would refuse a preview
        # during a backup for no reason.
        body = self._gui_function("api_preview_corrections")
        self.assertNotIn("claim_maintenance", body)

    def test_the_live_socket_reads_the_rules_once_and_corrects_before_storing(self):
        body = self._gui_function("live_stream")
        self.assertEqual(body.count("db_client_correction_rules"), 1,
                         msg="a per-segment lookup would put a Neo4j round trip "
                             "in front of the microphone")
        self.assertLess(body.index("apply_corrections("),
                        body.index("queue.put_nowait(item)"),
                        msg="correcting after the queue stores the mishear")
        self.assertIn("asyncio.to_thread", body.split("db_client_correction_rules")[0][-80:],
                      msg="a blocking Neo4j read must not run on the event loop")

    def test_the_panel_is_scoped_to_the_selected_client(self):
        html = self._template()
        self.assertIn('id="live-corrections-btn"', html)
        self.assertIn("openCorrections()", html)
        self.assertIn('id="corrections-overlay"', html)
        self.assertIn("liveDetail.transcription", html,
                      msg="the panel must read the client off the open recording")

    def test_the_panel_refuses_to_edit_without_a_client(self):
        html = self._template()
        self.assertIn("correctionsClientId", html)
        self.assertIn("Choose a client on this recording first", html)

    def test_apply_is_disabled_until_a_preview_finds_something(self):
        html = self._template()
        self.assertIn('id="corrections-apply"', html)
        apply_tag = html.split('id="corrections-apply"', 1)[1][:200]
        self.assertIn("disabled", apply_tag,
                      msg="Apply must not be pressable before a dry run")
        self.assertIn("apply.disabled = (r.segments.changed + r.entries.changed) === 0", html)

    def test_the_rows_are_escaped(self):
        # A rule's text is user input and it is rendered into innerHTML.
        html = self._template()
        self.assertIn("lcEsc(r.wrong)", html)
        self.assertIn("lcEsc(r.right)", html)


if __name__ == "__main__":
    unittest.main()
