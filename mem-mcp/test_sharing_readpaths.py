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

import ast
import audit
import builtins
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


def _bound_in(func_node):
    """Every name this function can name: assignments, parameters, builtins.

    Builtins are in scope or the lint flags `list(owner_ids or [user_id])` for
    calling `list`, which is the same class of self-inflicted failure as
    `assertNotIn` over a selector that does not exist.
    """
    names = set(dir(builtins))
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _param_value_names(func_node, param):
    """Names *read* in the value of every `param=` argument in this function.

    The keyword's own name is the store-side parameter (`ownerIds`), which is
    arbitrary and is never a Python variable — so it is the value that has to
    be resolved, and it is usually a compound expression (`owner_ids or
    [user_id]`). Collecting only bare `ast.Name` values would miss exactly the
    shape that shipped the 500.
    """
    reads = set()
    found = False
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != param:
                continue
            found = True
            reads.update(read.id for read in ast.walk(kw.value)
                         if isinstance(read, ast.Name) and isinstance(read.ctx, ast.Load))
    return found, reads


def _func_node(source, name):
    """The parsed top-level function, or fail loudly."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _assert_widening(case, source, func_name, param, expected_read):
    """The call passes `param`, and everything it reads is bound in the function.

    This replaces the needle `"ownerIds=owner_ids or [user_id]"`, which was
    present — and green — while `owner_ids` was never assigned, making every
    call to the main list endpoint a NameError. The substring pinned the
    *spelling*; this pins the two properties that actually matter (the
    parameter is passed at all, and its value resolves) and derives both.
    """
    node = _func_node(source, func_name)
    passed, reads = _param_value_names(node, param)
    case.assertTrue(
        passed,
        f"{func_name}() never passes {param!r} to any call — the query is "
        f"not widened to other owners' records",
    )
    case.assertTrue(
        reads & expected_read,
        f"{func_name}() passes {param}={reads or '{}'} — expected the value to "
        f"read {sorted(expected_read)}",
    )
    bound = _bound_in(node)
    unbound = reads - bound
    case.assertFalse(
        unbound,
        f"{func_name}() passes {param} built from {sorted(unbound)}, which "
        f"nothing in the function assigns — NameError on every call",
    )


def _lift(source, name, **namespace):
    """Exec one function out of real source with the given names in scope."""
    segment = _segment(source, name)
    scope = dict(namespace)
    exec(compile(segment, f"<lifted {name}>", "exec"), scope)  # noqa: S102
    return scope[name]


# ---------------------------------------------------------------------------
# Unbound-global lint
#
# The read paths above are LIFTED: their helpers are injected into the exec
# namespace, so a helper the shipping module never imports is satisfied by the
# test harness and nothing fails. That is exactly how `db_list_diary` shipped
# calling `_shared_record_visible` with no import of it anywhere in
# diary_manager.py — every sharing test was green, and `/api/diary` raised
# NameError on the first request, so the diary tab came back empty.
#
# A helper tested in isolation is not a test of its call site, and this is the
# same lesson in its purest form: the test supplied the binding production
# lacked. The lint resolves every name a function READS against what the
# module and the function actually bind, so a missing import fails here rather
# than in the browser.
# ---------------------------------------------------------------------------

_DUNDERS = {"__name__", "__file__", "__doc__", "__package__", "__spec__",
            "__loader__", "__builtins__", "__debug__", "__all__"}


def _imported_names(node):
    out = set()
    for alias in node.names:
        out.add((alias.asname or alias.name).split(".")[0])
    return out


def _module_bindings(source):
    """Every name a module-level statement binds."""
    names = set(dir(builtins)) | _DUNDERS
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(n.id for n in ast.walk(target)
                             if isinstance(n, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= _imported_names(node)
        else:
            # if / try / with / for at module level: take every name it stores
            # anywhere inside. A superset, which only ever makes the lint
            # quieter — never a false positive.
            names.update(n.id for n in ast.walk(node)
                         if isinstance(n, ast.Name)
                         and isinstance(n.ctx, (ast.Store, ast.Del)))
    return names


def _function_bindings(func_node):
    """Every name bound anywhere inside one function, nested defs included."""
    names = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= _imported_names(node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _unbound_reads(source, func_name):
    """Names ``func_name`` reads that nothing in scope binds.

    Empty for correct code. A name here is a NameError on the first call —
    which for a read path means the endpoint 500s and the UI shows nothing,
    with nothing in the logs to say why.
    """
    node = _func_node(source, func_name)
    bound = _module_bindings(source) | _function_bindings(node)
    return sorted({n.id for n in ast.walk(node)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id not in bound})


class UnboundGlobalTests(unittest.TestCase):
    """No read path may call a helper its module never imports.

    Table-driven over every function the sharing feature touched, because the
    defect was not in one function: it was in the two I edited, and the third
    one I edited next would have had it too.
    """

    TARGETS = (
        (FACT_MANAGER, "db_search_memories"),
        (FACT_MANAGER, "db_list_memories"),
        (FACT_MANAGER, "db_get_fact_by_id"),
        (FACT_MANAGER, "db_get_connections_by_type"),
        (FACT_MANAGER, "db_get_graph"),
        (FACT_MANAGER, "db_get_neighborhood"),
        (FACT_MANAGER, "_shared_records_visible"),
        (FACT_MANAGER, "_shared_records_visible_by_ids"),
        (FACT_MANAGER, "_record_related_ids"),
        (FACT_MANAGER, "_record_related_ids_sync"),
        (FACT_MANAGER, "_shared_record_visible"),
        (DIARY_MANAGER, "db_search_diary"),
        (DIARY_MANAGER, "db_list_diary"),
        (DIARY_MANAGER, "db_list_diary_entries"),
    )

    def test_every_target_read_path_resolves_every_name_it_calls(self):
        for path, func in self.TARGETS:
            with self.subTest(module=os.path.basename(path), func=func):
                unbound = _unbound_reads(_source(path), func)
                self.assertEqual(
                    [], unbound,
                    msg=f"{func}() reads {unbound}, which nothing in "
                        f"{os.path.basename(path)} binds — a NameError on the "
                        f"first call, invisible to every lifted test because "
                        f"the harness injects the missing name itself")

    def test_the_lint_sees_a_missing_import(self):
        """The self-check: without it the test above is an assertion about
        nothing, which is the failure mode this whole file exists to avoid.

        Both diary read paths are checked, because both shipped the defect —
        the import was added to one and not the other, and the lift-based
        suites were green on both.
        """
        line = ("from fact_manager import _shared_record_visible, "
                "_record_related_ids_sync\n")
        broken = _source(DIARY_MANAGER).replace(line, "", 1)
        self.assertNotIn(broken, "")
        for func in ("db_list_diary", "db_list_diary_entries"):
            with self.subTest(func=func):
                self.assertIn(
                    "_shared_record_visible", _unbound_reads(broken, func),
                    msg=f"removing the import does not make {func}() read an "
                        f"unbound name, so the lint above cannot see the "
                        f"defect it exists to catch")


class _Row(dict):
    """A Neo4j record: key and attribute access, like the real Record."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


