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
import asyncio
import os
import re
import sys
import unittest

from matching_utils import MergeDraftTooLarge

HERE = os.path.dirname(os.path.abspath(__file__))

# A FOREACH declares its variable:  FOREACH (thing IN <list> | <body>)
# The variable is not in scope inside <list>, so <list> may only mention things
# bound earlier in the query. Referencing it there is always a mistake.
_FOREACH_RE = re.compile(r"FOREACH\s*\(\s*(\w+)\s+IN\s+(.*?)\s*\|\s*(.*?)\)", re.I | re.S)
_CYPHER_RE = re.compile(r"\b(MATCH|MERGE|CREATE|DELETE|DETACH|REMOVE|RETURN|CALL)\b")


def _docstring_nodes(tree):
    """The `ast.Constant` nodes that are a docstring, at any nesting depth.

    A docstring is prose that happens to quote a query, and a quoted query is
    not a query: it is never sent to Neo4j. Linting them is a false positive
    that only ever gets "fixed" by rewording a comment — and the check is about
    Cypher, not English, so the comment is the wrong place to be careful.
    """
    found = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.add(id(first.value))
    return found


def _cypher_strings(path):
    """Every string constant in the file that looks like a Cypher statement."""
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - py_compile is the gate for this
        return
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
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
        checked = 0
        for path in _python_sources():
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            for lineno, query, _list_names in _run_calls(source):
                if "count(DISTINCT n) AS cleared" not in query:
                    continue
                checked += 1
                self.assertRegex(
                    query, r"WHERE\s+n\.id\s+IN\s+\$ids",
                    f"{os.path.basename(path)}:{lineno} — the scope clear must test "
                    f"ids with IN; a list in a property map matches nothing")
                self.assertNotRegex(
                    query, r"\{\s*id\s*:\s*\$ids",
                    f"{os.path.basename(path)}:{lineno} — a list in a property map "
                    f"matches nothing and fails silently")
        # Without this the whole test passes on a batch clear that no longer
        # exists: the loop body is the only place either assertion can run, so a
        # renamed alias or a deleted query leaves it vacuously green. The broken
        # form this pins was exactly a query that returned `cleared = 0`.
        self.assertGreaterEqual(checked, 1, "the batch scope clear was not found at all")


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
        examined = 0
        for path in _python_sources():
            for lineno, query in _cypher_strings(path):
                examined += 1
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
        # This lint asserts absence over every query in the tree, so it passes
        # vacuously if the extractor stops finding any — which is what a
        # docstring- or f-string-handling regression would look like.
        self.assertGreater(examined, 0, "no Cypher strings were examined; it is not running")

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

    def test_batch_clear_also_drops_the_boot_stamp(self):
        """`REMOVE n.scopeCheckedSig` has to be in the same statement.

        It is the mark that says "the classifier has already ruled on this item,
        do not ask again". Leaving it on the record means the *next* boot skips
        an item a reclassify just re-decided — which is exactly how a run that
        appeared to do nothing looked while it was overwriting links. It was
        silent in production for the same reason the id test was: the query
        returned `cleared = 0`, indistinguishable from nothing to clear.
        """
        _, query = self.find("migrate_client_context.py", "count(DISTINCT n) AS cleared")
        self.assertIn("REMOVE n.scopeCheckedSig", query,
                      "the batch clear must retire the stamp, or the next "
                      "boot treats a re-decided item as already decided")

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


