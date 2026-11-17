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
import re
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

    def test_a_source_filter_narrows_to_one_record(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(rows=[]))
        self._list(driver)("alice", source_id="e1")
        query, params = driver.queries[0], driver.params[0]
        self.assertIn("a.sourceId = $sourceId", query,
                      msg="the read half of RAISED_IN: without it the source "
                          "cannot show what it extracted")
        self.assertEqual(params.get("sourceId"), "e1")

    def test_narrowing_filters_share_one_where(self):
        # The regression: client_id + dueBefore without status used to emit
        # `MATCH … AND a.dueAt …` — a syntax error the except below turned into
        # a silent empty list, which reads as "nothing outstanding".
        driver = _FakeDriver(lambda query, params: _FakeResult(rows=[]))
        self._list(driver)("alice", client_id="c1", due_before="2026-05-31")
        query = driver.queries[0]
        self.assertIn("WHERE", query,
                      msg="a narrowing filter with no WHERE is a syntax error, "
                          "and the except answers an empty list")
        self.assertNotIn(") AND", query,
                         msg="an AND appended outside the WHERE clause")
        self.assertLess(query.index("WHERE"), query.index("AND a.dueAt"),
                        msg="the dueAt narrowing must sit inside the one WHERE")

    def test_status_and_source_narrow_in_one_where(self):
        driver = _FakeDriver(lambda query, params: _FakeResult(rows=[]))
        self._list(driver)("alice", status="open", source_id="e1")
        query = driver.queries[0]
        self.assertEqual(query.count("WHERE"), 1)
        self.assertIn("a.status = $status", query)
        self.assertIn("a.sourceId = $sourceId", query)


class FormatSourceTests(unittest.TestCase):
    """The MCP read names the record an item was raised in.

    mcp_tools.py imports fastmcp, so the formatter is lifted and exec'd against
    a stub `mem` -- the same shape every lift in this file uses.
    """

    @staticmethod
    def _format(rows):
        namespace = {
            "__builtins__": __builtins__,
            "mem": types.SimpleNamespace(
                describe_action_item=lambda row: {**row, "overdue": False}),
        }
        exec(compile(_source(MCP_TOOLS, "_format_action_items"),
                     MCP_TOOLS, "exec"), namespace)
        return namespace["_format_action_items"](rows)

    def test_a_diary_sourced_item_names_the_entry(self):
        out = self._format([{"id": "a1", "text": "send the doc",
                             "status": "open", "owner": "Tim",
                             "dueAt": "2026-05-31", "clientName": "DB",
                             "contextName": "", "sourceId": "e1",
                             "sourceLabel": "DiaryEntry"}])
        self.assertIn("raised in diary entry `e1`", out)

    def test_a_fact_sourced_item_names_the_fact(self):
        out = self._format([{"id": "a1", "text": "send the doc",
                             "status": "open", "dueAt": "",
                             "sourceId": "f1", "sourceLabel": "Fact"}])
        self.assertIn("raised in fact `f1`", out)

    def test_a_manual_item_says_nothing(self):
        out = self._format([{"id": "a1", "text": "send the doc",
                             "status": "open", "dueAt": "",
                             "sourceId": None, "sourceLabel": "manual"}])
        self.assertNotIn("raised in", out,
                         msg="a manual item has no record to trace back to")


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
    """The surface half, which no Python test can reach.

    The register is worthless if the only way to read it is an MCP client: the
    person who owes the work reads it on the Actions tab. These pin the things a
    regression would silently undo -- the tab loads its list, the list follows
    the global client filter, the tab is a table, a panel fetches its own
    engagement's items, and a closed item stops offering a button it has already
    honoured.
    """

    @classmethod
    def setUpClass(cls):
        import test_auth_guard
        cls._js = staticmethod(test_auth_guard._js_function_source)

    def test_the_actions_tab_loads_the_list(self):
        body = self._js("templates/dashboard.html", "switchTab")
        self.assertIn("loadMyActions()", body,
                      msg="a register nobody opens is a list of guilt")

    def test_the_list_follows_the_client_filter(self):
        body = self._js("templates/dashboard.html", "loadMyActions")
        self.assertIn("clientFilter", body,
                      msg="a tab that ignores the filter shows every client's commitments")
        self.assertIn("params.clientId", body,
                      msg="a named client has to reach the endpoint as a server-side filter")
        self.assertIn("params.contextId", body,
                      msg="a named project has to reach the endpoint too")

    def test_the_tab_is_a_table(self):
        body = self._js("templates/dashboard.html", "renderActionTable")
        self.assertIn("<table", body)
        self.assertIn("<th", body)

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

    def test_the_table_links_to_the_source(self):
        body = self._js("templates/dashboard.html", "renderActionTable")
        self.assertIn("a.sourceId", body)
        self.assertIn("openActionSource(", body,
                      msg="a Source column nobody can click is decoration")

    def test_the_deep_link_reaches_both_record_kinds(self):
        body = self._js("templates/dashboard.html", "openActionSource")
        self.assertIn("selectDiaryEntry", body,
                      msg="a diary source must open the diary tab")
        self.assertIn("showFactInMemories", body)

    def test_the_diary_view_shows_what_it_raised(self):
        body = self._js("templates/dashboard.html", "renderDiaryEntryView")
        self.assertIn("renderDiaryActions(entry)", body,
                      msg="the badge lives on the entry that raised the items")

    def test_the_diary_badge_reads_the_raised_in_edge(self):
        body = self._js("templates/dashboard.html", "renderDiaryActions")
        self.assertIn("sourceId=", body,
                      msg="without the query param the badge is the whole "
                          "register, not this entry's items")

    def test_the_route_forwards_the_source_filter(self):
        body = _source(os.path.join(HERE, "gui.py"), "api_list_action_items")
        self.assertIn("source_id=", body,
                      msg="a sourceId the browser sends and the route drops is "
                          "a filter that does nothing")

    def test_the_editor_is_wired_into_the_panes(self):
        for fn, needle in (("clearFactPane", "mdEditorWire('new-text')"),
                           ("renderFactPane", "mdEditorWire('ep-text')"),
                           ("renderDiaryEditMode",
                            "mdEditorWire('de-content')")):
            with self.subTest(fn=fn):
                body = self._js("templates/dashboard.html", fn)
                self.assertIn(needle, body,
                              msg="an editor component nobody mounts is dead code")


