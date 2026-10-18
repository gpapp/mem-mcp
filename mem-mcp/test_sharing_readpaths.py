"""Phase 3 read-path access tests: a grantee must see what a grant covers.

The store under test is `audit.py`/`sharing.py` (SQLite-only, importable);
the read paths themselves live in `fact_manager.py` / `diary_manager.py`,
which cannot be imported without the DB drivers, so the functions under
test are lifted with `ast`/source slicing and driven against fakes — the
same pattern as `test_graph_scope.py` and `test_people_extraction.py`.

Two halves, for the reason "a test of a helper is not a test of its call
site" is in AGENTS.md:

* Behavioural tests drive the lifted functions with real grants in a real
  `audit` store, so a post-filter that drops its `record_visible` call or
  threads `owners` wrongly fails here and not in production.
* Call-site pins assert on the *segment* of the shipping source (never the
  whole file), because a widening helper tested in isolation does not prove
  the search path calls it.

Silent defects this suite exists to catch:
* an exact-match or list query still keyed `{userId: $userId}` (shared
  rows silently absent);
* a Qdrant filter still `MatchValue(user_id)` when the caller holds grants
  (shared vectors silently absent);
* a post-filter skipped (leaks an owner's records) or applied to the
  caller's own rows (hides the grantee's own vault);
* a hidden item answering *differently* from an unknown id.
"""

import asyncio
import datetime
import json
import os
import re
import shutil
import tempfile
import types
import typing
import unittest

import audit
import sharing

HERE = os.path.dirname(os.path.abspath(__file__))
FACT_MANAGER = os.path.join(HERE, "fact_manager.py")
DIARY_MANAGER = os.path.join(HERE, "diary_manager.py")

TRUTHY = {"1", "true", "yes", "on"}


def _source(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _segment(source, name):
    """One top-level function (or async function) source, real text."""
    for prefix in ("async def ", "def "):
        marker = f"{prefix}{name}("
        if marker in source:
            start = source.index(marker)
            break
    else:
        raise AssertionError(f"{name} not found")
    tail = source[start + len(marker):]
    m = re.search(r"(?m)^(?:async def |def |class |[A-Z_]+ = )", tail)
    if m:
        return source[start:start + len(marker) + m.start()]
    return source[start:]


def _lift(source, name, **namespace):
    """Exec one function out of real source with the given names in scope."""
    segment = _segment(source, name)
    scope = dict(namespace)
    exec(compile(segment, f"<lifted {name}>", "exec"), scope)  # noqa: S102
    return scope[name]


class _Row(dict):
    """A Neo4j record: key and attribute access, like the real Record."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


class _Result:
    """Minimal Neo4j result: iterable, list(), and .single()."""

    def __init__(self, rows):
        self.rows = rows

    def single(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)


class _DispatchSession:
    """Records queries; answers each by a marker in the query text."""

    def __init__(self, fact_rows=None, conn_rows=None, uid=None,
                 client_rows=None, context_rows=None):
        self.fact_rows = fact_rows or []
        self.conn_rows = conn_rows or []
        self.uid = uid
        self.client_rows = client_rows or []
        self.context_rows = context_rows or []
        self.queries = []

    def run(self, query, **params):
        self.queries.append((query, params))
        if "neighbor.userId = f.userId" in query:
            return _Result(self.conn_rows)
        if "RETURN f.userId as uid" in query:
            return _Result([_Row({"uid": self.uid})] if self.uid else [])
        if "as cid" in query:
            return _Result(self.client_rows)
        if "as cxid" in query:
            return _Result(self.context_rows)
        return _Result(self.fact_rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _DispatchDriver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class _MatchValue:
    def __init__(self, value=None):
        self.kind = "value"
        self.value = value


class _MatchAny:
    def __init__(self, any_=None):
        self.kind = "any"
        self.any_ = any_


class _FieldCondition:
    def __init__(self, key, match=None):
        self.key = key
        self.match = match


class _Filter:
    def __init__(self, must=None):
        self.must = must


class _Qdrant:
    def __init__(self):
        self.calls = []

    async def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(points=[])


class ReadPathStoreCase(unittest.TestCase):
    """Env + audit store scaffolding: a temp DB per test, grants via audit."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sharing-readpaths-")
        self._old_env = {}
        self._set("MEM_AUDIT_DB", os.path.join(self._tmp, "audit.db"))
        self._set("MEM_SESSION_DIR", os.path.join(self._tmp, "sessions"))
        audit._initialised_path = None
        audit.init_db()

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        audit._initialised_path = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _set(self, key, value):
        self._old_env[key] = os.environ.get(key)
        os.environ[key] = value

    def enable_sharing(self):
        self._set("MEM_SHARING_ENABLED", "1")

    def grant(self, owner, grantee, target_type, target_id, permission):
        return audit.grant_access(owner, grantee, target_type, target_id,
                                  permission)