class ManualScopeGuardTests(unittest.TestCase):
    """A scope a human set must survive every pass that re-derives scope.

    The defect was not a wrong answer but an unanswerable one: the full
    reclassify selected *every* item, cleared its links and asked the model
    again, so a client/project chosen on the save form was replaced by a guess
    with no record that it had been chosen. ``scopeManual`` is the marker that
    says "decided, not derived", and these pin every place that has to honour
    it — the selection query, the single-item entry point, and both writers
    that have to set it.
    """

    def setUp(self):
        self.queries = {}
        for path in _python_sources():
            for lineno, query in _cypher_strings(path):
                self.queries.setdefault(os.path.basename(path), []).append((lineno, query))
        with open(os.path.join(HERE, "migrate_client_context.py"), encoding="utf-8") as handle:
            self.mcc_source = handle.read()
        with open(os.path.join(HERE, "client_manager.py"), encoding="utf-8") as handle:
            self.cm_source = handle.read()
        with open(os.path.join(HERE, "diary_manager.py"), encoding="utf-8") as handle:
            self.dm_source = handle.read()
        self.functions = {}
        for name, source in (("migrate_client_context.py", self.mcc_source),
                             ("client_manager.py", self.cm_source),
                             ("diary_manager.py", self.dm_source)):
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.functions.setdefault(node.name, (source, node))

    def find(self, filename, needle):
        for lineno, query in self.queries.get(filename, []):
            if needle in query:
                return lineno, query
        self.fail(f"no Cypher containing {needle!r} in {filename}")

    def segment(self, name):
        source, node = self.functions[name]
        return ast.get_source_segment(source, node)

    def test_every_full_selection_query_excludes_manually_scoped_items(self):
        """Derived, not a needle.

        A full reclassify selects every item in the vault, so "the reclassify
        query has the guard" is really "any query that selects *all* facts or
        *all* diary entries has the guard" — a property that also covers a
        query added later. Searching for a literal first and asserting on the
        match is the trap AGENTS.md warns about: the backfill's query carries
        the same WHERE clauses, so a needle picked the wrong one and the
        assertion passed or failed for the wrong reason.
        """
        seen = 0
        for query in self.queries.get("migrate_client_context.py", []):
            _lineno, text = query
            selects_everything = (
                "MATCH (f:Fact {userId: $userId})" in text
                or "MATCH (d:DiaryEntry {userId: $userId})" in text
            )
            if not selects_everything:
                continue
            # A query that already filters on the links is the boot backfill,
            # which cannot touch a linked item by construction.
            if "FOR_CLIENT]->" in text:
                continue
            # Nor is every whole-vault query a threat: the Client-category
            # migration reads every Fact too, but takes only id and name and
            # writes no scope. What makes a query dangerous is that it hands
            # the record *body* to the classifier, which is what
            # f.text / d.content being RETURNed means.
            if "AS text" not in text and "AS content" not in text:
                continue
            seen += 1
            self.assertIn("scopeManual", text,
                          "a query selects the whole vault and would overwrite a chosen scope")
        self.assertGreaterEqual(seen, 2,
                                "expected the reclassify's fact and diary selection queries")

    def test_the_skip_is_reported_rather_than_silent(self):
        """A filter that drops items with no counter is a filter nobody trusts.

        ``total`` is the count the job classifies, so a vault where half the
        entries are manual reads as "half your diary vanished" with nothing in
        the status to explain it.
        """
        counted = [text for _lineno, text in self.queries.get("migrate_client_context.py", [])
                   if "scopeManual" in text and "count(" in text]
        self.assertTrue(counted, "the manual-scope skip is filtered but never counted")
        self.assertTrue('job["manual_skipped"]' in self.mcc_source)

    def test_single_item_reclassify_clears_the_marker_before_it_clears(self):
        """Order, still -- but the failure it prevents has changed sides.

        This used to be a *refusal* ordered before the clear, because clearing
        is unrecoverable. Now the single-item path drops the marker instead, and
        a crash between the two statements is the thing to avoid. If the links
        were cleared first and the marker survived, the item would be left with
        no scope at all AND the flag that makes every later reclassify skip it
        -- unrecoverable without a direct Cypher edit, and invisible because a
        skipped item looks exactly like a correctly-unscoped one.

        So the marker goes first: the worst case is then an item that is merely
        no longer protected, which the next reclassify can still fix.
        """
        for name in ("_reclassify_single_fact", "_reclassify_single_diary"):
            segment = self.segment(name)
            clear_marker = segment.find("_clear_manual_scope")
            clear_links = segment.find("clear_scope_links_async")
            self.assertNotEqual(clear_marker, -1, f"{name} does not clear the manual marker")
            self.assertNotEqual(clear_links, -1, f"{name} no longer clears its links")
            self.assertLess(clear_marker, clear_links,
                            f"{name} clears the links before dropping the marker that protects them")

    def test_the_single_item_path_no_longer_refuses_a_hand_set_scope(self):
        """The 409 is gone, and with it the exception that only it raised.

        A single-item reclassify is one person clicking "reclassify this" on one
        record. Refusing it protected a scope the same person had just
        overridden, and the only route to a reclassify was to clear the scope by
        hand first -- the destructive step the marker exists to make deliberate.
        The full reclassify still skips manual items; that one is unattended.
        """
        for name in ("_reclassify_single_fact", "_reclassify_single_diary"):
            segment = self.segment(name)
            self.assertFalse(
                "_assert_not_manual_scope" in segment,
                f"{name} still refuses instead of clearing the marker")
        self.assertNotIn("ManualScopeError", self.mcc_source,
                         "the exception has no raiser left")
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as handle:
            gui = handle.read()
        self.assertNotIn("ManualScopeError", gui,
                         "gui.py still imports and catches an exception nothing raises")

    def test_the_marker_clear_is_a_remove_not_a_set(self):
        """`SET n.scopeManual = true` here would be a no-op with the worst timing.

        The clear has to actually unset the flag. Setting it to false would work
        today, but it leaves a property that reads as meaningful, and once a
        value is always written, an absent property and an explicit false stop
        being distinguishable -- which is the same distinction the coalesce lint
        above exists to make reliable.
        """
        segment = self.segment("_clear_manual_scope")
        self.assertIn("REMOVE n.scopeManual", segment,
                      "the marker is not being removed, so the skip query still matches it")
        self.assertNotIn("scopeManual =", segment,
                         "the clear assigns the marker instead of removing it")

    def test_every_scopeManual_read_treats_an_unset_property_as_false(self):
        """The property is absent on every node written before it existed.

        `coalesce` is not decoration. Neo4j has no boolean type here: an unset
        property reads as `null`, and `NOT null` is `null`, not `true` -- so a
        bare `AND NOT n.scopeManual` **excludes** the row rather than including
        it. That is a WHERE clause silently dropping every item the marker was
        never written on, which is all of them, and the reclassify would report
        success having done nothing.

        The failure directions differ per read site, which is why this is a lint
        over every read rather than a note:
          - skip query, bare NOT   -> null -> row excluded  (vault silently skipped)
          - count query, bare      -> null -> not counted    (manual_skipped under-reports)
          - RETURN alias, bare     -> null -> falsy in Python (the safe direction, by luck)

        Only reads are linted. A write is not a read: `SET n.scopeManual = true`
        is the stamp, and `REMOVE n.scopeManual` is this fix -- neither is
        subject to the coalesce rule, and a lint that flagged them would be
        flagging the code it is meant to protect. A write is told apart by shape
        rather than by position, because a single query holds both: an
        assignment target is always followed by `=`, and a read never is.
        """
        reads = 0
        for filename, entries in self.queries.items():
            for lineno, text in entries:
                if "scopeManual" not in text:
                    continue
                for m in re.finditer(r"scopeManual", text):
                    before = text[max(0, m.start() - 80):m.start()]
                    tight = text[m.end():m.end() + 2]   # is it an assignment?
                    wide = text[m.end():m.end() + 40]    # is it a coalesce arg?
                    if re.search(r"REMOVE\s+[A-Za-z_]\w*\.$", before):
                        continue  # a removal
                    if re.match(r"\s*=(?!=)", tight):
                        continue  # an assignment target: the stamp
                    reads += 1
                    self.assertRegex(
                        before, r"coalesce\(\s*[A-Za-z_]\w*\.$",
                        f"{filename}:{lineno} reads scopeManual outside a coalesce(..., false); "
                        f"an unset property is null, and `NOT null` excludes the row")
                    self.assertRegex(
                        wide, r"^\s*,\s*false\s*\)",
                        f"{filename}:{lineno} coalesces scopeManual to something other than "
                        f"false, so an absent property does not mean 'not manual'")
        self.assertGreaterEqual(reads, 3,
                                "expected the skip, count and RETURN reads to still exist")

    def test_the_stamp_sets_the_marker_not_only_the_signature(self):
        """A signature is written by the classifier too, so it proves nothing.

        Both writers stamped byte-identical scopeCheckedSig values, which is
        why nothing downstream could distinguish a decision from a guess.
        """
        segment = self.segment("_stamp_manual_scope")
        self.assertIn("scopeManual", segment)

    def test_both_diary_writers_mark_a_supplied_scope(self):
        for name in ("db_save_diary", "db_update_diary"):
            segment = self.segment(name)
            self.assertIn("_stamp_manual_scope", segment,
                          f"{name} links a supplied scope without marking it as chosen")

    def test_the_update_rebuilds_the_payload_with_the_scope_still_in_it(self):
        """The half that fails quietly.

        The update path builds its Qdrant payload from scratch and writes it
        with replace=True, so without the merge every edit dropped clientId /
        contextId from the vector store. The graph filter still showed the
        entry — the links were untouched — while client-filtered search
        silently stopped returning it, and nothing errored in between.

        Asserting the four key *names* over the whole function is what this used
        to do, and it does not bite: the same names appear in the query's RETURN
        aliases and in the scope-clearing assignments, so deleting the key from
        the payload dict it was written for, or replacing the alias with a
        literal `null`, both left it green (both verified by re-injection). So
        the two halves are located rather than searched for: the dict literal
        that carries the keys, and the pattern the aliases are derived from.
        """
        _source, node = self.functions["db_update_diary"]
        scope_keys = {"clientId", "clientName", "contextId", "contextName"}
        dicts = [
            (ast.unparse(n.targets[0]), n.value)
            for n in ast.walk(node)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
        ]
        carrying = [
            (name, value) for name, value in dicts
            if scope_keys <= {k.value for k in value.keys if isinstance(k, ast.Constant)}
        ]
        self.assertEqual(
            len(carrying), 1,
            msg="expected exactly one dict literal carrying all four scope keys; "
                f"found {[n for n, _ in carrying]}",
        )
        scope_name, _value = carrying[0]
        # ...and it has to reach the payload, or the keys are built and dropped.
        updates = [
            n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "update" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "payload"
        ]
        self.assertTrue(updates, msg="the rebuilt payload is never merged with the scope")
        self.assertTrue(
            any(scope_name in ast.unparse(call) for call in updates),
            msg=f"payload.update does not read {scope_name}, so the four scope "
                f"keys never reach the vector store: "
                f"{[ast.unparse(c) for c in updates]}",
        )
        # ...and the values have to come off the links, not off a stale payload.
        queries = "\n".join(
            _function_queries(os.path.join(HERE, "diary_manager.py"), "db_update_diary")
        )
        for rel, keys in (("FOR_CLIENT", ("clientId", "clientName")),
                          ("IN_CONTEXT", ("contextId", "contextName"))):
            match = re.search(rf"\(d\)-\[:{rel}\]->\(\s*(\w+)\s*:", queries)
            self.assertIsNotNone(
                match, msg=f"db_update_diary no longer reads the {rel} edge")
            sources = _alias_closure(queries, match.group(1))
            for key in keys:
                self.assertTrue(
                    any(re.search(rf"\b{re.escape(src)}\.\w+\s+as\s+{key}\b", queries)
                        for src in sources),
                    msg=f"the {key} alias is not derived from the {rel} edge "
                        f"(bound as {sorted(sources)}); a literal or a stale-payload "
                        f"value silently strips the scope from every edited entry",
                )

    def test_the_edit_endpoint_actually_forwards_the_scope(self):
        """The field was on the request body and nothing read it.

        A pre-filled client on the edit form was accepted, validated by
        pydantic, and discarded — so editing an entry unlinked it from the
        client it was filed under, with no error on either side.
        """
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as handle:
            gui = ast.parse(handle.read())
        endpoint = next(
            n for n in ast.walk(gui)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "api_update_diary_entry"
        )
        calls = [ast.unparse(n) for n in ast.walk(endpoint) if isinstance(n, ast.Call)]
        update = next(c for c in calls if "db_update_diary" in c)
        self.assertIn("client_id=client_id", update)
        self.assertIn("context_id=context_id", update)


