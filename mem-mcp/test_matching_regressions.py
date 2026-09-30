from matching_utils import (
    QUERY_CACHE_DEFAULT_MAX,
    QUERY_CACHE_DEFAULT_TTL,
    TTLCache,
    cache_key,
)
import ast
import asyncio
import json
import os
import unittest
from matching_utils import (
    EVIDENCE_ALIAS,
    EVIDENCE_CONFLICT,
    EVIDENCE_EXACT,
    EVIDENCE_FIRST_LAST,
    EVIDENCE_FIRST_NAME,
    EVIDENCE_NONE,
    EVIDENCE_PARTIAL,
    EVIDENCE_SURNAME_STRONG,
    IDENTITY_STRENGTH,
    MIN_MATCH_CONFIDENCE,
    PEOPLE_RESOLVE_OVERLAP,
    PEOPLE_RESOLVE_WINDOW,
    VECTOR_CEIL,
    VECTOR_FLOOR,
    cluster_has_core,
    combine_duplicate_signals,
    execute_merge,
    merge_draft_output_budget,
    MergeDraftTooLarge,
    format_people_merge_text,
    identity_confidence,
    looks_like_person_name,
    text_windows,
    people_match_allowed,
    resolve_people_candidates,
    scopes_compatible,
    validate_merge_ids,
    validate_merge_records,
    vector_confidence,
)
from matching_utils import (
    SCOPE_ASSIGNED,
    SCOPE_CONTEXT_WEIGHT,
    SCOPE_EVIDENCE_CONTAINS,
    SCOPE_EVIDENCE_EXACT,
    SCOPE_EVIDENCE_FUZZY,
    SCOPE_EVIDENCE_NONE,
    SCOPE_EVIDENCE_TOKENS,
    SCOPE_INFERRED,
    SCOPE_RELEVANT,
    SCOPE_TIER_ASSIGNED,
    SCOPE_TIER_INFERRED,
    SCOPE_TIER_RELEVANT,
    SCOPE_UNSCOPED,
    client_header_value,
    client_tags_in_text,
    context_named_in_text,
    plan_search_scope,
    resolve_scope_name,
    scope_axis_tier,
    scope_strength,
)


class IdentityConfidenceTests(unittest.TestCase):
    """The production failures captured in mcp_tools.log must not match."""

    def test_exact_name_is_maximum_confidence(self):
        confidence, evidence = identity_confidence("Oleg Tolstashov", name="Oleg Tolstashov", raw_vector=0.55)
        self.assertEqual(evidence, EVIDENCE_EXACT)
        self.assertEqual(confidence, 1.0)

    def test_exact_match_ignores_a_weak_embedding(self):
        low, low_evidence = identity_confidence("Ismael Capel", name="Ismael Capel", raw_vector=VECTOR_FLOOR)
        high, high_evidence = identity_confidence("Ismael Capel", name="Ismael Capel", raw_vector=VECTOR_CEIL)
        self.assertEqual(low, high)
        self.assertEqual(low_evidence, high_evidence)

    def test_near_miss_surname_is_not_an_identity_match(self):
        # 'Stefan Siprell' -> 'Stefan Zipfel' scored 1.627 under the old blend
        # (0.55 vector + 0.652 name ratio + 0.4 first-name substring).
        confidence, evidence = identity_confidence(
            "Stefan Siprell", name="Stefan Zipfel", raw_vector=0.55, name_like=True
        )
        self.assertEqual(evidence, EVIDENCE_PARTIAL)
        self.assertLess(confidence, MIN_MATCH_CONFIDENCE)

    def test_truncated_surname_is_not_an_identity_match(self):
        confidence, _ = identity_confidence(
            "Stefan Siprev", name="Stefan Zipfel", raw_vector=0.55, name_like=True
        )
        self.assertLess(confidence, MIN_MATCH_CONFIDENCE)

    def test_conflicting_first_name_vetoes_a_shared_surname(self):
        # 'Ben Deutsche' -> 'Lukas Deutsch' scored 1.184 because 'deutsch' is a
        # substring of 'deutsche'; the first names are entirely different people.
        confidence, evidence = identity_confidence(
            "Ben Deutsche", name="Lukas Deutsch", raw_vector=0.78, name_like=True
        )
        self.assertEqual(evidence, EVIDENCE_CONFLICT)
        self.assertLess(confidence, MIN_MATCH_CONFIDENCE)

    def test_surname_strong_first_name_marginal_stays_below_link_gate(self):
        # 'Jan Smith' vs 'John Smith': strong surname, only a partial first name.
        confidence, evidence = identity_confidence(
            "Jan Smith", name="John Smith", raw_vector=0.75, name_like=True
        )
        self.assertEqual(evidence, EVIDENCE_SURNAME_STRONG)
        self.assertLess(confidence, MIN_MATCH_CONFIDENCE)

    def test_first_and_last_name_match_is_confident(self):
        confidence, evidence = identity_confidence(
            "Johnny Smith", name="John Smith", raw_vector=0.55, name_like=True
        )
        self.assertEqual(evidence, EVIDENCE_FIRST_LAST)
        self.assertGreaterEqual(confidence, MIN_MATCH_CONFIDENCE)

    def test_alias_is_confident(self):
        confidence, evidence = identity_confidence("Allie", name="Alice Smith", aliases=["Allie"], name_like=True)
        self.assertEqual(evidence, EVIDENCE_ALIAS)
        self.assertGreaterEqual(confidence, MIN_MATCH_CONFIDENCE)

    def test_unrelated_person_caps_below_confidence(self):
        # 'Radoslav' returned 'Oleg Tolstashov' as a success with no name overlap.
        confidence, evidence = identity_confidence(
            "Radoslav", name="Oleg Tolstashov", raw_vector=0.55, name_like=True
        )
        self.assertEqual(evidence, EVIDENCE_NONE)
        self.assertLess(confidence, MIN_MATCH_CONFIDENCE)

    def test_damping_is_suppressed_for_keyword_queries(self):
        name_like, _ = identity_confidence("Radoslav", name="Oleg Tolstashov", raw_vector=0.55, name_like=True)
        keyword, _ = identity_confidence("Radoslav", name="Oleg Tolstashov", raw_vector=0.55, name_like=False)
        self.assertLess(name_like, keyword)

    def test_semantic_search_still_ranks_on_vector_strength(self):
        confidence, _ = identity_confidence(
            "architecture", name="Unrelated Title", raw_vector=0.75
        )
        self.assertGreaterEqual(confidence, MIN_MATCH_CONFIDENCE)

    def test_single_word_query_matching_first_name(self):
        confidence, evidence = identity_confidence("Ben", name="Ben Deutsche", name_like=True)
        self.assertEqual(evidence, EVIDENCE_FIRST_NAME)
        self.assertGreaterEqual(confidence, MIN_MATCH_CONFIDENCE)

    def test_vector_confidence_is_clamped(self):
        self.assertEqual(vector_confidence(None), 0.0)
        self.assertEqual(vector_confidence(0.1), 0.0)
        self.assertEqual(vector_confidence(5.0), 1.0)
        self.assertAlmostEqual(vector_confidence(VECTOR_CEIL), 1.0)

    def test_identity_strength_is_ordered_by_trust(self):
        ordered = [
            EVIDENCE_NONE,
            EVIDENCE_CONFLICT,
            EVIDENCE_PARTIAL,
            EVIDENCE_SURNAME_STRONG,
            EVIDENCE_FIRST_NAME,
            EVIDENCE_FIRST_LAST,
            EVIDENCE_ALIAS,
            EVIDENCE_EXACT,
        ]
        strengths = [IDENTITY_STRENGTH[label] for label in ordered]
        self.assertEqual(strengths, sorted(strengths))

    def test_looks_like_person_name_rejects_keywords(self):
        self.assertTrue(looks_like_person_name("Stefan Siprell"))
        self.assertTrue(looks_like_person_name("Oleg"))
        self.assertFalse(looks_like_person_name("correction"))
        self.assertFalse(looks_like_person_name("work"))
        self.assertFalse(looks_like_person_name("database migration rollout plan"))


class PeopleMatchGateTests(unittest.TestCase):
    def test_single_name_does_not_match_similar_person(self):
        result = {"name": "Alice Jones", "score": 2.3, "metadata": {}}
        self.assertFalse(people_match_allowed("Alice", result))

    def test_full_name_match_is_retained(self):
        result = {"name": "Alice Smith", "score": 1.7, "metadata": {}}
        self.assertTrue(people_match_allowed("Alice Smith", result))

    def test_alias_match_is_retained(self):
        result = {"name": "Alice Smith", "score": 1.2, "metadata": {"aliases": ["Allie"]}}
        self.assertTrue(people_match_allowed("Allie", result))

    def test_production_near_miss_is_rejected(self):
        result = {
            "name": "Stefan Zipfel",
            "score": 1.627,
            "raw_score": 0.55,
            "metadata": {"first_name": "Stefan", "last_name": "Zipfel"},
        }
        self.assertFalse(people_match_allowed("Stefan Siprell", result))

    def test_production_conflicting_first_name_is_rejected(self):
        result = {
            "name": "Lukas Deutsch",
            "score": 1.184,
            "raw_score": 0.78,
            "metadata": {"first_name": "Lukas", "last_name": "Deutsch"},
        }
        self.assertFalse(people_match_allowed("Ben Deutsche", result))

    def test_production_missing_person_is_rejected(self):
        result = {
            "name": "Oleg Tolstashov",
            "score": 1.4,
            "raw_score": 0.55,
            "metadata": {"first_name": "Oleg", "last_name": "Tolstashov"},
        }
        self.assertFalse(people_match_allowed("Radoslav", result))

    def test_weak_flagged_result_is_always_rejected(self):
        result = {
            "name": "Stefan Zipfel",
            "score": 1.627,
            "raw_score": 0.55,
            "weak": True,
            "metadata": {"first_name": "Stefan", "last_name": "Zipfel"},
        }
        self.assertFalse(people_match_allowed("Stefan Siprell", result))

    def test_precomputed_confidence_is_honoured(self):
        result = {"name": "Stefan Zipfel", "score": 1.627, "confidence": 0.9, "metadata": {}}
        self.assertTrue(people_match_allowed("Stefan Zipfel", result))

    def test_low_precomputed_confidence_rejects(self):
        result = {"name": "Stefan Zipfel", "score": 1.627, "confidence": 0.2, "metadata": {}}
        self.assertFalse(people_match_allowed("Stefan Siprell", result))


