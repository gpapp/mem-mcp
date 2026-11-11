"""Tests for the engagement timeline -- the merging rules and the reading half.

``timeline.py`` has no database in it, so it is imported and **called**: the
properties worth guarding there are all about ordering, and ordering is not
visible in the shape of the code. A merge that puts a decision above the meeting
it came from looks exactly like a merge that does not.

``timeline_manager.py`` needs a Neo4j driver this box does not have, so it is
exec'd against stub modules installed in ``sys.modules``. The properties driven
here are the ones a timeline gets wrong silently: a shared panel rendering an
empty decision list as if the engagement had none, a both-scopes call answering
"nothing recorded", and a closed commitment filed under its due date instead of
the day it closed.
"""

import io
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import timeline as tl  # noqa: E402


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
        self.errors = []
        self.infos = []

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        self.infos.append(args)

    def warning(self, *args, **kwargs):
        self.warnings.append(args)

    def error(self, *args, **kwargs):
        self.errors.append(args)


def _load_manager(script=None, scope=None, decisions=None, driver=True):
    """Exec timeline_manager.py with stub collaborators.

    Returns ``(namespace, session, calls)`` where ``calls`` records the decision
    reads, so a test can assert that a shared panel never made one.
    """
    session = _FakeSession(script)
    calls = {"decisions": []}

    common = types.ModuleType("common")
    common.get_neo4j = lambda: (_FakeDriver(session) if driver else None)
    common.logger = _StubLogger()

    client_manager = types.ModuleType("client_manager")
    client_manager._scope_items = lambda user_id, clause, params: scope

    decision_manager = types.ModuleType("decision_manager")

    def _list_decisions(user_id, **kwargs):
        calls["decisions"].append(kwargs)
        return list(decisions or [])

    decision_manager.db_list_decisions = _list_decisions

    saved = {}
    for name, module in (("common", common), ("client_manager", client_manager),
                         ("decision_manager", decision_manager)):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module

    namespace = {"__name__": "timeline_manager_under_test"}
    path = os.path.join(HERE, "timeline_manager.py")
    with io.open(path, encoding="utf-8") as handle:
        source = handle.read()
    try:
        exec(compile(source, "timeline_manager.py", "exec"), namespace)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return namespace, session, calls


def _scope(facts=None, diary=None):
    return {"facts": list(facts or []), "diary": list(diary or [])}


DIARY = [
    {"id": "d1", "date": "2026-11-12", "name": "Kickoff", "timestamp": "2026-11-12T09:00:00"},
    {"id": "d2", "date": "2026-10-04", "name": "Steering", "timestamp": "2026-10-04T15:00:00"},
]


class DayAndMonthTests(unittest.TestCase):
    def test_a_full_timestamp_is_cut_to_its_day(self):
        self.assertEqual(tl.day_of("2026-11-12T10:00:00"), "2026-11-12")

    def test_a_bare_date_is_already_a_day(self):
        self.assertEqual(tl.day_of("2026-11-12"), "2026-11-12")

    def test_an_empty_value_has_no_day(self):
        self.assertEqual(tl.day_of(""), "")
        self.assertEqual(tl.day_of(None), "")

    def test_a_partial_date_is_not_a_day(self):
        self.assertEqual(tl.day_of("2026-11"), "")

    def test_the_month_is_the_first_seven_characters(self):
        self.assertEqual(tl.month_of("2026-11-12T10:00:00"), "2026-11")
        self.assertEqual(tl.month_of(""), "")

    def test_a_month_key_gets_a_readable_label(self):
        self.assertEqual(tl.month_label("2026-11"), "November 2026")
        self.assertEqual(tl.month_label("2026-01"), "January 2026")

    def test_a_month_out_of_range_is_returned_unchanged(self):
        self.assertEqual(tl.month_label("2026-13"), "2026-13")

    def test_an_unparseable_key_is_never_invalid_date(self):
        self.assertEqual(tl.month_label("sometime"), "sometime")
        self.assertEqual(tl.month_label(""), "")


class SortKeyTests(unittest.TestCase):
    def _event(self, at, kind="fact", title="x"):
        return {"at": at, "kind": kind, "title": title}

    def test_an_undated_row_sorts_last_under_reverse(self):
        dated = self._event("2026-01-01")
        undated = self._event("")
        self.assertGreater(tl.sort_key(dated), tl.sort_key(undated))

    def test_a_meeting_leads_its_own_day(self):
        meeting = self._event("2026-11-12T09:00:00", "diary", "Kickoff")
        fact = self._event("2026-11-12T09:00:00", "fact", "Kickoff")
        self.assertGreater(tl.sort_key(meeting), tl.sort_key(fact))

    def test_a_later_time_on_one_day_sorts_first(self):
        late = self._event("2026-11-12T17:00:00")
        early = self._event("2026-11-12T09:00:00")
        self.assertGreater(tl.sort_key(late), tl.sort_key(early))