class MergeDraftBudgetTests(unittest.TestCase):
    """How the merge draft's caller spends the budget the helper hands it.

    The draft prompt is the one uncapped prompt in the app, so its size is
    max_cluster x record length with nothing to stop it. The output budget used
    to be a constant sized from a four-record run, which served that run and
    truncated a twelve-record one mid-JSON -- a 502 with no cause, on a request
    that was really over budget and had a known remedy.

    The arithmetic now lives in matching_utils.merge_draft_output_budget, and is
    tested there by calling it, because the property is arithmetic on measured
    numbers. What is left here is the call site, which a test of the helper
    cannot see: that the model is given the helper's return value, and that the
    refusal survives the except chain on its way out.
    """

    def setUp(self):
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as handle:
            self.src = handle.read()
        self.tree = ast.parse(self.src)
        self.draft = next(
            n for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "api_generate_duplicate_draft"
        )
        self.calls = [n for n in ast.walk(self.draft) if isinstance(n, ast.Call)]

    def _llm_call(self):
        return next(c for c in self.calls if "get_llm_response" in ast.unparse(c.func))

    def test_the_budget_is_the_context_remainder_not_a_literal(self):
        """num_predict must be the helper's return value, and must be computed
        before the call.

        Both halves matter. A literal reappears the moment someone sizes a run
        from a selection that is not the largest one; and an assignment after
        the call would leave the guard checking a budget the model never got.
        """
        call = self._llm_call()
        kwarg = next(kw for kw in call.keywords if kw.arg == "num_predict")
        self.assertIsInstance(
            kwarg.value, ast.Name,
            msg="num_predict must be the budget the helper returned, not a "
                "literal and not an inline expression",
        )
        bindings = [
            n for n in ast.walk(self.draft)
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == kwarg.value.id for t in n.targets)
        ]
        self.assertEqual(
            len(bindings), 1,
            msg="the budget must be computed once; a second assignment means "
                "the value the model is given may not be the one that was checked",
        )
        binding = bindings[0]
        self.assertIsInstance(
            binding.value, ast.Call, msg="the budget must come from the helper"
        )
        self.assertIn(
            "merge_draft_output_budget", ast.unparse(binding.value.func),
            msg="the budget must be derived from the context, not a constant",
        )
        self.assertLess(
            binding.lineno, call.lineno,
            msg="the budget has to be resolved before the request goes out",
        )
        self.assertNotRegex(
            self.src, r"num_predict\s*=\s*\d",
            msg="a literal num_predict is back in gui.py",
        )
        self.assertNotIn(
            "MERGE_DRAFT_NUM_PREDICT", self.src,
            msg="the fixed budget is back; the answer scales with the selection",
        )

    def test_the_floor_default_is_the_measured_minimum(self):
        """Pin the floor default: 2,776 tokens is what the smallest draft costs."""
        assign = next(
            n for n in self.tree.body
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == "MERGE_DRAFT_MIN_NUM_PREDICT"
                    for t in n.targets)
        )
        self.assertIn("3000", ast.unparse(assign.value),
                      msg="measured 2,776 output tokens for 4 records; 3000 is "
                          "the floor under which nothing can finish")

    def test_the_refusal_is_not_swallowed_by_the_502_handler(self):
        """MergeDraftTooLarge is a ValueError, so handler order decides the status.

        Each fact alone is harmless and together they are the original bug: a
        ValueError subclass caught by the generic ValueError handler comes back
        as the 502 that the refusal exists to prevent, and the guard looks like
        it is there.
        """
        self.assertTrue(
            issubclass(MergeDraftTooLarge, ValueError),
            msg="the refusal must be a ValueError so an unhandled one is not a 500",
        )
        try_blocks = [n for n in ast.walk(self.draft) if isinstance(n, ast.Try)]
        self.assertEqual(
            len(try_blocks), 1,
            msg="the endpoint must have exactly one guarded block; the handler "
                "order being pinned is that block's",
        )
        try_block = try_blocks[0]
        catches = {}
        for index, handler in enumerate(try_block.handlers):
            caught = ast.unparse(handler.type) if handler.type else ""
            names = {caught, *re.findall(r"[A-Za-z_][A-Za-z_0-9]*", caught)}
            for name in names:
                catches.setdefault(name, []).append(index)
        self.assertEqual(
            len(catches.get("MergeDraftTooLarge", [])), 1,
            msg="exactly one handler may convert the refusal, or 'which one runs "
                "first' stops being a question with one answer",
        )
        self.assertEqual(
            len(catches.get("ValueError", [])), 1,
            msg="exactly one handler may catch ValueError; a second one means "
                "the order assertion below is measuring the wrong pair",
        )
        self.assertIn("ValueError", catches, msg="the 502 handler is gone")
        self.assertLess(
            catches["MergeDraftTooLarge"][0], catches["ValueError"][0],
            msg="the specific refusal is caught after the generic ValueError "
                "handler, so it surfaces as the 502 it exists to avoid",
        )

    def test_the_count_cap_is_enforced_where_the_records_are_sent(self):
        """api_find_duplicates caps the scan; the draft is a separate POST.

        It used to accept any number of records and rely on the char budget
        alone, so the cap the UI advertises was not the cap the draft honoured.

        The assertion is on the comparison that drives a raise, not on the
        constant appearing somewhere in the function: a message that quotes the
        cap satisfies a substring check even when the check itself is `if False`.
        """
        guards = [
            n for n in ast.walk(self.draft)
            if isinstance(n, ast.If)
            and "MERGE_MAX_CLUSTER" in ast.unparse(n.test)
            and "fact_ids" in ast.unparse(n.test)
        ]
        self.assertEqual(
            len(guards), 1,
            msg="the draft endpoint must refuse a selection larger than the cap "
                "the scan uses, in exactly one place",
        )
        guard = guards[0]
        for side in [guard.test.left, *guard.test.comparators]:
            for sub in ast.walk(side):
                self.assertNotIsInstance(
                    sub, ast.Constant,
                    msg="a literal in the cap comparison makes it a constant "
                        "that never fires",
                )
        raises = [
            n for n in ast.walk(guard)
            if isinstance(n, ast.Raise)
            and isinstance(n.exc, ast.Call)
            and "HTTPException" in ast.unparse(n.exc.func)
            and "400" in ast.unparse(n.exc)
        ]
        self.assertEqual(
            len(raises), 1,
            msg="the cap must refuse the request with a 400, not merely be read",
        )
        self.assertIn(
            "MERGE_MAX_CLUSTER", ast.unparse(next(
                n for n in ast.walk(self.tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "api_find_duplicates"
            )),
            msg="the cap must be the constant, not a literal",
        )

    def test_the_advertised_cluster_range_matches_the_measured_ceiling(self):
        """The 2-20 range was a lie: 20 records is a 13,123-token prompt."""
        scan = next(
            n for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "api_find_duplicates"
        )
        segment = ast.unparse(scan)
        self.assertIn("MERGE_MAX_CLUSTER", segment,
                      msg="the cap must be the constant, not a literal 20")
        self.assertFalse(
            re.search(r"max_cluster\s*(?:<=|>=|<|>)\s*20\b", segment),
            msg="the old 20-record cap is back",
        )
        self.assertFalse("must be between 2 and 20" in self.src,
                         msg="the old error message still promises 20")

    def test_the_template_and_the_server_read_one_cap(self):
        """A literal 20 in the input attribute is a silent drift from the server.

        The input's max, the client-side check and the server-side 400 are
        three places stating the same number. Passing it through the template
        context is what keeps them from disagreeing, which is the failure a
        user sees as "the UI allows 20 and the server refuses it".
        """
        self.assertIn('ctx["MERGE_MAX_CLUSTER"] = MERGE_MAX_CLUSTER', self.src)
        with open(os.path.join(HERE, "templates", "dashboard.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('max="{{MERGE_MAX_CLUSTER}}"', html)
        self.assertIn("maxCluster > MERGE_MAX_CLUSTER", html)
        self.assertFalse(
            re.search(r'id="dedup-max-cluster"[^>]*\bmax="20"', html),
            msg="the input still advertises 20 clusters",
        )


class MergeDraftEndpointTests(unittest.TestCase):
    """api_generate_duplicate_draft, actually run against a stub model.

    The other two suites check the arithmetic and the call site separately, and
    neither sees what the endpoint hands the model or what a user gets back when
    the selection is too big. Both are properties of running it. gui.py cannot be
    imported here (httpx, the drivers, FastAPI), so the function is lifted out
    of the source with ast.get_source_segment and executed against stubs.

    This is the test that would have caught the original defect: at 4,000 fixed
    output tokens a twelve-record selection was served a budget below what the
    draft costs, and the request went out to be truncated.
    """

    CONTEXT_TOKENS = 16_332
    CHARS_PER_TOKEN = 3.0
    MIN_NUM_PREDICT = 3_000
    OUTPUT_TOKENS_12 = 5_657  # what a twelve-record draft measures

    class _HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def _runner(self, text_chars, category="Notes"):
        """Exec the lifted endpoint; returns (run, calls)."""
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "api_generate_duplicate_draft"
        )
        calls = []

        async def fake_llm(prompt, system=None, model=None, num_predict=0):
            calls.append({"prompt": prompt, "num_predict": num_predict})
            return '{"name": "Merged", "text": "merged body"}'

        class _Mem:
            MERGE_MODEL = "stub-model"

            def db_get_fact_by_id(self, fact_id, user_id):
                return {"id": fact_id, "name": "note " + fact_id,
                        "text": "x" * text_chars, "category": category,
                        "metadata": {}}

            async def get_llm_response(self, prompt, system=None, model=None,
                                       num_predict=0):
                return await fake_llm(prompt, system=system, model=model,
                                      num_predict=num_predict)

        class _Body:
            def __init__(self, fact_ids):
                self.factIds = fact_ids

        namespace = {
            "HTTPException": self._HTTPException,
            "MemoryMergeDraft": _Body,
            "Request": object,
            "asyncio": __import__("asyncio"),
            "format_people_merge_text": lambda *a: "formatted",
            "json": __import__("json"),
            "mem": _Mem(),
            "re": __import__("re"),
            "merge_draft_output_budget": __import__("matching_utils").merge_draft_output_budget,
            "MergeDraftTooLarge": __import__("matching_utils").MergeDraftTooLarge,
            "MERGE_MAX_CLUSTER": 12,
            "MERGE_CONTEXT_TOKENS": self.CONTEXT_TOKENS,
            "MERGE_DRAFT_MIN_NUM_PREDICT": self.MIN_NUM_PREDICT,
            "MERGE_PROMPT_CHARS_PER_TOKEN": self.CHARS_PER_TOKEN,
            "_require_user": lambda request: "test-user",
            "_require_admin": lambda request: "test-user",
            "_service_unavailable": lambda exc: exc,
        }
        # The route decorator comes with the lifted source and refers to the
        # FastAPI app; it has no bearing on what the function does.
        func.decorator_list = []
        exec(compile(ast.Module(body=[func], type_ignores=[]), "gui.py", "exec"),
             namespace)
        endpoint = namespace["api_generate_duplicate_draft"]
        return endpoint, calls

    def _draft(self, count, text_chars, category="Notes"):
        endpoint, calls = self._runner(text_chars, category)
        body = type("B", (), {"factIds": ["f%d" % i for i in range(count)]})()
        return asyncio.run(endpoint(object(), body)), calls

    def test_a_twelve_record_draft_is_given_a_budget_that_can_finish_it(self):
        """The regression, end to end: 12 records is the advertised maximum."""
        result, calls = self._draft(12, 2_150)
        self.assertEqual(len(calls), 1, msg="the draft request never went out")
        self.assertGreaterEqual(
            calls[0]["num_predict"], self.OUTPUT_TOKENS_12,
            msg="the model was handed less than this selection's draft costs, so "
                "it will stop mid-JSON and the parse will report a 502",
        )
        self.assertEqual(
            calls[0]["num_predict"],
            self.CONTEXT_TOKENS
            - int(len(calls[0]["prompt"]) / self.CHARS_PER_TOKEN),
            msg="the budget must be exactly what the context left over, not a "
                "second, slightly different computation of it",
        )
        self.assertEqual(result["mergedName"], "Merged")

    def test_a_small_draft_still_works(self):
        result, calls = self._draft(2, 400)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["factIds"], ["f0", "f1"])

    # One property -- a selection the endpoint cannot serve is refused with a
    # 400 *before* the request goes out -- over the three ways a selection is
    # unservable, so one test covers the group. Each case carries its own record
    # count, text size and the wording its refusal owes, and every case asserts
    # that no LLM call was made, so an over-budget case can never be satisfied by
    # the count cap's message or the other way round.
    _REFUSALS = (
        # label, record count, text chars per record, substrings the detail owes
        ("over-budget text", 12, 40_000, ("12 records", "characters")),
        ("thirteen records, text that would fit", 13, 10, ("at most 12",)),
        ("a single record", 1, 400, ()),
    )

    def _refusal(self, label, count, text_chars, expected):
        endpoint, calls = self._runner(text_chars)
        body = type("B", (), {"factIds": ["f%d" % i for i in range(count)]})()
        with self.subTest(selection=label, records=count, chars=text_chars,
                          expected_detail=expected):
            with self.assertRaises(self._HTTPException) as caught:
                asyncio.run(endpoint(object(), body))
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(calls, [], msg="an unservable selection was sent anyway")
            for phrase in expected:
                self.assertIn(phrase, caught.exception.detail)

    def test_an_unservable_selection_is_a_400_before_the_request_goes_out(self):
        """Not a 502: the request is never issued, and the message says why."""
        for label, count, text_chars, expected in self._REFUSALS:
            self._refusal(label, count, text_chars, expected)

    def test_a_people_draft_is_formatted_through_the_schema(self):
        result, calls = self._draft(2, 400, category="People")
        self.assertEqual(result["mergedText"], "formatted")


class DedupUnderServiceTests(unittest.TestCase):
    """Deduplicate lives on the admin-only Service page.

    Two failure modes, both silent. A leftover `switchTab('deduplicate')`
    dereferences a page div that no longer exists, and because that runs
    inside init() the whole page comes up dead. And `activeTab` is persisted
    in localStorage, so a returning user still has 'deduplicate' saved even
    once the button is gone.
    """

    def setUp(self):
        with open(os.path.join(HERE, "templates", "dashboard.html"), encoding="utf-8") as handle:
            self.html = handle.read()
        self.script = "\n".join(
            re.findall(r"<script>(.*?)</script>", self.html, re.S))

    def test_the_dedup_tab_is_gone_from_the_rail(self):
        self.assertNotIn('id="page-deduplicate"', self.html,
                         msg="the standalone dedup page is still present")
        self.assertNotIn("switchTab('deduplicate')", self.html,
                         msg="a tab button or link still points at the removed page")

    def test_the_dedup_controls_live_inside_the_service_page(self):
        """Containment, not adjacency — being near Service is not being in it."""
        start = self.html.index('<div id="page-service"')
        depth, end = 0, None
        for match in re.finditer(r"<div\b|</div>", self.html[start:]):
            depth += 1 if match.group(0) != "</div>" else -1
            if depth == 0:
                end = start + match.end()
                break
        self.assertIsNotNone(end, msg="could not find the end of the service page")
        service = self.html[start:end]
        for control in ("dedup-max-cluster", "dedup-threshold", "dedup-category",
                        "dedup-scan-btn", "dedup-status", "dedup-clusters"):
            self.assertIn(control, service,
                          msg=f"{control} is not inside the Service page")

    def test_a_stale_persisted_tab_cannot_kill_init(self):
        """The tab name outlives the tab, in localStorage, across deploys.

        switchTab used to do `document.getElementById('page-' + tab).classList`
        on whatever came out of storage. A null there is a TypeError thrown
        from init(), which is a blank page rather than a wrong tab.
        """
        self.assertTrue(
            re.search(r"const\s+page\s*=\s*document\.getElementById\('page-'\s*\+\s*tab\)",
                      self.script),
            msg="switchTab must look the page up before dereferencing it",
        )
        self.assertIn("if (!page)", self.script,
                      msg="a missing page must be handled, not assumed away")
        self.assertIn("saveSessionState()", self.script,
                      msg="the fallback must be persisted, or it returns every reload")

    def test_entering_setup_still_primes_the_dedup_panel(self):
        """Moving the panel means moving its lazy first-render with it.

        The empty state used to be primed by a `tab === 'deduplicate'` branch.
        Drop the branch while moving the markup and the panel renders as a
        blank div until the user presses Scan — which looks like a broken
        button, because pressing it works.
        """
        self.assertIn("renderDuplicateEmptyState", self.script)
        self.assertTrue(
            re.search(r"tab\s*===\s*'setup'[\s\S]{0,400}renderDuplicateEmptyState", self.script),
            msg="entering Setup no longer primes the dedup empty state",
        )


# The queries whose rows are pushed one-per into a client array and rendered.
# Derived from the blast radius, not from taste: a repeated row in any of these
# is a repeated card. `db_list_clients` is here because a report of "the client
# is showing up twice" was answered by the same fan-out as the diary list, which
# is the cost of scoping this to two functions on the first attempt -- the
# analyzer had already flagged the query and the scope was narrowed anyway.
_RENDERED_LIST_QUERIES = (
    ("diary_manager.py", "db_list_diary"),
    ("fact_manager.py", "db_list_memories"),
    ("client_manager.py", "db_list_clients"),
)


def _function_queries(path, function_name):
    """Every Cypher string literal inside one function, docstrings excluded.

    A docstring is prose that quotes the query's own vocabulary, so including it
    would make the structural checks read the explanation of the fix as another
    copy of the thing being fixed — which is the `assertIn`-over-a-whole-file
    lesson in a new place.
    """
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    func = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == function_name)
    for node in ast.walk(func):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings and _CYPHER_RE.search(node.value)):
            yield node.value


