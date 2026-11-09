"""The stakeholder roster: role parsing, staleness, and the query behind it.

`roster.py` is standard-library-only, so it is imported and **called** rather
than read — the properties worth guarding here are all "this looks right and is
wrong". A role regex that matches a sentence which merely mentions a job word
invents a title the consultant then repeats in the room. A staleness test
written `gap >= window` is arithmetically correct and makes a window of zero
flag everybody, which is the opposite of what `MEM_ROSTER_STALE_DAYS=0` asks
for.

`roster_manager.py` imports `common`, so it cannot be imported here as it
stands. Rather than lift pieces of it with `ast.get_source_segment`, a stub
`common` is installed in `sys.modules` (the same trick `test_vault_migration.py`
uses) and the real module is imported. That matters because the query it builds
is the part that decides which half of the room appears, and a lifted copy would
not prove the shipped string was the one executed.
"""

import os
import sys
import types
import unittest
from datetime import date, timedelta

import roster


def _stub_common():
    """A `common` module good enough for `roster_manager` to import."""
    module = sys.modules.get("common")
    if module is not None and hasattr(module, "_roster_stub"):
        return module
    stub = types.ModuleType("common")
    stub._roster_stub = True
    stub.get_neo4j = lambda: None
    stub.logger = types.SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None)
    sys.modules["common"] = stub
    return stub


_stub_common()
import roster_manager  # noqa: E402  (after the stub, by necessity)


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def single(self):
        return self._rows[0] if self._rows else None

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
        return [q for session in self.sessions for q, _ in session.queries]

    @property
    def params(self):
        return [p for session in self.sessions for _, p in session.queries]


class ParseRoleTests(unittest.TestCase):
    """A role is read from the body, or it is blank. Never inferred."""

    def test_a_labelled_line_is_read(self):
        self.assertEqual(roster.parse_role("Role: CTO"), "CTO")
        self.assertEqual(roster.parse_role("**Title:** Head of Data"),
                         "Head of Data")
        self.assertEqual(roster.parse_role("- role: Enterprise Architect"),
                         "Enterprise Architect")

    def test_a_predicate_on_the_name_is_read(self):
        self.assertEqual(
            roster.parse_role("Alice is the CTO at Deutsche Bank", "Alice"),
            "CTO")
        self.assertEqual(
            roster.parse_role("Bob works as a data engineer", "Bob"),
            "data engineer")

    def test_a_dash_form_is_read(self):
        self.assertEqual(roster.parse_role("Alice \u2014 CTO", "Alice"), "CTO")
        self.assertEqual(roster.parse_role("Bob - Solution Architect", "Bob"),
                         "Solution Architect")

    def test_a_sentence_that_merely_mentions_a_job_is_not_a_role(self):
        # The failure mode: "the CTO said the migration is late" becomes a
        # title, and the consultant repeats it in the room.
        self.assertIsNone(
            roster.parse_role("The CTO said the migration is late"))
        self.assertIsNone(
            roster.parse_role("We need to hire a data engineer"))

    def test_nothing_readable_is_none_not_a_guess(self):
        self.assertIsNone(roster.parse_role("", "Alice"))
        self.assertIsNone(roster.parse_role(None, "Alice"))
        self.assertIsNone(roster.parse_role("Alice", "Alice"))

    def test_an_over_long_value_is_refused(self):
        long_role = "Role: " + ("x" * (roster.ROLE_MAX_CHARS + 20))
        self.assertIsNone(roster.parse_role(long_role))


class DaysSinceTests(unittest.TestCase):
    def test_a_date_counts_whole_days(self):
        today = date(2026, 5, 20)
        self.assertEqual(roster.days_since("2026-05-10", today), 10)

    def test_a_timestamp_is_cut_to_its_date(self):
        today = date(2026, 5, 20)
        self.assertEqual(roster.days_since("2026-05-10T14:20:00Z", today), 10)

    def test_a_missing_or_unreadable_date_is_none(self):
        for value in (None, "", "not a date", "2026-13-40"):
            self.assertIsNone(roster.days_since(value, date(2026, 5, 20)))


