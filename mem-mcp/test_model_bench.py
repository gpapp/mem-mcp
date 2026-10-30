"""Tests for model_bench.py -- the parts that decide a verdict.

The harness's output is a *recommendation about which model to deploy*, so the
scorers have to be right in both directions: a scorer that never fires reports
every model as perfect, and one that fires too easily reports a good model as
broken. These tests call the real functions rather than reading their source.

`model_bench.py` is stdlib-only on purpose, so it imports here with no database
drivers and no web framework -- the same constraint every other suite works
under, except this one does not need `ast.get_source_segment` lifting.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import model_bench as mb


class SamplingTests(unittest.TestCase):
    def test_a_thinking_model_gets_its_vendor_sampling(self):
        """MiniCPM5's card says 1.0 / top_p 0.95 / min_p 0.0; the app's 0.0 is not its setting."""
        think = mb.sampling_for("openbmb/minicpm5-2b:latest", True)
        nothink = mb.sampling_for("openbmb/minicpm5-2b:latest", False)
        self.assertEqual(think["temperature"], 1.0)
        self.assertEqual(nothink["temperature"], 1.0)
        self.assertEqual(think["min_p"], 0.0)
        # One set on the card, so there is deliberately no think/no-think split
        # here; the families that *do* split are covered in RoleSamplingTests.
        self.assertEqual(think, nothink)

    def test_only_the_silent_card_keeps_greedy(self):
        """Granite's README states no sampling; every other card does."""
        for think in (True, False):
            self.assertEqual(mb.sampling_for("granite3.3:2b", think),
                             {"temperature": 0.0})
        for model in ("nemotron-3-nano:4b", "batiai/gemma4-e2b:q4",
                      "openbmb/minicpm5-2b:latest",
                      "hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M"):
            for think in (True, False):
                with self.subTest(model=model, think=think):
                    self.assertNotEqual(
                        mb.sampling_for(model, think), {"temperature": 0.0})

    def test_the_family_match_is_not_a_prefix_match_on_the_tag(self):
        """`minicpm` must be found in the model name, not in the harness's own URL."""
        self.assertEqual(mb.sampling_for("granite3.3:2b", True)["temperature"], 0.0)


class DegenerateTests(unittest.TestCase):
    def test_an_empty_answer_is_reported(self):
        self.assertEqual(mb.degenerate("   "), "empty")

    def test_a_single_token_run_is_reported(self):
        """Production shape: granite answered `" A A A A A A …"` to `done_reason: length`."""
        self.assertTrue(mb.degenerate(" A" * 40))

    def test_a_repeated_phrase_is_not_a_token_run(self):
        """Only a single-token run is detected, and that limit is deliberate.

        A repeated *phrase* is a decoder problem of a different shape -- the
        production sample was `"Pro Ad / Access / At S An Ac A U Ar Under Of"`,
        a low-content word salad, not a loop. It does not parse as JSON, which
        is what catches it, and the raw answer is printed in the report for the
        case it does not catch. Asserting the limitation keeps a later reader
        from believing `degenerate()` is a general-purpose quality gate.
        """
        self.assertEqual(mb.degenerate("the model said " * 10), "")
        self.assertEqual(mb.degenerate("Pro Ad / Access / At S An Ac A U Ar Under Of"), "")

    def test_normal_prose_is_not_degenerate(self):
        note = ("The inventory covers forty credit risk models. Sandra owns the "
                "workstream, and the template is due on the fourteenth. The "
                "governance draft follows, with a quarterly attestation.")
        self.assertEqual(mb.degenerate(note), "")

    def test_a_short_answer_with_one_repeated_word_is_not_degenerate(self):
        """A two-word answer is not a run; failing it would reject valid JSON."""
        self.assertEqual(mb.degenerate('{"client": null}'), "")


class RetentionTests(unittest.TestCase):
    def test_a_draft_that_keeps_everything_scores_one(self):
        source = "Two workstreams: a model inventory of 40 models, and governance. Owner: Sandra Klein."
        self.assertEqual(mb.retention(source, source), 1.0)

    def test_a_draft_that_drops_a_record_scores_low(self):
        """The property that killed granite on merge: 90.7/5.1/9.3/14.6."""
        dropped = "A model inventory of 40 models."
        self.assertLess(mb.retention(dropped, "Owner: Sandra Klein. Due on the 14th."), 0.35)

    def test_an_empty_source_does_not_divide_by_zero(self):
        self.assertEqual(mb.retention("anything", ""), 1.0)