def _alias_closure(queries, bound):
    """Names whose value is derived from ``bound``, following ``AS`` aliases.

    A RETURN alias rarely reads the pattern variable directly: this query reads
    ``(d)-[:FOR_CLIENT]->(c:Client)``, folds it with
    ``head(collect({id: c.id, ...})) AS cl``, and only then projects
    ``cl.id as clientId``. Pinning ``c.id as clientId`` would be pinning one
    rewrite of that; what matters is that the alias chain reaches the edge at
    all, so an alias is admitted when its defining projection mentions a name
    already in the set. Fixed-point over a short chain, not a single hop.
    """
    known = {bound}
    for _ in range(4):
        grew = False
        for match in re.finditer(r"\bAS\s+(\w+)", queries, re.I):
            alias = match.group(1)
            if alias in known:
                continue
            window = queries[max(0, match.start() - 160):match.start()]
            if any(re.search(rf"\b{re.escape(name)}\b", window) for name in known):
                known.add(alias)
                grew = True
        if not grew:
            break
    return known


def _row_reads(func):
    """The string keys read off a *result row*, not off a node.

    Two distinctions matter and an earlier version of this got both wrong:

    * it collected every `x["key"]` in the function, which included
      `f_node["timestamp"]` — a property of the `Fact` node the row carries, not
      a column of the row. Requiring `timestamp` to appear in the query then
      failed a correct query, which is the failure mode that makes a lint get
      switched off.
    * the loop variable is identified by iterating **the driver result**, so
      only a `for`/comprehension over a plain Name counts. `db_list_clients`
      walks `r["contexts"]` in an inner comprehension, and treating `ctx` as a
      row made `id`, `name` and `active` look like dropped columns of the query.
    """
    targets = set()
    for node in ast.walk(func):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if isinstance(node.iter, ast.Name):
                targets.add(node.target.id)
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
            if isinstance(node.iter, ast.Name):
                targets.add(node.target.id)

    reads = set()
    for node in ast.walk(func):
        base = None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            base = node.value.id
            key = node.slice
        elif (isinstance(node, ast.Call) and node.args
              and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("get", "getOrDefault")
              and isinstance(node.func.value, ast.Name)):
            base = node.func.value.id
            key = node.args[0]
        if base in targets and isinstance(key, ast.Constant) and isinstance(key.value, str):
            reads.add(key.value)
    return reads


