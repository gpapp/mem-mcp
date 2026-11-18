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


if __name__ == "__main__":
    unittest.main()
