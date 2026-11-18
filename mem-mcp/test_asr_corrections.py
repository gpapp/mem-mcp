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
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            # One namespace for every requested name: `_correction_dict` is a
            # global to `db_create_correction`, not an argument to it.
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


if __name__ == "__main__":
    unittest.main()