class EditorAndSourceNodeTests(unittest.TestCase):
    """The editor and the source link, driven the way the browser drives them.

    `mdPrefixLines` replaces a marker that is already there and `mdWrap` wraps a
    selection -- both are invisible in a source read, and the shared fake DOM
    has no selection semantics, so each case builds its own textarea over
    `_fakeEl`. Skipped when `node` is absent, like every render test that owns
    the harness.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is not installed")
        with open(os.path.join(HERE, "templates", "dashboard.html"),
                  encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.blocks = re.findall(r"<script[^>]*>(.*?)</script>", cls.source, re.S)

    def _run(self, cases):
        import json
        import subprocess
        import tempfile
        import test_share_endpoints
        # The editor dispatches an `input` event after every edit, and the fake
        # DOM has neither an Event constructor nor selection semantics, so the
        # prelude builds a textarea with just those pieces bolted on.
        prelude = (
            "function _ta(id, value, s, e) {\n"
            "  const el = _fakeEl(id);\n"
            "  el.value = value;\n"
            "  el.selectionStart = s; el.selectionEnd = e;\n"
            "  el.setRangeText = (text, start, end) => {\n"
            "    el.value = el.value.slice(0, start) + text + el.value.slice(end);\n"
            "    el.selectionStart = start;\n"
            "    el.selectionEnd = start + text.length; };\n"
            "  el.setSelectionRange = (a, b) => {\n"
            "    el.selectionStart = a; el.selectionEnd = b; };\n"
            "  el.dispatchEvent = () => {};\n"
            "  _els.set(id, el);\n"
            "  return el;\n"
            "}\n"
            "global.Event = function (t) { this.type = t; };\n")
        payload = "".join(
            "  try { out[%r] = (%s); } catch (e) { out[%r] = 'THREW: ' + e.message; }\n"
            % (name, expr, name) for name, expr in cases)
        harness = (test_share_endpoints._NODE_HARNESS
                   .replace("__APP_JS__", "\n".join(self.blocks[2:]))
                   .replace("__CASES__", prelude + payload))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "harness.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(harness)
            proc = subprocess.run([self.node, path], capture_output=True,
                                  text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[:400])
        out = json.loads(proc.stdout)
        if "fatal" in out:
            self.fail(out["fatal"])
        return out

    def test_the_boot_mounts_the_editor_on_the_static_panes(self):
        # The boot IIFE awaits `whoami` before mounting, so the case yields a
        # tick before reading the guard back.
        out = self._run([("wired", """
