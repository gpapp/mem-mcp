"""Tests for the deliverables register.

`deliverables.py` has no database in it, so the parsing and labelling rules are
imported and **called**. The properties worth guarding are the ones that are
invisible in the shape of the code: that a status marker is part of the *key*
rule and not of the name, that a re-save keeps a human's status, and that
nothing ever deletes a delivered artifact.

`deliverable_manager.py` needs a driver, so it is exec'd against a stub `common`
installed in `sys.modules`, the same harness shape as `test_decision_log.py`.
"""

import asyncio
import io
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import deliverables as dv  # noqa: E402


class _FakeResult(object):
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None

    def data(self):
        return self._rows


class _FakeSession(object):
    def __init__(self, script=None):
        self.queries = []
        self._script = script or (lambda query, params: _FakeResult([]))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **params):
        self.queries.append((query, params))
        return self._script(query, params)

    def ran(self, needle):
        return [(q, p) for q, p in self.queries if needle in q]


class _FakeDriver(object):
    def __init__(self, session_obj):
        self.session_obj = session_obj

    def session(self):
        return self.session_obj


class _StubLogger(object):
    def __init__(self):
        self.warnings = []
        self.infos = []

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        self.infos.append(a)

    def warning(self, *a, **k):
        self.warnings.append(a)

    def error(self, *a, **k):
        pass


def _load_manager(script=None, driver=True):
    session = _FakeSession(script)
    common = types.ModuleType("common")
    common.get_neo4j = lambda: (_FakeDriver(session) if driver else None)
    common.logger = _StubLogger()

    previous = sys.modules.get("common")
    sys.modules["common"] = common
    namespace = {"__name__": "deliverable_manager_under_test"}
    with io.open(os.path.join(HERE, "deliverable_manager.py"), encoding="utf-8") as fh:
        source = fh.read()
    try:
        exec(compile(source, "deliverable_manager.py", "exec"), namespace)
    finally:
        if previous is None:
            sys.modules.pop("common", None)
        else:
            sys.modules["common"] = previous
    return namespace, session


class ParseDeliverablesTests(unittest.TestCase):
    BODY = """## Kickoff

Some prose.

## Deliverables

- AI readiness assessment — [draft]
- [sent] Target architecture deck
- Data platform roadmap
- Weekly status report

## Actions

- Send the deck
"""

    def test_only_the_deliverables_section_is_read(self):
        items = dv.parse_deliverables(self.BODY)
        self.assertEqual(len(items), 4)
        self.assertNotIn("Send the deck", [i["name"] for i in items])

    def test_a_leading_status_marker_is_read_and_removed_from_the_name(self):
        items = dv.parse_deliverables(self.BODY)
        deck = [i for i in items if "deck" in i["name"]][0]
        self.assertEqual(deck["status"], "sent")
        self.assertEqual(deck["name"], "Target architecture deck")

    def test_a_section_with_no_bullets_yields_nothing(self):
        self.assertEqual(dv.parse_deliverables("## Deliverables\n\nnone yet\n"), [])

    def test_a_document_with_no_section_yields_nothing(self):
        self.assertEqual(dv.parse_deliverables("## Notes\n\n- something\n"), [])

    def test_duplicate_bullets_collapse_on_the_key(self):
        body = "## Deliverables\n\n- Readiness assessment\n- readiness assessment.\n"
        self.assertEqual(len(dv.parse_deliverables(body)), 1)

    def test_the_source_date_becomes_the_delivery_date(self):
        items = dv.parse_deliverables(self.BODY, delivered_at="2026-11-12")
        self.assertEqual({i["deliveredAt"] for i in items}, {"2026-11-12"})


class KindTests(unittest.TestCase):
    def test_a_word_list_picks_the_bucket(self):
        self.assertEqual(dv.infer_kind("AI readiness assessment"), "assessment")
        self.assertEqual(dv.infer_kind("Data platform roadmap"), "roadmap")
        self.assertEqual(dv.infer_kind("Steering committee deck"), "deck")
        self.assertEqual(dv.infer_kind("Findings report"), "report")

    def test_an_unrecognised_name_is_other_rather_than_a_guess(self):
        self.assertEqual(dv.infer_kind("Weekly sync"), "other")
        self.assertEqual(dv.infer_kind(""), "other")

    def test_an_assessment_report_is_an_assessment(self):
        self.assertEqual(dv.infer_kind("readiness assessment report"), "assessment")