class SingleVectorSearchWideningTests(ReadPathStoreCase):
    """`_single_vector_search` translates the owner set into the Qdrant filter.

    `MatchValue` takes a scalar, so a widened set handed to it as a list
    matches nothing at all — the filter must become `MatchAny`. A None set
    (sharing off) keeps the exact pre-sharing shape.
    """

    def _run(self, owner_ids, top_p=0.0, category=None):
        qdrant = _Qdrant()
        source = _source(FACT_MANAGER)

        async def get_embedding(text):
            return [0.1]

        fn = _lift(
            source, "_single_vector_search",
            get_embedding=get_embedding,
            COLLECTION_NAME="ea_memories",
            VECTOR_FLOOR=0.30, VECTOR_CEIL=0.95,
            MatchValue=_MatchValue, MatchAny=_MatchAny,
            FieldCondition=_FieldCondition, Filter=_Filter,
            Optional=typing.Optional,
        )
        asyncio.run(fn(qdrant, "team structure", "bob", category,
                       fetch_limit=50, top_p=top_p, owner_ids=owner_ids))
        self.assertEqual(len(qdrant.calls), 1)
        return qdrant.calls[0]

    def test_sharing_off_keeps_the_scalar_match(self):
        call = self._run(owner_ids=None)
        must = call["query_filter"].must
        self.assertEqual(must[0].key, "userId")
        self.assertEqual(must[0].match.kind, "value")
        self.assertEqual(must[0].match.value, "bob")

    def test_widened_set_becomes_match_any(self):
        call = self._run(owner_ids=["bob", "alice"])
        must = call["query_filter"].must
        self.assertEqual(must[0].match.kind, "any")
        self.assertEqual(must[0].match.any_, ["bob", "alice"])

    def test_a_singleton_widened_set_does_not_reach_match_any(self):
        # owner_ids == [user_id] means "no grants" — the scalar form stays,
        # because MatchAny over one value is a behaviour change for nothing.
        call = self._run(owner_ids=["bob"])
        must = call["query_filter"].must
        self.assertEqual(must[0].match.kind, "value")
        self.assertEqual(must[0].match.value, "bob")

    def test_threshold_is_translated_independent_of_the_widening(self):
        call = self._run(owner_ids=["bob", "alice"], top_p=0.5)
        expected = 0.30 + 0.5 * (0.95 - 0.30)
        self.assertAlmostEqual(call["score_threshold"], expected)


_USER = "bob"


class ScopeHelperTests(ReadPathStoreCase):
    """`_record_scope_ids` + `_shared_records_visible`: the visibility check.

    A record is visible through any ONE granted scope id; own records never
    reach this function.
    """

    def _lift_helpers(self, session):
        source = _source(FACT_MANAGER)
        scope_ids = _lift(source, "_record_scope_ids")
        visible = _lift(source, "_shared_records_visible",
                       sharing=sharing, _record_scope_ids=scope_ids)
        return scope_ids, visible

    def test_scope_ids_comes_back_per_record_from_both_edge_types(self):
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c1"}),
                         _Row({"id": "f1", "cid": "c2"})],
            context_rows=[_Row({"id": "f1", "cxid": "x1"})],
        )
        scope_ids, _ = self._lift_helpers(session)
        clients, contexts = scope_ids(session, ["f1"])
        self.assertEqual(clients.get("f1"), {"c1", "c2"})
        self.assertEqual(contexts.get("f1"), {"x1"})

    def test_a_record_covered_by_any_one_granted_scope_is_visible(self):
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c9"})],
            context_rows=[_Row({"id": "f1", "cxid": "x1"})],
        )
        _, visible = self._lift_helpers(session)
        owners = {"alice": {"clients": {}, "contexts": {"x1": "read"}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), {"f1"})

    def test_an_uncovered_record_stays_invisible(self):
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c9"})],
            context_rows=[],
        )
        _, visible = self._lift_helpers(session)
        owners = {"alice": {"clients": {"c1": "read"}, "contexts": {}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), set())

    def test_empty_input_or_no_owners_is_an_empty_answer_not_a_crash(self):
        session = _DispatchSession()
        _, visible = self._lift_helpers(session)
        self.assertEqual(visible(session, [], {"alice": {}}), set())
        owners = {"alice": {"clients": {"c1": "read"}, "contexts": {}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), set())