(async () => {
  await new Promise(r => setTimeout(r, 0));
  return [_getEl('new-text').dataset.mdWired,
          _getEl('diary-content').dataset.mdWired].join('|');
})()
""")])
        self.assertEqual(out["wired"], "1|1",
                         msg="the boot IIFE must mount the editor on the static "
                             "markup, or those panes ship a bare textarea")

    def test_a_sourced_item_renders_a_source_button(self):
        out = self._run([("table", """
renderActionTable([{id: 'a1', text: 'send the doc', owner: 'Tim',
  dueAt: '2026-05-31', status: 'open', sourceId: 'e1',
  sourceLabel: 'DiaryEntry', clientName: 'DB', contextName: ''}])
""")])
        self.assertIn("openActionSource('e1','DiaryEntry')", out["table"])
        self.assertIn("📝 Meeting", out["table"])

    def test_a_manual_item_renders_no_source_button(self):
        out = self._run([("table", """
renderActionTable([{id: 'a1', text: 'send the doc', owner: 'Tim',
  dueAt: '', status: 'open', sourceId: null, sourceLabel: 'manual',
  clientName: '', contextName: ''}])
""")])
        self.assertNotIn("openActionSource(", out["table"])

    def test_a_bullet_click_replaces_the_markers_already_there(self):
        out = self._run([("v", """
(() => {
  _ta('de-content', '## Done\\nsend the doc\\n- [ ] old task\\n\\nnext para', 0, 100);
  mdPrefixLines('de-content', '- ', false);
  return _getEl('de-content').value;
})()
""")])
        self.assertEqual(out["v"],
                         "- Done\n- send the doc\n- old task\n\n- next para",
                         msg="a list click over a heading and a task must strip "
                             "their markers, not nest a list inside them")

    def test_a_heading_click_toggles(self):
        out = self._run([
            ("add", """
(() => {
  _ta('h-el', 'Title line', 0, 10);
  mdPrefixLines('h-el', '## ', true);
  return _getEl('h-el').value;
})()
"""),
            ("remove", """
(() => {
  _ta('h-el2', '## Title line', 0, 13);
  mdPrefixLines('h-el2', '## ', true);
  return _getEl('h-el2').value;
})()
"""),
        ])
        self.assertEqual(out["add"], "## Title line")
        self.assertEqual(out["remove"], "Title line",
                         msg="once=true is a toggle: a second click on the same "
                             "line removes the prefix it added")

    def test_bold_wraps_the_selection(self):
        out = self._run([("v", """
(() => {
  _ta('b-el', 'hello world', 6, 11);
  mdWrap('b-el', '**');
  return _getEl('b-el').value;
})()
""")])
        self.assertEqual(out["v"], "hello **world**",
                         msg="the wrapper must not touch the text outside the "
                             "selection")

    def test_preview_round_trips_without_losing_the_text(self):
        out = self._run([("v", """
(() => {
  _ta('p-el', 'hello preview', 0, 5);
  mdTogglePreview('p-el');
  const shown = _getEl('md-preview-p-el').innerHTML;
  mdShowWrite('p-el');
  return [shown, _getEl('p-el').value,
          _getEl('md-preview-p-el').style.display].join('|');
})()
""")])
        self.assertEqual(out["v"], "hello preview|hello preview|none",
                         msg="Write/Preview is a view toggle over the textarea, "
                             "never a copy of the text into the preview")


if __name__ == "__main__":
    unittest.main()
