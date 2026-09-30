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
import re
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

    def test_a_diary_entry_scopes_through_its_own_client(self):
        nodes = [diary("d1"), diary("d2"), client_node("c1", "Acme"), client_node("c2", "Other")]
        result = self.scope(nodes, {"d1": ["c1", ""], "d2": ["c2", ""]}, client_id="c1")
        self.assertIn("d1", ids(result))
        self.assertNotIn("d2", ids(result))

    def test_a_diary_entry_scopes_through_its_own_context(self):
        nodes = [diary("d1"), diary("d2"), context_node("x1", "Atlas"), context_node("x2", "Hedron")]
        result = self.scope(nodes, {"d1": ["", "x1"], "d2": ["", "x2"]}, context_id="x1")
        self.assertIn("d1", ids(result))
        self.assertNotIn("d2", ids(result))

    def test_an_unscoped_diary_entry_is_dropped_under_a_scope(self):
        result = self.scope([diary("d1")], {"d1": ["", ""]}, client_id="c1")
        self.assertNotIn("d1", ids(result))

    def test_a_diary_entry_missing_from_the_scope_map_is_dropped(self):
        """No entry at all means unlinked, which is not the same as in-scope."""
        result = self.scope([diary("d1")], {}, client_id="c1")
        self.assertNotIn("d1", ids(result))


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

    def test_a_linked_fact_is_excluded(self):
        result = self.scope([fact("a"), fact("b")], fact_clients={"a": {"c1"}})
        self.assertNotIn("a", ids(result))

    def test_an_unlinked_fact_is_kept(self):
        result = self.scope([fact("a"), fact("b")], fact_clients={"a": {"c1"}})
        self.assertIn("b", ids(result))

    def test_a_fact_absent_from_the_map_entirely_counts_as_unassigned(self):
        """No entry means no FOR_CLIENT edge, which is what unassigned selects.

        This is the direction that matters: treating "missing" as "in scope for
        everyone" is what the old positive test did, and it is correct there --
        a missing edge fails the client test. Inverting must not carry that
        assumption over, or every record the pass failed to see would appear
        under Unassigned.
        """
        result = self.scope([fact("ghost")], fact_clients={})
        self.assertIn("ghost", ids(result))

    def test_a_diary_entry_with_a_client_is_excluded(self):
        nodes = [diary("d1"), diary("d2")]
        node_map = {n["id"]: n for n in nodes}
        result = _scope_and_cap_graph(
            node_map, [], {}, {}, {"d1": ["c1", ""], "d2": ["", ""]}, "", "", 0, True,
        )
        self.assertNotIn("d1", ids(result))
        self.assertIn("d2", ids(result))

    def test_a_diary_entry_missing_from_the_scope_map_counts_as_unassigned(self):
        result = self.scope([diary("d1")], diary_scope={})
        self.assertIn("d1", ids(result))

    def test_client_and_context_nodes_are_dropped(self):
        """No record in scope is linked to one, so they render as orphan dots.

        The general rule keeps Client and Context nodes so a client-scoped graph
        is not client-less. That reasoning is about *preserving* the node that
        is the filter's subject; here there is no such node, and keeping all of
        them draws a screen of unconnected clients.
        """
        result = self.scope([fact("a"), client_node("c1", "Acme"),
                             context_node("x1", "Atlas"), category_node("g1", "Work")])
        self.assertNotIn("c1", ids(result))
        self.assertNotIn("x1", ids(result))

    def test_category_nodes_are_kept(self):
        """They are shared across the whole vault and still label what is shown."""
        result = self.scope([fact("a"), category_node("g1", "Work")])
        self.assertIn("g1", ids(result))

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
        nodes = [fact("a"), client_node("c1", "Acme"), context_node("x1", "Atlas")]
        kept = {n["id"] for n in _filter_neighborhood_scope(nodes, "", "", True)}
        self.assertEqual(kept, {"a"})

    def test_the_record_survives(self):
        kept = {n["id"] for n in _filter_neighborhood_scope([fact("a")], "", "", True)}
        self.assertIn("a", kept)

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