class MatchingRegressionTests(unittest.TestCase):
    def test_merge_master_cannot_be_a_duplicate(self):
        with self.assertRaises(ValueError):
            validate_merge_ids("master", ["master", "duplicate"])

    def test_merge_duplicate_ids_must_be_unique(self):
        with self.assertRaises(ValueError):
            validate_merge_ids("master", ["duplicate", "duplicate"])

    def test_merge_rejects_unresolved_user_owned_record(self):
        with self.assertRaises(ValueError):
            validate_merge_records("master", ["duplicate"], [{"id": "master"}, None])

    def test_merge_accepts_all_resolved_user_owned_records(self):
        self.assertEqual(
            validate_merge_records(
                " master ", [" duplicate "], [{"id": "master"}, {"id": "duplicate"}]
            ),
            ["duplicate"],
        )

    def test_duplicate_cluster_rejects_bridge_without_core(self):
        self.assertFalse(
            cluster_has_core(
                [0, 1, 2, 3],
                {(0, 1): 0.9, (1, 2): 0.9, (2, 3): 0.9},
                0.75,
            )
        )

    def test_duplicate_cluster_accepts_shared_core(self):
        self.assertTrue(
            cluster_has_core(
                [0, 1, 2], {(0, 1): 0.9, (0, 2): 0.85, (1, 2): 0.6}, 0.75
            )
        )

    def test_fuzzy_duplicate_evidence_cannot_override_weak_vectors(self):
        self.assertAlmostEqual(combine_duplicate_signals(0.5, 0.88), 0.633, places=3)

    def test_exact_duplicate_identity_remains_decisive(self):
        self.assertEqual(combine_duplicate_signals(0.5, 1.0, strong_identity=True), 1.0)

    def test_people_merge_text_uses_fixed_markdown_sections(self):
        self.assertEqual(
            format_people_merge_text("Lead", "Acme", "Payments", "Owns Atlas"),
            "**Role:** Lead\n\n**Company:** Acme\n\n**Domain:** Payments\n\n**Notes:** Owns Atlas",
        )

    def test_people_merge_text_fills_missing_sections(self):
        self.assertIn("**Domain:** Not specified", format_people_merge_text("", None, "", ""))

    def test_merge_boundary_does_not_mutate_when_target_is_unresolved(self):
        calls = []

        def get_record(record_id, user_id):
            return {"id": record_id} if record_id == "master" else None

        async def update_memory(*args):
            calls.append("update")

        async def merge_memories(*args):
            calls.append("merge")

        with self.assertRaises(ValueError):
            __import__("asyncio").run(
                execute_merge(
                    "master", ["duplicate"], "Master", "Merged", "user",
                    get_record, update_memory, merge_memories,
                )
            )
        self.assertEqual(calls, [])

    def test_merge_boundary_mutates_only_after_all_targets_resolve(self):
        calls = []

        def get_record(record_id, user_id):
            return {"id": record_id}

        async def update_memory(*args):
            calls.append(("update", args[0]))

        async def merge_memories(*args):
            calls.append(("merge", args[0], args[1]))

        result = __import__("asyncio").run(
            execute_merge(
                " master ", [" duplicate "], "Master", "Merged", "user",
                get_record, update_memory, merge_memories,
            )
        )
        self.assertEqual(result, ("master", ["duplicate"]))
        self.assertEqual(calls, [("update", "master"), ("merge", "master", ["duplicate"])])

    def test_different_clients_are_not_compatible(self):
        self.assertFalse(
            scopes_compatible(
                {"clientName": "Acme", "contextName": "Atlas"},
                {"clientName": "Globex", "contextName": "Atlas"},
            )
        )

    def test_same_client_without_conflicting_context_is_compatible(self):
        self.assertTrue(
            scopes_compatible(
                {"clientName": "Acme", "contextName": "Atlas"},
                {"clientName": "Acme", "contextName": None},
            )
        )

    def test_llm_resolver_accepts_only_valid_high_confidence_ids(self):
        captured = {}

        async def llm(*args, **kwargs):
            captured["prompt"] = args[0]
            captured["system"] = kwargs["system"]
            return ('{"matches": [{"fact_id": "alice", "confidence": 0.92}, '
                    '{"fact_id": "unknown", "confidence": 1.0}]}')

        candidates = [
            {"id": "alice", "name": "Alice Smith", "text": "Atlas lead", "score": 1.7},
            {"id": "bob", "name": "Bob Jones", "text": "Finance lead", "score": 1.7},
        ]
        selected = __import__("asyncio").run(
            resolve_people_candidates(["Alice Smith"], "Atlas meeting", candidates, llm)
        )
        self.assertEqual([candidate["id"] for candidate in selected], ["alice"])
        self.assertIn("Alice Smith", captured["prompt"])
        self.assertIn("Atlas meeting", captured["prompt"])
        self.assertIn("alice", captured["prompt"])
        self.assertIn("candidate records", captured["system"])


class ScopeNameResolutionTests(unittest.TestCase):
    """The LLM returns whatever spelling the prompt coaxed out of it. These
    names come back as 'Deutsche Bank', 'Siemens', or 'Nonexistent Ltd' and must
    resolve to a stored Client/Context name or to nothing at all."""

    CLIENTS = ["Deutsche Bank (DB)", "Siemens AG", "Acme Corp", "Nordwind Energie"]

    def test_exact_stored_spelling_is_exact_evidence(self):
        self.assertEqual(resolve_scope_name("Deutsche Bank (DB)", self.CLIENTS),
                         ("Deutsche Bank (DB)", SCOPE_EVIDENCE_EXACT))

    def test_partial_multiword_name_resolves_to_stored_spelling(self):
        name, evidence = resolve_scope_name("Deutsche Bank", self.CLIENTS)
        self.assertEqual(name, "Deutsche Bank (DB)")
        self.assertEqual(evidence, SCOPE_EVIDENCE_TOKENS)

    def test_suffix_is_not_required(self):
        name, _ = resolve_scope_name("Deutsche Bank Group (2026)", self.CLIENTS)
        self.assertEqual(name, "Deutsche Bank (DB)")

    def test_single_token_falls_back_to_fuzzy(self):
        name, evidence = resolve_scope_name("Siemens", self.CLIENTS)
        self.assertEqual(name, "Siemens AG")
        self.assertEqual(evidence, SCOPE_EVIDENCE_FUZZY)

    def test_generic_contained_word_does_not_resolve(self):
        # 'bank' is inside 'Deutsche Bank (DB)' but means nothing on its own.
        self.assertEqual(resolve_scope_name("bank", self.CLIENTS), (None, SCOPE_EVIDENCE_NONE))

    def test_short_first_token_is_rejected(self):
        # 'Acme' is 4 chars — below the single-token first-name guard, so a
        # typo of a 4-letter name is not promoted to a link.
        self.assertEqual(resolve_scope_name("Acme", self.CLIENTS), (None, SCOPE_EVIDENCE_NONE))

    def test_hallucinated_client_resolves_to_nothing(self):
        self.assertEqual(resolve_scope_name("Nonexistent Ltd", self.CLIENTS),
                         (None, SCOPE_EVIDENCE_NONE))

    def test_ambiguous_candidates_resolve_to_nothing(self):
        # Returning a wrong-but-confident client is worse than returning none,
        # because it writes a FOR_CLIENT link the user never asked for.
        self.assertEqual(resolve_scope_name("Alpha", ["Alpha One", "Alpha Two"]),
                         (None, SCOPE_EVIDENCE_NONE))

    def test_context_names_use_the_same_ladder(self):
        contexts = ["Atlas Migration", "Hedron Replatform", "Platform"]
        self.assertEqual(resolve_scope_name("atlas", contexts)[0], "Atlas Migration")
        self.assertEqual(resolve_scope_name("atlas migration project", contexts),
                         ("Atlas Migration", SCOPE_EVIDENCE_TOKENS))
        self.assertEqual(resolve_scope_name("Hedron Replatform (2026)", contexts)[0],
                         "Hedron Replatform")
        self.assertEqual(resolve_scope_name("Migration", contexts), (None, SCOPE_EVIDENCE_NONE))

    def test_empty_candidate_list_never_resolves(self):
        self.assertEqual(resolve_scope_name("Anything", []), (None, SCOPE_EVIDENCE_NONE))
        self.assertEqual(resolve_scope_name("", self.CLIENTS), (None, SCOPE_EVIDENCE_NONE))

    def test_returned_name_is_always_the_stored_spelling(self):
        for raw in ("siemens ag", "SIEMENS AG", "  Nordwind   Energie "):
            name, _ = resolve_scope_name(raw, self.CLIENTS)
            self.assertIn(name, self.CLIENTS)

    def test_shared_first_word_is_not_enough(self):
        # Both candidates keep two tokens, so token coverage is satisfied, but
        # they are different projects and the raw answer matches neither.
        contexts = ["Atlas Migration", "Atlas Reporting"]
        self.assertEqual(resolve_scope_name("Atlas Rollout", contexts), (None, SCOPE_EVIDENCE_NONE))

    def test_one_shared_token_with_qualifier_does_not_resolve(self):
        self.assertEqual(resolve_scope_name("Deutsche Group", self.CLIENTS),
                         (None, SCOPE_EVIDENCE_NONE))


class ClientHeaderTests(unittest.TestCase):
    """An explicit 'Client:' header is the strongest scope signal in a note, so
    it is read directly. It arrives in whatever shape the author typed."""

    def test_plain_header(self):
        self.assertEqual(client_header_value("Client: Acme Corp\n\nMet them today."),
                         "Acme Corp")

    def test_bold_header(self):
        self.assertEqual(client_header_value("**Client:** Acme Corp"), "Acme Corp")

    def test_bullet_header(self):
        # A bullet turned the header into a list item; the old regex missed it
        # and the note was classified with no client signal at all.
        self.assertEqual(client_header_value("- Client: Acme Corp"), "Acme Corp")
        self.assertEqual(client_header_value("• **Client:** Acme Corp"), "Acme Corp")

    def test_heading_marker_and_dash_separator(self):
        self.assertEqual(client_header_value("## Client – Acme Corp"), "Acme Corp")

    def test_case_insensitive(self):
        self.assertEqual(client_header_value("CLIENT: Acme Corp"), "Acme Corp")

    def test_trailing_prose_is_cut(self):
        self.assertEqual(client_header_value("Client: Acme Corp — discussed pricing"),
                         "Acme Corp")
        self.assertEqual(client_header_value("Client: Acme Corp | weekly sync"),
                         "Acme Corp")

    def test_trailing_punctuation_is_stripped(self):
        self.assertEqual(client_header_value("Client: Acme Corp."), "Acme Corp")

    def test_header_must_start_a_line(self):
        # Mid-sentence "client" mentions are ordinary prose, not a header.
        self.assertEqual(client_header_value("I spoke to the client: Acme Corp"), "")

    def test_missing_or_empty_header(self):
        self.assertEqual(client_header_value("No scope here."), "")
        self.assertEqual(client_header_value("Client:   \nDone."), "")
        self.assertEqual(client_header_value(""), "")
        self.assertEqual(client_header_value(None), "")

