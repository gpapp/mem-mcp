"""Tests for the :Decision register.

`decision_manager.py` imports `common`, so it cannot be imported directly on a
box with no database driver. The source is exec'd instead, with a stub `common`
installed in `sys.modules` -- the same approach `test_roster.py` uses, and for
the same reason: the properties worth guarding here are behavioural (what
happens to a row that already exists, and what happens when a supersession
sentence is ambiguous), and none of them is visible in the shape of the code.

The parser half is pure and is **called** rather than read. `matching_utils.py`
has no app imports, so `parse_decisions` is the real function from the shipping
module.
"""

import io
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import matching_utils as mu


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows or [])

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None

    def data(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, script):
        self._script = script
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.queries.append((query, params))
        return self._script(query, params)

    def ran(self, needle):
        return [(q, p) for q, p in self.queries if needle in q]


class _FakeDriver:
    def __init__(self, script):
        self.session_obj = _FakeSession(script)

    def session(self):
        return self.session_obj


class _StubLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        pass


def _load_manager(script):
    """exec `decision_manager.py` against a stub `common`.

    Returns `(module_globals, session)`. The real module is not imported, so
    nothing is left behind in `sys.modules`.
    """
    driver = _FakeDriver(script)
    stub = types.ModuleType("common")
    stub.get_neo4j = lambda: driver
    stub.logger = _StubLogger()

    previous = sys.modules.get("common")
    sys.modules["common"] = stub
    try:
        with io.open(os.path.join(HERE, "decision_manager.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        namespace = {"__name__": "decision_manager_under_test"}
        exec(compile(source, "decision_manager.py", "exec"), namespace)
    finally:
        if previous is not None:
            sys.modules["common"] = previous
        else:
            sys.modules.pop("common", None)
    return namespace, driver.session_obj


def _empty_script(query, params):
    return _FakeResult([])


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------

class ParseDecisionsTests(unittest.TestCase):

    def test_a_bullet_under_a_decisions_heading_is_a_decision(self):
        text = "## Decisions\n\n- We will use Postgres for the ledger.\n"
        items = mu.parse_decisions(text, decided_at="2026-05-31")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "We will use Postgres for the ledger.")
        self.assertEqual(items[0]["decidedAt"], "2026-05-31")
        self.assertEqual(items[0]["status"], "active")

    def test_an_actions_section_contributes_nothing(self):
        """`## Actions` is full of things somebody decided to do.

        Reading those as decisions fills the log with obligations nobody chose
        and buries the actual decisions among them.
        """
        text = "## Actions\n\n- Tim: send the architecture doc by 2026-05-31\n"
        self.assertEqual(mu.parse_decisions(text), [])

    def test_only_bullets_are_read(self):
        """A paragraph under `## Decisions` is prose, not a log entry."""
        text = ("## Decisions\n\nWe discussed several options at length and did "
                "not converge.\n\n- We will use Postgres.\n")
        items = mu.parse_decisions(text)
        self.assertEqual([i["text"] for i in items],
                         ["We will use Postgres."])

    def test_the_entry_date_is_the_default_and_the_wall_clock_is_never_used(self):
        """A decision taken in March was taken in March.

        Measuring from the wall clock would date a three-week-old decision
        today, which is the one field the reader trusts to be exact.
        """
        items = mu.parse_decisions("## Decisions\n- Use Postgres\n",
                                   decided_at="2026-03-14")
        self.assertEqual(items[0]["decidedAt"], "2026-03-14")
        self.assertIsNone(mu.parse_decisions("## Decisions\n- Use Postgres\n")[0]["decidedAt"])

    def test_a_decider_prefix_is_read(self):
        items = mu.parse_decisions("## Decisions\n- Decider: Alice: Use Postgres\n")
        self.assertEqual(items[0]["decider"], "Alice")
        self.assertEqual(items[0]["text"], "Use Postgres")

    def test_a_decider_tail_is_read_with_its_attribution_date(self):
        items = mu.parse_decisions(
            "## Decisions\n- Migrate to Postgres \u2014 Alice, 2026-05-31\n")
        self.assertEqual(items[0]["decider"], "Alice")
        self.assertEqual(items[0]["decidedAt"], "2026-05-31")
        self.assertEqual(items[0]["text"], "Migrate to Postgres")

    def test_a_label_word_is_not_a_decider(self):
        """`- Note: the migration is late` is not signed off by Note."""
        items = mu.parse_decisions("## Decisions\n- Note: the migration is late\n")
        self.assertIsNone(items[0]["decider"])
        self.assertIn("migration", items[0]["text"])

    def test_a_written_marker_sets_the_status(self):
        items = mu.parse_decisions(
            "## Decisions\n- [reversed] We will self-host the search index\n")
        self.assertEqual(items[0]["status"], "reversed")
        self.assertEqual(items[0]["text"], "We will self-host the search index")

    def test_a_superseded_by_sentence_is_captured_as_text(self):
        """The link is resolved later, against real rows; the parser records
        only what a human wrote."""
        items = mu.parse_decisions(
            "## Decisions\n- Use MySQL (superseded by: We use Postgres)\n")
        self.assertEqual(items[0]["supersededByText"], "We use Postgres")
        self.assertEqual(items[0]["text"], "Use MySQL")

    def test_the_key_ignores_attribution_so_changing_the_decider_does_not_fork_it(self):
        """Changing who signed a decision off is not a new decision."""
        a = mu.parse_decisions("## Decisions\n- Use Postgres \u2014 Alice\n")[0]["key"]
        b = mu.parse_decisions("## Decisions\n- Use Postgres \u2014 Bob\n")[0]["key"]
        self.assertEqual(a, b)

    def test_a_date_inside_the_sentence_is_part_of_the_decision(self):
        """"extend the contract to 2027-01-01" is not "extend the contract"."""
        a = mu.normalise_decision_text("Extend the contract to 2027-01-01")
        b = mu.normalise_decision_text("Extend the contract")
        self.assertNotEqual(a, b)

    def test_duplicates_within_one_record_collapse(self):
        items = mu.parse_decisions(
            "## Decisions\n- Use Postgres\n- use postgres.\n")
        self.assertEqual(len(items), 1)

    def test_a_checkbox_is_stripped(self):
        items = mu.parse_decisions("## Decisions\n- [x] Use Postgres\n")
        self.assertEqual(items[0]["text"], "Use Postgres")

    def test_an_empty_body_is_empty(self):
        self.assertEqual(mu.parse_decisions(""), [])
        self.assertEqual(mu.parse_decisions(None), [])

    def test_the_section_name_is_recorded(self):
        items = mu.parse_decisions("### Key Decisions\n- Use Postgres\n")
        self.assertEqual(items[0]["section"], "key decisions")

    def test_every_status_is_a_known_one(self):
        for status in mu.DECISION_STATUSES:
            items = mu.parse_decisions(f"## Decisions\n- [{status}] Use Postgres\n")
            self.assertEqual(items[0]["status"], status)


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

def _sync_script(existing=None, link_rows=None):
    """A scripted session for one `db_sync_decisions` call."""
    existing = list(existing or [])
    link_rows = list(link_rows or [])

    def script(query, params):
        if "sourceId: $sourceId})" in query and "RETURN d.id AS id" in query:
            return _FakeResult(existing)
        if "RETURN d.id AS id, coalesce(d.key, '') AS key" in query:
            return _FakeResult(link_rows)
        return _FakeResult([])

    return script


class SyncDecisionsTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_new_bullet_creates_a_row(self):
        ns, session = _load_manager(_sync_script())
        items = mu.parse_decisions("## Decisions\n- Use Postgres\n",
                                   decided_at="2026-05-31")
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", items)
        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(session.ran("CREATE (d:Decision")), 1)

    async def test_an_existing_bullet_updates_in_place(self):
        """Re-extraction reconciles; it does not append.

        A second copy of the same decision is indistinguishable from a decision
        that was taken twice, and the log is read as a history.
        """
        existing = [{"id": "d1",
                     "key": mu.normalise_decision_text("Use Postgres"),
                     "status": "active"}]
        ns, session = _load_manager(_sync_script(existing=existing))
        items = mu.parse_decisions("## Decisions\n- Use Postgres\n")
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", items)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(session.ran("CREATE (d:Decision")), 0)

    async def test_a_superseded_row_is_not_reopened_by_a_resave(self):
        """Somebody said this decision stopped applying.

        Re-saving the minutes that contain the original bullet must not quietly
        undo that, or the log starts claiming a superseded decision is current.
        """
        existing = [{"id": "d1",
                     "key": mu.normalise_decision_text("Use MySQL"),
                     "status": "superseded"}]
        ns, session = _load_manager(_sync_script(existing=existing))
        items = mu.parse_decisions("## Decisions\n- Use MySQL\n")
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", items)
        self.assertEqual(result["updated"], 1)
        update = session.ran("SET d.text = $text")[0]
        self.assertEqual(update[1]["status"], "superseded")

    async def test_a_withdrawn_row_is_revived_when_the_bullet_returns(self):
        """"withdrawn" means this record stopped mentioning it, which says
        nothing about whether it is still in force."""
        existing = [{"id": "d1",
                     "key": mu.normalise_decision_text("Use Postgres"),
                     "status": "withdrawn"}]
        ns, session = _load_manager(_sync_script(existing=existing))
        items = mu.parse_decisions("## Decisions\n- Use Postgres\n")
        await ns["db_sync_decisions"]("entry-1", "DiaryEntry", "alice", items)
        update = session.ran("SET d.text = $text")[0]
        self.assertEqual(update[1]["status"], "active")

    async def test_silence_withdraws_nothing(self):
        """A record with no decisions section says nothing about decisions.

        Treating that as a withdrawal would empty the register the first time
        somebody re-saved a file under a different heading.
        """
        existing = [{"id": "d1", "key": "use postgres", "status": "active"}]
        ns, session = _load_manager(_sync_script(existing=existing))
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", [], authoritative=False)
        self.assertEqual(result["withdrawn"], 0)
        self.assertEqual(len(session.ran("SET d.status = 'withdrawn'")), 0)

    async def test_an_authoritative_empty_section_does_withdraw(self):
        """The section exists and now lists nothing -- that is a statement."""
        existing = [{"id": "d1", "key": "use postgres", "status": "active"}]
        ns, session = _load_manager(_sync_script(existing=existing))
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", [], authoritative=True)
        self.assertEqual(result["withdrawn"], 1)

    async def test_a_terminal_row_is_never_withdrawn(self):
        """`superseded` already says why it stopped applying.

        Rewriting it to `withdrawn` would replace the reason with a weaker one.
        """
        existing = [{"id": "d1", "key": "use mysql", "status": "superseded"}]
        ns, session = _load_manager(_sync_script(existing=existing))
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", [], authoritative=True)
        self.assertEqual(result["withdrawn"], 0)

    async def test_an_unavailable_graph_does_not_fail_the_save(self):
        """A diary save must not die because the register could not be updated."""
        ns, _session = _load_manager(_sync_script())
        ns["get_neo4j"] = lambda: None
        items = mu.parse_decisions("## Decisions\n- Use Postgres\n")
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", items)
        self.assertFalse(result["ok"])
        self.assertEqual(result["created"], 0)

    async def test_the_source_link_is_written(self):
        """Without provenance, "who decided this?" cannot be answered."""
        ns, session = _load_manager(_sync_script())
        items = mu.parse_decisions("## Decisions\n- Use Postgres\n")
        await ns["db_sync_decisions"]("entry-1", "DiaryEntry", "alice", items)
        link = session.ran("MERGE (d)-[:DECIDED_IN]->(s)")
        self.assertEqual(len(link), 1)
        self.assertEqual(link[0][1]["sourceId"], "entry-1")


# ---------------------------------------------------------------------------
# supersession
# ---------------------------------------------------------------------------

class SupersedeLinkTests(unittest.IsolatedAsyncioTestCase):

    async def test_an_unambiguous_reference_is_linked(self):
        """A sentence a human wrote, resolved against one real row."""
        target_key = mu.normalise_decision_text("We use Postgres")
        existing = [{"id": "d1",
                     "key": mu.normalise_decision_text("Use MySQL"),
                     "status": "active"}]
        ns, session = _load_manager(
            _sync_script(existing=existing,
                         link_rows=[{"id": "d2", "key": target_key}]))
        items = mu.parse_decisions(
            "## Decisions\n- Use MySQL (superseded by: We use Postgres)\n")
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", items)
        self.assertEqual(result["linked"], 1)
        setter = session.ran("SET d.supersededBy = $targetId")
        self.assertEqual(setter[0][1]["targetId"], "d2")

    async def test_an_ambiguous_reference_is_left_unlinked(self):
        """Two candidates is not a decision about which one replaced what.

        A confidently wrong link is worse than none, because the whole point of
        the field is that it can be trusted.
        """
        target_key = mu.normalise_decision_text("We use Postgres")
        existing = [{"id": "d1",
                     "key": mu.normalise_decision_text("Use MySQL"),
                     "status": "active"}]
        ns, session = _load_manager(
            _sync_script(existing=existing,
                         link_rows=[{"id": "d2", "key": target_key},
                                    {"id": "d3", "key": target_key}]))
        items = mu.parse_decisions(
            "## Decisions\n- Use MySQL (superseded by: We use Postgres)\n")
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", items)
        self.assertEqual(result["linked"], 0)
        self.assertEqual(session.ran("SET d.supersededBy = $targetId"), [])
        self.assertTrue(any("matched 2 decisions" in w
                            for w in ns["logger"].warnings))

    async def test_a_reference_to_nothing_is_left_unlinked(self):
        existing = [{"id": "d1",
                     "key": mu.normalise_decision_text("Use MySQL"),
                     "status": "active"}]
        ns, session = _load_manager(_sync_script(existing=existing))
        items = mu.parse_decisions(
            "## Decisions\n- Use MySQL (superseded by: We use Postgres)\n")
        result = await ns["db_sync_decisions"](
            "entry-1", "DiaryEntry", "alice", items)
        self.assertEqual(result["linked"], 0)

    async def test_an_explicit_call_refuses_self_supersession(self):
        ns, _session = _load_manager(_empty_script)
        result = ns["db_supersede_decision"]("d1", "d1", "alice")
        self.assertFalse(result["ok"])
        self.assertIn("itself", result["reason"])

    async def test_an_explicit_call_refuses_missing_ids(self):
        ns, _session = _load_manager(_empty_script)
        self.assertFalse(ns["db_supersede_decision"]("", "d2", "alice")["ok"])
        self.assertFalse(ns["db_supersede_decision"]("d1", "", "alice")["ok"])

    async def test_an_explicit_call_refuses_a_decision_outside_the_vault(self):
        """Ownership is in the MATCH, so a stale id answers like an unknown one."""
        ns, _session = _load_manager(_empty_script)
        result = ns["db_supersede_decision"]("d1", "d2", "alice")
        self.assertFalse(result["ok"])
        self.assertIn("no such decision", result["reason"])


# ---------------------------------------------------------------------------
# update / list
# ---------------------------------------------------------------------------

class UpdateDecisionTests(unittest.TestCase):

    def test_an_empty_text_is_refused(self):
        ns, _session = _load_manager(_empty_script)
        result = ns["db_update_decision"]("d1", "alice", text="   ")
        self.assertFalse(result["ok"])

    def test_nothing_to_update_is_refused(self):
        """A call that changes nothing and reports success is a write nobody
        can see the result of."""
        ns, _session = _load_manager(_empty_script)
        self.assertFalse(ns["db_update_decision"]("d1", "alice")["ok"])

    def test_a_missing_row_is_not_found(self):
        ns, _session = _load_manager(_empty_script)
        result = ns["db_update_decision"]("d1", "alice", status="reversed")
        self.assertFalse(result["ok"])
        self.assertIn("no such decision", result["reason"])

    def test_the_match_carries_the_owner(self):
        ns, session = _load_manager(
            lambda q, p: _FakeResult([{"id": "d1"}]))
        ns["db_update_decision"]("d1", "alice", status="reversed")
        query, params = session.ran("SET d.status = $status")[0]
        self.assertIn("userId: $userId", query)
        self.assertEqual(params["userId"], "alice")


class ListDecisionsTests(unittest.TestCase):

    def test_an_unavailable_graph_returns_an_empty_list(self):
        """A read must not 500 the panel."""
        ns, _session = _load_manager(_empty_script)
        ns["get_neo4j"] = lambda: None
        self.assertEqual(ns["db_list_decisions"]("alice"), [])

    def test_the_order_is_newest_first(self):
        ns, session = _load_manager(
            lambda q, p: _FakeResult([{"id": "d1", "text": "x",
                                       "decider": "", "decidedAt": "2026-05-31",
                                       "status": "active", "supersededBy": "",
                                       "replaces": [], "sourceId": "",
                                       "sourceName": "", "clientId": "",
                                       "clientName": "", "contextId": "",
                                       "contextName": ""}]))
        rows = ns["db_list_decisions"]("alice")
        self.assertEqual(len(rows), 1)
        query, _params = session.queries[0]
        self.assertIn("ORDER BY coalesce(d.decidedAt, '') DESC", query)

    def test_scope_filters_are_matchers_not_ranking_terms(self):
        """The panel asks a closed question, so it is a MATCH clause."""
        ns, session = _load_manager(_empty_script)
        ns["db_list_decisions"]("alice", client_id="c1")
        query, params = session.queries[0]
        self.assertIn("(d)-[:FOR_CLIENT]->(:Client {id: $clientId", query)
        self.assertEqual(params["clientId"], "c1")

    def test_superseded_rows_are_returned_by_default(self):
        """Hiding them shows the current answer with no history, which is the
        state this register exists to escape."""
        ns, session = _load_manager(_empty_script)
        ns["db_list_decisions"]("alice")
        query, _params = session.queries[0]
        self.assertNotIn("NOT coalesce(d.status", query)

    def test_include_inactive_false_filters_in_cypher(self):
        ns, session = _load_manager(_empty_script)
        ns["db_list_decisions"]("alice", include_inactive=False)
        query, params = session.queries[0]
        self.assertIn("NOT coalesce(d.status, 'active') IN $inactive", query)
        self.assertNotIn("active", params["inactive"])


class DescribeDecisionTests(unittest.TestCase):

    def test_a_superseded_row_is_flagged(self):
        ns, _session = _load_manager(_empty_script)
        row = ns["describe_decision"]({"status": "superseded", "replaces": None})
        self.assertTrue(row["superseded"])
        self.assertEqual(row["replaces"], [])

    def test_an_active_row_is_not_flagged(self):
        ns, _session = _load_manager(_empty_script)
        self.assertFalse(ns["describe_decision"]({"status": "active"})["superseded"])


if __name__ == "__main__":
    unittest.main()
