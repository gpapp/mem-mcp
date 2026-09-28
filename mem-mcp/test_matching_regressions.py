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
    VECTOR_CEIL,
    VECTOR_FLOOR,
    cluster_has_core,
    combine_duplicate_signals,
    execute_merge,
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
    SCOPE_EVIDENCE_CONTAINS,
    SCOPE_EVIDENCE_EXACT,
    SCOPE_EVIDENCE_FUZZY,
    SCOPE_EVIDENCE_NONE,
    SCOPE_EVIDENCE_TOKENS,
    client_header_value,
    resolve_scope_name,
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
        self.assertEqual(self.source.count("text_windows("), 2)


if __name__ == "__main__":
    unittest.main()
