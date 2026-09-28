"""Static lint over the Cypher embedded in the Python sources.

Cypher cannot be parsed here — there is no Neo4j and no driver — so nothing in
the local test suite would notice a malformed query. A query typo ships, sits
in a code path that only runs on a nightly schedule or a rare UI action, and
then fails every time it is reached. That is exactly how a ``FOREACH`` in
``clear_scope_links_batch`` reached production: ``ELSE [x] END`` where ``x`` was
the variable being defined, so the query raised
``Variable `x` not defined`` and every reclassify aborted on its first call.

This catches that class of error. It is not a substitute for running the query.
"""

import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# A FOREACH declares its variable:  FOREACH (thing IN <list> | <body>)
# The variable is not in scope inside <list>, so <list> may only mention things
# bound earlier in the query. Referencing it there is always a mistake.
_FOREACH_RE = re.compile(r"FOREACH\s*\(\s*(\w+)\s+IN\s+(.*?)\s*\|\s*(.*?)\)", re.I | re.S)
_CYPHER_RE = re.compile(r"\b(MATCH|MERGE|CREATE|DELETE|DETACH|REMOVE|RETURN|CALL)\b")


def _cypher_strings(path):
    """Every string constant in the file that looks like a Cypher statement."""
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - py_compile is the gate for this
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _CYPHER_RE.search(node.value):
                yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            # f-string queries interpolate the filter clauses, so the literal
            # parts are what we can still see.
            text = "".join(part.value for part in node.values
                           if isinstance(part, ast.Constant) and isinstance(part.value, str))
            if _CYPHER_RE.search(text):
                yield node.lineno, text


def _python_sources():
    for name in sorted(os.listdir(HERE)):
        if name.endswith(".py") and not name.startswith("test_"):
            yield os.path.join(HERE, name)


class ForeachScopeTests(unittest.TestCase):
    def test_no_foreach_list_references_its_own_variable(self):
        checked = 0
        for path in _python_sources():
            for lineno, query in _cypher_strings(path):
                for variable, collection, _body in _FOREACH_RE.findall(query):
                    checked += 1
                    with self.subTest(file=os.path.basename(path), line=lineno):
                        self.assertNotRegex(
                            collection, rf"\b{re.escape(variable)}\b",
                            f"{os.path.basename(path)}:{lineno} — FOREACH variable "
                            f"`{variable}` is not in scope inside its own IN list",
                        )
        self.assertGreater(checked, 0, "the lint found no FOREACH at all; it is not running")


# A property map, i.e. the `{k: v, ...}` of MATCH (n {..}) / MERGE / CREATE.
_PROP_MAP_RE = re.compile(r"\{([^{}]*)\}")


def _is_list_expr(expr):
    """Does this expression evaluate to a list?"""
    if isinstance(expr, (ast.List, ast.ListComp, ast.SetComp)):
        return True
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        return expr.func.id in ("list", "sorted", "set")
    return False


def _list_arg_names(node):
    """Names bound to a *list* inside the enclosing function body.

    Deliberately derived from the Python rather than guessed from the parameter
    name. A plural-looking name is a guess that misses `where_ids` and flags
    `status`; the AST does not. Covers a list literal, a list comprehension, a
    call to ``list()``/``sorted()``/``set()``, and a name assigned from any of
    those at any point in the same function.
    """
    lists = set()
    for child in ast.walk(node):
        targets = []
        if isinstance(child, ast.Assign):
            targets = [t for t in child.targets if isinstance(t, ast.Name)]
            value = child.value
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            targets = [child.target]
            value = child.value
        else:
            continue
        if value is not None and _is_list_expr(value):
            lists.update(t.id for t in targets)
    return lists


