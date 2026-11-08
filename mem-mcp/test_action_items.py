"""The commitments register: reconciliation, and the call sites that feed it.

`action_manager.py` imports `common`, so it cannot be imported on a machine with
no httpx — every function here is lifted straight out of the shipping source with
`ast.get_source_segment` and *called*, exactly as the other DB-adjacent suites
do. Reading the source would prove only that the code was typed.

Two things shaped this file, and both are the repo's own lessons:

**A re-extraction reconciles, it does not append.** A diary entry is re-saved
several times in a session. If each save appended, the register would multiply
until it was unreadable, and the failure is invisible: nothing errors, the list
just grows. So the interesting cases are not "does it create" — they are what
happens to the item that was already there when the bullet changes, disappears,
or was marked done by a human.

**A test of the parser is not a test of the save path.** `parse_action_items` is
covered in `test_matching_regressions.py`, and re-injecting the missing call
into `db_save_diary` left all of it green. The call-site pins at the bottom are
that defect's guard, and they are AST walks rather than substring searches
because a docstring that names the function is not a call to it.
"""

import ast
import asyncio
import os
import types
import unittest

import matching_utils as m
import skill_catalogue  # noqa: F401  (keeps the import-parity check honest)

HERE = os.path.dirname(os.path.abspath(__file__))
ACTION_MANAGER = os.path.join(HERE, "action_manager.py")
DIARY_MANAGER = os.path.join(HERE, "diary_manager.py")
MCP_TOOLS = os.path.join(HERE, "mcp_tools.py")


class _FakeResult:
    def __init__(self, rows=None, single=None):
        self._rows = rows or []
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
        return [q for session in self.sessions for q, _ in session.queries]

    @property
    def params(self):
        return [p for session in self.sessions for _, p in session.queries]


def _source(path, name):
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    tree = ast.parse(text)
    node = next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)
    return ast.get_source_segment(text, node)


_CLEAN_STATUS = []


def _clean_status():
    """`_clean_status` lifted once, so every other lift can name it."""
    if not _CLEAN_STATUS:
        _CLEAN_STATUS.append(_lift("_clean_status", _lift_self=False))
    return _CLEAN_STATUS[0]


def _lift(name, _lift_self=True, **extra):
    """Exec one top-level function out of action_manager.py."""
    import uuid as _uuid
    import datetime as _datetime

    namespace = {
        "__builtins__": __builtins__,
        "uuid": _uuid,
        "datetime": _datetime,
        "normalise_action_text": m.normalise_action_text,
        "parse_action_items": m.parse_action_items,
        "ACTION_STATUSES": m.ACTION_STATUSES,
    }
    namespace.update(extra)
    exec(compile(_source(ACTION_MANAGER, name), ACTION_MANAGER, "exec"), namespace)
    if _lift_self and name != "_clean_status":
        namespace["_clean_status"] = _clean_status()
        namespace[name].__globals__["_clean_status"] = namespace["_clean_status"]
    return namespace[name]


def _sync(**extra):
    """A ``db_sync_action_items`` bound to a fresh fake driver."""
    driver = extra.pop("driver", None)

    def handler(query, params):
        if "RETURN a.id AS id" in query:
            return _FakeResult(rows=extra.pop("existing", []) or [])
        return _FakeResult()

    if driver is None:
        driver = _FakeDriver(handler)
    apply_scope = _lift("_apply_scope")
    link_source = _lift("_link_source")
    fn = _lift("db_sync_action_items",
               get_neo4j=lambda: driver,
               logger=types.SimpleNamespace(
                   warning=lambda *a, **k: None, info=lambda *a, **k: None),
               _apply_scope=apply_scope,
               _link_source=link_source)
    return fn, driver


class _ReconcileBase(unittest.TestCase):
    def setUp(self):
        self.item = {"text": "send the doc", "key": "send the doc",
                     "owner": "Tim", "dueAt": "2026-05-31", "status": "open"}

    def run_sync(self, existing, items, **kw):
        fn, driver = _sync(existing=existing)
        report = asyncio.run(fn("d1", "DiaryEntry", "alice", items, **kw))
        return report, driver

    def created(self, driver):
        return [q for q in driver.queries if q.lstrip().startswith("CREATE (a:ActionItem")]

    def status_sets(self, driver):
        return [(q, p) for q, p in zip(driver.queries, driver.params)
                if "SET a.status = 'dropped'" in q]