class BuildRosterTests(unittest.TestCase):
    TODAY = date(2026, 5, 20)

    def _people(self):
        return [
            {"id": "p1", "name": "Alice", "text": "Role: CTO",
             "lastSeen": "2026-05-18", "mentionCount": 4},
            {"id": "p2", "name": "Bob", "text": "Bob is a data engineer",
             "lastSeen": "2026-01-01", "mentionCount": 2},
            {"id": "p3", "name": "Carol", "text": "", "mentionCount": 1},
        ]

    def test_the_unmeasured_come_first_then_the_quietest(self):
        # A person with no date at all is the biggest unknown on the roster --
        # nothing recorded is not the same as recently recorded -- so they sort
        # above somebody who was simply mentioned a while ago. The docstring in
        # build_roster says "no date at all, then the oldest" and this pins it.
        rows = roster.build_roster(self._people(), today=self.TODAY)
        self.assertEqual([r["name"] for r in rows], ["Carol", "Bob", "Alice"])

    def test_staleness_is_measured_not_guessed(self):
        rows = {r["name"]: r for r in
                roster.build_roster(self._people(), today=self.TODAY,
                                    stale_days=60)}
        self.assertTrue(rows["Bob"]["stale"])
        self.assertFalse(rows["Alice"]["stale"])
        self.assertFalse(rows["Carol"]["stale"],
                         msg="no date is unmeasured, which is not stale")

    def test_a_zero_window_disables_the_flag(self):
        rows = roster.build_roster(self._people(), today=self.TODAY,
                                   stale_days=0)
        self.assertFalse(any(r["stale"] for r in rows),
                         msg="0 is 'stop warning me', not 'everyone is late': "
                             "gap >= 0 is true of everyone ever mentioned")

    def test_open_actions_are_counted_by_owner(self):
        actions = [{"owner": "Alice"}, {"owner": "alice"},
                   {"owner": "Bob"}, {"owner": None}, {}]
        rows = {r["name"]: r for r in
                roster.build_roster(self._people(), actions=actions,
                                    today=self.TODAY)}
        self.assertEqual(rows["Alice"]["openActions"], 2)
        self.assertEqual(rows["Bob"]["openActions"], 1)
        self.assertEqual(rows["Carol"]["openActions"], 0)

    def test_the_role_comes_from_the_body(self):
        rows = {r["name"]: r for r in
                roster.build_roster(self._people(), today=self.TODAY)}
        self.assertEqual(rows["Alice"]["role"], "CTO")
        self.assertIsNone(rows["Carol"]["role"])

    def test_junk_rows_are_skipped_rather_than_rendered(self):
        rows = roster.build_roster([None, {}, {"name": ""},
                                    {"name": "Alice"}], today=self.TODAY)
        self.assertEqual([r["name"] for r in rows], ["Alice"])


class RenderRosterTests(unittest.TestCase):
    ROWS = [
        {"id": "p1", "name": "Alice", "role": "CTO", "lastSeen": "2026-01-01",
         "daysSince": 100, "mentionCount": 3, "openActions": 2,
         "stale": True, "measured": True},
        {"id": "p2", "name": "Bob", "role": None, "lastSeen": None,
         "daysSince": None, "mentionCount": 1, "openActions": 0,
         "stale": False, "measured": False},
    ]

    def test_an_empty_roster_says_so(self):
        self.assertEqual(roster.render_roster([]), roster.EMPTY)

    def test_columns_appear_only_when_something_can_fill_them(self):
        text = roster.render_roster(self.ROWS)
        self.assertIn("Role", text)
        self.assertIn("Open actions", text)
        bare = roster.render_roster([self.ROWS[1]])
        self.assertNotIn("Role", bare,
                         msg="an all-empty column makes the table look broken")
        self.assertNotIn("Open actions", bare)

    def test_an_unmeasured_person_says_not_recorded(self):
        text = roster.render_roster(self.ROWS)
        self.assertIn("not recorded", text)

    def test_a_stale_person_is_marked_and_named_underneath(self):
        text = roster.render_roster(self.ROWS, stale_days=60)
        self.assertIn("**2026-01-01**", text)
        self.assertIn("1 stakeholder(s) not mentioned in 60 days", text)
        self.assertIn("Alice", text.splitlines()[-1])

    def test_no_stale_line_when_the_window_is_off(self):
        rows = roster.build_roster(
            [{"name": "Alice", "lastSeen": "2020-01-01"}],
            today=date(2026, 5, 20), stale_days=0)
        text = roster.render_roster(rows, stale_days=0)
        self.assertNotIn("not mentioned in", text)


class RosterQueryTests(unittest.TestCase):
    """The query decides which half of the room appears."""

    def test_people_are_constrained_to_the_people_category(self):
        query = roster_manager._query("client")
        self.assertIn("toLower(coalesce(p.category, '')) = 'people'", query,
                      msg="a MENTIONS edge is not a person: link management "
                          "lets a user attach any fact, so the target "
                          "category has to be named explicitly")

    def test_both_scope_halves_are_read(self):
        # A person can be scoped directly, or only reachable through a scoped
        # diary entry that mentions them. A roster that reads one half lists
        # half the room.
        for kind in ("client", "context"):
            query = roster_manager._query(kind)
            self.assertIn("directScope", query)
            self.assertIn("viaScope", query)

    def test_mentions_are_counted_across_the_whole_vault(self):
        query = roster_manager._query("client")
        self.assertIn("count(DISTINCT d) AS mentionCount", query)
        self.assertIn("max(d.timestamp) AS lastSeen", query)

    def test_a_client_and_a_context_query_differ(self):
        self.assertNotEqual(roster_manager._query("client"),
                            roster_manager._query("context"))