class FactItemAccessTests(ReadPathStoreCase):
    """Item reads: covered grantee reads an owner's fact; uncovered reads 404.

    `db_get_fact_by_id` and `db_get_connections_by_type` must answer an
    uncovered id exactly like an unknown one — None and {} — so the endpoint
    cannot be probed to learn which ids exist.
    """

    ALICE_FACT = _Row({"id": "f1", "text": "Team structure", "userId": "alice"})

    def setUp(self):
        super().setUp()
        self.enable_sharing()
        self.grant("alice", "bob", "client", "c1", "read")

    def _fact_fn(self):
        source = _source(FACT_MANAGER)
        scope_ids = _lift(source, "_record_scope_ids")
        return _lift(
            source, "db_get_fact_by_id",
            get_neo4j=lambda: self._driver,
            sharing=sharing,
            _record_scope_ids=scope_ids,
            Optional=typing.Optional,
        )

    def _connections_fn(self):
        source = _source(FACT_MANAGER)
        scope_ids = _lift(source, "_record_scope_ids")
        return _lift(
            source, "db_get_connections_by_type",
            get_neo4j=lambda: self._driver,
            sharing=sharing,
            _record_scope_ids=scope_ids,
        )

    def _wire(self, *, facts=None, conns=None, uid="alice",
              clients=None, contexts=None):
        self._session = _DispatchSession(
            fact_rows=facts or [], conn_rows=conns or [], uid=uid,
            client_rows=clients or [], context_rows=contexts or [])
        self._driver = _DispatchDriver(self._session)

    def test_a_covered_grant_reads_the_owners_fact(self):
        self._wire(facts=[_Row({"f": self.ALICE_FACT})],
                   clients=[_Row({"id": "f1", "cid": "c1"})])
        fact = self._fact_fn()("f1", "bob")
        self.assertEqual(fact["id"], "f1")
        self.assertEqual(fact["text"], "Team structure")

    def test_an_uncovered_grant_reads_exactly_the_unknown_answer(self):
        self._wire(facts=[_Row({"f": self.ALICE_FACT})],
                   clients=[_Row({"id": "f1", "cid": "c9"})])
        self.assertIsNone(self._fact_fn()("f1", "bob"))

    def test_connected_context_grant_covers_the_record_too(self):
        self._wire(facts=[_Row({"f": self.ALICE_FACT})],
                   clients=[_Row({"id": "f1", "cid": "c9"})],
                   contexts=[_Row({"id": "f1", "cxid": "x1"})])
        self.grant("alice", "bob", "context", "x1", "read")
        fact = self._fact_fn()("f1", "bob")
        self.assertEqual(fact["id"], "f1")

    def test_sharing_off_hides_the_owners_fact_pre_sharing(self):
        self._old_env["MEM_SHARING_ENABLED"] = None
        os.environ["MEM_SHARING_ENABLED"] = ""
        self._wire(facts=[_Row({"f": self.ALICE_FACT})])
        self.assertIsNone(self._fact_fn()("f1", "bob"))
        # And the widened list never even contained the owner.
        params = self._session.queries[0][1]
        self.assertEqual(params["ownerIds"], ["bob"])

    def test_connections_hidden_answer_is_the_empty_map(self):
        self._wire(conns=[_Row({"rel_type": "MENTIONS", "connections": []})],
                   uid="alice",
                   clients=[_Row({"id": "f1", "cid": "c9"})])
        self.assertEqual(self._connections_fn()("f1", "bob"), {})

    def test_connections_visible_through_a_client_grant(self):
        self._wire(conns=[_Row({"rel_type": "MENTIONS",
                                "connections": [{"id": "n1", "name": "N",
                                                 "text": "some text",
                                                 "category": "People"}]})],
                   uid="alice",
                   clients=[_Row({"id": "f1", "cid": "c1"})])
        conns = self._connections_fn()("f1", "bob")
        self.assertEqual(list(conns), ["MENTIONS"])
        self.assertEqual(conns["MENTIONS"][0]["id"], "n1")

    def test_the_widened_query_is_not_the_property_map_form(self):
        # A list on the right of a property map matches nothing, ever.
        self._wire()
        self._fact_fn()("f1", "bob")
        query = self._session.queries[0][0]
        self.assertIn("$ownerIds", query)
        self.assertNotIn("{userId: $userId}", query)