class StatusTests(unittest.TestCase):
    def test_spellings_of_in_review_become_one_status(self):
        for value in ("In review", "in-review", "IN_REVIEW", "in review"):
            self.assertEqual(dv.normalise_status(value), "in-review")

    def test_an_unknown_status_falls_back_rather_than_inventing_one(self):
        self.assertEqual(dv.normalise_status("delivered"), dv.DEFAULT_STATUS)
        self.assertEqual(dv.normalise_status(""), dv.DEFAULT_STATUS)

    def test_only_approved_is_terminal(self):
        self.assertEqual(dv.TERMINAL_STATUSES, ("approved",))


class DescribeTests(unittest.TestCase):
    def test_an_undescribed_status_gets_a_readable_label(self):
        item = dv.describe_deliverable({"name": "Deck", "status": "in-review"})
        self.assertEqual(item["statusLabel"], "in review")

    def test_an_unknown_kind_is_normalised(self):
        self.assertEqual(dv.describe_deliverable({"kind": "slides"})["kind"], "other")

    def test_age_is_computed_and_never_stored(self):
        item = dv.describe_deliverable({"deliveredAt": "2026-11-01"}, today="2026-11-12")
        self.assertEqual(item["ageDays"], 11)

    def test_an_undated_deliverable_has_no_age(self):
        item = dv.describe_deliverable({}, today="2026-11-12")
        self.assertFalse(item["delivered"])
        self.assertEqual(item["ageDays"], -1)


class SortTests(unittest.TestCase):
    def test_newest_first_and_undated_last(self):
        rows = [
            {"name": "Old", "deliveredAt": "2026-01-01"},
            {"name": "Undated"},
            {"name": "New", "deliveredAt": "2026-11-12"},
        ]
        self.assertEqual([r["name"] for r in dv.sort_deliverables(rows)],
                         ["New", "Old", "Undated"])


