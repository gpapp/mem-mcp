import unittest
from matching_utils import (
    cluster_has_core,
    combine_duplicate_signals,
    execute_merge,
    people_match_allowed,
    resolve_people_candidates,
    scopes_compatible,
    validate_merge_ids,
    validate_merge_records,
)


class MatchingRegressionTests(unittest.TestCase):
    def test_single_name_does_not_match_similar_person(self):
        result = {"name": "Alice Jones", "score": 2.3, "metadata": {}}
        self.assertFalse(people_match_allowed("Alice", result))

    def test_full_name_match_is_retained(self):
        result = {"name": "Alice Smith", "score": 1.7, "metadata": {}}
        self.assertTrue(people_match_allowed("Alice Smith", result))

    def test_alias_match_is_retained(self):
        result = {"name": "Alice Smith", "score": 1.2, "metadata": {"aliases": ["Allie"]}}
        self.assertTrue(people_match_allowed("Allie", result))

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


if __name__ == "__main__":
    unittest.main()