class DiaryListAccessTests(ReadPathStoreCase):
    """`db_list_diary_entries`: own rows pass; owner rows through a grant.

    The query widens to the readable owners, then each row is filtered by
    what the grants actually cover. A hidden entry is dropped, not null
    rendered, and its absence is indistinguishable from an empty date range.
    """

    def _runner(self, rows):
        driver = _DispatchDriver(_DispatchSession(fact_rows=rows))
        source = _source(DIARY_MANAGER)
        fn = _lift(
            source, "db_list_diary_entries",
            get_neo4j=lambda: driver,
            sharing=sharing,
            json=json,
            datetime=datetime.datetime,
            timezone=datetime.timezone,
            timedelta=datetime.timedelta,
            Optional=typing.Optional,
        )
        return fn, driver._session

    def _rows(self):
        return [
            _Row({"id": "own", "userId": "bob", "timestamp": "2026-05-01T10:00",
                  "name": "Own note", "metadata": "{}", "clientId": "c0",
                  "contextId": None, "clientName": "Own Co", "contextName": None}),
            _Row({"id": "covered", "userId": "alice", "timestamp": "2026-05-02T10:00",
                  "name": "Shared note", "metadata": "{}", "clientId": "c1",
                  "contextId": None, "clientName": "Bank", "contextName": None}),
            _Row({"id": "hidden", "userId": "alice", "timestamp": "2026-05-03T10:00",
                  "name": "Secret note", "metadata": "{}", "clientId": "c9",
                  "contextId": None, "clientName": "Other", "contextName": None}),
        ]

    def test_own_rows_pass_and_covered_owner_rows_are_flagged(self):
        self.enable_sharing()
        self.grant("alice", "bob", "client", "c1", "read")
        fn, session = self._runner(self._rows())
        entries = fn("bob", None, None)
        ids = [e["id"] for e in entries]
        self.assertIn("own", ids)
        self.assertIn("covered", ids)
        self.assertNotIn("hidden", ids)
        by_id = {e["id"]: e for e in entries}
        self.assertIsNone(by_id["own"]["ownerUserId"])
        self.assertEqual(by_id["covered"]["ownerUserId"], "alice")

    def test_without_sharing_the_owners_rows_are_simply_absent(self):
        self._old_env["MEM_SHARING_ENABLED"] = None
        os.environ["MEM_SHARING_ENABLED"] = ""
        fn, session = self._runner(self._rows())
        entries = fn("bob", None, None)
        self.assertEqual([e["id"] for e in entries], ["own"])
        # The widened list is the scalar vault: only the caller's own id.
        self.assertEqual(session.queries[0][1]["ownerIds"], ["bob"])

    def test_a_grant_on_the_context_covers_the_entry(self):
        self.enable_sharing()
        self.grant("alice", "bob", "context", "x1", "read")
        rows = [_Row({"id": "cx", "userId": "alice",
                      "timestamp": "2026-05-04T10:00", "name": "Project note",
                      "metadata": "{}", "clientId": None, "contextId": "x1",
                      "clientName": None, "contextName": "Adoption"})]
        fn, _ = self._runner(rows)
        entries = fn("bob", None, None)
        self.assertEqual([e["id"] for e in entries], ["cx"])