class FilterEchoTests(unittest.TestCase):
    def test_the_filter_line_echoed_back_is_caught(self):
        """Measured at 69 of 469 rewrites, and cached for the TTL."""
        for leak in ("People category", "categories", "client scope", "context filter"):
            self.assertTrue(mb.filter_echo(leak), leak)

    def test_a_real_keyword_containing_a_filter_word_in_passing_is_kept(self):
        """A token-subset test would drop this; a stem match keeps it."""
        self.assertFalse(mb.filter_echo("deutsche bank ai adoption"))


class ResolveNameTests(unittest.TestCase):
    NAMES = ["Deutsche Bank (DB)", "SAP SE", "EPAM", "Daimler AG (MBAG)"]

    def test_an_exact_name_resolves_case_insensitively(self):
        self.assertEqual(mb.resolve_name("sap se", self.NAMES), "SAP SE")

    def test_a_declared_abbreviation_resolves(self):
        self.assertEqual(mb.resolve_name("DB", self.NAMES), "Deutsche Bank (DB)")

    def test_a_token_subset_resolves(self):
        self.assertEqual(mb.resolve_name("Deutsche Bank", self.NAMES), "Deutsche Bank (DB)")

    def test_an_unknown_name_does_not_resolve(self):
        """A hallucinated client must score as a null, not as a near match."""
        self.assertEqual(mb.resolve_name("Commerzbank", self.NAMES), "")

    def test_a_null_stays_unresolved(self):
        for value in (None, "", "null"):
            self.assertEqual(mb.resolve_name(value, self.NAMES), "")


class JsonExtractionTests(unittest.TestCase):
    def test_a_fenced_object_is_found(self):
        data, err = mb.json_object('Sure!\n```json\n{"client": "EPAM"}\n```')
        self.assertEqual(err, "")
        self.assertEqual(data["client"], "EPAM")

    def test_prose_only_is_an_error_not_a_crash(self):
        data, err = mb.json_object("I cannot help with that.")
        self.assertIsNone(data)
        self.assertIn("no JSON", err)

    def test_truncated_json_is_an_error(self):
        """The merge budget failure: the model runs out mid-JSON."""
        data, err = mb.json_object('{"title": "x", "text": "y')
        self.assertIsNone(data)


class ScopeScoringTests(unittest.TestCase):
    def _case(self, **expect):
        return {"id": "x", "expect": expect}

    def test_a_correct_null_verdict_scores_as_correct(self):
        """A null is a real answer. Scoring it as a failure would push the
        recommendation toward the model that guesses."""
        answer = '{"client": null, "context": null, "related": []}'
        score = mb.score("scope", self._case(client=None, context=None), answer)
        self.assertTrue(score["client_ok"])
        self.assertTrue(score["context_ok"])

    def test_a_confident_wrong_client_scores_as_wrong(self):
        answer = '{"client": "EPAM", "context": null, "related": []}'
        score = mb.score("scope", self._case(client=None, context=None), answer)
        self.assertFalse(score["client_ok"])

    def test_a_context_the_client_does_not_have_resolves_to_nothing(self):
        """`Enterprise Architecture` is EPAM's; pairing it with SAP must not pass."""
        answer = '{"client": "SAP SE", "context": "Enterprise Architecture", "related": []}'
        score = mb.score("scope", self._case(client="SAP SE", context=None), answer)
        self.assertTrue(score["client_ok"])
        self.assertTrue(score["context_ok"])

    def test_a_non_json_answer_is_reported_without_a_verdict(self):
        score = mb.score("scope", self._case(client="EPAM"), "EPAM")
        self.assertIn("no JSON", score["json"])
        self.assertNotIn("client_ok", score)