class TextWindowTests(unittest.TestCase):
    """text_windows is how a long document reaches an LLM pass whole.

    The bug it replaces was not a precision loss. A classifier handed
    ``text[:1500]`` reaches a verdict on a fragment and then *records* that
    verdict, so the unread text could never influence the answer -- not on that
    run and not on any later one. The window count is therefore the thing worth
    asserting: one extra window is one extra place the client can be found.
    """

    def test_blank_input_yields_no_windows(self):
        for value in (None, "", "   \n\t "):
            self.assertEqual(text_windows(value), [], value)

    def test_a_document_that_fits_is_a_single_window(self):
        self.assertEqual(text_windows("short body", window=100, overlap=10), ["short body"])

    def test_a_long_document_is_split(self):
        windows = text_windows("x" * 250, window=100, overlap=10)
        self.assertGreater(len(windows), 1)
        for window in windows:
            self.assertLessEqual(len(window), 100)

    def test_every_character_is_covered(self):
        # Non-repetitive on purpose: a repetitive body matches at every offset,
        # which is how a coverage test passes while the middle is skipped.
        body = "".join(f"{i:06d}" for i in range(6000))
        windows = text_windows(body, window=1000, overlap=100)
        self.assertTrue(
            all(marker in "".join(windows) for marker in ("000000", "003000", "005999")),
            windows,
        )

    def test_evidence_after_the_old_1500_char_cut_is_still_reachable(self):
        """The regression itself: a mention past the old slice must be read."""
        body = "meeting notes. " * 130 + "The account is with Deutsche Bank."
        self.assertGreater(len(body), 1500)
        windows = text_windows(body, window=600, overlap=100)
        self.assertTrue(any("Deutsche Bank" in w for w in windows))

    def test_overlap_repeats_the_boundary_region(self):
        windows = text_windows("abcdefghij" * 30, window=100, overlap=50)
        self.assertGreaterEqual(len(windows), 2)
        self.assertTrue(windows[0][-50:] in "".join(windows[1:]))

    def test_a_degenerate_overlap_still_terminates(self):
        windows = text_windows("y" * 500, window=100, overlap=100)
        self.assertTrue(windows)
        self.assertTrue(all(0 < len(w) <= 100 for w in windows))

    def test_crlf_is_normalised_so_boundaries_are_clean(self):
        windows = text_windows("line\r\n" * 200, window=100, overlap=10)
        self.assertNotIn("\r", "".join(windows))

    def test_defaults_are_used_when_no_window_is_given(self):
        self.assertEqual(text_windows("tiny"), ["tiny"])


class ScopeClassificationInputTests(unittest.TestCase):
    """The scope classifier must not be handed a prefix of the item.

    These assert on the *source* of the reclassify module. They cannot call it:
    migrate_client_context imports the DB drivers, which are not installed here.
    What they can do is fail if the truncation comes back, and that is the whole
    point -- the truncation was load-bearing enough that a diary entry was filed
    under "generic" and stamped, permanently, from its opening 1500 characters.

    The slice test walks the AST rather than grepping the text. A substring
    search here matched my own docstring, which quotes the very slice it is
    meant to forbid -- the guard failed on the explanation of the bug.
    """

    TEXT_CARRIERS = frozenset((
        "item_text", "content", "text", "body", "snippet", "snippet_text",
    ))

    @classmethod
    def setUpClass(cls):
        import ast
        import os
        path = os.path.join(os.path.dirname(__file__), "migrate_client_context.py")
        with open(path, "r", encoding="utf-8", newline="") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)
        # Prefix slices -- text[:N] -- applied to the *text* carriers only.
        # A numeric prefix slice on a list is a different thing and often
        # deliberate: keywords[:10] caps the keyword list, it does not truncate
        # prose. Flagging every numeric slice would have failed on that.
        cls.text_slices = []
        for node in ast.walk(cls.tree):
            if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
                continue
            # A prefix slice is text[:N] -- no lower bound, numeric upper bound.
            if node.slice.lower is not None or node.slice.upper is None:
                continue
            if not (isinstance(node.slice.upper, ast.Constant)
                    and isinstance(node.slice.upper.value, int)):
                continue
            name = node.value.id if isinstance(node.value, ast.Name) else None
            if name in cls.TEXT_CARRIERS:
                cls.text_slices.append((node.lineno, name, node.slice.upper.value))

    def test_no_text_is_sliced_to_a_prefix_anywhere(self):
        # 1500 is the cut that shipped for the item, 2000 for the people text.
        # Either coming back is a long entry being judged on its opening.
        self.assertEqual(
            self.text_slices, [],
            f"a text prefix slice reappeared: {self.text_slices}",
        )

    def test_both_callers_use_the_whole_text_classifier(self):
        self.assertFalse(
            "await _classify_scope(body, clients)" in self.source,
            "a caller still routes through the single-window classifier",
        )
        self.assertEqual(self.source.count("await classify_scope_full(body, clients)"), 2)

    def test_the_windowing_helper_is_actually_used(self):
        self.assertFalse("from matching_utils import" not in self.source, "import block missing")
        # Assert on the classifier's call site by name, not on a count of
        # "text_windows(" across the module. The count used to be 2 and one of
        # the two was a private copy of people extraction that reclassification
        # triggered -- deleted, because a reclassify must classify scope only.
        # A bare count would have kept passing had the twin still been there
        # and the classifier's own call been the one removed.
        self.assertTrue(
            "text_windows(item_text, SCOPE_TEXT_WINDOW, SCOPE_TEXT_OVERLAP)" in self.source,
            "the scope classifier no longer windows the item text",
        )
        # And the people extractor must not have crept back: it belongs to
        # diary_manager, reached from the save and update paths only.
        self.assertFalse(
            "_extract_people_names" in self.source,
            "participant extraction is back in the reclassify module; it has "
            "its own entry points and a different model",
        )


# ---------------------------------------------------------------------------
# People candidate resolution
# ---------------------------------------------------------------------------
class PeopleResolverWindowTests(unittest.TestCase):
    """The resolver must see the whole entry, and a bad window must be survivable.

    ``resolve_people_candidates`` is pure and dependency-light, so unlike most of
    the reclassify path these call the shipping function rather than reading its
    source. That is the reason it is worth testing behaviourally: the truncation
    it replaced (``(content or '')[:2500]``) could not be caught by a call-count
    assertion, because the old code also made exactly one call. What changed was
    *what that call contained*, so the test has to inspect the prompt and which
    bindings survive it.
    """

    # Two windows at the shipped 6000/600 defaults. 300 lines is ~9.9k chars,
    # which is past one window and short of the 10.8k that would make three.
    BODY = "".join(f"line {i:06d} of the transcript. " for i in range(300))

    def setUp(self):
        self.candidates = [
            {"id": "c1", "name": "Alice Smith", "text": "met at the conference"},
            {"id": "c2", "name": "Bob Jones", "text": "owns the account"},
        ]
        # Self-checking fixture: derive the window count instead of hardcoding
        # it, so a change to the tunables fails here with a clear message
        # rather than as a baffling "3 != 2" in three unrelated tests.
        self.window_count = len(
            text_windows(self.BODY, PEOPLE_RESOLVE_WINDOW, PEOPLE_RESOLVE_OVERLAP)
        )
        if self.window_count != 2:
            self.fail(
                f"fixture assumption broken: BODY yields {self.window_count} windows, "
                f"not 2 (len={len(self.BODY)}, window={PEOPLE_RESOLVE_WINDOW}, "
                f"overlap={PEOPLE_RESOLVE_OVERLAP})"
            )

    def _run(self, responder, names=None, content=None, candidates=None):
        """Drive the resolver with a scripted llm_call. Returns (result, prompts)."""
        prompts = []

        async def llm_call(prompt, system=None, num_predict=None):
            prompts.append(prompt)
            return responder(prompt, len(prompts))

        result = asyncio.run(resolve_people_candidates(
            ["Alice Smith"] if names is None else names,
            self.BODY if content is None else content,
            self.candidates if candidates is None else candidates,
            llm_call,
        ))
        return result, prompts

    @staticmethod
    def _matches(*pairs):
        return json.dumps({"matches": [{"fact_id": i, "confidence": c} for i, c in pairs]})

    def test_a_short_entry_still_costs_exactly_one_call(self):
        """Windowing must not tax the common case."""
        _, prompts = self._run(lambda p, n: self._matches(("c1", 0.9)), content="Alice called Bob.")
        self.assertEqual(len(prompts), 1)

    def test_every_window_is_sent_to_the_model(self):
        _, prompts = self._run(lambda p, n: self._matches())
        self.assertEqual(len(prompts), self.window_count)

    def test_the_prompt_carries_text_past_the_old_2500_char_cut(self):
        """The defect itself: a binding supported only by the tail was lost."""
        marker = "PILOT-AGREED-BY-HEDRON-IN-THE-LAST-PAGE"
        body = ("padding. " * 400) + marker + " " + ("tail. " * 200)

        def responder(prompt, _n):
            return self._matches(("c2", 0.95)) if marker in prompt else self._matches()

        result, _ = self._run(responder, names=["Bob Jones"], content=body)
        self.assertEqual([c["id"] for c in result], ["c2"])

    def test_bindings_from_several_windows_are_unioned(self):
        def responder(_prompt, n):
            return self._matches(("c1", 0.95)) if n == 1 else self._matches(("c2", 0.9))

        result, _ = self._run(responder)
        self.assertEqual(sorted(c["id"] for c in result), ["c1", "c2"])

    def test_a_candidate_bound_twice_appears_once(self):
        def responder(_prompt, _n):
            return self._matches(("c1", 0.95), ("c1", 0.99))

        result, _ = self._run(responder)
        self.assertEqual([c["id"] for c in result], ["c1"])

    def test_one_failing_window_does_not_discard_the_others(self):
        def responder(_prompt, n):
            if n == 1:
                raise RuntimeError("ollama timeout")
            return self._matches(("c1", 0.9))

        result, _ = self._run(responder)
        self.assertEqual([c["id"] for c in result], ["c1"])

    def test_the_confidence_gate_still_applies_in_every_window(self):
        """A thin window must not become a weaker gate."""
        def responder(_prompt, _n):
            return self._matches(("c1", 0.79))

        result, prompts = self._run(responder)
        self.assertEqual(len(prompts), self.window_count)
        self.assertEqual(result, [])

    def test_a_boolean_confidence_does_not_clear_the_gate(self):
        """bool is an int subclass, so ``True >= 0.8`` was reachable."""
        result, _ = self._run(lambda p, n: '{"matches":[{"fact_id":"c1","confidence":true}]}')
        self.assertEqual(result, [])

    def test_an_unknown_candidate_id_is_never_returned(self):
        """A hallucinated id must not become a MENTIONS edge."""
        result, _ = self._run(lambda p, n: self._matches(("not-a-candidate", 1.0)))
        self.assertEqual(result, [])

    def test_blank_content_binds_nothing_and_costs_nothing(self):
        """There is no evidence at all, so any binding would be a guess."""
        _, prompts = self._run(lambda p, n: self._matches(("c1", 1.0)), content="   ")
        self.assertEqual(prompts, [])

    def test_a_single_candidate_still_short_circuits_without_the_model(self):
        only = [{"id": "c1", "name": "Alice Smith", "text": "", "metadata": {}}]
        result, prompts = self._run(
            lambda p, n: self._matches(), names=["Alice Smith"], candidates=only
        )
        self.assertEqual(prompts, [], "the exact-identity fast path must not call the LLM")
        self.assertEqual([c["id"] for c in result], ["c1"])

    def test_an_unparseable_reply_in_one_window_is_skipped(self):
        def responder(_prompt, n):
            return "not json at all" if n == 1 else self._matches(("c1", 0.9))

        result, _ = self._run(responder)
        self.assertEqual([c["id"] for c in result], ["c1"])