class BuildEventsTests(unittest.TestCase):
    def test_a_decision_is_nested_into_its_meeting(self):
        payload = tl.build_timeline(
            diary=DIARY,
            decisions=[{"id": "x1", "text": "Use Postgres", "decidedAt": "2026-11-12",
                        "sourceId": "d1"}],
        )
        rows = [r for r in payload["events"] if r["kind"] == "diary"]
        self.assertEqual(len(rows), 2)
        kickoff = [r for r in rows if r["id"] == "d1"][0]
        self.assertEqual([d["title"] for d in kickoff["decisions"]], ["Use Postgres"])

    def test_a_decision_with_no_visible_meeting_becomes_its_own_row(self):
        payload = tl.build_timeline(
            diary=DIARY,
            decisions=[{"id": "x1", "text": "Use Postgres", "decidedAt": "2026-11-12",
                        "sourceId": "elsewhere"}],
        )
        decision_rows = [r for r in payload["events"] if r["kind"] == "decision"]
        self.assertEqual(len(decision_rows), 1)
        self.assertEqual(decision_rows[0]["title"], "Use Postgres")

    def test_the_nested_decisions_read_oldest_first(self):
        payload = tl.build_timeline(
            diary=DIARY,
            decisions=[
                {"id": "x2", "text": "Second", "decidedAt": "2026-11-12T15:00:00",
                 "sourceId": "d1"},
                {"id": "x1", "text": "First", "decidedAt": "2026-11-12T09:00:00",
                 "sourceId": "d1"},
            ],
        )
        kickoff = [r for r in payload["events"] if r["id"] == "d1"][0]
        self.assertEqual([d["title"] for d in kickoff["decisions"]], ["First", "Second"])

    def test_an_unnamed_entry_still_has_a_title(self):
        payload = tl.build_timeline(diary=[{"id": "d9", "date": "2026-11-12", "name": ""}])
        self.assertEqual(payload["events"][0]["title"], "Untitled diary entry")

    def test_participants_survive_the_merge(self):
        payload = tl.build_timeline(
            diary=[{"id": "d1", "date": "2026-11-12", "name": "Kickoff",
                    "participants": ["Alice", "Bob"]}]
        )
        self.assertEqual(payload["events"][0]["participants"], ["Alice", "Bob"])

    def test_a_long_fact_title_is_truncated_without_cutting_mid_word(self):
        payload = tl.build_timeline(facts=[{"id": "f1", "name": "", "text": "A" * 300,
                                          "timestamp": "2026-11-12"}])
        title = payload["events"][0]["title"]
        self.assertTrue(title.endswith("..."))
        self.assertLessEqual(len(title), 140)

    def test_every_row_carries_its_source(self):
        payload = tl.build_timeline(
            diary=DIARY,
            decisions=[{"id": "x1", "text": "Use Postgres", "decidedAt": "2026-11-12",
                        "sourceId": "d1"}],
            actions=[{"id": "a1", "text": "Send the deck", "closedAt": "2026-11-13",
                      "sourceId": "d1"}],
            facts=[{"id": "f1", "name": "Roster", "timestamp": "2026-11-14"}],
        )
        actions = [r for r in payload["events"] if r["kind"] == "action"]
        self.assertEqual(actions[0]["sourceId"], "d1")