class PeopleScoringTests(unittest.TestCase):
    def test_the_six_participants_are_counted_when_present(self):
        names = ["Alice Smith", "Bob Jones", "Carol Meyer"]
        answer = json.dumps(names)
        score = mb.score("people", {"id": "x", "expect": {"names": names}}, answer)
        self.assertEqual(score["recall"], "3/3")
        self.assertEqual(score["missed"], [])

    def test_the_documented_empty_answer_scores_zero_recall(self):
        """phi4-mini returned [] for every window of a 6.3k entry naming six people."""
        names = ["Alice Smith", "Bob Jones", "Carol Meyer"]
        score = mb.score("people", {"id": "x", "expect": {"names": names}}, "[]")
        self.assertEqual(score["recall"], "0/3")

    def test_a_speaker_label_and_a_parenthetical_are_flagged(self):
        answer = json.dumps(["SPEAKER 1", "Frank Moreau (PM)", "Bob Jones"])
        score = mb.score("people", {"id": "x", "expect": {"names": ["Bob Jones"]}}, answer)
        self.assertEqual(score["speaker_labels"], ["SPEAKER 1"])
        self.assertEqual(score["parenthetical"], ["Frank Moreau (PM)"])


class KeywordScoringTests(unittest.TestCase):
    def test_a_keyword_absent_from_the_text_counts_as_invented(self):
        case = {"id": "x", "expect": {"source": "the ingestion pipeline dropped under load"}}
        answer = '{"keywords": ["ingestion pipeline", "kubernetes operator"]}'
        score = mb.score("keywords", case, answer)
        self.assertEqual(score["invented"], ["kubernetes operator"])
        self.assertEqual(score["invented_share"], 0.5)


class PromptLiftTests(unittest.TestCase):
    def test_all_four_shipped_prompts_are_found(self):
        """A prompt that moved must fail here, not silently measure a stale copy."""
        prompts = mb.shipping_prompts()
        self.assertEqual(set(prompts),
                         {"scope", "keywords", "people", "people_bind", "search_rewrite"})
        for role, text in prompts.items():
            self.assertGreater(len(text), 100, role)

    def test_the_scope_prompt_is_the_long_one(self):
        """The 2483-char rewrite measured worse and was not shipped."""
        self.assertGreater(len(mb.shipping_prompts()["scope"]), 3000)

    def test_a_missing_prompt_raises_rather_than_returning_empty(self):
        with self.assertRaises(mb.PromptDrift):
            mb._module_string("diary_manager.py", "_NO_SUCH_PROMPT")


class PipelineTests(unittest.TestCase):
    def test_the_stub_run_produces_every_case_and_scores_them(self):
        prompts = mb.shipping_prompts()
        cases = mb.build_cases(prompts, long_people=False)
        results = mb.run_stub(cases, list(mb.ROLES), 99, log=lambda *a: None)
        seen = {(run["role"], run["case"]) for run in results["runs"]}
        expected = {(role, case["id"]) for role in mb.ROLES for case in cases[role]}
        self.assertEqual(seen, expected)
        # The stub's people answer names a speaker label, which must be flagged.
        people = [r for r in results["runs"] if r["role"] == "people"][0]
        self.assertEqual(people["score"]["speaker_labels"], ["SPEAKER1"])

    def test_the_markdown_report_carries_the_raw_answer(self):
        """The report exists so a human can judge; a summary alone would not."""
        prompts = mb.shipping_prompts()
        cases = mb.build_cases(prompts, long_people=False)
        results = mb.run_stub(cases, ["scope"], 1, log=lambda *a: None)
        markdown = mb.to_markdown(results)
        self.assertIn("## Raw outputs", markdown)
        self.assertIn('"client": "EPAM"', markdown)

    def test_the_long_people_fixture_is_actually_long(self):
        """The failure it reproduces is length-dependent; a short fixture cannot see it."""
        self.assertGreater(len(mb.long_people_case()), 6000)
        self.assertLess(len(mb.long_people_case()), 8000)

    def test_the_long_people_fixture_names_the_participants_in_the_heading(self):
        body = mb.long_people_case()
        self.assertLess(body.index("Alice Smith"), 200)