class SyncTests(unittest.TestCase):
    def _sync(self, namespace, *args, **kwargs):
        return asyncio.run(namespace["db_sync_deliverables"](*args, **kwargs))

    def _script(self, existing=None):
        def script(query, params):
            if "sourceId: $sourceId})" in query or "sourceId: $sourceId}\n" in query:
                return _FakeResult(existing or [])
            if "$sourceId" in query and "RETURN d.id AS id" in query:
                return _FakeResult(existing or [])
            if "SET d.name" in query:
                return _FakeResult([{"id": "existing"}])
            return _FakeResult([])
        return script

    def test_a_new_bullet_creates_a_row(self):
        namespace, session = _load_manager(script=self._script())
        result = self._sync(namespace, 
            "d1", "DiaryEntry", "alice",
            [{"name": "Readiness assessment", "key": "readiness assessment",
              "kind": "assessment", "status": "draft", "deliveredAt": "2026-11-12"}],
        )
        self.assertEqual(result["created"], 1)
        self.assertTrue(session.ran("CREATE (d:Deliverable"))

    def test_an_existing_bullet_updates_and_never_recreates(self):
        namespace, session = _load_manager(
            script=self._script([{"id": "x1", "key": "readiness assessment",
                                  "status": "sent"}]))
        result = self._sync(namespace, 
            "d1", "DiaryEntry", "alice",
            [{"name": "Readiness assessment", "key": "readiness assessment",
              "kind": "assessment", "status": "draft", "deliveredAt": ""}],
        )
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(session.ran("CREATE (d:Deliverable"), [])

    def test_a_resave_never_resets_a_human_status(self):
        namespace, session = _load_manager(
            script=self._script([{"id": "x1", "key": "deck", "status": "sent"}]))
        self._sync(namespace, 
            "d1", "DiaryEntry", "alice",
            [{"name": "Deck", "key": "deck", "kind": "deck", "status": "draft"}],
        )
        updates = [q for q, p in session.queries if "SET d.name" in q]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("d.status", updates[0])

    def test_a_delivered_artifact_is_never_deleted(self):
        namespace, session = _load_manager(script=self._script())
        self._sync(namespace, 
            "d1", "DiaryEntry", "alice",
            [{"name": "Deck", "key": "deck", "kind": "deck", "status": "draft"}],
        )
        joined = " ".join(q for q, _ in session.queries)
        self.assertNotIn("DELETE d", joined)
        self.assertNotIn("DETACH DELETE", joined)

    def test_scope_is_replaced_not_added(self):
        namespace, session = _load_manager(script=self._script())
        self._sync(namespace, 
            "d1", "DiaryEntry", "alice",
            [{"name": "Deck", "key": "deck", "kind": "deck", "status": "draft"}],
            client_id="c1",
        )
        self.assertTrue(session.ran("DELETE r"))
        self.assertTrue(session.ran("MERGE (d)-[:FOR_CLIENT]->(c)"))

    def test_the_source_is_linked_with_its_own_relationship(self):
        namespace, session = _load_manager(script=self._script())
        self._sync(namespace, 
            "d1", "DiaryEntry", "alice",
            [{"name": "Deck", "key": "deck", "kind": "deck", "status": "draft"}],
        )
        self.assertTrue(session.ran("DELIVERED_IN"))

    def test_an_unreachable_graph_is_reported_not_raised(self):
        namespace, _ = _load_manager(driver=False)
        result = self._sync(namespace, "d1", "DiaryEntry", "alice", [])
        self.assertFalse(result["ok"])

    def test_the_text_helper_parses_and_delegates(self):
        namespace, session = _load_manager(script=self._script())
        result = asyncio.run(namespace["sync_deliverables_from_text"](
            "d1", "DiaryEntry", "alice",
            "## Deliverables\n\n- Readiness assessment\n",
        ))
        self.assertEqual(result["created"], 1)


class ListTests(unittest.TestCase):
    def test_scope_is_a_match_and_not_a_post_filter(self):
        namespace, session = _load_manager()
        namespace["db_list_deliverables"]("alice", client_id="c1")
        queries = [q for q, _ in session.queries]
        self.assertTrue(any("MATCH (d)-[:FOR_CLIENT]" in q for q in queries))

    def test_a_status_filter_is_normalised(self):
        namespace, session = _load_manager()
        namespace["db_list_deliverables"]("alice", status="In review")
        params = [p for _, p in session.queries]
        self.assertEqual(params[0]["status"], "in-review")

    def test_an_unreachable_graph_lists_nothing_rather_than_failing(self):
        namespace, _ = _load_manager(driver=False)
        self.assertEqual(namespace["db_list_deliverables"]("alice"), [])


class CreateUpdateTests(unittest.TestCase):
    def test_a_nameless_deliverable_is_refused(self):
        namespace, _ = _load_manager()
        self.assertFalse(namespace["db_create_deliverable"]("alice", "  ")["ok"])

    def test_a_manual_entry_defaults_to_draft(self):
        namespace, _ = _load_manager(
            script=lambda q, p: _FakeResult([{"id": "n1", "name": "Deck",
                                              "kind": "deck", "status": "draft",
                                              "deliveredAt": "", "sourceId": ""}]))
        item = namespace["db_create_deliverable"]("alice", "Steering deck")["item"]
        self.assertEqual(item["status"], "draft")

    def test_an_update_with_nothing_to_change_is_refused(self):
        namespace, _ = _load_manager()
        self.assertFalse(namespace["db_update_deliverable"]("x1", "alice")["ok"])

    def test_an_update_cannot_blank_the_name(self):
        namespace, _ = _load_manager()
        self.assertFalse(namespace["db_update_deliverable"]("x1", "alice", name=" ")["ok"])

    def test_an_unknown_id_answers_as_unknown(self):
        namespace, _ = _load_manager()
        result = namespace["db_update_deliverable"]("nope", "alice", status="sent")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