class _NullLogger:
    """A logger that records nothing.

    The lifted helpers log a warning before degrading — a failure that is
    swallowed silently is the absence-of-a-log-line bug this repo has already
    been bitten by, so the log call stays and the test supplies the sink.
    """

    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


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
                 client_rows=None, context_rows=None, related_rows=None):
        self.fact_rows = fact_rows or []
        self.conn_rows = conn_rows or []
        self.uid = uid
        self.client_rows = client_rows or []
        self.context_rows = context_rows or []
        self.related_rows = related_rows or []
        self.queries = []

    def run(self, query, **params):
        self.queries.append((query, params))
        if "neighbor.userId = f.userId" in query:
            return _Result(self.conn_rows)
        if "RETURN f.userId as uid" in query:
            return _Result([_Row({"uid": self.uid})] if self.uid else [])
        if "as rids" in query:
            return _Result(self.related_rows)
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

    def _read_helpers(self):
        """Every fact_manager helper the read paths under test now name.

        The visibility rule moved into ``_shared_record_visible`` so it is
        stated once rather than at six call sites; lifting a read path without
        it in scope is a NameError that reads exactly like a defect in the
        shipping code.
        """
        source = _source(FACT_MANAGER)
        scope_ids = _lift(source, "_record_scope_ids")
        related_ids = _lift(source, "_record_related_ids", logger=_NullLogger())
        one_visible = _lift(source, "_shared_record_visible", sharing=sharing)
        return {
            "sharing": sharing,
            "_record_scope_ids": scope_ids,
            "_record_related_ids": related_ids,
            "_shared_record_visible": one_visible,
            "_shared_records_visible": _lift(
                source, "_shared_records_visible",
                sharing=sharing, _record_scope_ids=scope_ids,
                _record_related_ids=related_ids,
                _shared_record_visible=one_visible),
            "_record_related_ids_sync": _lift(
                source, "_record_related_ids_sync",
                logger=_NullLogger(), _record_related_ids=related_ids),
        }


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

    A record is visible through any ONE granted scope id — its own
    FOR_CLIENT / IN_CONTEXT edges, or a RELEVANT_TO target. Own records never
    reach this function.
    """

    def _lift_helpers(self, session):
        source = _source(FACT_MANAGER)
        scope_ids = _lift(source, "_record_scope_ids")
        related_ids = _lift(source, "_record_related_ids", logger=_NullLogger())
        one_visible = _lift(source, "_shared_record_visible", sharing=sharing)
        visible = _lift(source, "_shared_records_visible",
                        sharing=sharing, _record_scope_ids=scope_ids,
                        _record_related_ids=related_ids,
                        _shared_record_visible=one_visible)
        return scope_ids, related_ids, visible

    def test_scope_ids_comes_back_per_record_from_both_edge_types(self):
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c1"}),
                         _Row({"id": "f1", "cid": "c2"})],
            context_rows=[_Row({"id": "f1", "cxid": "x1"})],
        )
        scope_ids, _, _ = self._lift_helpers(session)
        clients, contexts = scope_ids(session, ["f1"])
        self.assertEqual(clients.get("f1"), {"c1", "c2"})
        self.assertEqual(contexts.get("f1"), {"x1"})

    def test_related_ids_are_read_from_both_label_kinds(self):
        # RELEVANT_TO targets a Client or a Context and the edge type is the
        # same for both, so the query must not pin a label — a `(rc:Client)`
        # match would silently drop every project target.
        session = _DispatchSession(
            related_rows=[_Row({"id": "f1", "rids": ["c9", "x9"]}),
                          _Row({"id": "f2", "rids": [None]})],
        )
        _, related_ids, _ = self._lift_helpers(session)
        out = related_ids(session, ["f1", "f2"])
        self.assertEqual(out.get("f1"), {"c9", "x9"})
        self.assertEqual(out.get("f2"), set())
        query = session.queries[0][0]
        self.assertIn("(n:Fact OR n:DiaryEntry)", query)
        self.assertNotIn("(rc:Client)", query)

    def test_related_ids_survive_a_failed_query_as_no_links(self):
        # Degrading to "no related links" is a weaker grant match and never a
        # lost result; raising would take the whole read path down with it.
        class _Boom:
            def run(self, query, **params):
                raise RuntimeError("neo4j is down")

        _, related_ids, _ = self._lift_helpers(None)
        self.assertEqual(related_ids(_Boom(), ["f1"]), {})

    def test_a_record_covered_by_any_one_granted_scope_is_visible(self):
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c9"})],
            context_rows=[_Row({"id": "f1", "cxid": "x1"})],
        )
        _, _, visible = self._lift_helpers(session)
        owners = {"alice": {"clients": {}, "contexts": {"x1": "read"}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), {"f1"})

    def test_a_record_whose_only_evidence_is_a_related_target_is_visible(self):
        # The defect this closes: a null-primary record's ONLY scope evidence
        # is a RELEVANT_TO link, so reading the primary edges alone made it
        # unreachable through the very grant that covers the project it names.
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c9"})],
            context_rows=[],
            related_rows=[_Row({"id": "f1", "rids": ["x9"]})],
        )
        _, _, visible = self._lift_helpers(session)
        owners = {"alice": {"clients": {}, "contexts": {"x9": "read"}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), {"f1"})

    def test_a_related_target_in_another_owners_vault_does_not_cover_it(self):
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c9"})],
            context_rows=[],
            related_rows=[_Row({"id": "f1", "rids": ["x9"]})],
        )
        _, _, visible = self._lift_helpers(session)
        owners = {"bob": {"clients": {}, "contexts": {"x9": "read"}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), set())

    def test_an_uncovered_record_stays_invisible(self):
        session = _DispatchSession(
            client_rows=[_Row({"id": "f1", "cid": "c9"})],
            context_rows=[],
            related_rows=[],
        )
        _, _, visible = self._lift_helpers(session)
        owners = {"alice": {"clients": {"c1": "read"}, "contexts": {}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), set())

    def test_empty_input_or_no_owners_is_an_empty_answer_not_a_crash(self):
        session = _DispatchSession()
        _, _, visible = self._lift_helpers(session)
        self.assertEqual(visible(session, [], {"alice": {}}), set())
        owners = {"alice": {"clients": {"c1": "read"}, "contexts": {}}}
        self.assertEqual(visible(session, [("f1", "alice")], owners), set())


class RelatedGrantPolicyTests(ReadPathStoreCase):
    """`sharing`'s related-target rules, imported and called directly.

    These are pure and stdlib-only, so unlike the read paths above they are
    driven for real rather than lifted. What they pin is the pair of properties
    that make a related-only record safe to show at all: it is visible, and the
    owner's own scope on it is not.
    """

    def setUp(self):
        super().setUp()
        self.enable_sharing()
        self.grant("alice", "bob", "context", "x9", "read")
        self.owners = sharing.accessible_owner_scope("bob")["owners"]

    def test_a_related_target_grants_visibility(self):
        self.assertTrue(sharing.record_visible_related("alice", {"x9"}, self.owners))
        self.assertFalse(sharing.record_visible_related("alice", {"x8"}, self.owners))
        # An empty set is not a grant, and a foreign owner's map is not this one.
        self.assertFalse(sharing.record_visible_related("alice", set(), self.owners))
        self.assertFalse(sharing.record_visible_related("carol", {"x9"}, self.owners))

    def test_a_client_target_counts_as_well_as_a_context_one(self):
        self.grant("alice", "bob", "client", "c9", "read")
        owners = sharing.accessible_owner_scope("bob")["owners"]
        self.assertTrue(sharing.record_visible_related("alice", {"c9"}, owners))

    def test_primary_scope_is_hidden_only_when_the_related_target_is_what_shows_it(self):
        # Visible through the primary: nothing is hidden, the names are the
        # caller's own to see.
        self.grant("alice", "bob", "client", "c9", "read")
        owners = sharing.accessible_owner_scope("bob")["owners"]
        self.assertFalse(sharing.primary_scope_hidden("alice", "c9", None, {"x9"}, owners))
        # Visible only through the related target: the primary names are the
        # owner's unshared scope and must not be rendered.
        self.assertTrue(sharing.primary_scope_hidden("alice", "c9", None, {"x9"}, self.owners))
        # Not visible at all is NOT "hidden" — the caller never renders it.
        self.assertFalse(sharing.primary_scope_hidden("alice", "c9", None, {"x8"}, self.owners))
        self.assertFalse(sharing.primary_scope_hidden("carol", "c9", None, {"x9"}, self.owners))

    def test_a_related_only_record_is_read_only_and_a_write_grant_is_not(self):
        # Without the related argument this record's permission came back None,
        # which record_read_only read as "own record" — and the grantee was
        # offered Edit on a record the server refuses with a 403.
        self.assertTrue(sharing.record_read_only("bob", "alice", "c9", None,
                                                 self.owners, {"x9"}))
        self.grant("alice", "bob", "context", "x9", "write")
        owners = sharing.accessible_owner_scope("bob")["owners"]
        self.assertFalse(sharing.record_read_only("bob", "alice", "c9", None,
                                                  owners, {"x9"}))
        # A granted primary still decides, and the weaker of the two wins.
        self.grant("alice", "bob", "client", "c9", "read")
        owners = sharing.accessible_owner_scope("bob")["owners"]
        self.assertTrue(sharing.record_read_only("bob", "alice", "c9", None,
                                                 owners, {"x9"}))

    def test_permission_takes_a_write_grant_over_a_read_one(self):
        # Same rule as record_permission: a write grant on any one target wins.
        # A RELEVANT_TO edge is an association the owner chose to draw, so a
        # record cross-referenced to an editable project is editable.
        self.grant("alice", "bob", "context", "x8", "write")
        owners = sharing.accessible_owner_scope("bob")["owners"]
        self.assertEqual(sharing.record_permission_related("alice", {"x9", "x8"}, owners), "write")
        self.assertEqual(sharing.record_permission_related("alice", {"x8"}, owners), "write")
        self.assertEqual(sharing.record_permission_related("alice", {"x9"}, owners), "read")
        self.assertIsNone(sharing.record_permission_related("alice", set(), owners))
        self.assertIsNone(sharing.record_permission_related("carol", {"x9"}, owners))


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
        return _lift(
            source, "db_get_fact_by_id",
            get_neo4j=lambda: self._driver,
            Optional=typing.Optional,
            **self._read_helpers(),
        )

    def _connections_fn(self):
        source = _source(FACT_MANAGER)
        return _lift(
            source, "db_get_connections_by_type",
            get_neo4j=lambda: self._driver,
            **self._read_helpers(),
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
            json=json,
            datetime=datetime.datetime,
            timezone=datetime.timezone,
            timedelta=datetime.timedelta,
            Optional=typing.Optional,
            **self._read_helpers(),
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


class SharedClientListTests(ReadPathStoreCase):
    """`_shared_client_entries`: where a shared project appears in the list.

    The reported defect was a granted project showing up as a *separate
    top-level entry* rather than nested under its client. The cause was one
    line reading `if ctx["id"] not in entries` — which looks like "this
    context has no parent to nest under" and is always true, because a Context
    id is never a key of `entries` unless that very line put it there. Every
    granted project therefore appeared twice.

    `client_manager.py` imports `common`, so the function is lifted and driven
    against a fake session that dispatches on the query's shape.
    """

    def _entries(self, owners, *, client_rows=None, context_rows=None,
                 parent_rows=None):
        source = _source(os.path.join(HERE, "client_manager.py"))
        fn = _lift(source, "_shared_client_entries",
                   _shared_stamp=lambda owner, perm: {"shared": True,
                                                      "owner": owner,
                                                      "permission": perm},
                   _effective_active=lambda node: True,
                   # The row shape. Lifted separately from the real source
                   # rather than stubbed, so the keys asserted below are the
                   # ones the shipping function produces.
                   _shared_client_node=_lift(source, "_shared_client_node",
                                             _shared_stamp=lambda owner, perm: {
                                                 "shared": True,
                                                 "owner": owner,
                                                 "permission": perm},
                                             _effective_active=lambda node: True))

        class _Session:
            def __init__(self):
                self.queries = []

            def run(self, query, **params):
                self.queries.append((query, params))
                # Order matters: the Pass 2 query ALSO contains
                # `HAS_CONTEXT]->(ctx)` (it walks up from the context to its
                # parent), so the specific RETURN has to be tested first or
                # Pass 2 is answered with Pass 1's rows.
                if "RETURN ctx, parent.id AS parentId" in query:
                    return _Result(context_rows or [])
                if "collect(ctx) as contexts" in query:
                    return _Result(client_rows or [])
                if "WHERE c.id IN $parentIds" in query:
                    return _Result(parent_rows or [])
                return _Result([])

        session = _Session()
        return fn(session, owners), session

    def test_a_granted_project_nests_under_its_client_and_nowhere_else(self):
        owners = {"alice": {"clients": {}, "contexts": {"x1": "read"}}}
        rows, _ = self._entries(
            owners,
            context_rows=[_Row({"ctx": _Row({"id": "x1", "name": "Secret Project",
                                             "active": True}),
                                "parentId": "c1", "parentName": "ACME"})],
            parent_rows=[_Row({"c": _Row({"id": "c1", "name": "ACME"})})],
        )
        by_id = {e["id"]: e for e in rows}
        # The client is a navigation entry carrying the one granted project.
        self.assertIn("c1", by_id)
        self.assertEqual([x["id"] for x in by_id["c1"]["contexts"]], ["x1"])
        self.assertEqual(by_id["c1"]["contexts"][0]["name"], "Secret Project")
        # And the project is NOT also a top-level row. This is the defect: it
        # read as "not under the shared project, but a separate entry".
        self.assertNotIn("x1", by_id,
                         msg="a granted project appears as its own top-level "
                             "entry as well as nested under its client")
        self.assertEqual(len(rows), 1)

    def test_a_granted_project_next_to_a_granted_client_appears_once(self):
        # Both halves granted: Pass 1 already put every context under the
        # client, so the context pass must not add a second row for it.
        owners = {"alice": {"clients": {"c1": "read"}, "contexts": {"x1": "read"}}}
        rows, _ = self._entries(
            owners,
            client_rows=[_Row({"c": _Row({"id": "c1", "name": "ACME"}),
                               "contexts": [_Row({"id": "x1", "name": "Secret",
                                                  "active": True}),
                                            _Row({"id": "x2", "name": "Other",
                                                  "active": True})]})],
            context_rows=[_Row({"ctx": _Row({"id": "x1", "name": "Secret",
                                             "active": True}),
                                "parentId": "c1", "parentName": "ACME"})],
        )
        by_id = {e["id"]: e for e in rows}
        self.assertEqual([x["id"] for x in by_id["c1"]["contexts"]], ["x1", "x2"])
        self.assertNotIn("x1", by_id,
                         msg="a granted project appears as its own top-level "
                             "entry as well as nested under its client")
        self.assertEqual(len(rows), 1)

    def test_an_orphaned_project_still_gets_an_entry(self):
        # A context with no parent has nowhere to nest. Dropping it would make
        # it unreachable, so it keeps an entry of its own — the one case the
        # `nested` flag is false for.
        owners = {"alice": {"clients": {}, "contexts": {"x9": "read"}}}
        rows, _ = self._entries(
            owners,
            context_rows=[_Row({"ctx": _Row({"id": "x9", "name": "Orphan",
                                             "active": True}),
                                "parentId": None, "parentName": None})],
        )
        by_id = {e["id"]: e for e in rows}
        self.assertIn("x9", by_id)
        self.assertEqual(by_id["x9"]["name"], "Orphan")
        self.assertTrue(by_id["x9"]["shared"])

    def test_a_sibling_project_never_rides_along(self):
        # The whole point of the three-pass shape: sharing one project must not
        # share its siblings.
        owners = {"alice": {"clients": {}, "contexts": {"x1": "read"}}}
        rows, _ = self._entries(
            owners,
            context_rows=[_Row({"ctx": _Row({"id": "x1", "name": "Secret",
                                             "active": True}),
                                "parentId": "c1", "parentName": "ACME"})],
            parent_rows=[_Row({"c": _Row({"id": "c1", "name": "ACME"})})],
        )
        by_id = {e["id"]: e for e in rows}
        self.assertEqual([x["id"] for x in by_id["c1"]["contexts"]], ["x1"])
        # The parent's own permission stays navigation-only: a context grant
        # covers the context and its records, never the client node.
        self.assertEqual(by_id["c1"]["permission"], "read")
        self.assertEqual(by_id["c1"]["contexts"][0]["permission"], "read")


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
            "WHERE f.userId IN $ownerIds",      # exact-match query
            "sharing.record_visible(",
            "sharing.record_visible_related(",
            "sharing.primary_scope_hidden(",
            '"ownerUserId"',
        )
        self._refute(seg, "{userId: $userId}")
        _assert_widening(self, self.fact, "db_search_memories",
                         "owner_ids", {"owner_ids"})

    def test__single_vector_search_carries_the_widening(self):
        seg = self._fact_seg("_single_vector_search")
        self._assert(seg, "MatchAny(any_=owner_ids)", "MatchValue(value=user_id)")

    def test__db_relevant_scope_widens_the_record_match(self):
        seg = self._fact_seg("_db_relevant_scope")
        self._assert(seg, "n.userId IN $ownerIds")
        _assert_widening(self, self.fact, "_db_relevant_scope",
                         "ownerIds", {"owner_ids"})

    def test_db_list_memories_widens_and_scopes_links_to_the_records_vault(self):
        seg = self._fact_seg("db_list_memories")
        self._assert(
            seg,
            "WHERE f.userId IN $ownerIds",
            "other.userId = f.userId",          # links belong to the record's vault
            "_shared_record_visible(",
            "sharing.primary_scope_hidden(",
            '"ownerUserId"',
        )
        self._refute(seg, "{userId: $userId}")
        _assert_widening(self, self.fact, "db_list_memories",
                         "ownerIds", {"owner_ids"})

    def test_db_get_fact_by_id_hides_uncovered_ids_as_unknown(self):
        seg = self._fact_seg("db_get_fact_by_id")
        self._assert(
            seg,
            "f.userId IN $ownerIds",
            "owner != user_id",
            "_shared_records_visible(",
            "return None",
        )

    def test_db_get_connections_by_type_hides_uncovered_ids_as_empty(self):
        seg = self._fact_seg("db_get_connections_by_type")
        self._assert(
            seg,
            "f.userId IN $ownerIds",
            "neighbor.userId = f.userId",       # neighbours from the fact's vault
            "_shared_records_visible(",
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
            "_record_related_ids(",
            "_shared_record_visible(",
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
            "sharing.record_visible_related(",
            "sharing.primary_scope_hidden(",
            "d.userId IN $ownerIds",            # MENTIONS enrichment
            '"ownerUserId"',
        )

    def test_db_list_diary_entries_widens_and_filters(self):
        seg = self._diary_seg("db_list_diary_entries")
        self._assert(
            seg,
            "WHERE d.userId IN $ownerIds",
            "_shared_record_visible(",
            "sharing.primary_scope_hidden(",
            '"ownerUserId"',
        )
        _assert_widening(self, self.diary, "db_list_diary_entries",
                         "ownerIds", {"owner_ids"})

    def test_db_list_diary_widens_without_touching_the_row_shape(self):
        seg = self._diary_seg("db_list_diary")
        self._assert(
            seg,
            "WHERE d.userId IN $ownerIds",
            "_shared_record_visible(",
            "sharing.primary_scope_hidden(",
            '"ownerUserId"',
        )
        # The OPTIONAL MATCH chain is pinned by OneRowPerRecordTests; here the
        # property-map owner filter must simply be gone.
        self._refute(seg, "(d:DiaryEntry {userId: $userId})")


if __name__ == "__main__":
    unittest.main()