class ContextWindowTests(unittest.TestCase):
    """`num_ctx` is what made the first run an unequal comparison.

    Ollama falls back to the tag's own default, so `openbmb/minicpm5-2b` was
    answering at 4096 while `granite3.3:2b` used 16332 -- two different windows,
    and therefore two different questions.  Nothing in the source shape of a
    request shows that, so the test captures the body that would be posted.
    """

    def _body(self, **kwargs):
        captured = {}

        def fake_post(url, body, timeout):
            captured.update(body)
            return {"message": {"content": "{}"}, "eval_count": 1}

        original = mb._post
        mb._post = fake_post
        try:
            mb.chat("http://x", "m", "sys", "prompt",
                    think=False, options={}, **kwargs)
        finally:
            mb._post = original
        return captured

    def test_num_ctx_reaches_the_request_options(self):
        body = self._body(num_ctx=16332)
        self.assertEqual(body["options"]["num_ctx"], 16332)

    def test_the_default_is_the_deployment_window(self):
        """16332 is what `OLLAMA_CONTEXT_LENGTH` gives, and what the merge path needs."""
        body = self._body()
        self.assertEqual(body["options"]["num_ctx"], mb.DEFAULT_NUM_CTX)
        self.assertGreaterEqual(mb.DEFAULT_NUM_CTX, 16332)

    def test_zero_means_leave_it_to_the_model(self):
        body = self._body(num_ctx=0)
        self.assertNotIn("num_ctx", body["options"])

    def test_the_report_records_the_window(self):
        prompts = mb.shipping_prompts()
        cases = mb.build_cases(prompts, long_people=False)
        results = mb.run_stub(cases, ["scope"], 1, log=lambda *a: None)
        results["num_ctx"] = 16332
        self.assertIn("num_ctx: 16332", mb.to_markdown(results))