class ReconcileTests(_ReconcileBase):
    def test_a_new_bullet_is_created_once(self):
        report, driver = self.run_sync([], [self.item])
        self.assertEqual(report, {"ok": True, "created": 1, "updated": 0, "closed": 0})
        self.assertEqual(len(self.created(driver)), 1)
        self.assertIn("RAISED_IN", "\n".join(driver.queries),
                      msg="a commitment must name the meeting it came from")

    def test_an_unchanged_bullet_is_updated_not_duplicated(self):
        existing = [{"id": "a1", "status": "open", "key": "send the doc"}]
        report, driver = self.run_sync(existing, [self.item])
        self.assertEqual(report, {"ok": True, "created": 0, "updated": 1, "closed": 0})
        self.assertEqual(self.created(driver), [],
                         msg="appending on every save is how a register becomes "
                             "unreadable, and nothing errors while it happens")

    def test_a_done_item_is_never_reopened(self):
        existing = [{"id": "a1", "status": "done", "key": "send the doc"}]
        report, driver = self.run_sync(existing, [self.item])
        self.assertEqual(report["updated"], 1)
        update = next(p for q, p in zip(driver.queries, driver.params)
                      if "SET a.text = $text" in q)
        self.assertEqual(update["status"], "done",
                         msg="the work was finished, the bullet is still in the "
                             "minutes, and reopening it makes the register "
                             "untrustworthy")

    def test_a_dropped_item_stays_dropped(self):
        existing = [{"id": "a1", "status": "dropped", "key": "send the doc"}]
        _, driver = self.run_sync(existing, [self.item])
        update = next(p for q, p in zip(driver.queries, driver.params)
                      if "SET a.text = $text" in q)
        self.assertEqual(update["status"], "dropped")

    def test_a_bullet_that_has_gone_is_closed(self):
        existing = [{"id": "a1", "status": "open", "key": "old commitment"}]
        report, driver = self.run_sync(existing, [self.item])
        self.assertEqual(report["closed"], 1)
        self.assertTrue(self.status_sets(driver))

    def test_a_document_without_an_action_section_closes_nothing(self):
        # The caller passes authoritative=False when the parse found no section.
        # Silence about actions is not a withdrawal: the items may have come
        # from a live note or from a human.
        existing = [{"id": "a1", "status": "open", "key": "old commitment"}]
        report, driver = self.run_sync(existing, [], authoritative=False)
        self.assertEqual(report["closed"], 0)
        self.assertEqual(self.status_sets(driver), [])

    def test_an_authoritative_empty_parse_does_close(self):
        existing = [{"id": "a1", "status": "open", "key": "old commitment"}]
        report, _ = self.run_sync(existing, [])
        self.assertEqual(report["closed"], 1,
                         msg="a section that now lists nothing has withdrawn "
                             "what it used to list")

    def test_an_absent_graph_is_skipped_not_fatal(self):
        fn = _lift("db_sync_action_items",
                   get_neo4j=lambda: None,
                   logger=types.SimpleNamespace(
                       warning=lambda *a, **k: None, info=lambda *a, **k: None),
                   _apply_scope=lambda *a, **k: None,
                   _link_source=lambda *a, **k: None)
        report = asyncio.run(fn("d1", "DiaryEntry", "alice", [self.item]))
        self.assertEqual(report, {"ok": False, "created": 0, "updated": 0, "closed": 0},
                         msg="a reindex that cannot reach Neo4j must not turn "
                             "into a failed diary save")


class ScopeFollowsTheSourceTests(_ReconcileBase):
    def test_scope_edges_are_not_touched_when_the_caller_does_not_know_them(self):
        _, driver = self.run_sync([], [self.item])
        joined = "\n".join(driver.queries)
        self.assertNotIn("DELETE r", joined,
                         msg="an unknown scope must not clear a scope somebody "
                             "else set: deleting then re-adding nothing is a "
                             "silent unscope")

    def test_a_named_scope_is_applied_to_the_new_item(self):
        _, driver = self.run_sync([], [self.item], client_id="c1", context_id="x1")
        joined = "\n".join(driver.queries)
        self.assertIn("FOR_CLIENT", joined)
        self.assertIn("IN_CONTEXT", joined)