class GroupByMonthTests(unittest.TestCase):
    def _events(self, count, month="2026-11"):
        return [{"id": "e%d" % i, "at": "%s-%02d" % (month, i + 1), "kind": "fact",
                 "title": "t%d" % i, "sourceId": ""} for i in range(count)]

    def test_groups_are_newest_month_first(self):
        events = [
            {"id": "a", "at": "2026-10-04", "kind": "fact", "title": "a", "sourceId": ""},
            {"id": "b", "at": "2026-11-12", "kind": "fact", "title": "b", "sourceId": ""},
        ]
        groups = tl.group_by_month(events)
        self.assertEqual([g["label"] for g in groups], ["November 2026", "October 2026"])

    def test_the_cap_is_applied_before_grouping(self):
        groups = tl.group_by_month(self._events(9), limit=3)
        self.assertEqual(sum(len(g["events"]) for g in groups), 3)

    def test_undated_rows_share_one_final_group(self):
        events = [
            {"id": "a", "at": "2026-11-12", "kind": "fact", "title": "a", "sourceId": ""},
            {"id": "b", "at": "", "kind": "fact", "title": "b", "sourceId": ""},
            {"id": "c", "at": "", "kind": "fact", "title": "c", "sourceId": ""},
        ]
        groups = tl.group_by_month(events)
        self.assertEqual(groups[-1]["label"], "Undated")
        self.assertEqual(len(groups[-1]["events"]), 2)

    def test_no_events_produces_no_groups(self):
        self.assertEqual(tl.group_by_month([]), [])


class SummariseTests(unittest.TestCase):
    def test_the_summary_counts_every_kind(self):
        payload = tl.build_timeline(
            diary=DIARY,
            decisions=[{"id": "x1", "text": "Use Postgres", "decidedAt": "2026-11-12",
                        "sourceId": "d1"},
                       {"id": "x2", "text": "Ship in May", "decidedAt": "2026-10-04",
                        "sourceId": "gone"}],
            actions=[{"id": "a1", "text": "Send the deck", "updatedAt": "2026-11-13",
                      "sourceId": "d1"}],
            facts=[{"id": "f1", "name": "Roster", "timestamp": "2026-11-14"}],
        )
        summary = payload["summary"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["diary"], 2)
        self.assertEqual(summary["decision"], 2)
        self.assertEqual(summary["action"], 1)
        self.assertEqual(summary["fact"], 1)

    def test_the_first_and_last_dates_span_the_stream(self):
        payload = tl.build_timeline(diary=DIARY)
        summary = payload["summary"]
        self.assertEqual(summary["last"], "2026-11-12")
        self.assertEqual(summary["first"], "2026-10-04")

    def test_undated_rows_are_counted_separately(self):
        payload = tl.build_timeline(facts=[{"id": "f1", "name": "Roster"}])
        self.assertEqual(payload["summary"]["undated"], 1)


class CleanLimitTests(unittest.TestCase):
    def test_a_missing_or_bad_limit_falls_back_to_the_default(self):
        namespace, _, _ = _load_manager()
        clean = namespace["_clean_limit"]
        self.assertEqual(clean(None), namespace["DEFAULT_LIMIT"])
        self.assertEqual(clean("abc"), namespace["DEFAULT_LIMIT"])
        self.assertEqual(clean(0), namespace["DEFAULT_LIMIT"])
        self.assertEqual(clean(-4), namespace["DEFAULT_LIMIT"])

    def test_a_positive_limit_is_used(self):
        namespace, _, _ = _load_manager()
        self.assertEqual(namespace["_clean_limit"](25), 25)


class RecentFactsTests(unittest.TestCase):
    def test_the_newest_fact_comes_first_not_the_alphabetically_first(self):
        namespace, _, _ = _load_manager()
        facts = [
            {"id": "a", "name": "Aardvark", "timestamp": "2025-01-01"},
            {"id": "z", "name": "Zebra", "timestamp": "2026-11-12"},
        ]
        ordered = namespace["_recent_facts"](facts, 10)
        self.assertEqual([f["id"] for f in ordered], ["z", "a"])

    def test_an_undated_fact_trails_the_dated_ones(self):
        namespace, _, _ = _load_manager()
        facts = [{"id": "n", "name": "NoDate"}, {"id": "d", "name": "Dated",
                                                 "timestamp": "2026-11-12"}]
        ordered = namespace["_recent_facts"](facts, 10)
        self.assertEqual([f["id"] for f in ordered], ["d", "n"])