class ResolverInputTests(unittest.TestCase):
    """No prompt in matching_utils may be handed a prefix of the entry.

    Same AST walk as ScopeClassificationInputTests, for the same reason: the old
    ``(content or '')[:2500]`` is quoted in the function's own docstring, so a
    substring guard would fail on the explanation of the bug rather than on the
    bug. Docstrings are ``ast.Constant`` and cannot trip an AST walk.
    """

    CARRIERS = ("content", "text", "body")

    def setUp(self):
        import ast
        import os

        path = os.path.join(os.path.dirname(__file__), "matching_utils.py")
        with open(path, "r", encoding="utf-8", newline="") as handle:
            self.source = handle.read()
        self.slices = []
        for node in ast.walk(ast.parse(self.source)):
            if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
                continue
            if node.slice.lower is not None or node.slice.upper is None:
                continue
            if not (isinstance(node.slice.upper, ast.Constant)
                    and isinstance(node.slice.upper.value, int)):
                continue
            name = node.value.id if isinstance(node.value, ast.Name) else None
            if name in self.CARRIERS:
                self.slices.append((node.lineno, name, node.slice.upper.value))

    def test_no_entry_text_is_sliced_to_a_prefix(self):
        self.assertEqual(
            self.slices, [],
            f"a text prefix slice reappeared in matching_utils: {self.slices}",
        )

    def test_the_windowed_resolver_is_the_one_in_use(self):
        """Pin the call, not just the helper: the slice could come back
        alongside a windowed call that nothing actually reaches."""
        segment = self.source[self.source.index("async def resolve_people_candidates"):]
        # assertTrue rather than assertIn: the haystack here is a whole
        # function, and assertIn echoes both operands, so a failure dumps
        # kilobytes of source into the report.
        for needle in (
            "text_windows(content, PEOPLE_RESOLVE_WINDOW, PEOPLE_RESOLVE_OVERLAP)",
            "for window in windows:",
            "DIARY CONTEXT: {window}",
        ):
            self.assertTrue(
                needle in segment,
                f"resolve_people_candidates no longer contains {needle!r}",
            )

    def test_matching_utils_still_imports_without_the_app(self):
        """No app import may ever appear here. The stdlib set is allowed to move.

        Every test that imports matching_utils directly relies on this, and so
        does the module's stated design: it is the dependency-light home for the
        shared helpers precisely so it can be unit-tested without DB drivers.

        The allowlist grew by ``time`` for the query-rewrite cache's expiry.
        That is a widening, so the guard that actually matters is asserted
        separately below rather than being left to the allowlist: the point is
        "nothing from this app", and an allowlist alone would quietly accept a
        new app module if somebody added it to the list.
        """
        import ast

        imported = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(
            imported <= {"os", "re", "json", "difflib", "time"},
            f"matching_utils gained a dependency: {sorted(imported)}",
        )

    def test_matching_utils_imports_no_app_module(self):
        """The load-bearing half, and the reason for the allowlist existing.

        ``from common import logger`` is the specific thing that has been
        tempting here: every failure path in this module would want to log, and
        logging from ``common`` is what makes the module unimportable without
        the DB drivers — which would silently delete this entire suite, because
        a module that cannot be imported raises at collection and every test
        errors rather than one failing.
        """
        import ast

        app_modules = {
            "common", "fact_manager", "diary_manager", "client_manager",
            "migrate_client_context", "gui", "server", "mcp_tools", "memory",
            "chunking", "backup", "neo4j", "qdrant_client", "httpx", "fastapi",
        }
        imported = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(
            imported & app_modules, set(),
            f"matching_utils imported app modules: {sorted(imported & app_modules)}",
        )


class ClientTagTests(unittest.TestCase):
    """`client_tags_in_text` reads the neighbourhood evidence, not the answer.

    A `[client: X]` tag on a neighbouring fact describes that *person or fact*.
    Treating it as a reason to file the item under X is what stamped a handover
    meeting about another organisation's SAP estate with the consultancy whose
    two Enterprise Architects happened to run it.
    """

    def test_a_tag_is_read(self):
        self.assertEqual(
            client_tags_in_text("## Participants\n- Gergely Papp: ... [client: EPAM]"),
            ["EPAM"],
        )

    def test_tags_are_first_seen_order_and_deduplicated(self):
        body = "a [client: EPAM] b [client: SAP SE] c [client: EPAM]"
        self.assertEqual(client_tags_in_text(body), ["EPAM", "SAP SE"])

    def test_a_parenthesised_stored_spelling_survives(self):
        """Client names carry qualifiers; the whole one is the lookup key."""
        self.assertEqual(
            client_tags_in_text("x [client: Deutsche Bank (DB)] y"),
            ["Deutsche Bank (DB)"],
        )

    def test_extra_whitespace_is_trimmed(self):
        self.assertEqual(client_tags_in_text("[client:   EPAM  ]"), ["EPAM"])

    def test_a_tag_does_not_span_lines(self):
        """A runaway match would swallow the rest of the enriched text."""
        self.assertEqual(client_tags_in_text("[client: EPAM\nmore text here]"), [])

    def test_blank_and_none_are_empty(self):
        self.assertEqual(client_tags_in_text(""), [])
        self.assertEqual(client_tags_in_text(None), [])

    def test_text_without_tags_is_empty(self):
        self.assertEqual(client_tags_in_text("SAP GRC handover notes"), [])


class RelatedClientSelectionTests(unittest.TestCase):
    """`_related_clients_for` decides what gets a RELEVANT_TO edge.

    Pure logic lifted out of the module, so it is *called* here rather than
    read. A source assertion could confirm the function is mentioned on each
    call site without ever showing what it returns, and the whole point of this
    change is which names come out.
    """

    CLIENTS = ["Deutsche Bank (DB)", "EPAM", "LC Security", "SAP SE", "White Cube"]

    @classmethod
    def setUpClass(cls):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "migrate_client_context.py"
        )
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        segment = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_related_clients_for":
                segment = ast.get_source_segment(source, node)
        assert segment, "_related_clients_for is missing from migrate_client_context.py"
        namespace = {
            "resolve_scope_name": resolve_scope_name,
            "client_tags_in_text": client_tags_in_text,
        }
        exec(segment, namespace)
        cls.fn = staticmethod(namespace["_related_clients_for"])

    def _run(self, text, related, primary):
        return self.fn(text, related, [{"name": n} for n in self.CLIENTS], primary)

    def test_a_tag_becomes_a_secondary_link(self):
        body = "- Gergely Papp [client: EPAM]\n- Oleg Tolstashov [client: EPAM]"
        self.assertEqual(self._run(body, [], "Deutsche Bank (DB)"), ["EPAM"])

    def test_the_primary_is_never_also_related(self):
        """An item linked to its own client twice appears twice in a filtered list."""
        body = "- Gergely Papp [client: EPAM]"
        self.assertEqual(self._run(body, [], "EPAM"), [])

    def test_the_model_list_and_the_tags_are_unioned(self):
        body = "- Gergely Papp [client: EPAM]"
        self.assertEqual(
            sorted(self._run(body, ["SAP SE"], "Deutsche Bank (DB)")),
            ["EPAM", "SAP SE"],
        )

    def test_an_unresolvable_name_is_dropped_not_written(self):
        """Writing it would either invent a client or silently no-op."""
        self.assertEqual(self._run("", ["Nonexistent Ltd"], "EPAM"), [])

    def test_a_tag_using_an_abbreviation_still_binds(self):
        body = "- Gergely Papp [client: Deutsche Bank]"
        self.assertEqual(self._run(body, [], "SAP SE"), ["Deutsche Bank (DB)"])

    def test_no_tags_and_no_model_list_is_empty(self):
        self.assertEqual(self._run("plain text", [], "EPAM"), [])

    def test_a_null_primary_does_not_filter_everything_out(self):
        """A generic item can still be about several clients."""
        body = "- Gergely Papp [client: EPAM]"
        self.assertEqual(self._run(body, [], None), ["EPAM"])