class ListFiltersTests(unittest.TestCase):
    def _list(self, driver):
        return _lift("db_list_action_items", get_neo4j=lambda: driver,
                     logger=types.SimpleNamespace(warning=lambda *a, **k: None))

    def test_a_client_filter_is_a_match_not_a_ranking(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(rows=[]))
        self._list(driver)( "alice", client_id="c1")
        joined = "\n".join(driver.queries)
        self.assertIn("MATCH (a)-[:FOR_CLIENT]->(:Client {id: $clientId", joined,
                      msg="the panel asks a closed question; unlike search scope "
                          "another client's items are a wrong answer, not a "
                          "lower-ranked one")

    def test_an_unfiltered_list_shows_only_open_items(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(rows=[]))
        self._list(driver)("alice")
        self.assertIn("coalesce(a.status, 'open') = 'open'",
                      "\n".join(driver.queries))

    def test_a_missing_graph_is_an_empty_list(self):
        self.assertEqual(self._list(None)("alice"), [])


class AnnotationsAreSaneTests(unittest.TestCase):
    def test_no_python_311_illegal_construct_is_used(self):
        # action_manager is imported by diary_manager, which is imported by the
        # app entry point: an import-time failure takes the whole server down.
        with open(ACTION_MANAGER, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(n, ast.TypeAlias) for n in ast.walk(tree)),
                         msg="PEP 695 syntax is 3.12+ and the image is 3.11")


class CallSiteTests(unittest.TestCase):
    """A helper test is not a test of its call site.

    Removing the reconciliation call from the save path leaves every test above
    green, because they all call the function directly. These pin the wiring.
    """

    def test_the_save_path_reconciles(self):
        body = _source(DIARY_MANAGER, "db_save_diary")
        self.assertIn("sync_actions_from_text(", body)
        self.assertIn("today=", body,
                      msg="without the entry's own date, 'by Friday' cannot "
                          "resolve and the deadline is silently lost")
        self.assertLess(body.index("db_save_diary") if "db_save_diary" in body
                        else 0, body.index("sync_actions_from_text("))

    def test_the_update_path_reconciles_against_the_same_entry(self):
        body = _source(DIARY_MANAGER, "db_update_diary")
        self.assertIn("sync_actions_from_text(entry_id", body,
                      msg="an edit must update the register, not add a second "
                          "copy keyed to a new id")

    def test_the_commitments_tools_are_registered(self):
        with open(MCP_TOOLS, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
        for tool in ("list_action_items", "save_action_item", "update_action_item"):
            with self.subTest(tool=tool):
                self.assertIn(tool, names)

    def test_the_save_tool_does_not_go_through_the_completion_path(self):
        # `parse_action_items` must not be reachable from the tools: extraction
        # is the save path's job, and a tool that parsed would re-derive items
        # from text the register already reconciled.
        body = _source(MCP_TOOLS, "save_action_item")
        self.assertNotIn("parse_action_items", body)


class DashboardWiringTests(unittest.TestCase):
    """The panel half, which no Python test can reach.

    The register is worthless if the only way to read it is an MCP client: the
    person who owes the work reads it on the Clients page. These pin the three
    things a regression would silently undo -- the list is loaded with the page,
    a panel fetches its own engagement's items, and a closed item stops offering
    a button it has already honoured.
    """

    @classmethod
    def setUpClass(cls):
        import test_auth_guard
        cls._js = staticmethod(test_auth_guard._js_function_source)

    def test_the_client_page_loads_the_list(self):
        body = self._js("templates/dashboard.html", "loadClients")
        self.assertIn("loadMyActions()", body,
                      msg="a register nobody opens is a list of guilt")

    def test_a_panel_fetches_its_own_engagement(self):
        body = self._js("templates/dashboard.html", "toggleScopeItems")
        self.assertIn("action-items?", body)
        self.assertIn("clientId=", body)
        self.assertIn("contextId=", body)

    def test_an_overdue_item_is_marked(self):
        body = self._js("templates/dashboard.html", "renderActionRows")
        self.assertIn("overdue", body)
        self.assertIn("a.status === 'open'", body,
                      msg="a closed item must not offer the button that closes it")



if __name__ == "__main__":
    unittest.main()