def _run_calls(source):
    """(lineno, query_text, list_kwarg_names) for every ``s.run(...)``."""
    tree = ast.parse(source)
    # The binding we need is a *sibling* statement in the enclosing function
    # (``ids = [...]`` above the call), not a descendant of the call, so the
    # search has to start at the function.
    enclosing = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing[child] = node
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if not isinstance(node.args[0].value, str):
            continue
        # A kwarg is a list if its value is one, or a name bound to one.
        list_names = set()
        known = _list_arg_names(enclosing.get(node, node))
        for kw in node.keywords:
            if kw.arg and (_is_list_expr(kw.value) or
                           (isinstance(kw.value, ast.Name) and kw.value.id in known)):
                list_names.add(kw.arg)
        yield node.lineno, node.args[0].value, list_names


class ListParameterInPropertyMapTests(unittest.TestCase):
    """A list in a MATCH property map matches nothing, and fails silently.

    ``MATCH (n {id: $ids})`` reads like "n whose id is in $ids" and is not: a
    list on the right of a property test is compared for *equality*, so it
    matches a node whose ``id`` literally is that list -- no node, ever. The
    query returns zero rows, which is indistinguishable from "nothing to clear".

    That silence is what made this dangerous. The batch scope clear is how a
    reclassify overwrites the previous verdict, so with the id test broken every
    reclassify only ever *added* links: a real entry accumulated two
    FOR_CLIENT edges and an IN_CONTEXT left over from answers the classifier had
    stopped giving, ``REMOVE n.scopeCheckedSig`` silently never ran, and
    re-running the reclassify appeared to change nothing at all.
    """

    def test_no_list_parameter_is_used_as_a_property_map_value(self):
        checked = 0
        offenders = []
        for path in _python_sources():
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            for lineno, query, list_names in _run_calls(source):
                if not list_names:
                    continue
                checked += 1
                for match in _PROP_MAP_RE.finditer(query):
                    body = match.group(1)
                    for name in list_names:
                        if re.search(rf":\s*\${re.escape(name)}\b", body):
                            offenders.append(
                                f"{os.path.basename(path)}:{lineno} — ${name} is a list "
                                f"used in a property map `{match.group(0).strip()}`; "
                                f"use `WHERE n.<key> IN ${name}` instead")
        self.assertEqual(offenders, [], "\n".join(offenders))
        self.assertGreater(checked, 0, "the lint found no parameterised run() at all; "
                                       "it is not running")

    def test_the_batch_clear_tests_its_ids_with_in(self):
        """Pin the one query whose whole job is to delete by a list of ids."""
        for path in _python_sources():
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            for lineno, query, _list_names in _run_calls(source):
                if "count(DISTINCT n) AS cleared" not in query:
                    continue
                self.assertRegex(
                    query, r"WHERE\s+n\.id\s+IN\s+\$ids",
                    f"{os.path.basename(path)}:{lineno} — the scope clear must test "
                    f"ids with IN; a list in a property map matches nothing")
                self.assertNotRegex(
                    query, r"\{\s*id\s*:\s*\$ids",
                    f"{os.path.basename(path)}:{lineno} — a list in a property map "
                    f"matches nothing and fails silently")