class EngagementTimelineTests(unittest.TestCase):
    def _run(self, **kwargs):
        namespace, session, calls = _load_manager(**kwargs)
        return namespace["db_engagement_timeline"], session, calls

    def test_two_scopes_is_refused(self):
        call, _, _ = self._run(scope=_scope())
        with self.assertRaises(ValueError) as caught:
            call("alice", client_id="c1", context_id="x1")
        self.assertIn("not both", str(caught.exception))

    def test_no_scope_is_refused(self):
        call, _, _ = self._run(scope=_scope())
        with self.assertRaises(ValueError):
            call("alice")

    def test_an_unreachable_graph_is_a_runtime_error(self):
        call, _, _ = self._run(scope=_scope(), driver=False)
        with self.assertRaises(RuntimeError):
            call("alice", client_id="c1")

    def test_an_unknown_scope_is_refused_rather_than_answer_empty(self):
        call, _, _ = self._run(scope=None)
        with self.assertRaises(ValueError) as caught:
            call("alice", client_id="nope")
        self.assertIn("No such client", str(caught.exception))

    def test_a_shared_panel_shows_the_records_and_says_what_is_missing(self):
        call, session, calls = self._run(
            scope=_scope(diary=[{"id": "d1", "date": "2026-11-12", "name": "Kickoff",
                                 "timestamp": "2026-11-12T09:00:00", "readOnly": True}])
        )
        payload = call("alice", client_id="c1")
        self.assertTrue(payload["shared"])
        self.assertIn("shared with you", payload["note"])
        self.assertEqual(session.ran("ActionItem"), [])
        self.assertEqual(calls["decisions"], [])

    def test_a_shared_panel_still_shows_the_meetings(self):
        call, _, _ = self._run(
            scope=_scope(diary=[{"id": "d1", "date": "2026-11-12", "name": "Kickoff",
                                 "timestamp": "2026-11-12T09:00:00", "readOnly": True}])
        )
        payload = call("alice", client_id="c1")
        self.assertEqual([e["title"] for e in payload["events"]], ["Kickoff"])

    def test_a_closed_commitment_appears_on_its_closing_day(self):
        def script(query, params):
            if "ActionItem" in query:
                return _FakeResult([{"id": "a1", "text": "Send the deck", "owner": "Alice",
                                     "status": "done", "updatedAt": "2026-11-13T10:00:00",
                                     "sourceId": "d1"}])
            return _FakeResult([])

        call, _, _ = self._run(script=script, scope=_scope())
        payload = call("alice", client_id="c1")
        actions = [e for e in payload["events"] if e["kind"] == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["at"], "2026-11-13T10:00:00")
        self.assertEqual(actions[0]["title"], "Send the deck")

    def test_the_decision_log_is_read_for_a_non_shared_panel(self):
        call, _, calls = self._run(
            scope=_scope(),
            decisions=[{"id": "x1", "text": "Use Postgres", "decidedAt": "2026-11-12",
                        "sourceId": "d1"}],
        )
        payload = call("alice", client_id="c1")
        self.assertEqual(len(calls["decisions"]), 1)
        self.assertEqual(calls["decisions"][0]["client_id"], "c1")
        self.assertEqual([e["title"] for e in payload["events"]], ["Use Postgres"])

    def test_a_project_timeline_passes_the_context_not_the_client(self):
        call, _, calls = self._run(scope=_scope())
        call("alice", context_id="x1")
        self.assertIsNone(calls["decisions"][0]["client_id"])
        self.assertEqual(calls["decisions"][0]["context_id"], "x1")

    def test_participants_are_read_from_the_mentions_edges(self):
        def script(query, params):
            if "MENTIONS" in query:
                return _FakeResult([
                    {"id": "d1", "name": "Alice", "text": ""},
                    {"id": "d1", "name": "", "text": "Bob Smith"},
                    {"id": "d1", "name": "Alice", "text": ""},
                ])
            return _FakeResult([])

        call, _, _ = self._run(script=script, scope=_scope(diary=[DIARY[0]]))
        payload = call("alice", client_id="c1")
        meeting = [e for e in payload["events"] if e["id"] == "d1"][0]
        self.assertEqual(meeting["participants"], ["Alice", "Bob Smith"])

    def test_participants_are_capped_per_entry(self):
        def script(query, params):
            if "MENTIONS" in query:
                return _FakeResult([{"id": "d1", "name": "Person %d" % i, "text": ""}
                                    for i in range(30)])
            return _FakeResult([])

        namespace, _, _ = _load_manager(script=script, scope=_scope(diary=[DIARY[0]]))
        payload = namespace["db_engagement_timeline"]("alice", client_id="c1")
        meeting = [e for e in payload["events"] if e["id"] == "d1"][0]
        self.assertEqual(len(meeting["participants"]), namespace["MAX_PARTICIPANTS"])

    def test_the_payload_names_the_scope_it_answered_for(self):
        call, _, _ = self._run(scope=_scope())
        payload = call("alice", client_id="c1")
        self.assertEqual(payload["clientId"], "c1")
        self.assertIsNone(payload["contextId"])
        self.assertFalse(payload["shared"])


if __name__ == "__main__":
    unittest.main()
