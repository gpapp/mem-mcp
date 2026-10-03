"""Unit tests for the graph scoping and cap policy.

``_scope_and_cap_graph`` and ``_filter_neighborhood_scope`` are lifted out of
``fact_manager.py`` with ``ast.get_source_segment`` because that module cannot be
imported here (it needs the DB drivers). Testing the shipping function rather
than a transcription of it matters here: every way this policy is wrong is
silent. A fact left in a client-scoped graph is data the user filtered out; a
fact dropped from one is a result they cannot find, and neither produces an
error.
"""

import ast
import asyncio
import os
import re
import time
import unittest
from typing import Optional

from matching_utils import (
    SCOPE_UNSCOPED,
    identity_confidence,
    plan_search_scope,
    scope_axis_tier,
    scope_context_tier,
    scope_strength,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FACT_MANAGER = os.path.join(HERE, "fact_manager.py")


def _load_function(name):
    """Exec the real ``name`` from fact_manager.py in an empty namespace."""
    with open(FACT_MANAGER, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment, f"could not lift the source of {name}"
            namespace = {}
            exec(compile(segment, FACT_MANAGER, "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {FACT_MANAGER}")


_scope_and_cap_graph = _load_function("_scope_and_cap_graph")
_filter_neighborhood_scope = _load_function("_filter_neighborhood_scope")


def fact(node_id, category="Work"):
    return {"id": node_id, "label": "Fact", "name": node_id, "group": category}


def diary(node_id, name=None):
    return {"id": node_id, "label": "DiaryEntry", "name": name or node_id, "group": "Diary"}


def category_node(node_id, name):
    return {"id": node_id, "label": "Category", "name": name, "group": "CategoryNode"}


def client_node(node_id, name):
    return {"id": node_id, "label": "Client", "name": name, "group": "Client"}


def context_node(node_id, name):
    return {"id": node_id, "label": "Context", "name": name, "group": "Context"}


def edge(a, b, label="MENTIONS"):
    return {"from": a, "to": b, "label": label}


def ids(result):
    return {n["id"] for n in result["nodes"]}


class GraphScopeTests(unittest.TestCase):
    def scope(self, nodes, edges=(), fact_clients=None, fact_contexts=None,
              diary_scope=None, client_id="", context_id="", limit=0):
        node_map = {n["id"]: n for n in nodes}
        return _scope_and_cap_graph(
            node_map, list(edges), fact_clients or {}, fact_contexts or {},
            diary_scope or {}, client_id, context_id, limit,
        )

    def test_no_scope_keeps_everything(self):
        result = self.scope([fact("a"), fact("b"), client_node("c", "Acme")])
        self.assertEqual(ids(result), {"a", "b", "c"})
        self.assertFalse(result["truncated"])

    # One test per axis because both assert the identical rule -- membership is
    # read from the record's own edge, so the record joined to the requested node
    # is kept and the one joined elsewhere is dropped -- over the two axes.
    def test_a_scope_keeps_the_records_on_that_axis_and_drops_the_rest(self):
        cases = (
            ("client",
             [fact("a"), fact("b"), client_node("c1", "Acme"), client_node("c2", "Other")],
             {"fact_clients": {"a": {"c1"}, "b": {"c2"}}, "client_id": "c1"},
             {"a", "c1", "c2"}),
            ("context",
             [fact("a"), fact("b"), context_node("x1", "Atlas"), context_node("x2", "Hedron")],
             {"fact_contexts": {"a": {"x1"}, "b": {"x2"}}, "context_id": "x1"},
             {"a", "x1", "x2"}),
        )
        for axis, nodes, kwargs, kept in cases:
            with self.subTest(axis=axis):
                self.assertEqual(ids(self.scope(nodes, **kwargs)), kept)

    def test_the_other_client_node_is_still_drawn(self):
        """A scoped graph with no client node in it reads as "no facts"."""
        result = self.scope(
            [fact("a"), client_node("c1", "Acme"), client_node("c2", "Other")],
            fact_clients={"a": {"c1"}}, client_id="c1",
        )
        self.assertIn("c1", ids(result))
        self.assertIn("c2", ids(result))

    def test_a_fact_with_no_client_link_is_dropped_under_a_client_scope(self):
        """Membership is read from the edges, so an unlinked fact cannot leak in."""
        result = self.scope([fact("orphan")], fact_clients={"orphan": set()}, client_id="c1")
        self.assertNotIn("orphan", ids(result))

    def test_client_and_context_filters_are_conjunctive(self):
        result = self.scope(
            [fact("a"), fact("b")],
            fact_clients={"a": {"c1"}, "b": {"c1"}},
            fact_contexts={"a": {"x1"}, "b": {"x2"}},
            client_id="c1", context_id="x1",
        )
        self.assertEqual(ids(result) - {"c1", "x1"}, {"a"})

    def test_edges_to_dropped_nodes_are_removed(self):
        result = self.scope(
            [fact("a"), fact("b")],
            [edge("a", "b"), edge("a", "a")],
            fact_clients={"a": {"c1"}}, client_id="c1",
        )
        self.assertNotIn("b", ids(result))
        self.assertEqual([e["to"] for e in result["edges"]], ["a"])


class GraphCapTests(unittest.TestCase):
    # One test per input because both assert the identical rule -- a limit the
    # node count never reaches caps nothing and reports no truncation -- over a
    # finite limit and the `0` sentinel.
    def test_a_cap_the_node_count_never_reaches_keeps_everything(self):
        under = {"a": fact("a"), "b": fact("b"), "c": client_node("c", "Acme")}
        cases = (
            ("finite limit above the node count", under, [edge("a", "b")], 5, 3),
            ("zero limit means no cap",
             {f"f{i}": fact(f"f{i}") for i in range(50)}, [], 0, 50),
        )
        for label, nodes, edges, limit, kept in cases:
            with self.subTest(label):
                result = _scope_and_cap_graph(
                    nodes, edges, {}, {}, {}, "", "", limit,
                )
                self.assertFalse(result["truncated"])
                self.assertEqual(len(result["nodes"]), kept)
                self.assertEqual(result["total"], kept)

    def test_the_cap_keeps_the_most_connected_records(self):
        """A graph of leaves explains nothing, so degree is what we rank on."""
        nodes = {f"f{i}": fact(f"f{i}") for i in range(5)}
        edges = [edge("f0", "f1"), edge("f0", "f2"), edge("f0", "f3"), edge("f0", "f4")]
        result = _scope_and_cap_graph(nodes, edges, {}, {}, {}, "", "", 2)
        self.assertTrue(result["truncated"])
        self.assertIn("f0", ids(result))
        self.assertLessEqual(len(result["nodes"]), 2)

    def test_the_cap_reports_the_uncapped_total(self):
        nodes = {f"f{i}": fact(f"f{i}") for i in range(9)}
        result = _scope_and_cap_graph(nodes, [], {}, {}, {}, "", "", 4)
        self.assertEqual(result["total"], 9)
        self.assertEqual(len(result["nodes"]), 4)

    def test_the_cap_never_drops_a_category_client_or_context_node(self):
        nodes = {f"f{i}": fact(f"f{i}") for i in range(6)}
        nodes["cat"] = category_node("cat", "Work")
        nodes["cl"] = client_node("cl", "Acme")
        nodes["ctx"] = context_node("ctx", "Atlas")
        result = _scope_and_cap_graph(nodes, [], {}, {}, {}, "", "", 1)
        self.assertTrue({"cat", "cl", "ctx"} <= ids(result))

    def test_the_cap_drops_edges_to_records_it_dropped(self):
        nodes = {f"f{i}": fact(f"f{i}") for i in range(4)}
        edges = [edge("f0", "f1"), edge("f2", "f3")]
        result = _scope_and_cap_graph(nodes, edges, {}, {}, {}, "", "", 2)
        for e in result["edges"]:
            self.assertIn(e["from"], ids(result))
            self.assertIn(e["to"], ids(result))

    def test_a_tie_in_degree_is_broken_deterministically_by_id(self):
        nodes = {f"f{i}": fact(f"f{i}") for i in range(4)}
        edges = [edge("f2", "f3"), edge("f0", "f1")]
        first = _scope_and_cap_graph(nodes, edges, {}, {}, {}, "", "", 2)
        second = _scope_and_cap_graph(nodes, list(reversed(edges)), {}, {}, {}, "", "", 2)
        self.assertEqual(ids(first), ids(second))

    def test_scoped_out_records_do_not_count_toward_the_cap(self):
        nodes = {f"f{i}": fact(f"f{i}") for i in range(5)}
        result = _scope_and_cap_graph(
            nodes, [], {"f0": {"c1"}}, {}, {}, "c1", "", 4,
        )
        self.assertFalse(result["truncated"])
        self.assertEqual(ids(result), {"f0"})


class NeighborhoodScopeTests(unittest.TestCase):
    def test_no_scope_returns_the_list_unchanged(self):
        nodes = [{"id": "a", "label": "Fact"}]
        self.assertIs(_filter_neighborhood_scope(nodes, "", ""), nodes)

    # One test per scope combination because all three assert the identical rule
    # -- a Client or Context node *is* the scope, so it survives the filter it is
    # the subject of (drop it and the view looks broken), a record reached under
    # it is kept, everything else goes -- over the three combinations.
    def test_the_scoped_nodes_survive_and_the_rest_do_not(self):
        clients_and_records = [
            {"id": "f1", "label": "Fact"},
            {"id": "c1", "label": "Client"},
            {"id": "c2", "label": "Client"},
            {"id": "x1", "label": "Context"},
        ]
        cases = (
            # x1 goes: a project node reached from here may belong to another
            # client entirely, and the client scope is the only thing keeping
            # that honest. The full project list for a client comes from
            # client.contexts, not here.
            ("client scope", clients_and_records, "c1", "", {"f1", "c1"}),
            ("context scope",
             [{"id": "f1", "label": "Fact"},
              {"id": "x1", "label": "Context"},
              {"id": "x2", "label": "Context"}],
             "", "x2", {"f1", "x2"}),
            ("both scopes",
             [{"id": "c1", "label": "Client"},
              {"id": "x1", "label": "Context"},
              {"id": "c9", "label": "Client"},
              {"id": "x9", "label": "Context"}],
             "c1", "x1", {"c1", "x1"}),
        )
        for label, nodes, client_id, context_id, kept in cases:
            with self.subTest(label):
                result = _filter_neighborhood_scope(nodes, client_id, context_id)
                self.assertEqual({n["id"] for n in result}, kept)


class DiaryScopeSourceTests(unittest.TestCase):
    """Diary scope must come from the edges, like every other record.

    ``db_get_graph`` reads a fact's client from its ``FOR_CLIENT`` edge but read
    a diary entry's from a ``clientId`` property. No Fact or DiaryEntry node has
    that property in Neo4j -- scope lives on the ``FOR_CLIENT``/``IN_CONTEXT``
    edges, and ``clientId`` is only ever a Qdrant payload key. So the read
    returned None for every entry, every diary entry came out unscoped, and a
    client or project filter dropped all of them. Separately, the two scope
    passes matched ``:Fact`` only, so a diary entry's scope edge produced no
    graph edge at all.
    """

    @classmethod
    def setUpClass(cls):
        with open(FACT_MANAGER, "r", encoding="utf-8") as handle:
            cls.source = handle.read()
        tree = ast.parse(cls.source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "db_get_graph":
                cls.body = ast.get_source_segment(cls.source, node)
                break
        else:
            raise AssertionError("db_get_graph not found in fact_manager.py")

    def test_the_scope_passes_cover_diary_entries(self):
        """Both scope passes must match DiaryEntry, or its edges are invisible."""
        for rel in ("FOR_CLIENT", "IN_CONTEXT"):
            pattern = re.compile(
                r"OPTIONAL MATCH \(n\)-\[:" + rel + r"\]->\(\w+\)\s*\n"
                r"\s*WHERE \(n:Fact OR n:DiaryEntry\)",
            )
            self.assertTrue(
                pattern.search(self.body),
                f"the {rel} pass still matches :Fact only, so a diary entry's "
                f"scope edge produces no graph edge and its scope can only be "
                f"read from the stale property",
            )

    def test_diary_scope_comes_only_from_the_edges(self):
        """No property read, and the edge passes must write into it.

        The original read ``d_node.get("clientId")``. A Fact or DiaryEntry never
        has a clientId property in Neo4j -- it is a Qdrant payload key -- so that
        read returned None for every entry, every diary entry came out unscoped,
        and a client filter dropped all of them. Pinning the absence of the read
        is the point: a "harmless fallback" that can never hold anything is not a
        fallback, it is a silent unscoped verdict.
        """
        self.assertFalse(
            'd_node.get("clientId")' in self.body,
            "db_get_graph reads a clientId property that no Fact or DiaryEntry "
            "node has; that read is always None and unscopes the entry",
        )
        self.assertFalse(
            'd_node.get("contextId")' in self.body,
            "db_get_graph reads a contextId property that no DiaryEntry node has",
        )
        # A list, not a tuple: the passes assign into it by index, and a tuple
        # would raise TypeError the moment the first edge lands. The node is
        # called `n_node` because the pass returns a Fact *or* a DiaryEntry
        # under the Cypher variable `n` -- see ReturnAliasTests.
        self.assertTrue(
            'diary_scope.setdefault(n_node["id"], ["", ""])[0] = c_id' in self.body,
            "the FOR_CLIENT pass no longer writes diary scope",
        )
        self.assertTrue(
            'diary_scope.setdefault(n_node["id"], ["", ""])[1] = ctx_id' in self.body,
            "the IN_CONTEXT pass no longer writes diary scope",
        )


class DiaryScopeTests(unittest.TestCase):
    """A diary entry scopes through the same conjunctive rule as a fact."""

    def scope(self, nodes, diary_scope, client_id="", context_id=""):
        node_map = {n["id"]: n for n in nodes}
        return _scope_and_cap_graph(
            node_map, [], {}, {}, diary_scope, client_id, context_id, 0,
        )

    # One test per input because all five assert the identical rule -- a diary
    # entry is in scope only under the axis its own scope row names, and no
    # client link on either axis means it is out -- over the client axis, the
    # context axis, a tuple-encoded row and two ways of having no link at all.
    def test_a_diary_entry_is_in_scope_only_under_the_axis_it_is_linked_on(self):
        cases = (
            ("client axis, list-encoded row",
             [diary("d1"), diary("d2")],
             {"d1": ["c1", ""], "d2": ["c2", ""]}, "c1", "", {"d1"}),
            ("context axis, list-encoded row",
             [diary("d1"), diary("d2")],
             {"d1": ["", "x1"], "d2": ["", "x2"]}, "", "x1", {"d1"}),
            ("client axis, tuple-encoded row",
             [diary("d1"), diary("d2")],
             {"d1": ("c1", "x1"), "d2": ("c2", "x1")}, "c1", "", {"d1"}),
            ("row present but no client on it",
             [diary("d1")], {"d1": ["", ""]}, "c1", "", set()),
            # No entry at all means unlinked, which is not the same as in-scope:
            # a missing FOR_CLIENT edge fails the client test.
            ("absent from the scope map entirely",
             [diary("d1")], {}, "c1", "", set()),
        )
        for label, nodes, diary_scope, client_id, context_id, kept in cases:
            with self.subTest(label):
                self.assertEqual(
                    ids(self.scope(nodes, diary_scope, client_id, context_id)), kept,
                )


class ReturnAliasTests(unittest.TestCase):
    """A Cypher ``RETURN`` alias and the key Python reads back are a contract.

    Production shipped a ``KeyError: 'f'`` that killed the entire graph
    response on every call. The two client/context passes matched
    ``(n:Fact OR n:DiaryEntry)`` and returned ``c, n``, but the Python still
    read ``cr["f"]`` -- the variable had been renamed and the read had not.
    Every request to ``/api/graph`` raised, so the whole tab was dead.

    The existing source guard for this function checked the *query text* (that
    it mentions ``DiaryEntry``) and passed throughout, because the query was
    correct. What it never checked is that the consuming code reads the keys the
    query actually returns. This does.
    """

    _FACT_MANAGER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "fact_manager.py")

    def _source(self):
        with open(self._FACT_MANAGER, "r", encoding="utf-8") as handle:
            return handle.read()

    def _return_aliases(self, query):
        """The keys a RETURN hands back: ``type(r) as rel_type`` -> rel_type."""
        tail = query[query.upper().rfind("RETURN"):]
        aliases = set()
        for part in tail[len("RETURN"):].split(","):
            part = part.strip()
            if not part:
                continue
            if " as " in part.lower():
                aliases.add(part.lower().split(" as ")[-1].strip())
            else:
                aliases.add(part.split()[0].strip() if part.split() else "")
        return aliases

    def _loops(self):
        """(loop var, keys read off it, the query just above it)."""
        source = self._source()
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "db_get_graph")

        queries = []  # (lineno, query text)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "RETURN" in node.value.upper()):
                queries.append((node.lineno, node.value))

        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
                continue
            var = node.target.id
            keys = {
                s.slice.value for s in ast.walk(node)
                if isinstance(s, ast.Subscript)
                and isinstance(s.value, ast.Name) and s.value.id == var
                and isinstance(s.slice, ast.Constant)
                and isinstance(s.slice.value, str)
            }
            if not keys:
                continue
            earlier = [q for ln, q in queries if ln < node.lineno]
            if not earlier:
                continue
            out.append((var, keys, earlier[-1]))
        return out

    def test_db_get_graph_has_queries_to_check(self):
        """If this fails the guard is vacuous, so it is checked first."""
        self.assertGreaterEqual(len(self._loops()), 4)

    def test_every_key_read_off_a_result_is_produced_by_its_own_query(self):
        for var, keys, query in self._loops():
            aliases = self._return_aliases(query)
            missing = keys - aliases
            self.assertEqual(
                missing, set(),
                msg=(
                    f"the loop over `{var}` reads {sorted(missing)} but its query "
                    f"only returns {sorted(aliases)} -- a renamed Cypher variable "
                    f"that the Python never followed. Every call raises KeyError, "
                    f"which takes down the whole response rather than one edge."
                ),
            )


if __name__ == "__main__":
    unittest.main()


class UnassignedScopeTests(unittest.TestCase):
    """`unassigned` selects on the *absence* of a client link.

    The other three scopes are all positive membership tests: a record passes
    when it is joined to a named client, project, or a client in a set. There is
    no node for an unlinked record to be joined to, so "records with no client"
    is the one scope that cannot be expressed that way -- it has to invert the
    test, and it has to say so in the signature rather than as a sentinel
    ``client_id``. A sentinel would be a value on a parameter whose every other
    value is a real node id, and would silently match nothing.
    """

    def scope(self, nodes, fact_clients=None, fact_contexts=None, diary_scope=None,
              client_id="", context_id=""):
        node_map = {n["id"]: n for n in nodes}
        return _scope_and_cap_graph(
            node_map, [], fact_clients or {}, fact_contexts or {},
            diary_scope or {}, client_id, context_id, 0, True,
        )

    # One test per node kind because all five assert the identical rule --
    # `unassigned` selects on the *absence* of a client link, so a linked record
    # goes and an unlinked one stays, whichever label it carries -- over facts,
    # diary entries and the vault-wide category node.
    def test_the_selection_is_the_absence_of_a_client_link(self):
        cases = (
            # The direction that matters on the fact side: treating "missing" as
            # "in scope for everyone" is what the old positive test did, and it
            # is correct there -- a missing edge fails the client test. Inverting
            # must not carry that assumption over, or every record the pass
            # failed to see would appear under Unassigned.
            ("fact linked to a client", [fact("a"), fact("b")],
             {"fact_clients": {"a": {"c1"}}}, {"b"}),
            ("fact absent from the map entirely", [fact("ghost")],
             {"fact_clients": {}}, {"ghost"}),
            ("diary entry with a client", [diary("d1"), diary("d2")],
             {"diary_scope": {"d1": ["c1", ""], "d2": ["", ""]}}, {"d2"}),
            ("diary entry absent from the scope map", [diary("d1")],
             {"diary_scope": {}}, {"d1"}),
            # Categories are shared across the whole vault and still label what
            # is shown, so the absence of a client link does not remove one.
            ("category node", [fact("a"), category_node("g1", "Work")],
             {}, {"a", "g1"}),
        )
        for label, nodes, kwargs, kept in cases:
            with self.subTest(label):
                self.assertEqual(ids(self.scope(nodes, **kwargs)), kept)

    def test_client_and_context_nodes_are_dropped(self):
        """No record in scope is linked to one, so they render as orphan dots.

        The general rule keeps Client and Context nodes so a client-scoped graph
        is not client-less. That reasoning is about *preserving* the node that
        is the filter's subject; here there is no such node, and keeping all of
        them draws a screen of unconnected clients.
        """
        result = self.scope([fact("a"), client_node("c1", "Acme"),
                             context_node("x1", "Atlas"), category_node("g1", "Work")])
        self.assertIn("a", ids(result))
        self.assertNotIn("c1", ids(result))
        self.assertNotIn("x1", ids(result))

    def test_the_edges_left_dangling_are_dropped(self):
        """Otherwise an edge to a removed client node survives into the response."""
        nodes = [fact("a"), client_node("c1", "Acme")]
        node_map = {n["id"]: n for n in nodes}
        result = _scope_and_cap_graph(
            node_map, [edge("a", "c1", "FOR_CLIENT")], {"a": set()}, {}, {},
            "", "", 0, True,
        )
        self.assertEqual(result["edges"], [])

    def test_unassigned_ignores_a_client_id_rather_than_conflating_them(self):
        """Both set is not a valid reading; the flag is the selection.

        Guards against a future "treat a missing client_id as unassigned" edit,
        which would silently turn the default (no filter) into a filter.
        """
        nodes = [fact("a")]
        node_map = {n["id"]: n for n in nodes}
        unscoped = _scope_and_cap_graph(node_map, [], {"a": set()}, {}, {}, "", "", 0, False)
        self.assertIn("a", ids(unscoped))

    def test_the_cap_still_applies(self):
        nodes = [fact(f"f{i}") for i in range(5)] + [fact("linked")]
        node_map = {n["id"]: n for n in nodes}
        result = _scope_and_cap_graph(
            node_map, [], {"linked": {"c1"}}, {}, {}, "", "", 2, True,
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total"], 5)
        self.assertEqual(len([n for n in result["nodes"] if n["label"] == "Fact"]), 2)


class UnassignedNeighborhoodTests(unittest.TestCase):
    """`unassigned` has no scope node, so none is preserved.

    ``_filter_neighborhood_scope`` normally keeps the Client and Context nodes
    matching the filter, because a client-scoped graph without that client on it
    looks broken. There is no "the client these records are *not* filed under",
    so every one of them goes.
    """

    def test_client_and_context_nodes_are_dropped(self):
        """`test_the_record_survives` is the same call read as a membership test."""
        nodes = [fact("a"), client_node("c1", "Acme"), context_node("x1", "Atlas")]
        kept = {n["id"] for n in _filter_neighborhood_scope(nodes, "", "", True)}
        self.assertEqual(kept, {"a"})

    def test_it_is_distinct_from_a_neighbourhood_with_no_scope(self):
        """Both args empty is the *unfiltered* case, and must stay unfiltered.

        `unassigned` and "no scope at all" differ only by the flag, so a missing
        one here would make Unassigned a no-op on "Show all connected" -- which
        is how a half-wired filter presents: the main graph is right and the
        expand button quietly ignores the scope.
        """
        nodes = [fact("a"), client_node("c1", "Acme")]
        unfiltered = {n["id"] for n in _filter_neighborhood_scope(nodes, "", "", False)}
        unassigned = {n["id"] for n in _filter_neighborhood_scope(nodes, "", "", True)}
        self.assertIn("c1", unfiltered)
        self.assertNotIn("c1", unassigned)


class GraphScopeParamTests(unittest.TestCase):
    """`unassigned` is a query param, and it has to be a real one.

    The params this function produces are dropped on the floor unless three
    separate things line up: the JS builds them, `api.get` actually sends them,
    and the endpoint declares them. A break anywhere is silent -- the graph
    renders, just unscoped, which is indistinguishable from the filter not
    working.
    """

    TEMPLATE = os.path.join(HERE, "templates", "dashboard.html")

    def setUp(self):
        with open(self.TEMPLATE, "r", encoding="utf-8") as handle:
            self.html = handle.read()
        with open(FACT_MANAGER, "r", encoding="utf-8") as handle:
            self.fm = handle.read()
        with open(os.path.join(HERE, "gui.py"), "r", encoding="utf-8") as handle:
            self.gui = handle.read()

    def _app_script(self):
        """The one real <script> block, by attribute.

        Splitting on a bare "<script>" is ambiguous: the page also carries
        `type="text/markdown"` sample blocks, so an index-based split hands one
        of those to the next step.
        """
        blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
                            self.html, re.S)
        assert len(blocks) == 1, f"expected exactly one inline app script, got {len(blocks)}"
        return blocks[0]

    @staticmethod
    def _js_function(src, name):
        """The body of a JS function, by brace counting.

        Deliberately not ast.parse: this is JavaScript and that is Python's
        parser, where a `//` comment is a floor-division operator and the first
        line of this very script is one. Node is not a dependency of this suite.
        """
        start = src.index(f"function {name}(")
        i = src.index("{", start)
        depth = 0
        while True:
            ch = src[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[start:i + 1]
            i += 1

    def test_the_filter_travels_as_its_own_param_not_a_fake_client_id(self):
        self.assertTrue(
            "if (clientFilter === 'unassigned') params.unassigned = 1;" in self.html,
            "graphScopeParams no longer sends the unassigned flag",
        )
        # The negative half: params.clientId must not be reachable when the
        # filter is the sentinel, or "unassigned" is sent as a node id and
        # matches no client. Checked on the extracted function rather than by
        # slicing the file around a string, which passes on any reformatting.
        #
        # The shape is an if/else-if: the first arm takes the sentinel, the
        # second assigns the id. What must hold is that the id arm cannot see
        # 'unassigned' -- either it excludes the literal, or it is unreachable.
        body = self._js_function(self._app_script(), "graphScopeParams")
        sentinel_arm = body.index("clientFilter === 'unassigned'")
        id_arm = body.index("params.clientId = clientFilter")
        self.assertLess(sentinel_arm, id_arm,
                        "the id is assigned before the sentinel is handled")
        self.assertIn("else if", body[sentinel_arm:id_arm],
                      "the id assignment is not in the branch after the sentinel")
        # And the id arm must not also match the sentinel.
        id_branch = body[body.rindex("else if", sentinel_arm, id_arm):id_arm]
        self.assertNotIn("'unassigned'", id_branch,
                         "the clientId branch can still fire for the sentinel")

    def test_api_get_sends_the_params_it_is_handed(self):
        """The reason the server-side scope was dead in the first place.

        `api.get` took one argument, so all four graph call sites handed it a
        second that was discarded, and `/api/graph` never received
        `clientId`/`contextId` at all. A source assertion on the call sites
        passes against that file -- the calls are all there.
        """
        self.assertTrue(
            "get:    (url, params) => fetch(apiUrl(url, params))" in self.html,
            "api.get takes a second argument it does not forward",
        )
        self.assertTrue("function apiUrl(url, params)" in self.html,
                        "the query string is never built")
        self.assertTrue("new URLSearchParams()" in self.html,
                        "apiUrl does not encode the params")

    def test_the_endpoint_declares_the_param(self):
        for fn in ("api_get_graph", "api_get_neighbors", "api_focus_graph"):
            with self.subTest(fn=fn):
                self.assertIn(f"def {fn}(", self.gui)
                self.assertRegex(
                    self.gui, rf"def {fn}\([^)]*unassigned: int = 0",
                    f"{fn} does not declare the unassigned query param",
                )

    def test_the_server_function_accepts_and_uses_it(self):
        for fn in ("db_get_graph", "db_get_neighborhood"):
            with self.subTest(fn=fn):
                self.assertRegex(
                    self.fm, rf"def {fn}\([^)]*unassigned: bool = False",
                    f"{fn} does not accept unassigned",
                )
        # And it reaches the policy, rather than being accepted and dropped.
        self.assertIn("client_id, context_id, limit, unassigned,",
                      self.fm.split("def _scope_and_cap_graph")[0][-400:])
        self.assertIn("return _filter_neighborhood_scope(nodes, client_id, context_id, unassigned)",
                      self.fm)


# --- End-to-end scope ranking -------------------------------------------------
#
# `db_search_memories` is lifted whole and driven against stubs, because the
# property under test is the one no single helper can show: the two Johns are
# both returned, in the order the caller asked for, and the one that wins on
# scope wins *despite* scoring lower. Every way this is wrong is silent -- a
# filter would return fewer records, a sort key in the wrong place would return
# them in vector order, and neither raises.

LOCHAS_TEXT = (
    "**Role:** Managing Director (MD) - Senior leader, direct report to Christian Reno "
    "(DB CIO) / **Company/Team:** Deutsche Bank (DB) - Private Bank / **Domain:** AI "
    "adoption, organizational strategy, Private Bank / Technology, Data Platforms / "
    "**Notes:** Key senior stakeholder for the PBAI adoption program. Approver for the "
    "'AI Adoption Office' investment and staffing proposal. Also referenced as "
    "'John Locas' in some contexts (spelling variant)."
)
PORTER_TEXT = (
    "Deutsche Bank regional architecture manager for US. Manages three architects in "
    "the US who came from finance business analyst background."
)
BONIN_TEXT = (
    "**Role:** Diligent Account Owner / **Company:** SAP (SGSC) / "
    "**Email:** joao.bonin@sap.com / **Domain:** Software vendor account management"
)
RUEDIGER_TEXT = "Attended the SAP SE steering committee; listed as John on the attendee list."

SCOPE_TREE = [
    {"id": "c-db", "name": "Deutsche Bank (DB)", "active": True,
     "contexts": [{"id": "x-ai", "name": "AI Enablement Hub", "active": True},
                  {"id": "x-adopt", "name": "DB AI Adoption", "active": True}]},
    {"id": "c-sapse", "name": "SAP SE", "active": False,
     "contexts": [{"id": "x-handover", "name": "EA Handover", "active": True}]},
    {"id": "c-sap", "name": "SAP", "active": True, "contexts": []},
    {"id": "c-epam", "name": "EPAM", "active": True,
     "contexts": [{"id": "x-ppc", "name": "PPC", "active": True},
                  {"id": "x-mufg", "name": "MUFG", "active": True}]},
    {"id": "c-wc", "name": "White Cube", "active": True, "contexts": []},
    {"id": "c-lc", "name": "LC Security", "active": True, "contexts": []},
    {"id": "c-val", "name": "Valantic FSA", "active": True, "contexts": []},
]


def _point(node_id, score, payload):
    return type("P", (), {"id": node_id, "score": score, "payload": payload})()


# Porter deliberately outscores Lochas on the vector axis: if scope did not
# outrank score, this table would come back the wrong way round.
JOHN_POINTS = [
    _point("f-porter", 0.74, {
        "text": PORTER_TEXT, "name": "John Benjamin Porter", "category": "People",
        "clientName": "Deutsche Bank (DB)",
        "metadata": {"first_name": "John Benjamin", "last_name": "Porter",
                     "company": "Deutsche Bank"}}),
    _point("f-lochas", 0.71, {
        "text": LOCHAS_TEXT, "name": "John Lochas", "category": "People",
        "clientName": "Deutsche Bank (DB)",
        "metadata": {"first_name": "John", "last_name": "Lochas"}}),
    _point("f-bonin", 0.70, {
        "text": BONIN_TEXT, "name": "Joao Bonin", "category": "People",
        "clientName": "SAP SE",
        "metadata": {"first_name": "Joao", "last_name": "Bonin",
                     "company": "SAP (SGSC)", "aliases": ["SAP"]}}),
    _point("f-ruediger", 0.68, {
        "text": RUEDIGER_TEXT, "name": "Ruediger John", "category": "People",
        "clientName": "SAP SE",
        "metadata": {"first_name": "Ruediger", "last_name": "John"}}),
]

# One record whose ONLY scope evidence is a RELEVANT_TO edge -- the shape the
# classifier writes when it cannot pick a primary client.
RELEVANT_TO_DB = {"f-lochas-rev": {"clients": ["Deutsche Bank (DB)"], "contexts": []}}


class ScopePrioritySearchTests(unittest.TestCase):
    """The user's own case: 'John' is two DB people; the scope decides which."""

    def search(self, client=None, context=None, points=None, relevant=None,
               category="People"):
        from test_matching_regressions import REAL_SCOPE_TREE  # same vault shape
        relevant = RELEVANT_TO_DB if relevant is None else relevant

        class _Session:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def run(self_inner, *args, **kwargs):
                return []

        class _Driver:
            def session(self_inner):
                return _Session()

        async def _vector(*args, **kwargs):
            return list(JOHN_POINTS if points is None else points)

        async def _hydrate(*args, **kwargs):
            return None

        def _plan(client_arg, context_arg, user_id):
            c, x, ce, xe = plan_search_scope(client_arg, context_arg, REAL_SCOPE_TREE)
            return {"client": c, "context": x, "clientEvidence": ce, "contextEvidence": xe,
                    "unresolvedClient": client_arg if not c else None,
                    "unresolvedContext": context_arg if not x else None,
                    "knownClients": [n for n, _ in REAL_SCOPE_TREE]}

        namespace = {
            "Optional": Optional, "time": time,
            "get_qdrant": _sync(_Stub()), "get_neo4j": lambda: _Driver(),
            "looks_like_person_name": lambda q: True,
            "db_plan_search_scope": _plan,
            "rewrite_search_query": _sync([("John", 1.0)]),
            "CHUNK_FETCH_MULTIPLIER": 3, "WEAK_RESULT_LIMIT": 2,
            "INACTIVE_PENALTY": 0.4, "INFERRED_SCOPE_BOOST": 0.3,
            "_single_vector_search": _vector, "_hydrate_chunk_texts": _hydrate,
            "_boost_result_score": lambda r, q: r.score,
            "parent_of": lambda pid, payload: str(pid),
            "_db_relevant_scope": lambda driver, uid, ids: relevant,
            "db_get_client_status_map": lambda uid: {
                "deutsche bank (db)": True, "sap se": False, "sap": True,
                "epam": True, "white cube": True, "lc security": True,
                "valantic fsa": True},
            "scope_strength": scope_strength, "scope_axis_tier": scope_axis_tier,
            "scope_context_tier": scope_context_tier,
            "infer_scope_from_text": lambda q, uid: (None, ""),
            "identity_confidence": identity_confidence,
            "log_search_stats": lambda **kwargs: None,
        }
        fn = _lift_into("db_search_memories", namespace)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                fn("John", "memories", limit=10, category=category, top_p=0.0,
                   client=client, context=context))
        finally:
            loop.close()

    # -- the user's case ------------------------------------------------------

    def test_both_johns_are_still_returned(self):
        """Scope prioritises; it never removes. This is the whole contract."""
        results = self.search(client="DB", context="AI Adoption")
        names = [r["name"] for r in results]
        self.assertIn("John Lochas", names)
        self.assertIn("John Benjamin Porter", names)
        # And the out-of-scope records are not deleted either -- a misclassified
        # entry must still be findable, just last.
        self.assertIn("Joao Bonin", names)
        self.assertEqual(len(results), len(JOHN_POINTS))

    def test_the_db_project_puts_lochas_first_despite_a_lower_score(self):
        results = self.search(client="DB", context="AI Adoption")
        porter = next(r for r in results if r["name"] == "John Benjamin Porter")
        lochas = next(r for r in results if r["name"] == "John Lochas")
        self.assertLess(lochas["score"], porter["score"],
                        "the fixture no longer proves scope outranks the vector")
        self.assertEqual([r["name"] for r in results][0], "John Lochas")
        self.assertGreater(lochas["scopeStrength"], porter["scopeStrength"])
        self.assertEqual(lochas["scopeContextTier"], "inferred")
        self.assertEqual(lochas["scopeClientTier"], "assigned")

    def test_the_client_alone_still_leaves_the_two_johns_ordered_by_score(self):
        """With no project there is nothing to separate them, so score decides."""
        results = self.search(client="DB")
        top_two = [r["name"] for r in results[:2]]
        self.assertEqual(top_two, ["John Benjamin Porter", "John Lochas"])

    def test_a_sap_search_surfaces_the_sap_people_first(self):
        results = self.search(client="SAP SE")
        names = [r["name"] for r in results]
        self.assertLess(names.index("Joao Bonin"), names.index("Ruediger John"))
        # Both DB people come back, but both are behind the SAP pair.
        self.assertEqual(names[2:], ["John Benjamin Porter", "John Lochas"],
                         "a DB record should rank below the SAP records")
        bonin = next(r for r in results if r["name"] == "Joao Bonin")
        self.assertEqual(bonin["scopeClientTier"], "assigned")

    def test_an_inactive_client_demotes_records_but_keeps_its_own(self):
        """`SAP SE` is pinned inactive, and the caller named it on purpose.

        The penalty exists so an unscoped search surfaces live work. Demoting
        the client the caller explicitly asked for would answer with less than
        was requested -- and Joao Bonin, the record this whole feature was built
        for, lives under it.
        """
        results = self.search(client="SAP SE")
        bonin = next(r for r in results if r["name"] == "Joao Bonin")
        self.assertAlmostEqual(bonin["score"], 0.70, places=6,
                               msg="the inactive penalty was applied to the requested client")
        # The demotion half, read off the same record under a scope that does not
        # name SAP SE: the penalty is real, it is just skipped for the client the
        # caller asked for. Asserting it makes the check above non-vacuous.
        elsewhere = self.search(client="EPAM")
        demoted = next(r for r in elsewhere if r["name"] == "Joao Bonin")
        self.assertAlmostEqual(demoted["score"], 0.70 - 0.4, places=6,
                               msg="SAP SE is pinned inactive and was not the requested client")

    def test_a_null_primary_record_is_still_found_by_its_relevant_link(self):
        """RELEVANT_TO is the only scope evidence a null-primary record has.

        The classifier writes those links before it gives up on a primary, so a
        partition that reads only `clientName` demotes exactly the records that
        need the help.
        """
        point = _point("f-lochas-rev", 0.55, {
            "text": "PBAI steering update, notes circulated.", "name": "AI Adoption notes",
            "category": "Work", "clientName": None, "metadata": {}})
        results = self.search(client="DB", context="AI Adoption",
                              points=[point], relevant=RELEVANT_TO_DB,
                              category="Work")
        first = results[0]
        self.assertEqual(first["name"], "AI Adoption notes")
        self.assertEqual(first["scopeClientTier"], "relevant")
        self.assertGreater(first["scopeStrength"], SCOPE_UNSCOPED)

    def test_an_unresolved_client_is_reported_and_removes_nothing(self):
        """A typo must not read as "no results"."""
        results = self.search(client="Deutsche Bankk")
        self.assertEqual(len(results), len(JOHN_POINTS))
        self.assertEqual(results[0]["scope"]["requestedClient"], None)
        self.assertEqual(results[0]["scope"]["unresolvedClient"], "Deutsche Bankk")
        self.assertTrue(results[0]["scope"]["knownClients"])

    def test_no_requested_scope_leaves_the_ordering_untouched(self):
        results = self.search()
        self.assertEqual([r["name"] for r in results],
                         ["John Benjamin Porter", "John Lochas", "Joao Bonin",
                          "Ruediger John"])
        self.assertTrue(all(r["scopeStrength"] == 0.0 for r in results))


class _Stub:
    def __bool__(self):
        return True


def _sync(value):
    async def _f(*args, **kwargs):
        return value
    return _f


def _lift_into(name, namespace):
    """Exec `name` from fact_manager.py into a caller-supplied namespace."""
    with open(FACT_MANAGER, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment, f"could not lift the source of {name}"
            exec(compile(segment, FACT_MANAGER, "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {FACT_MANAGER}")