class DanglingConjunctionTests(unittest.TestCase):
    """A bare AND needs a WHERE to attach to.

    Queries that build an optional filter interpolate a fragment which used to
    be "AND f.id IN $onlyIds" into a template whose only preceding clause was
    MATCH. Cypher rejects that at parse time ("Invalid input 'AND': expected a
    graph pattern"), and the failure only ever happened on the single-item
    reclassify, because the full pass leaves the fragment empty. The identical
    trap to the FOREACH one: a construct that one caller happens to exercise.
    """

    def test_no_query_starts_a_line_with_and_without_a_where(self):
        offenders = []
        for path in _python_sources():
            for lineno, query in _cypher_strings(path):
                seen_where = False
                for line in query.splitlines():
                    text = line.strip()
                    if not text:
                        continue
                    if re.match(r"AND\b", text, re.I):
                        if not seen_where:
                            offenders.append(
                                f"{os.path.basename(path)}:{lineno}: {text}"
                            )
                        continue
                    # Any clause keyword resets the search: an AND after one of
                    # these has nothing to attach to.
                    if re.match(r"(MATCH|OPTIONAL|WITH|UNWIND|MERGE|CREATE|RETURN|SET|DELETE|DETACH|REMOVE|CALL)\b",
                                text, re.I):
                        seen_where = False
                    elif re.search(r"\bWHERE\b", text, re.I):
                        seen_where = True
        self.assertEqual(
            offenders, [],
            "a line-leading AND with no WHERE since the last clause needs fixing: "
            + "; ".join(offenders),
        )

    def test_conditional_fragments_render_a_complete_clause(self):
        """Pin the two _backfill_qdrant filters to a full WHERE, not a bare AND."""
        path = os.path.join(HERE, "migrate_client_context.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        found = 0
        for line in source.splitlines():
            if not re.match(r"\s*(?:diary_)?id_filter\s*=", line):
                continue
            found += 1
            self.assertIn(
                '"WHERE ', line,
                f"filter must render a complete WHERE clause: {line.strip()}",
            )
            self.assertNotIn(
                '"AND ', line,
                f"a bare AND is a parse error when injected: {line.strip()}",
            )
        self.assertEqual(found, 2, "expected the fact and diary only_ids filters")


class ClearScopeQueryTests(unittest.TestCase):
    """The query that broke reclassification, pinned so it cannot drift back."""

    def setUp(self):
        self.queries = {}
        for path in _python_sources():
            for lineno, query in _cypher_strings(path):
                self.queries.setdefault(os.path.basename(path), []).append((lineno, query))

    def find(self, filename, needle):
        for lineno, query in self.queries.get(filename, []):
            if needle in query:
                return lineno, query
        self.fail(f"no Cypher containing {needle!r} in {filename}")

    def test_batch_clear_deletes_the_matched_relationship(self):
        _, query = self.find("migrate_client_context.py", "count(DISTINCT n) AS cleared")
        # The list must be built from `r`, the OPTIONAL MATCH relationship.
        self.assertRegex(query, r"OPTIONAL MATCH \(n\)-\[r:FOR_CLIENT\|IN_CONTEXT\]->\(\)")
        self.assertRegex(query, r"ELSE \[r\] END")
        self.assertNotRegex(query, r"ELSE \[x\] END")

    def test_batch_clear_does_not_count_a_node_twice(self):
        """A node with both FOR_CLIENT and IN_CONTEXT yields two rows.

        ``count(n)`` would report two for one node, so the "cleared" tally in the
        log — and any future decision made from it — would overcount.
        """
        _, query = self.find("migrate_client_context.py", "count(DISTINCT n) AS cleared")
        self.assertNotRegex(query, r"RETURN count\(n\) AS cleared")

    def test_batch_clear_is_batched_not_per_item(self):
        """The whole point of the batch is one round trip for the whole vault."""
        _, query = self.find("migrate_client_context.py", "count(DISTINCT n) AS cleared")
        self.assertIn("$ids", query)
        self.assertNotIn("$id,", query)

    def test_reclassify_clear_is_reachable_from_every_entry_point(self):
        """Single-item, batch and the async wrapper must share one query.

        They diverged once: the single-item path had its own copy, and only the
        batch one was updated. Two copies of a query means one of them rots.
        """
        with open(os.path.join(HERE, "migrate_client_context.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        functions = {n.name: n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("clear_scope_links", "clear_scope_links_batch", "clear_scope_links_async"):
            self.assertIn(name, functions)
        # The two wrappers must delegate, not re-implement.
        batch_src = ast.get_source_segment(source, functions["clear_scope_links_batch"])
        single_src = ast.get_source_segment(source, functions["clear_scope_links"])
        self.assertEqual(batch_src.count("FOREACH"), 1, "the batch clear has a second query")
        self.assertNotIn("FOREACH", single_src, "the single-item clear re-implements the query")
        self.assertIn("clear_scope_links_batch", single_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