class RoleSamplingTests(unittest.TestCase):
    """Production does not sample the same way in every role.

    The first run of the harness sampled uniformly, and eight of granite's
    answers came back as `HTTP 500: prediction aborted, token repeat limit
    reached` -- the exact failure `EXTRACT_SAMPLING` exists to prevent, reported
    as a model defect. These tests pin the composition so the harness cannot
    quietly measure its own omission again.
    """

    def test_the_extraction_roles_carry_the_repeat_penalty(self):
        for role in ("keywords", "people", "live"):
            with self.subTest(role=role):
                options = mb.sampling_for("granite3.3:2b", False, role)
                self.assertEqual(options["repeat_penalty"], 1.15)
                self.assertEqual(options["repeat_last_n"], 256)

    def test_a_judgement_role_is_left_greedy(self):
        """Granite's card states no sampling, so it keeps the app's 0.0.

        It is the only family still greedy -- see
        `test_nemotron_uses_the_cards_reasoning_and_tool_rows`.
        """
        options = mb.sampling_for("granite3.3:2b", False, "scope")
        self.assertEqual(options, {"temperature": 0.0})

    def test_the_vendor_temperature_survives_the_role_penalty(self):
        """MiniCPM5 must not be run at 0.0 just because it also extracts keywords."""
        options = mb.sampling_for("openbmb/minicpm5-2b:latest", False, "people")
        self.assertEqual(options["temperature"], 1.0)
        self.assertEqual(options["top_p"], 0.95)
        self.assertEqual(options["min_p"], 0.0)
        self.assertEqual(options["repeat_penalty"], 1.15)

    def test_minicpm_makes_no_think_distinction_on_its_card(self):
        """The card gives one set of parameters, so the 0.9/0.7 split was invented."""
        name = "openbmb/minicpm5-2b:latest"
        self.assertEqual(mb.sampling_for(name, True, "scope"),
                         mb.sampling_for(name, False, "scope"))

    def test_nemotron_uses_the_cards_reasoning_and_tool_rows(self):
        """1.0/top_p 0.95 for reasoning, 0.6/top_p 0.95 for tool calling."""
        name = "nemotron-3-nano:4b"
        self.assertEqual(mb.sampling_for(name, True, "scope"),
                         {"temperature": 1.0, "top_p": 0.95})
        self.assertEqual(mb.sampling_for(name, False, "scope"),
                         {"temperature": 0.6, "top_p": 0.95})

    def test_gemma_carries_the_top_k_its_card_asks_for(self):
        options = mb.sampling_for("batiai/gemma4-e2b:q4", False, "keywords")
        self.assertEqual(options["temperature"], 1.0)
        self.assertEqual(options["top_p"], 0.95)
        self.assertEqual(options["top_k"], 64)

    def test_every_installed_family_is_matched_by_its_ollama_tag(self):
        """A family the scan misses silently falls back to greedy 0.0."""
        expected = {
            "openbmb/minicpm5-2b:latest": "minicpm",
            "hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M": "lfm",
            "nemotron-3-nano:4b": "nemotron",
            "batiai/gemma4-e2b:q4": "gemma",
            "hf.co/ibm-granite/granite-4.2-3b-GGUF:Q4_K_M": "granite-4",
            "granite3.3:2b": "default",
        }
        for tag, family in expected.items():
            with self.subTest(tag=tag):
                self.assertEqual(mb.family_for(tag), family)

    def test_the_lfm_family_carries_its_own_generation_parameters(self):
        """LFM2.5's card gives 0.1 / top_k 50 / repeat_penalty 1.1 and no greedy
        mode, so a run of it at the app's 0.0 would measure the harness."""
        name = "hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M"
        expected = {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1}
        self.assertEqual(mb.sampling_for(name, True, "scope"), expected)
        self.assertEqual(mb.sampling_for(name, False, "scope"), expected)

    def test_granite_4_carries_the_temperature_its_card_requires(self):
        """Granite 4.2's card is emphatic: 1.0 / top_p 0.95 "across all tasks
        and serving backends".  Its 3.3 predecessor states nothing and stays
        greedy, which is why the two need separate families."""
        name = "hf.co/ibm-granite/granite-4.2-3b-GGUF:Q4_K_M"
        expected = {"temperature": 1.0, "top_p": 0.95}
        self.assertEqual(mb.sampling_for(name, True, "scope"), expected)
        self.assertEqual(mb.sampling_for(name, False, "scope"), expected)
        self.assertNotEqual(mb.family_for(name), mb.family_for("granite3.3:2b"))

    def test_the_reasoning_allowance_is_the_number_the_cards_give(self):
        """A thinking model spends `num_predict` inside its reasoning block
        before it emits any content, and the production budgets are small on
        purpose -- scope asks for 80.  At `THINK_BUDGET_BONUS` = 1500 the
        harness reported `LFM2.5-2.6B` as failing `handover-epam` and
        `generic-vector` when it had in fact spent 7,530 and 7,310 thinking
        characters and been cut at `done_reason=length` with an empty answer.
        Granite 4.2's card asks for 8192 output tokens in thinking mode, so
        the constant carries that figure rather than a guess."""
        self.assertEqual(mb.THINK_BUDGET_BONUS, 8192)
        scope = mb.budget_for({"num_predict": 80}, think=True)
        self.assertEqual(scope, 80 + 8192)
        # The production budget is still what the *content* gets; a model that
        # would overrun it in production still has to show that.
        self.assertEqual(mb.budget_for({"num_predict": 80}, think=False), 80)
        # `0` means "run to the context window", so a bonus must not impose a
        # ceiling where production has none.
        self.assertEqual(mb.budget_for({}, think=True), 0)

    def test_every_role_the_runner_uses_resolves_to_options(self):
        for role in mb.ROLES:
            with self.subTest(role=role):
                self.assertIn("temperature", mb.sampling_for("x:1b", True, role))