class ScopePromptTests(unittest.TestCase):
    """The prompt is where the cross-company mistake was actually made.

    The classifier was told a neighbour's `[client: X]` tag was "the strongest
    signal available". That is a sentence, not code, and nothing else in the
    suite would have noticed it changing.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "migrate_client_context.py"
        )
        with open(path, "r", encoding="utf-8") as handle:
            cls.source = handle.read()
        tree = ast.parse(cls.source)
        for node in tree.body:
            targets = getattr(node, "targets", [])
            if any(
                isinstance(t, ast.Name) and t.id == "_SCOPE_SYSTEM" for t in targets
            ):
                cls.system = ast.get_source_segment(cls.source, node)
                # The source is a chain of adjacent string literals, so a
                # sentence is split across them and a substring test over the
                # source cannot see a phrase that IS present. Assert prose
                # against the assembled prompt instead.
                cls.prompt = ast.literal_eval(node.value)
                return
        raise AssertionError("_SCOPE_SYSTEM is missing from migrate_client_context.py")

    def test_it_still_instructs_the_model_to_prefer_a_scope_tag(self):
        """The regression this whole change exists to prevent."""
        self.assertFalse(
            "strongly prefer that client" in self.system,
            msg="the prompt still says a neighbour's scope tag overrides the text",
        )

    def test_being_named_on_an_item_is_not_evidence_against_being_its_client(self):
        """A rule that reads "a participant's employer is not the client" pushed
        the SAP RAM/GRC handover away from EPAM, which is the correct client: the
        two named Enterprise Architects are EPAM's own staff working on EPAM's
        own engagement. The rule was written from a wrong guess about which
        client the item belonged to, and then generalised into the prompt.
        """
        self.assertTrue(
            "IS the client" in self.prompt
            and "not evidence against being its client" in self.prompt,
            msg="an item about an organisation's own people and work has that "
                "organisation as its client",
        )

    def test_the_supposedly_harmful_rule_is_gone(self):
        self.assertFalse(
            "employer is not the client" in self.prompt,
            msg="this rule is actively wrong for internal items; it was reverted "
                "once already",
        )

    def test_it_still_says_to_decide_by_what_the_work_is_for(self):
        self.assertIn("what the work is FOR", self.system)

    def test_the_related_field_is_part_of_the_contract(self):
        # The prose assertions read the assembled prompt, not the source: a
        # sentence split across two adjacent string literals is invisible to a
        # substring search over the raw text, which is a property of how the
        # literal is wrapped rather than of the prompt.
        self.assertIn('\\"related\\"', self.system)
        self.assertIn("second subject", self.prompt)

    def test_the_context_list_order_is_not_a_ranking(self):
        """It answered EPAM/PPC, and PPC is simply first in EPAM's list."""
        self.assertIn("not a ranking", self.system)

    def test_a_client_header_is_evidence_but_not_an_answer(self):
        """The header rule was inverted, and deliberately.

        "**Client:** Daimler AG (MBAG)" in an RFI names the end CUSTOMER, and the
        old prompt called that declaration "authoritative -- use it and do not
        override it". So the model returned it verbatim, `resolve_scope_name`
        matched it against nothing (it is not a stored client), and the item was
        stamped with **no client at all** -- permanently, because a stamped null
        is never revisited. Measured: the client rule now answers EPAM, which is
        the client the work is actually delivered for.

        The header is still real evidence; what changed is that it may not be the
        answer. Pin the new sentence, and pin that "authoritative" is gone, so a
        re-edit cannot quietly restore the rule that lost the scope.
        """
        self.assertFalse(
            "authoritative" in self.system,
            msg="the header is evidence, not an answer; calling it authoritative "
                "made every customer-named document resolve to no client at all",
        )
        self.assertIn("names the end CUSTOMER", self.prompt)
        self.assertIn("Never return the header's name by itself", self.prompt)