class ReadPathCallSiteTests(unittest.TestCase):
    """Pins on the lifted *source segments*: the helper's own tests above
    prove the policy; these prove each read path actually consults it.

    Everything is asserted against the one function's segment with
    assertTrue/assertFalse, so a failure shows the needle, not the file.
    """

    @classmethod
    def setUpClass(cls):
        cls.fact = _source(FACT_MANAGER)
        cls.diary = _source(DIARY_MANAGER)

    def _fact_seg(self, name):
        return _segment(self.fact, name)

    def _diary_seg(self, name):
        return _segment(self.diary, name)

    def _assert(self, segment, *needles):
        for needle in needles:
            self.assertTrue(needle in segment, f"segment lacks {needle!r}")

    def _refute(self, segment, *needles):
        for needle in needles:
            self.assertFalse(needle in segment, f"segment still has {needle!r}")

    def test_db_search_memories_consults_the_policy_and_widens(self):
        seg = self._fact_seg("db_search_memories")
        self._assert(
            seg,
            "sharing.accessible_owner_scope(user_id)",
            "owner_ids=owner_ids",              # vector call site
            "WHERE f.userId IN $ownerIds",      # exact-match query
            "sharing.record_visible(",
            '"ownerUserId"',
        )
        self._refute(seg, "{userId: $userId}")

    def test__single_vector_search_carries_the_widening(self):
        seg = self._fact_seg("_single_vector_search")
        self._assert(seg, "MatchAny(any_=owner_ids)", "MatchValue(value=user_id)")

    def test__db_relevant_scope_widens_the_record_match(self):
        seg = self._fact_seg("_db_relevant_scope")
        self._assert(seg, "n.userId IN $ownerIds", "ownerIds=list(owner_ids or [user_id])")

    def test_db_list_memories_widens_and_scopes_links_to_the_records_vault(self):
        seg = self._fact_seg("db_list_memories")
        self._assert(
            seg,
            "WHERE f.userId IN $ownerIds",
            "other.userId = f.userId",          # links belong to the record's vault
            "sharing.record_visible(owner",
            '"ownerUserId"',
            "ownerIds=owner_ids or [user_id]",
        )
        self._refute(seg, "{userId: $userId}")

    def test_db_get_fact_by_id_hides_uncovered_ids_as_unknown(self):
        seg = self._fact_seg("db_get_fact_by_id")
        self._assert(
            seg,
            "f.userId IN $ownerIds",
            "owner != user_id",
            "sharing.record_visible(",
            "return None",
        )

    def test_db_get_connections_by_type_hides_uncovered_ids_as_empty(self):
        seg = self._fact_seg("db_get_connections_by_type")
        self._assert(
            seg,
            "f.userId IN $ownerIds",
            "neighbor.userId = f.userId",       # neighbours from the fact's vault
            "return {}",
        )

    def test_db_get_graph_widens_every_pass_and_runs_the_visibility_prepass(self):
        seg = self._fact_seg("db_get_graph")
        self._assert(
            seg,
            "f.userId IN $ownerIds",
            "d.userId IN $ownerIds",
            "c.userId IN $ownerIds",
            "ctx.userId IN $ownerIds",
            "sharing.record_visible(owner",
        )

    def test_db_get_neighborhood_gates_on_the_seed_being_visible(self):
        seg = self._fact_seg("db_get_neighborhood")
        self._assert(
            seg,
            "f.userId IN $ownerIds",
            "_shared_records_visible(",
        )

    def test_db_search_diary_widens_and_filters_and_flags(self):
        seg = self._diary_seg("db_search_diary")
        self._assert(
            seg,
            "MatchAny(any_=owner_ids)",
            "sharing.record_visible(",
            "d.userId IN $ownerIds",            # MENTIONS enrichment
            '"ownerUserId"',
        )

    def test_db_list_diary_entries_widens_and_filters(self):
        seg = self._diary_seg("db_list_diary_entries")
        self._assert(
            seg,
            "WHERE d.userId IN $ownerIds",
            "sharing.record_visible(",
            '"ownerUserId"',
            "ownerIds=owner_ids or [user_id]",
        )

    def test_db_list_diary_widens_without_touching_the_row_shape(self):
        seg = self._diary_seg("db_list_diary")
        self._assert(
            seg,
            "WHERE d.userId IN $ownerIds",
            "sharing.record_visible(",
            '"ownerUserId"',
        )
        # The OPTIONAL MATCH chain is pinned by OneRowPerRecordTests; here the
        # property-map owner filter must simply be gone.
        self._refute(seg, "(d:DiaryEntry {userId: $userId})")


if __name__ == "__main__":
    unittest.main()