class ThinkBudgetTests(unittest.TestCase):
    """A thinking model spends the budget before it emits content.

    With the production budget of 80 tokens the first run returned an empty
    `content` for almost every `think=True` call -- the model answered, the
    answer never fitted. The bonus is what makes think mode measurable at all,
    and it must not leak into non-thinking mode, where 80 is the real contract.
    """

    def _run(self, think_modes):
        prompts = mb.shipping_prompts()
        cases = mb.build_cases(prompts, long_people=False)
        seen = []

        class _Stub:
            pass

        original = mb.chat

        def fake_chat(url, model, system, prompt, **kwargs):
            seen.append((kwargs["think"], kwargs["options"], kwargs["num_predict"]))
            return {"content": "{}", "thinking": "", "seconds": 0.0,
                    "prompt_tokens": 1, "answer_tokens": 1, "done_reason": "stop"}

        mb.chat = fake_chat
        try:
            mb.run("http://stub", ["stub:1b"], cases, think_modes=think_modes,
                   timeout=1.0, samples=1, roles=["scope"], log=lambda *a: None)
        finally:
            mb.chat = original
        return seen

    def test_think_mode_gets_room_for_the_reasoning_block(self):
        seen = self._run([True])
        self.assertTrue(all(think for think, _, _ in seen))
        self.assertTrue(all(budget >= 80 + mb.THINK_BUDGET_BONUS for _, _, budget in seen))

    def test_non_thinking_keeps_the_production_budget(self):
        seen = self._run([False])
        self.assertTrue(all(budget == 80 for _, _, budget in seen))

    def test_every_call_is_persisted_as_it_lands(self):
        """A pass killed mid-run used to write nothing at all.

        `bench-vendor2` reached 105 of 150 calls on a 4 GB card, was stopped,
        and left no `bench.json` — the file was written once, after the final
        call.  The three questions that run existed to answer were lost with it,
        so the callback has to fire per call rather than per run.
        """
        prompts = mb.shipping_prompts()
        cases = mb.build_cases(prompts)
        depths = []

        original = mb.chat

        def fake_chat(url, model, system, prompt, **kwargs):
            return {"content": "{}", "thinking": "", "seconds": 0.0,
                    "prompt_tokens": 1, "answer_tokens": 1, "done_reason": "stop"}

        mb.chat = fake_chat
        try:
            results = mb.run("http://stub", ["stub:1b"], cases,
                             think_modes=[False], timeout=1.0, samples=2,
                             roles=["scope"], log=lambda *a: None,
                             on_result=lambda current: depths.append(
                                 len(current["runs"])))
        finally:
            mb.chat = original
        calls = len(results["runs"])
        self.assertGreater(calls, 1)
        # One callback per call, seeing every earlier one: 1, 2, 3, ... calls.
        self.assertEqual(depths, list(range(1, calls + 1)))


class PeopleBindScoringTests(unittest.TestCase):
    """The binding hop is the only role that runs on LLM_QUERY_MODEL."""

    def _case(self):
        prompts = mb.shipping_prompts()
        cases = mb.build_cases(prompts)
        return cases["people_bind"][0]

    def test_the_correct_bind_is_accepted(self):
        scored = mb.score("people_bind", self._case(),
                          '{"matches":[{"fact_id":"c-lochas","confidence":0.92}]}')
        self.assertTrue(scored["correct"])
        self.assertEqual(scored["accepted"], ["c-lochas"])
        self.assertEqual(scored["wrong_binds"], [])

    def test_a_confident_wrong_bind_fails(self):
        scored = mb.score("people_bind", self._case(),
                          '{"matches":[{"fact_id":"c-porter","confidence":0.95}]}')
        self.assertFalse(scored["correct"])
        self.assertEqual(scored["wrong_binds"], ["c-porter"])

    def test_a_bool_confidence_does_not_clear_the_gate(self):
        # bool is an int subclass, so `confidence >= 0.8` is True for True.
        # The app excludes it explicitly; the scorer has to agree.
        scored = mb.score("people_bind", self._case(),
                          '{"matches":[{"fact_id":"c-lochas","confidence":true}]}')
        self.assertEqual(scored["accepted"], [])

    def test_a_low_confidence_bind_is_not_a_bind(self):
        scored = mb.score("people_bind", self._case(),
                          '{"matches":[{"fact_id":"c-lochas","confidence":0.55}]}')
        self.assertEqual(scored["accepted"], [])

    def test_an_invented_id_is_not_a_bind(self):
        scored = mb.score("people_bind", self._case(),
                          '{"matches":[{"fact_id":"c-nobody","confidence":0.99}]}')
        self.assertEqual(scored["accepted"], [])

    def test_the_binding_prompt_lifts_from_matching_utils(self):
        system = mb.shipping_prompts()["people_bind"]
        self.assertIn("Return ONLY JSON", system)
        self.assertIn("ambiguous", system)

    def test_the_prompt_carries_names_context_and_candidates(self):
        case = self._case()
        for needle in ("EXTRACTED NAMES:", "DIARY CONTEXT:", "CANDIDATES:"):
            self.assertIn(needle, case["prompt"])
        self.assertIn("c-lochas", case["prompt"])


if __name__ == "__main__":
    unittest.main()