class RelatedLinkWiringTests(unittest.TestCase):
    """RELEVANT_TO has to be written on both paths, or facts silently lose it.

    `_fast_diary_scope` already wrote these edges for a multi-client diary
    entry, which meant the feature existed and worked for exactly one of the
    two node types -- the kind of partial support that reads as "it works".
    """

    @classmethod
    def setUpClass(cls):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "migrate_client_context.py"
        )
        with open(path, "r", encoding="utf-8") as handle:
            cls.source = handle.read()
        tree = ast.parse(cls.source)
        cls.fns = {
            node.name: ast.get_source_segment(cls.source, node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_the_fact_path_writes_related_links(self):
        body = self.fns.get("_classify_and_link_fact", "")
        self.assertIn("_write_related_links(", body, msg=(
            "facts are filed under a client but never cross-referenced, so a fact "
            "about two clients is unreachable under the second one"
        ))
        self.assertIn('"Fact"', body)

    def test_the_diary_path_writes_related_links(self):
        body = self.fns.get("_classify_and_link_diary", "")
        self.assertIn("_write_related_links(", body)
        self.assertIn('"DiaryEntry"', body)

    def test_the_fact_path_always_writes_even_when_nothing_resolved(self):
        """A null primary is precisely when RELEVANT_TO is the only signal left.

        Asserted as *presence before position*: the earlier version only
        asserted that the call was not after the `return False`, which passes
        vacuously when the call is deleted outright.
        """
        body = self.fns.get("_classify_and_link_fact", "")
        self.assertTrue(
            "_write_related_links(" in body,
            msg="the fact path writes RELEVANT_TO only on the success branch",
        )
        self.assertLess(
            body.index("_write_related_links("),
            body.index("return False"),
            msg="a fact with no resolved client is returned before its related "
                "links are written, so it can never be cross-referenced",
        )

    def test_the_diary_path_also_writes_before_returning(self):
        body = self.fns.get("_classify_and_link_diary", "")
        self.assertLess(
            body.index("_write_related_links("),
            body.index("return False"),
        )

    def test_the_link_helper_matches_on_the_label_not_a_fixed_node_type(self):
        """Positive check, not `assertNotIn("(d:DiaryEntry"...)`.

        The negative form missed a real re-injection: the test looked for
        `(d:DiaryEntry {id: $did` while the query interpolates the *parameter*
        name, so swapping `n:{label}` for `d:DiaryEntry` left the guard green.
        Asserting the shape we require survives any rewording of the failure.
        """
        body = self.fns.get("_link_diary_relevant_to", "")
        self.assertTrue(
            "MATCH (n:{label}" in body,
            msg="the node match must interpolate the label, or facts never link",
        )
        self.assertTrue(
            "MATCH (n:" in body,
            msg=f"RELEVANT_TO no longer targets a parameterised node: {body[-400:]}",
        )

    def test_an_unexpected_label_is_rejected(self):
        """The label is interpolated into Cypher, so it must not be free text."""
        body = self.fns.get("_link_diary_relevant_to", "")
        self.assertIn("raise ValueError", body)

    def test_the_write_is_swallowed_on_failure(self):
        """A secondary link must not fail a classification that already worked."""
        body = self.fns.get("_write_related_links", "")
        self.assertIn("except Exception", body)
        self.assertIn("logger.warning", body)



class OwnClientTagTests(unittest.TestCase):
    """A person's own employer must be labelled as such in the prompt text.

    The bare `[client: EPAM]` tag was the single most concrete token in the
    classifier prompt for a diary entry whose own text named no client at all,
    so the model took it. Telling the model in prose that the tag meant
    something else did not help -- it read the tag, not the instruction. The
    tag now states its own meaning, and these tests pin both halves: the
    rendering, and the parser that still has to read it for the RELEVANT_TO
    union.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "migrate_client_context.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        for node in tree.body:
            if getattr(node, "name", None) == "_scope_tag":
                namespace = {}
                exec(ast.get_source_segment(source, node), namespace)
                cls.scope_tag = staticmethod(namespace["_scope_tag"])
                return
        raise AssertionError("migrate_client_context no longer defines _scope_tag")

    def test_a_person_tag_says_own_client(self):
        self.assertEqual(
            self.scope_tag(True, "EPAM"), " [own client: EPAM]"
        )

    def test_a_fact_tag_still_says_plain_client(self):
        self.assertEqual(
            self.scope_tag(False, "Deutsche Bank (DB)"),
            " [client: Deutsche Bank (DB)]",
        )

    def test_the_two_forms_are_distinguishable(self):
        """If both rendered identically the relabelling would achieve nothing."""
        self.assertNotEqual(
            self.scope_tag(True, "EPAM"), self.scope_tag(False, "EPAM")
        )

    def test_both_forms_still_parse_for_the_related_union(self):
        """Relabelling must not silently break RELEVANT_TO.

        The tag is retained precisely because a person's employer is good
        evidence for a *secondary* link. If the parser stopped reading the new
        form, the feature would look like it worked and quietly write nothing.
        """
        self.assertEqual(
            client_tags_in_text("- Oleg: architect [own client: EPAM]"), ["EPAM"]
        )
        self.assertEqual(
            client_tags_in_text("- Diligent [client: SAP SE]"), ["SAP SE"]
        )

    def test_the_own_tag_never_names_the_item_itself(self):
        """The word 'own' must be in the rendered tag, not only in the prompt."""
        self.assertIn("own client", self.scope_tag(True, "X"))


class NullableScopePromptTests(unittest.TestCase):
    """The user-facing ask: empty has to be a real option, not a failure.

    A 4420-char handover meeting about another organisation's SAP estate came
    back `{"client": "EPAM", "context": "PPC"}` and was stamped, when the entry
    named no client and no project anywhere. SAP SE -- the client whose name the
    text actually contains -- was left out of the client field entirely.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "migrate_client_context.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        for node in tree.body:
            # _SCOPE_SYSTEM is a module-level Assign, so the name lives on
            # a target, not on the node itself. A getattr(node, "name")
            # lookup finds nothing and the guard below would misreport this
            # as the constant having been deleted.
            targets = getattr(node, "targets", [])
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if "_SCOPE_SYSTEM" in names:
                cls.prompt = ast.literal_eval(node.value)
                break
        else:
            raise AssertionError("migrate_client_context no longer defines _SCOPE_SYSTEM")

        cls.source = source
        cls.tree = tree

    @classmethod
    def _fn(cls, name):
        """Source of one top-level function, for the code-level guards."""
        for node in cls.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(cls.source, node)
        raise AssertionError(f"migrate_client_context no longer defines {name}()")

    def test_a_null_answer_is_described_as_correct_and_expected(self):
        self.assertTrue(
            "null value is a real, correct, expected answer" in self.prompt,
            msg="the prompt must say that empty is a real answer, so the model "
                "does not treat it as something to avoid",
        )

    def test_it_says_never_fill_a_field_to_avoid_leaving_it_empty(self):
        self.assertTrue(
            "Never fill a field to avoid leaving it empty" in self.prompt,
            msg="the observed failure was a field filled to avoid an empty one",
        )

    def test_a_client_with_no_contexts_is_not_a_dead_end(self):
        # The placeholder was '(none)' on the client's line; the format is now a
        # "contexts" array, and an empty one says the same thing structurally.
        # Either wording works; what matters is that a contextless client is
        # described as a valid choice rather than a dead end.
        self.assertTrue(
            "is a normal, valid choice" in self.prompt,
            msg="SAP SE has no contexts and EPAM has three; without this the "
                "model picks whichever client can supply a context name",
        )
        self.assertFalse(
            "'(none)'" in self.prompt,
            msg="the prompt still describes a '(none)' line, but the client "
                "list is a JSON array now -- the two descriptions contradict",
        )

    def test_choosing_a_contextless_client_forces_a_null_context(self):
        self.assertTrue(
            "the context MUST be null" in self.prompt,
            msg="a client with no projects has no context to name; without this "
                "the model copies whatever its line contains",
        )

    def test_the_code_drops_a_context_for_a_client_with_no_projects(self):
        """The prompt is not the guarantee; this is.

        The model has lifted three different strings out of the client list and
        returned each as the context value, so reworded evidence is not a fix.
        The stored pair is decided here instead: a client with no projects cannot
        have a context, whatever the model said.
        """
        seg = self._fn("_classify_scope")
        self.assertTrue(
            "if not client.get(\"contexts\")" in seg
            and "resolved_context, _ = resolve_scope_name(" in seg,
            msg="_classify_scope must consult the chosen client's own project "
                "list before keeping a context",
        )
        # The keep must be inside the else, i.e. gated on the client having one.
        self.assertTrue(
            "else:" in seg,
            msg="without an else the resolve call is reached regardless",
        )

    def test_the_context_must_come_from_the_chosen_clients_own_line(self):
        self.assertTrue(
            "Never take a project from another client's array" in self.prompt,
            msg="the observed failure was SAP SE answered with DB AI Adoption, "
                "which is Deutsche Bank's project and was never in SAP SE's "
                "contexts array",
        )

    def test_the_old_placeholder_is_gone_from_the_prompt(self):
        """The placeholder was the bug: the model copied it into the answer.

        The client line now reads "- SAP SE: (none)". Every earlier form let the
        model attach a project belonging to a DIFFERENT client, because the
        pairing was never visible on one line.
        """
        self.assertFalse(
            "(no contexts)" in self.prompt
            or "[contexts:" in self.prompt,
            msg="this exact string was returned as the context value in "
                "production; the client list must render absence by omission",
        )

    def test_the_own_client_tag_is_read_as_who_that_person_is(self):
        """A tag is the person's employer, which is evidence FOR the client when
        the item is that employer's own work -- not the weakest signal in the
        prompt. The two Enterprise Architects on the SAP RAM/GRC entry carry no
        tag at all (they have no FOR_CLIENT edge), so this rule was never even
        reached for that item, while the "weakest evidence" wording it replaced
        was suppressing the correct answer on internal items."""
        self.assertTrue(
            "WHO THAT PERSON IS" in self.prompt,
            msg="the prompt must still name the [own client: X] form and say what "
                "it denotes, or the model matches a literal it has never seen",
        )

    def test_the_stale_tag_rule_is_gone(self):
        self.assertFalse(
            "a [client: X] tag on one of them" in self.prompt,
            msg="the old rule described only the plain form; leaving it invites "
                "the model to read the plain form as the strong one",
        )

class ContextNamedInTextTests(unittest.TestCase):
    """A project the item never names is not the item's project.

    Behavioural rather than a source check: matching_utils imports locally, and
    the defect this replaces was six prompt variants failing to stop the model
    picking a project off the client's list. What matters is which names survive
    a real body, not how the prompt is worded.
    """

    BODY = (
        "SAP RAM vs GRC Handover with Oleg\r\n"
        "## Participants\r\n"
        "- Gergely Papp: Enterprise Architect at EPAM (4 years at EPAM).\r\n"
        "- Oleg Tolstashov: Enterprise Architect, Company/Team: EPAM Systems.\r\n"
        "## Description\r\n"
        "- SAP RAM implementation for tax compliance paused, awaiting green light.\r\n"
        "- Diligent introduction as an alternative.\r\n"
    )

    def test_none_of_epams_projects_are_named_by_this_item(self):
        for project in ("PPC", "MBAG", "MUFG"):
            self.assertFalse(
                context_named_in_text(project, self.BODY),
                msg=f"{project!r} does not appear in the entry, so the model picked "
                    f"it off the client list rather than out of the text",
            )

    def test_a_project_the_item_does_name_is_kept(self):
        body = "Kickoff for the MBAG migration, covering the SAP RAM blockers."
        self.assertTrue(context_named_in_text("MBAG", body))

    def test_matching_is_case_and_punctuation_insensitive(self):
        self.assertTrue(context_named_in_text("db ai adoption",
                                              "we started DB AI Adoption last week"))
        self.assertTrue(context_named_in_text("MBAG Migration",
                                              "the mbag-migration slipped again"))

    def test_every_token_of_a_multi_word_project_must_appear(self):
        self.assertFalse(context_named_in_text("DB AI Adoption",
                                               "we discussed the AI rollout for db"))

    def test_blank_inputs_are_false_not_an_error(self):
        for name, body in ((None, self.BODY), ("", self.BODY),
                           ("PPC", ""), ("PPC", None), (None, None),
                           ("PPC", "   ")):
            self.assertFalse(context_named_in_text(name, body),
                             msg=f"({name!r}, {body!r}) must be False, not a raise")

class QueryRewriteCacheTests(unittest.TestCase):
    """The cache is only worth having if it is both bounded and actually expiring.

    The clock is injected rather than slept through, so these pin the boundary
    exactly instead of racing it: a test that sleeps for the TTL is a test that
    fails on a loaded CI box and passes on a fast one.
    """

    def test_a_fresh_entry_is_returned(self):
        c = TTLCache(ttl=100.0, max_entries=4)
        c.put("k", ["a"], now=0.0)
        self.assertEqual(c.get("k", now=99.9), ["a"])

    def test_expiry_is_exclusive_of_the_boundary_and_inclusive_of_it(self):
        c = TTLCache(ttl=100.0, max_entries=4)
        c.put("k", ["a"], now=0.0)
        self.assertEqual(c.get("k", now=100.0), None,
                         msg="at exactly ttl the entry is stale, not fresh")

    def test_an_expired_entry_is_removed_on_read(self):
        c = TTLCache(ttl=10.0, max_entries=4)
        c.put("k", ["a"], now=0.0)
        c.get("k", now=11.0)
        self.assertEqual(len(c), 0,
                         msg="a cache that only evicts on write still holds every "
                             "query ever asked, which is what the bound prevents")

    def test_the_bound_evicts_the_oldest_first(self):
        c = TTLCache(ttl=1000.0, max_entries=3)
        for i, k in enumerate("abc"):
            c.put(k, [k], now=float(i))
        c.put("d", ["d"], now=3.0)
        self.assertEqual(len(c), 3)
        self.assertEqual(c.get("a", now=4.0), None, msg="oldest must go first")
        self.assertEqual(c.get("d", now=4.0), ["d"])

    def test_reinserting_a_key_refreshes_its_recency(self):
        c = TTLCache(ttl=1000.0, max_entries=3)
        c.put("a", ["a"], now=0.0)
        c.put("b", ["b"], now=1.0)
        c.put("c", ["c"], now=2.0)
        c.put("a", ["a2"], now=3.0)
        c.put("d", ["d"], now=4.0)
        self.assertEqual(c.get("a", now=5.0), ["a2"],
                         msg="a re-put is a use, so it must not be the eviction victim")
        self.assertEqual(c.get("b", now=5.0), None)

    def test_a_cache_with_no_ttl_or_no_room_is_disabled_and_inert(self):
        for ttl, mx in ((0.0, 4), (-1.0, 4), (100.0, 0), (100.0, -1)):
            c = TTLCache(ttl=ttl, max_entries=mx)
            self.assertFalse(c.enabled, msg=f"ttl={ttl} max={mx} must be disabled")
            c.put("k", ["a"], now=0.0)
            self.assertEqual(c.get("k", now=0.0), None,
                             msg=f"a disabled cache must not serve ttl={ttl} max={mx}")
            self.assertEqual(len(c), 0,
                             msg=f"a disabled cache must not retain ttl={ttl} max={mx}")

    def test_clear_empties_it(self):
        c = TTLCache(ttl=100.0, max_entries=4)
        c.put("k", ["a"], now=0.0)
        c.clear()
        self.assertEqual(len(c), 0)

    def test_defaults_are_the_documented_ones(self):
        self.assertEqual(TTLCache().ttl, QUERY_CACHE_DEFAULT_TTL)
        self.assertEqual(TTLCache().max_entries, QUERY_CACHE_DEFAULT_MAX)
        self.assertTrue(TTLCache().enabled)

    def test_cache_key_is_hashable_for_none_and_unhashable_parts(self):
        # Filters are optional strings the caller does not control. A stray list
        # must not raise on the lookup path, where the caller can only get a 500.
        self.assertIsInstance(cache_key("q", None, None, None, False), tuple)
        for parts in (("q", ["a", "b"]), ("q", {"k": "v"}), ("q", {1, 2}),
                      ("q", None, None, None, False)):
            try:
                key = cache_key(*parts)
                hash(key)
            except TypeError as exc:
                self.fail(f"cache_key{parts!r} raised TypeError: {exc}")

    def test_cache_key_separates_different_parts(self):
        base = cache_key("q", "cat", "cl", "ctx", False)
        for other in (cache_key("q2", "cat", "cl", "ctx", False),
                      cache_key("q", "cat2", "cl", "ctx", False),
                      cache_key("q", "cat", "cl2", "ctx", False),
                      cache_key("q", "cat", "cl", "ctx2", False),
                      cache_key("q", "cat", "cl", "ctx", True)):
            self.assertNotEqual(base, other,
                                msg="two different rewrites must not share a key")


FACT_MANAGER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "fact_manager.py")
_REWRITE = "rewrite_search_query"
_REWRITE_HELPERS = ("_expand_query",)


def _load_rewrite(cache, responder):
    """Exec the real rewrite_search_query against a stub LLM.

    `fact_manager.py` cannot be imported without the DB drivers, so the function
    is lifted with `ast.get_source_segment` — this tests the shipping code. The
    cache is the real `TTLCache` from matching_utils, so the tests below are
    about the call site: that it uses the cache, and what it declines to cache.
    """
    with open(FACT_MANAGER_PY, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    chunks = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == _REWRITE or node.name in _REWRITE_HELPERS
        ):
            chunks.append(ast.get_source_segment(source, node))
            found.add(node.name)
    if _REWRITE not in found:
        raise AssertionError(f"fact_manager.py no longer defines {_REWRITE}")
    for name in _REWRITE_HELPERS:
        if name not in found:
            raise AssertionError(f"fact_manager.py no longer defines {name}")

    import re
    from typing import Optional

    class FakeLogger:
        def __init__(self):
            self.warnings = []

        def warning(self, msg, *a, **kw):
            self.warnings.append(str(msg))

        def debug(self, *a, **kw):
            pass

    ns = {
        "re": re,
        "Optional": Optional,
        "cache_key": cache_key,
        "QUERY_REWRITE_CACHE": cache,
        "get_llm_response": responder,
        "SEARCH_LLM_TIMEOUT": 45.0,
        # rewrite_search_query names the extraction model explicitly now. A
        # NameError on this name is swallowed by the function's own except and
        # silently degraded to the heuristic fallback, so without this stub entry
        # the cache tests would keep passing while testing nothing.
        "EXTRACT_MODEL": "stub-extract-model",
        "logger": FakeLogger(),
    }
    exec("\n\n".join(chunks), ns)
    return ns


class RewriteSearchQueryCallSiteTests(unittest.TestCase):
    """A test of TTLCache is not a test of its call site.

    The bug this guards is a wrong *call*: a cache that exists, is bounded and
    expires, and is never consulted. So these drive the real function and count
    LLM calls.
    """

    ANSWER = '{"keywords": ["ai adoption", "deutsche bank"]}'

    def _responder(self, answer=None, raises=None):
        self.calls = []
        self.models = []

        async def _r(prompt, system="", model="", num_predict=0, timeout=0.0):
            self.calls.append(prompt)
            self.models.append(model)
            if raises is not None:
                raise raises
            return self.ANSWER if answer is None else answer

        return _r

    def _run(self, cache, responder, query, **kw):
        ns = _load_rewrite(cache, responder)
        return asyncio.run(ns[_REWRITE](query, **kw))

    def test_a_repeat_query_does_not_call_the_llm_again(self):
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder()
        first = self._run(cache, responder, "who is running the ai adoption wave two")
        second = self._run(cache, responder, "who is running the ai adoption wave two")
        self.assertEqual(len(self.calls), 1,
                         msg=f"expected one LLM call, got {len(self.calls)}")
        self.assertEqual(first, second)

    def test_the_search_rewrite_calls_the_extraction_model(self):
        """Search rewriting moved onto MEM_EXTRACT_MODEL; prove it took.

        Counted off the value the stub was actually handed, not read from the
        source: the rewrite sits in front of the user, and the model that
        measured 2.3-4.2x faster is the whole reason it was moved. Silently
        inheriting the query model again would keep returning correct keywords
        and cost the user a slower search, with nothing failing.
        """
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder()
        self._run(cache, responder, "who is running the ai adoption wave two")
        self.assertEqual(self.models, ["stub-extract-model"])

    def test_the_returned_list_is_a_copy_of_the_cached_one(self):
        # Callers weight and slice the result. A shared list would let one
        # caller's in-place append rewrite what the next search sees.
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder()
        first = self._run(cache, responder, "who is running the ai adoption wave two")
        first.append(("poisoned", 1.0))
        second = self._run(cache, responder, "who is running the ai adoption wave two")
        self.assertNotIn(("poisoned", 1.0), second,
                         msg="a caller's in-place edit leaked into the cache")

    def test_the_fallback_is_not_cached(self):
        # A timeout is transient. Pinning the degraded heuristic result for the
        # TTL would turn one slow call into a permanently worse search.
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder(raises=RuntimeError("timed out"))
        self._run(cache, responder, "who is running the ai adoption wave two")
        self.assertEqual(len(cache), 0,
                         msg="a failed rewrite must not be cached")

    def test_an_unparseable_answer_is_not_cached(self):
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder(answer="I could not do that.")
        self._run(cache, responder, "who is running the ai adoption wave two")
        self.assertEqual(len(cache), 0,
                         msg="no keywords means the fallback ran, so nothing to cache")

    def test_a_different_filter_is_a_different_question(self):
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder()
        self._run(cache, responder, "who is running the ai adoption wave two",
                  client="Deutsche Bank (DB)")
        self._run(cache, responder, "who is running the ai adoption wave two",
                  client="EPAM")
        self.assertEqual(len(self.calls), 2,
                         msg="the client filter changes the prompt, so it must "
                             "not share a cache entry")

    def test_a_short_query_is_answered_without_the_llm_at_all(self):
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder()
        result = self._run(cache, responder, "correction")
        self.assertEqual(self.calls, [])
        self.assertEqual(result, [("correction", 1.0)])

    def test_expand_bypasses_the_short_circuit_and_is_cached_separately(self):
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder()
        self._run(cache, responder, "Radoslav", expand=True)
        self._run(cache, responder, "Radoslav", expand=False)
        self.assertEqual(len(self.calls), 1,
                         msg="expand=True and expand=False ask different questions")
        self.assertEqual(len(cache), 1)

    def test_fenced_json_is_still_parsed(self):
        cache = TTLCache(ttl=900.0, max_entries=8)
        responder = self._responder(
            answer="```json\n" + self.ANSWER + "\n```")
        result = self._run(cache, responder, "who is running the ai adoption wave two")
        self.assertIn(("ai adoption", 0.85), result)
        self.assertEqual(len(cache), 1)


class MergeDraftBudgetTests(unittest.TestCase):
    """The merge draft's output budget is what the context has left over.

    This one used to be a constant in gui.py, sized from a four-record run, and
    that was the bug: measured on real facts, nemotron-3-nano:4b spends 2,776
    output tokens on four records and 5,657 on twelve, so the 4,000-token budget
    truncated the twelve-record draft before its closing brace. The parse then
    found nothing and an over-budget request came back as a 502.

    These tests call the helper, because the property being pinned is
    arithmetic on measured numbers, and no source assertion can see arithmetic.
    """

    # Measured on real facts, in tokens: the prompt size of cumulative prefixes
    # of the 20 longest facts, and what the draft actually cost to write.
    PROMPT_TOKENS_4 = 3_783
    PROMPT_TOKENS_12 = 8_801
    OUTPUT_TOKENS_4 = 2_776
    OUTPUT_TOKENS_12 = 5_657
    CHARS_PER_TOKEN = 3.0
    CONTEXT_TOKENS = 16_332

    def _budget(self, prompt_tokens, min_predict=3_000):
        return merge_draft_output_budget(
            int(prompt_tokens * self.CHARS_PER_TOKEN),
            context_tokens=self.CONTEXT_TOKENS,
            min_predict=min_predict,
            chars_per_token=self.CHARS_PER_TOKEN,
        )

    def test_the_budget_covers_what_the_largest_allowed_selection_costs(self):
        """The regression: 12 records is what MERGE_MAX_CLUSTER advertises.

        A constant at 4,000 passed a four-record run and failed here, and the
        guard that should have caught it only checked that the prompt fit.
        """
        budget = self._budget(self.PROMPT_TOKENS_12)
        self.assertGreaterEqual(
            budget, self.OUTPUT_TOKENS_12,
            msg="the budget for the largest allowed selection does not cover "
                "the draft that selection actually costs; it will truncate "
                "mid-JSON and surface as a 502",
        )

    def test_a_four_record_draft_still_fits_with_room_to_spare(self):
        budget = self._budget(self.PROMPT_TOKENS_4)
        self.assertGreaterEqual(budget, self.OUTPUT_TOKENS_4)

    def test_the_budget_is_the_whole_remainder(self):
        """No hidden ceiling: whatever the context has left is what it gets.

        A cap here would reintroduce the original defect for the large
        selections that are the reason the budget is dynamic.
        """
        budget = self._budget(500)
        self.assertEqual(budget, self.CONTEXT_TOKENS - 500)

    def test_it_refuses_a_selection_with_no_room_to_write(self):
        """Below the floor nothing can finish, so it is a refusal, not a 502."""
        prompt_tokens = self.CONTEXT_TOKENS - 1_000
        with self.assertRaises(MergeDraftTooLarge) as caught:
            self._budget(prompt_tokens, min_predict=3_000)
        error = caught.exception
        self.assertEqual(error.prompt_tokens, prompt_tokens)
        self.assertEqual(error.available, 1_000)
        self.assertEqual(error.min_predict, 3_000)
        self.assertEqual(
            error.source_chars, 3_000,
            msg="the message quotes how much source text fits, so it has to be "
                "the budget that would have been left",
        )

    def test_the_floor_itself_is_allowed(self):
        """The boundary is inclusive: at exactly the floor it can still write."""
        self.assertEqual(
            self._budget(self.CONTEXT_TOKENS - 3_000, min_predict=3_000), 3_000
        )
        with self.assertRaises(MergeDraftTooLarge):
            # one more token of prompt, so one token less to write with
            self._budget(self.CONTEXT_TOKENS - 2_999, min_predict=3_000)

    def test_the_refusal_is_a_value_error(self):
        """It has to be, so an unhandled one cannot escape as a 500.

        The cost of that is handler ordering in the caller: a ValueError is
        caught before the more specific subclass. That is asserted in
        test_cypher_safety.MergeDraftBudgetTests, where the caller lives.
        """
        self.assertTrue(issubclass(MergeDraftTooLarge, ValueError))


# ---------------------------------------------------------------------------
# Scope-priority search
#
# These call the real functions rather than reading their source: the defect this
# replaces returned nothing at all for a client written the way a human writes
# it ("DB"), and a source assertion about a comparison operator would not notice
# a name that resolves to the wrong client.
# ---------------------------------------------------------------------------

# The real vault's scope list, taken from the graph export (7 clients, 6
# projects). The tests that use it are reproducing measured production data, not
# an idealised fixture -- "Deutsche Bank (DB)" declares "DB", "SAP" and "SAP SE"
# are two separate stored nodes, and only three projects have any edges at all.
REAL_SCOPE_TREE = [
    {"name": "Deutsche Bank (DB)", "contexts": [
        {"name": "AI Enablement Hub"}, {"name": "DB AI Adoption"}]},
    {"name": "SAP SE", "contexts": [{"name": "EA Handover"}]},
    {"name": "Valantic FSA", "contexts": []},
    {"name": "White Cube", "contexts": []},
    {"name": "SAP", "contexts": []},
    {"name": "LC Security", "contexts": []},
    {"name": "EPAM", "contexts": [
        {"name": "MBAG"}, {"name": "MUFG"}, {"name": "PPC"}]},
]

# Verbatim from the vault. Both are FOR_CLIENT "Deutsche Bank (DB)" with NO
# IN_CONTEXT edge -- the classifier dropped the project because this very guard
# said the text did not support it -- so the only thing that separates them for
# a DB AI Adoption search is the text itself.
LOCHAS_TEXT = (
    "**Role:** Managing Director (MD) — Senior leader, direct report to Christian "
    "Reno (DB CIO) / **Company/Team:** Deutsche Bank (DB) — Private Bank / "
    "**Domain:** AI adoption, organizational strategy, Private Bank / Technology, "
    "Data Platforms / **Notes:** Key senior stakeholder for the PBAI adoption "
    "program. Approver for the 'AI Adoption Office' investment and staffing "
    "proposal. Also referenced as 'John Locas' in some contexts (spelling variant)."
)
PORTER_TEXT = (
    "Deutsche Bank regional architecture manager for US. Manages three architects "
    "in the US who came from finance business analyst background. Described as "
    "having 'dead weight' that is useless but means well."
)


class ScopePlanningTests(unittest.TestCase):
    """The client/context a search asked for, resolved to stored spellings."""

    def test_a_declared_abbreviation_resolves(self):
        """The case that used to return an empty list.

        ``resolve_scope_name`` drops any input under SCOPE_NAME_MIN_CHARS, so
        "DB" was never a candidate. The stored name declares the abbreviation, so
        matching it exactly is both precise and cheap.
        """
        client, _ctx, client_ev, _ = plan_search_scope("DB", None, REAL_SCOPE_TREE)
        self.assertEqual(client, "Deutsche Bank (DB)")
        self.assertEqual(client_ev, SCOPE_EVIDENCE_EXACT)

    def test_a_full_name_resolves_to_the_stored_spelling(self):
        client, _ctx, _ev, _ = plan_search_scope("Deutsche Bank", None, REAL_SCOPE_TREE)
        self.assertEqual(client, "Deutsche Bank (DB)")

    def test_a_project_resolves_too(self):
        client, ctx, _c, ctx_ev = plan_search_scope("db", "AI Adoption", REAL_SCOPE_TREE)
        self.assertEqual(client, "Deutsche Bank (DB)")
        self.assertEqual(ctx, "DB AI Adoption")
        self.assertNotEqual(ctx_ev, SCOPE_EVIDENCE_NONE)

    def test_a_project_of_another_client_is_refused(self):
        """Pairing them is the cross-client guess the classifier is documented as
        making; the plan has to not repeat it."""
        client, ctx, _c, ctx_ev = plan_search_scope("EPAM", "DB AI Adoption", REAL_SCOPE_TREE)
        self.assertEqual(client, "EPAM")
        self.assertIsNone(ctx)
        self.assertEqual(ctx_ev, SCOPE_EVIDENCE_NONE)

    def test_sap_and_sap_se_stay_distinct(self):
        """Two stored nodes, and the resolver picks the one asked for."""
        self.assertEqual(plan_search_scope("SAP", None, REAL_SCOPE_TREE)[0], "SAP")
        self.assertEqual(plan_search_scope("SAP SE", None, REAL_SCOPE_TREE)[0], "SAP SE")

    def test_an_unknown_name_is_no_signal_not_an_error(self):
        client, ctx, client_ev, ctx_ev = plan_search_scope("Nonesuch", "Nonesuch", REAL_SCOPE_TREE)
        self.assertIsNone(client)
        self.assertIsNone(ctx)
        self.assertEqual(client_ev, SCOPE_EVIDENCE_NONE)
        self.assertEqual(ctx_ev, SCOPE_EVIDENCE_NONE)

    def test_a_claimed_by_two_clients_is_not_guessed_at(self):
        tree = [
            {"name": "First Bank (FB)", "contexts": []},
            {"name": "Second Bank (FB)", "contexts": []},
        ]
        client, _ctx, ev, _ = plan_search_scope("FB", None, tree)
        self.assertIsNone(client)
        self.assertEqual(ev, SCOPE_EVIDENCE_NONE)


class ScopeStrengthTests(unittest.TestCase):
    """One number per record, and it ranks rather than filters."""

    def _strength(self, *args, **kwargs):
        return scope_strength(*args, **kwargs)

    def test_nothing_requested_means_no_scope_signal(self):
        self.assertEqual(self._strength(None, None, result_client="EPAM", record_text="x"), 0.0)

    def test_a_context_is_the_finer_discriminator(self):
        """Same client, one has the project: the project decides."""
        with_ctx = self._strength("Deutsche Bank (DB)", "DB AI Adoption",
                                  result_client="Deutsche Bank (DB)",
                                  result_context="DB AI Adoption")
        without = self._strength("Deutsche Bank (DB)", "DB AI Adoption",
                                 result_client="Deutsche Bank (DB)")
        self.assertGreater(with_ctx, without)
        self.assertEqual(with_ctx, SCOPE_ASSIGNED * (1 + SCOPE_CONTEXT_WEIGHT))

    def test_assigned_beats_relevant_beats_inferred(self):
        assigned = scope_axis_tier("Deutsche Bank (DB)", "Deutsche Bank (DB)", (), "")
        relevant = scope_axis_tier("Deutsche Bank (DB)", None, ("Deutsche Bank (DB)",), "")
        inferred = scope_axis_tier("Deutsche Bank (DB)", None, (), "we met Deutsche Bank (DB) today")
        self.assertEqual([assigned, relevant, inferred],
                         [SCOPE_TIER_ASSIGNED, SCOPE_TIER_RELEVANT, SCOPE_TIER_INFERRED])
        self.assertGreater(SCOPE_ASSIGNED, SCOPE_RELEVANT)
        self.assertGreater(SCOPE_RELEVANT, SCOPE_INFERRED)

    def test_unrelated_is_a_weak_hit_not_a_miss(self):
        """A misclassified record stays findable -- that was the explicit ask."""
        wrong_client = self._strength("EPAM", None, result_client="SAP SE", record_text="SAP SE work")
        unscoped = self._strength("EPAM", None)
        self.assertEqual(wrong_client, SCOPE_UNSCOPED)
        self.assertEqual(unscoped, SCOPE_UNSCOPED)
        self.assertGreater(SCOPE_UNSCOPED, 0.0)

    def test_a_relevant_only_record_outranks_an_unrelated_one(self):
        """The classifier writes RELEVANT_TO *before* it gives up on a primary, so
        a clientName-only partition would demote exactly these."""
        only_relevant = self._strength("Deutsche Bank (DB)", None,
                                       relevant_client_names=["Deutsche Bank (DB)"])
        self.assertEqual(only_relevant, SCOPE_RELEVANT)
        self.assertGreater(only_relevant, self._strength("Deutsche Bank (DB)", None,
                                                        result_client="SAP SE"))


class ScopeSeparatesTheJohnsTests(unittest.TestCase):
    """The reported case, on the vault's actual text.

    Both records are assigned to Deutsche Bank (DB) and neither has an
    IN_CONTEXT edge, so client scope alone ties them. The project is what the
    user asked to discriminate on, and its presence in the text is the only
    evidence either has.
    """

    def test_only_lochas_text_names_the_project(self):
        self.assertTrue(context_named_in_text("DB AI Adoption", LOCHAS_TEXT))
        self.assertFalse(context_named_in_text("DB AI Adoption", PORTER_TEXT))

    def test_the_other_project_neither_text_names(self):
        """Guards against the inference passing for the wrong reason."""
        self.assertFalse(context_named_in_text("AI Enablement Hub", LOCHAS_TEXT))
        self.assertFalse(context_named_in_text("AI Enablement Hub", PORTER_TEXT))

    def test_lochas_wins_the_db_ai_adoption_search(self):
        lochas = scope_strength("Deutsche Bank (DB)", "DB AI Adoption",
                                result_client="Deutsche Bank (DB)",
                                record_text=LOCHAS_TEXT)
        porter = scope_strength("Deutsche Bank (DB)", "DB AI Adoption",
                                result_client="Deutsche Bank (DB)",
                                record_text=PORTER_TEXT)
        self.assertGreater(lochas, porter)

    def test_the_sap_search_puts_both_johns_on_the_floor(self):
        """Asked for SAP: both DB records are equally irrelevant, and the SAP
        record wins on its own assignment."""
        bonin = scope_strength("SAP SE", None, result_client="SAP SE", record_text="SAP (SGSC) Diligent")
        lochas = scope_strength("SAP SE", None, result_client="Deutsche Bank (DB)",
                                record_text=LOCHAS_TEXT)
        self.assertEqual(bonin, SCOPE_ASSIGNED)
        self.assertEqual(lochas, SCOPE_UNSCOPED)
        self.assertGreater(bonin, lochas)


class ScopeStaysOutOfConfidenceTests(unittest.TestCase):
    """`identity_confidence` is a max(), so any scope term inside it outranks
    identity evidence and authorises a merge against the wrong record."""

    def test_identity_confidence_takes_no_scope_argument(self):
        import inspect
        params = inspect.signature(identity_confidence).parameters
        for name in ("client", "context", "scope", "scope_strength", "requested_client"):
            self.assertNotIn(
                name, params,
                f"identity_confidence grew a {name!r} parameter; it returns "
                f"max(identity_strength, vector_confidence), so a scope term here "
                f"would report a record with no name match as a confirmed identity",
            )

    def test_an_in_scope_record_with_no_name_match_stays_unconfirmed(self):
        # A high raw vector alone saturates vector_confidence, so the assertion is
        # about the two together: whatever the embedding says, a record whose name
        # does not match is not a confirmed identity, and the write gate refuses it.
        confidence, evidence = identity_confidence(
            "Radoslav", name="John Lochas", raw_vector=0.55)
        self.assertLess(confidence, 1.0)
        self.assertNotEqual(evidence, EVIDENCE_EXACT)
        self.assertFalse(people_match_allowed("Radoslav", {"name": "John Lochas", "raw_score": 0.55}, 0.4))


if __name__ == "__main__":
    unittest.main()