class RosterManagerTests(unittest.TestCase):
    def setUp(self):
        self._original = roster_manager.get_neo4j

    def tearDown(self):
        roster_manager.get_neo4j = self._original

    def test_a_roster_without_a_scope_is_refused(self):
        roster_manager.get_neo4j = lambda: _FakeDriver(lambda q, p: None)
        with self.assertRaises(ValueError):
            roster_manager.db_stakeholder_roster("alice")

    def test_an_unreachable_graph_is_reported_not_raised(self):
        roster_manager.get_neo4j = lambda: None
        result = roster_manager.db_stakeholder_roster("alice", client_id="c1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["rows"], [])
        self.assertIn("not reachable", result["markdown"])

    def test_a_failed_query_is_reported_not_raised(self):
        def boom(query, params):
            raise RuntimeError("neo4j went away")

        roster_manager.get_neo4j = lambda: _FakeDriver(boom)
        result = roster_manager.db_stakeholder_roster("alice", client_id="c1")
        self.assertFalse(result["ok"])
        self.assertIn("could not be read", result["markdown"])

    def test_a_roster_is_built_from_the_rows(self):
        rows = [{"id": "p1", "name": "Alice", "text": "Role: CTO",
                 "mentionCount": 3, "lastSeen": "2026-05-01"}]
        driver = _FakeDriver(lambda q, p: _FakeResult(rows))
        roster_manager.get_neo4j = lambda: driver
        result = roster_manager.db_stakeholder_roster(
            "alice", client_id="c1", today="2026-05-20", stale_days=60)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"][0]["name"], "Alice")
        self.assertEqual(result["rows"][0]["role"], "CTO")
        self.assertEqual(result["staleDays"], 60)
        self.assertIn("Alice", result["markdown"])
        self.assertEqual(driver.params[0]["scopeId"], "c1")

    def test_a_context_roster_passes_the_context_id(self):
        driver = _FakeDriver(lambda q, p: _FakeResult([]))
        roster_manager.get_neo4j = lambda: driver
        roster_manager.db_stakeholder_roster("alice", context_id="x1")
        self.assertEqual(driver.params[0]["scopeId"], "x1")
        self.assertIn(":Context", driver.queries[0])

    def test_open_actions_fold_in_without_losing_the_role(self):
        rows = [{"id": "p1", "name": "Alice", "text": "Role: CTO",
                 "mentionCount": 3, "lastSeen": "2026-05-01"}]
        driver = _FakeDriver(lambda q, p: _FakeResult(rows))
        roster_manager.get_neo4j = lambda: driver
        base = roster_manager.db_stakeholder_roster(
            "alice", client_id="c1", today="2026-05-20", stale_days=60)
        merged = roster_manager.attach_open_actions(
            base, [{"owner": "Alice"}, {"owner": "Alice"}])
        self.assertEqual(merged["rows"][0]["openActions"], 2)
        self.assertEqual(merged["rows"][0]["role"], "CTO",
                         msg="the body is gone by now; the parsed role has to "
                             "be carried, not re-derived")

    def test_a_failed_roster_is_passed_through_untouched(self):
        broken = {"ok": False, "rows": [], "stale": [], "staleDays": 0,
                  "markdown": "_x_"}
        self.assertIs(roster_manager.attach_open_actions(broken, []), broken)


class WiringTests(unittest.TestCase):
    """The defects that live one layer away from a green unit test."""

    def setUp(self):
        self.here = os.path.dirname(os.path.abspath(__file__))

    def _read(self, *parts):
        with open(os.path.join(self.here, *parts), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_the_routes_read_the_roster_before_the_actions(self):
        source = self._read("gui.py")
        start = source.index("def _roster_with_actions(")
        block = source[start:start + 1200]
        self.assertLess(block.index("db_stakeholder_roster"),
                        block.index("db_list_action_items"),
                        msg="the graph read is the one that must survive the "
                            "action register being unavailable")

    def test_both_roster_routes_are_mounted(self):
        source = self._read("gui.py")
        self.assertIn('/api/clients/{client_id}/roster', source)
        self.assertIn('/api/contexts/{context_id}/roster', source)

    def test_the_tool_is_registered(self):
        self.assertIn('monitor_mcp_tool("stakeholder_roster"',
                      self._read("mcp_tools.py"))

    def test_the_panel_fetches_the_roster(self):
        # dashboard.html is JavaScript: an ast walk raises SyntaxError on it.
        import test_auth_guard
        body = test_auth_guard._js_function_source("templates/dashboard.html",
                                                   "toggleScopeItems")
        self.assertIn("/roster", body)
        self.assertIn("marked.parse", body)


class EnvWiringTests(unittest.TestCase):
    def setUp(self):
        self.here = os.path.dirname(os.path.abspath(__file__))

    def test_both_knobs_reach_the_container(self):
        root = os.path.dirname(self.here)
        with open(os.path.join(root, ".env.example"), encoding="utf-8") as fh:
            example = fh.read()
        with open(os.path.join(root, "docker-compose.yml"), encoding="utf-8") as fh:
            compose = fh.read()
        for name in ("MEM_ROSTER_STALE_DAYS", "MEM_ROSTER_LIMIT"):
            self.assertIn(name, example,
                          msg=f"{name} is documented nowhere")
            self.assertIn(name, compose,
                          msg=f"{name} is documented but compose never passes "
                              "it, so it sits at its default forever")


if __name__ == "__main__":
    unittest.main()