class OneRowPerRecordTests(unittest.TestCase):
    """Chained `OPTIONAL MATCH`es return a cross product, not a record list.

    A `MATCH`/`OPTIONAL MATCH` on a *relationship* pattern multiplies the rows
    it produces. Two such patterns in the same clause chain therefore return
    their product, so a record with five MENTIONS and one RELEVANT_TO comes
    back five times, each row carrying the same `d.id` and the same date.
    `collect(DISTINCT ...)` in the RETURN does not repair it: it deduplicates
    *within* a row, it does not merge rows.

    This shipped. `db_list_diary` chained four of them, so the diary list the
    client renders held the same entry once per combination — and only for
    entries written since auto-linking existed, which is why it read as "the
    new ones are duplicated" rather than as a data problem.

    The fix is structural: every expanding pattern is followed by its own
    aggregating `WITH` before the next one, so the query is back to one row per
    record. The check below derives that property from the clause structure
    rather than searching for a needle, so a query written later is covered by
    the same rule and a fix cannot be faked by rewording a comment.

    Single-valued patterns are exempt (`(d)-[:IN_CONTEXT]->(ctx:Context)` where
    an entry is linked to at most one project) but are *also* folded with
    `head(collect(...))` in the fixed query, because "at most one" is a
    property of the writers, not of the schema: a second edge would double the
    rows again. An `OPTIONAL MATCH` that matches nothing still yields one row
    with a null, so `head` is safe and never drops the record.

    **A lint that demands a rewrite of correct code is worse than no lint**, so
    this models all three ways a fan-out is legitimately collapsed, not just
    the one the fix happened to use:

    * an aggregating `WITH` between the patterns;
    * `RETURN DISTINCT`, which collapses whole rows;
    * a final `RETURN` that collects **every** variable the expanding patterns
      introduced. `fact_manager`'s merge query chains two expansions and then
      collects both, which is correct — collecting the full out/in sets with
      DISTINCT and returning them together is the right way to write it, and
      rewriting it "to satisfy" this lint would be damage.

    It also skips queries whose caller is not asking for a record list, which
    is the third thing that made a blanket rule unusable: the graph query that
    returns `(ctx, n, c)` triples is a join, and its rows are meant to repeat.
    Those are covered by a separate, narrower assertion rather than by
    pretending the fan-out does not exist.
    """

    # A clause that can multiply rows: a relationship pattern, or a bare node
    # pattern (kept, because a second node pattern is equally a second row).
    # OPTIONAL is part of the keyword so the two are not split apart.
    _CLAUSE = re.compile(
        r"\b(OPTIONAL\s+MATCH|MATCH|WITH|RETURN|UNWIND|MERGE|CREATE)\b", re.I)
    _AGGREGATE = re.compile(
        r"\b(collect|head|size|count|sum|avg|min|max|stdev|percentileCont|"
        r"percentileDisc|collectDistinct)\s*\(", re.I)
    # One relationship edge per node, per the writers' discipline: a record has
    # one category, one client and one project, and every setter replaces the
    # edge rather than adding to it. These cannot fan out a row, so a query that
    # only chains them is safe *and does not need rewriting* — flagging those
    # would be demanding churn on correct code, which is how a lint starts being
    # ignored.
    _SINGLE_VALUED_TYPES = frozenset({
        "IN_CATEGORY", "FOR_CLIENT", "IN_CONTEXT", "HAS_CONTEXT", "PART_OF",
    })
    # The node a relationship pattern binds, e.g. `-[:MENTIONS]->(f:Fact)`.
    _BINDS = re.compile(r"(<-\[[^\]]*\]-|\)-\[[^\]]*\]->|->\[[^\]]*\]-)\s*\(\s*(\w+)\s*[:)]")

    def _clauses(self, query):
        """The read half of a query, as a list of (keyword, body) clauses.

        Split on clause keywords so each MATCH's *pattern* can be judged on its
        own: a WHERE or a RETURN that follows belongs to the next clause, and
        treating them as part of the pattern is how a lint starts reading
        English as Cypher.
        """
        head = re.split(r"\b(?:MERGE|CREATE)\b", query, maxsplit=1, flags=re.I)[0]
        if not re.search(r"\bRETURN\b", head, re.I):
            return []
        parts = self._CLAUSE.split(head)
        out = []
        for i in range(1, len(parts) - 1, 2):
            out.append((parts[i].upper(), parts[i + 1]))
        return out

    def _expands(self, body):
        """Does this pattern bind a *many-valued* thing?

        Four things count, and the distinctions matter:

        * a relationship whose type is not in `_SINGLE_VALUED_TYPES` —
          MENTIONS, RELEVANT_TO, and anything added later;
        * an **anonymous** relationship (`-[r]->`, `-[]->`), because an unknown
          type is not evidence of a single edge;
        * a bare node pattern with **no** property map (`MATCH (c:Category)`),
          which is a join by definition.

        Two things that look like the above are not, and each one produced a
        green test that was green for the wrong reason:

        * the **tail node of a relationship pattern**. `(d)-[:FOR_CLIENT]->(cl)`
          matches "a node with a label and no property map", so counting it made
          every scoped query look like a fan-out — and then `withs == patterns`
          happened to hold anyway, because the extra counts cancelled out.
          Relationship patterns are stripped before the node check.
        * a node pattern **with** a property map is a lookup, not a join:
          `MATCH (d:DiaryEntry {id: $did, userId: $userId})` addresses one node
          and cannot fan out, and treating it as expandable flags every query
          that starts with the record it is about.
        """
        arrows = r"<-\[[^\]]*\]-|\)-\[[^\]]*\]->|->\[[^\]]*\]-|--|<-"
        for types in re.findall(arrows, body):
            spec = types
            named = re.findall(r":\s*(\w+)", spec)
            if not named or not set(named) <= self._SINGLE_VALUED_TYPES:
                return True
        # A relationship is a relationship even when it is written without a
        # type (`-[]->`), so strip the whole construct before hunting for a
        # standalone node pattern.
        rest = re.sub(arrows, " ", body)
        for node in re.findall(r"\(\s*\w+\s*:[^()]*\)", rest):
            if "{" not in node:
                return True
        return False

    @staticmethod
    def _bound_variables(body):
        """Variables introduced by a fan-out: the *other* end of the edge.

        The record itself is not one of them — it is what the query is
        projecting, and it is the thing being repeated.
        """
        found = set()
        for types, name in OneRowPerRecordTests._BINDS.findall(body):
            spec = types
            named = re.findall(r":\s*(\w+)", spec)
            if not named or not set(named) <= OneRowPerRecordTests._SINGLE_VALUED_TYPES:
                found.add(name)
        return found

    @staticmethod
    def _record_projection(return_body, record_vars):
        """Does the RETURN hand back the record itself, rather than only aggregates?

        This is the distinction that separates a real defect from a correct
        query that happens to chain two patterns. The merge query collects the
        full out/in relationship sets and projects no node, so every one of its
        repeated rows carries the *complete* answer and reading one is correct.
        `db_list_memories` collects the same way but also returns `f`, so each
        repeated row is another copy of the fact and the caller pushes all of
        them. A collect in the RETURN collapses values *within* a row; it never
        merges rows, and it only makes the repetition harmless when there is no
        record identity left to repeat.

        Function calls are blanked first, so `rc.id` inside `collect(...)` is
        not mistaken for a projected variable.
        """
        body = return_body
        # Strip innermost calls repeatedly: collect(...), head(collect(...)),
        # coalesce(other.text, other.content) -- all aggregates, not projections.
        previous = None
        while previous != body:
            previous = body
            body = re.sub(r"\w+\s*\([^()]*\)", " ", body)
        return sorted(v for v in record_vars if re.search(rf"\b{re.escape(v)}\b", body))

    def _unaggregated_expansions(self, query):
        """Fan-outs that are never collapsed, in a query that returns records.

        Returns a list of human-readable offenders, empty when the query is
        sound. A fan-out is collapsed by an aggregating `WITH` after it, or by
        `RETURN DISTINCT` (which deduplicates whole rows). Either way the
        repetition is only *harmless* when the query projects no record
        identity, because a collect collapses values within a row and never
        merges rows — see `_record_projection`.

        Note that a **single** uncollapsed fan-out is already a defect, not
        only a chain of two. An entry with two `RELEVANT_TO` links and one
        `OPTIONAL MATCH` for them is returned twice. Chaining is the worse case
        because the multiplier is the product, but the rule is the same and
        the first fix for `db_list_diary` got this wrong: it added an
        aggregating `WITH` after each pattern *except the last*, which turned a
        mentions×relevant product into a single relevant multiplier and still
        duplicated every cross-referenced entry.
        """
        clauses = self._clauses(query)
        if not clauses:
            return []
        record_vars = set()
        uncleared = []
        returns = []
        for keyword, body in clauses:
            if not record_vars and keyword in ("MATCH", "OPTIONAL MATCH"):
                # The first clause says what the query is *about*; everything
                # after it is enrichment. These are the variables a caller
                # would receive one-per, so a repeat is a repeat of the record.
                record_vars = set(re.findall(r"\(\s*(\w+)\s*[:)]", body))
                record_vars |= self._bound_variables(body)
            if keyword == "WITH":
                if self._AGGREGATE.search(body):
                    uncleared = []
                continue
            if keyword in ("MATCH", "OPTIONAL MATCH"):
                if self._expands(body):
                    uncleared.append(" ".join(body.split())[:60])
                continue
            if keyword == "RETURN":
                returns.append(body)
                if re.match(r"\s*DISTINCT\b", body, re.I):
                    return []          # whole rows are deduplicated
        if not uncleared:
            return []
        projected = sorted({v for body in returns
                            for v in self._record_projection(body, record_vars)})
        if not projected:
            return []                  # aggregates only: the repeats are harmless
        return [f"{o}  (projecting {', '.join(projected)})" for o in uncleared]

    def test_the_list_queries_are_one_row_per_record(self):
        """Scoped to the queries whose rows become a rendered list.

        `_RENDERED_LIST_QUERIES` is the whole blast radius of this defect, and
        it is what made the defect visible at all: a repeated row in any of them
        is a repeated card. An earlier version of this test scoped it to two
        functions and left `db_list_clients` out on the grounds that only the
        diary and fact lists were reachable — and then a report of "the Deutsche
        Bank client is showing up twice" turned out to be this same fan-out, on
        `HAS_CONTEXT`. The analyzer had already flagged that query and the scope
        was narrowed anyway.

        It is *not* applied to the other ~30 chained-pattern queries, and the
        reason is worth stating rather than leaving as an omission: none of them
        is reachable as a rendered list, and no Cypher can be executed here to
        check a rewrite. Rewriting thirty unrunnable queries to satisfy a lint is
        how the `FOREACH` bug in this file's docstring happened in the first
        place. When one of those does become a record list, add it here.
        """
        # The analyzer is the guard, so the guard is only as good as the
        # analyzer. It is driven here on the shape this whole class exists for
        # — two expansions with a projecting `WITH` between them, then a RETURN
        # projecting the record — because a derivation that stopped flagging
        # anything would otherwise leave this loop passing on every query.
        self.assertNotEqual(
            self._unaggregated_expansions(
                "MATCH (d:DiaryEntry {userId: $userId}) "
                "OPTIONAL MATCH (d)-[:MENTIONS]->(p:People) WITH d, p "
                "OPTIONAL MATCH (d)-[:RELEVANT_TO]->(c:Client) "
                "RETURN d, collect(p) AS mentions"
            ),
            [],
            msg="the analyzer no longer flags the fan-out it was written for, so "
                "the check below is passing on everything",
        )
        self.assertEqual(
            self._unaggregated_expansions(
                "MATCH (d:DiaryEntry {userId: $userId}) "
                "OPTIONAL MATCH (d)-[:MENTIONS]->(p:People) WITH d, collect(p) AS mentions "
                "OPTIONAL MATCH (d)-[:RELEVANT_TO]->(c:Client) "
                "WITH d, mentions, collect(c) AS relevant "
                "RETURN d, mentions, relevant"
            ),
            [],
            msg="the analyzer now flags the collapsed form, so it would demand "
                "churn on the correct queries this lint exists to protect",
        )
        for module, function in _RENDERED_LIST_QUERIES:
            with self.subTest(function=function):
                for query in _function_queries(os.path.join(HERE, module), function):
                    with self.subTest(query=" ".join(query.split())[:40]):
                        self.assertEqual(
                            self._unaggregated_expansions(query), [],
                            f"{module}:{function} returns one row per "
                            f"relationship combination, and the client renders "
                            f"every row as a separate card",
                        )

    def test_no_with_between_two_patterns_fails_to_aggregate(self):
        """`WITH d, cl, ctx` is the shape the bug had, verbatim.

        A WITH that only projects looks like it tidies the query and collapses
        nothing, so it reads as a style choice. It is the difference between one
        row per entry and one row per combination.
        """
        for module, function in _RENDERED_LIST_QUERIES:
            with self.subTest(function=function):
                withs = 0
                for query in _function_queries(os.path.join(HERE, module), function):
                    clauses = self._clauses(query)
                    withs += sum(1 for keyword, _body in clauses if keyword == "WITH")
                    for keyword, body in clauses:
                        if keyword != "WITH":
                            continue
                        with self.subTest(with_body=" ".join(body.split())[:60]):
                            self.assertRegex(
                                body, self._AGGREGATE,
                                "a WITH here projects without aggregating, so "
                                "the rows the previous pattern fanned out are "
                                "still fanned out",
                            )
                # Every assertion above lives inside the loop, so a query with no
                # WITH at all — or a clause splitter that stopped recognising one
                # — would leave this case green without having looked at
                # anything.
                self.assertGreater(withs, 0, f"{function}: no WITH clause found to check")

    def test_no_aggregation_happens_in_the_return(self):
        """The trailing `OPTIONAL MATCH` needs a `WITH` too.

        This is the shape of the first, wrong fix: aggregate between each pair
        of patterns, and let the last one collect in the RETURN. That turns a
        mentions×relevant product into a single relevant multiplier — better,
        and still wrong, because an entry with two cross-references comes back
        twice. A `collect` in the RETURN deduplicates values *within* a row; it
        can never merge rows, so it is the wrong place for the last collapse.
        """
        for module, function in _RENDERED_LIST_QUERIES:
            with self.subTest(function=function):
                returns = 0
                for query in _function_queries(os.path.join(HERE, module), function):
                    for keyword, body in self._clauses(query):
                        if keyword != "RETURN":
                            continue
                        returns += 1
                        with self.subTest(return_body=" ".join(body.split())[:60]):
                            self.assertNotRegex(
                                body, r"\bcollect\s*\(",
                                "aggregating in the RETURN cannot collapse the "
                                "rows; the last pattern needs its own WITH",
                            )
                # Same shape as the WITH check: without a RETURN there is
                # nothing to assert on, and this would pass without looking.
                self.assertGreater(returns, 0, f"{function}: no RETURN clause found to check")

    def test_every_field_the_python_reads_is_still_returned(self):
        """A `WITH` chain renames things on the way to the RETURN.

        Moving the collects into `WITH` clauses means the RETURN no longer
        derives them inline, so an alias can be dropped on the way through — and
        the failure is a `KeyError` on the first list load after the deploy, in
        a query nobody can run locally.

        The direction is **read ⊆ returned**, not the reverse. A Python read that
        the query no longer projects is silent (a `KeyError`, or a field quietly
        absent from every card). The opposite — a returned alias nobody reads —
        is harmless, and checking for it was what made an earlier version of
        this test pass on a query whose WITH alias no longer matched its RETURN:
        the renamed `ctxId` was not on the list of names being looked for, so
        the test had nothing to say. An unbound variable in the RETURN is loud
        and needs no guard.

        Scoped to the **RETURN** clause, not the whole query: a `WITH` that
        introduces `relevantClients` leaves the name lying around even after it
        has been dropped from the projection, so searching the whole string
        passed on a query that no longer returns the field at all.
        """
        for module, function in _RENDERED_LIST_QUERIES:
            with self.subTest(function=function):
                with open(os.path.join(HERE, module), encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                func = next(n for n in ast.walk(tree)
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and n.name == function)
                reads = _row_reads(func)
                # An empty read set makes the loop below vacuous, and the
                # derivation is exactly the sort of thing that rots silently: a
                # loop variable that stops being a bare Name, or a `row["x"]`
                # rewritten as `row.get("x")` on a different receiver, empties it
                # without failing anywhere.
                self.assertGreater(len(reads), 0,
                                   f"{function}: no row reads were derived, so there "
                                   f"is nothing to check the RETURN against")
                query = "\n".join(_function_queries(os.path.join(HERE, module), function))
                analyzer = OneRowPerRecordTests()
                returned = " ".join(body for keyword, body in analyzer._clauses(query)
                                    if keyword == "RETURN")
                self.assertTrue(returned, f"{function}: no RETURN clause found")
                for field in sorted(reads):
                    with self.subTest(field=field):
                        self.assertRegex(
                            returned, rf"\b{re.escape(field)}\b",
                            f"{function} reads `{field}` off every row but the "
                            f"RETURN no longer projects it, so the list raises a "
                            f"KeyError on the first load after deploy",
                        )


class ScopeIsRankedNotFilteredTests(unittest.TestCase):
    """`client`/`context` must rank search results, never delete them.

    The search path used to *filter* by scope, in two places at once: a Qdrant
    `FieldCondition` on the payload key `clientName`, and a Cypher `EXISTS((f)
    -[:FOR_CLIENT]->(:Client {name: $clientName}))`. Both compared a raw user
    string against the *stored* name, so `"DB"` against the node `Deutsche Bank
    (DB)` matched nothing and the search returned an empty list with no error.
    The filter was also self-defeating: the out-of-scope candidates it removed
    are precisely the ones `scope_strength` ranks below the in-scope ones, and
    the `+0.3` boosts that ran afterwards could therefore never fire.

    So these are the wrong-query-shaped guards here, not the search-function
    guards: the property being wrong is that the filter exists at all, and
    `assertNotIn("clientName")` over a whole file would also match the payload
    *reads* that are legitimate.
    """

    FILTERS = ("clientName", "contextName")

    def _sources(self):
        for path in _python_sources():
            with open(path, encoding="utf-8") as handle:
                yield os.path.basename(path), ast.parse(handle.read())

    def test_no_vector_filter_on_the_stored_scope_payload_keys(self):
        checked = 0
        for filename, tree in self._sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != "FieldCondition":
                    continue
                checked += 1
                # `FieldCondition("userId", match=...)` and
                # `FieldCondition(key="userId", ...)` are both in the tree, so
                # resolve the key from whichever form was written — the removed
                # scope filters used the positional one, and a positional-only
                # reader is a test that passes on the bug.
                key_node = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == "key"), None)
                name = key_node.value if isinstance(key_node, ast.Constant) else None
                self.assertNotIn(
                    name, self.FILTERS,
                    msg=(f"{filename}:{node.lineno} filters Qdrant on the stored "
                         f"scope payload key {name!r} — scope must rank, not exclude"),
                )
        self.assertGreater(checked, 0, msg="no FieldCondition found; the lint is not running")

    def test_the_exact_match_pass_has_no_scope_predicate(self):
        """The Neo4j name/alias pass must return every candidate, scoped or not.

        `db_search_memories` is the function whose own docstring promises scope
        never removes a result, so the guarantee is pinned there rather than on
        the string `"$clientName"` — which is also what the broken query
        contained, and `assertIn("$clientName", query)` passed on it happily.
        """
        with open(os.path.join(HERE, "fact_manager.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        functions = {n.name: n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        segment = ast.get_source_segment(source, functions["db_search_memories"])
        # The query is assembled from fragments, so look at the whole function
        # for the predicate shapes rather than at one literal.
        for needle in ("$clientName", "$contextName", "FOR_CLIENT]->(:Client",
                       "IN_CONTEXT]->(:Context"):
            self.assertFalse(
                needle in segment,
                msg=(f"db_search_memories builds a scope filter on {needle!r}; scope "
                     "ranks results and never removes them"),
            )
        # The scope is still read — it just comes from the ranking block.
        self.assertIn("db_plan_search_scope", segment)
        self.assertIn("scope_strength", segment)

    def test_the_relevant_to_read_covers_facts_and_diary_entries(self):
        _, query = self._relevant_to_query()
        self.assertIn("(n:Fact OR n:DiaryEntry)", query,
                      msg=(":Fact alone drops every diary entry, which is exactly "
                           "where RELEVANT_TO is the only scope evidence"))
        self.assertIn("RELEVANT_TO", query)

    def test_the_relevant_to_read_distinguishes_a_context_target(self):
        _, query = self._relevant_to_query()
        self.assertRegex(query, r"CASE\s+WHEN\s+rc:Context\s+THEN\s+'context'")
        self.assertFalse(
            re.search(r"OPTIONAL MATCH \(n\)-\[:RELEVANT_TO\]->\((\w+):Client\)", query),
            msg="RELEVANT_TO may target a Context; matching :Client drops project links",
        )

    def _relevant_to_query(self):
        """The one batched read, not the three existing RELEVANT_TO reads.

        Two other queries already collect RELEVANT_TO targets for a *detail*
        view (`db_get_fact` and the diary detail read), and they legitimately
        return a whole record rather than a ranking map. The batched read is
        identified by the id-list predicate and the `AS relevant` alias, which is
        why the selector is a shape and not the relationship name.

        The relationship name used to be part of the selector as well, which made
        ``assertIn("RELEVANT_TO", query)`` in the test above unsatisfiable — the
        query cannot fail an assertion about a string it was selected by. The
        alias and the id list are still unique to this read (checked: one match
        across every module), so the assertion now has something to say: a read
        that stopped reading RELEVANT_TO at all would be found here and fail.
        """
        queries = []
        for path in _python_sources():
            for lineno, query in _cypher_strings(path):
                if "n.id IN $ids" in query and "AS relevant" in query:
                    queries.append((os.path.basename(path), query))
        self.assertEqual(
            len(queries), 1,
            msg=f"expected exactly one batched RELEVANT_TO read, found {len(queries)}",
        )
        return queries[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)
