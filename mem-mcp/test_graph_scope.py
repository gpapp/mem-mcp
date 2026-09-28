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
import os
import unittest

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

    def test_client_scope_keeps_only_that_clients_facts(self):
        nodes = [fact("a"), fact("b"), client_node("c1", "Acme"), client_node("c2", "Other")]
        result = self.scope(nodes, fact_clients={"a": {"c1"}, "b": {"c2"}}, client_id="c1")
        self.assertIn("a", ids(result))
        self.assertNotIn("b", ids(result))

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

    def test_context_scope_filters_the_same_way(self):
        nodes = [fact("a"), fact("b"), context_node("x1", "Atlas"), context_node("x2", "Hedron")]
        result = self.scope(
            nodes, fact_contexts={"a": {"x1"}, "b": {"x2"}}, context_id="x1",
        )
        self.assertIn("a", ids(result))
        self.assertNotIn("b", ids(result))

    def test_client_and_context_filters_are_conjunctive(self):
        result = self.scope(
            [fact("a"), fact("b")],
            fact_clients={"a": {"c1"}, "b": {"c1"}},
            fact_contexts={"a": {"x1"}, "b": {"x2"}},
            client_id="c1", context_id="x1",
        )
        self.assertEqual(ids(result) - {"c1", "x1"}, {"a"})

    def test_diary_entries_scope_through_their_own_properties(self):
        result = self.scope(
            [diary("d1"), diary("d2")],
            diary_scope={"d1": ("c1", "x1"), "d2": ("c2", "x1")},
            client_id="c1",
        )
        self.assertIn("d1", ids(result))
        self.assertNotIn("d2", ids(result))

    def test_edges_to_dropped_nodes_are_removed(self):
        result = self.scope(
            [fact("a"), fact("b")],
            [edge("a", "b"), edge("a", "a")],
            fact_clients={"a": {"c1"}}, client_id="c1",
        )
        self.assertNotIn("b", ids(result))
        self.assertEqual([e["to"] for e in result["edges"]], ["a"])


class GraphCapTests(unittest.TestCase):
    def test_under_the_limit_is_not_truncated(self):
        result = _scope_and_cap_graph(
            {"a": fact("a"), "b": fact("b"), "c": client_node("c", "Acme")},
            [edge("a", "b")], {}, {}, {}, "", "", 5,
        )
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["nodes"]), 3)

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

    def test_a_zero_limit_means_no_cap(self):
        nodes = {f"f{i}": fact(f"f{i}") for i in range(50)}
        result = _scope_and_cap_graph(nodes, [], {}, {}, {}, "", "", 0)
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["nodes"]), 50)

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

    def test_the_scoped_node_survives_its_own_filter(self):
        """The client node is the scope, so dropping it makes the view look broken."""
        nodes = [
            {"id": "f1", "label": "Fact"},
            {"id": "c1", "label": "Client"},
            {"id": "c2", "label": "Client"},
            {"id": "x1", "label": "Context"},
        ]
        kept = {n["id"] for n in _filter_neighborhood_scope(nodes, "c1", "")}
        # x1 goes: a project node reached from here may belong to another client
        # entirely, and the client scope is the only thing keeping that honest.
        # The full project list for a client comes from client.contexts, not here.
        self.assertEqual(kept, {"f1", "c1"})

    def test_a_context_scope_keeps_the_context_node(self):
        nodes = [
            {"id": "f1", "label": "Fact"},
            {"id": "x1", "label": "Context"},
            {"id": "x2", "label": "Context"},
        ]
        kept = {n["id"] for n in _filter_neighborhood_scope(nodes, "", "x2")}
        self.assertEqual(kept, {"f1", "x2"})

    def test_both_scopes_keep_both_nodes(self):
        nodes = [
            {"id": "c1", "label": "Client"},
            {"id": "x1", "label": "Context"},
            {"id": "c9", "label": "Client"},
            {"id": "x9", "label": "Context"},
        ]
        kept = {n["id"] for n in _filter_neighborhood_scope(nodes, "c1", "x1")}
        self.assertEqual(kept, {"c1", "x1"})


if __name__ == "__main__":
    unittest.main()
