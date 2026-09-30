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
        """
        segment = self.segment("db_update_diary")
        for key in ("clientId", "clientName", "contextId", "contextName"):
            self.assertIn(key, segment, f"db_update_diary no longer carries {key} into the payload")
        self.assertIn("payload.update", segment)
        # And the source of that scope has to be the link, not a stale payload.
        self.assertIn("FOR_CLIENT", segment)
        self.assertIn("IN_CONTEXT", segment)

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

    def test_an_oversized_selection_is_a_400_naming_the_remedy(self):
        """Not a 502: the request is never issued, and the message says why."""
        endpoint, calls = self._runner(40_000)
        body = type("B", (), {"factIds": ["f%d" % i for i in range(12)]})()
        with self.assertRaises(self._HTTPException) as caught:
            asyncio.run(endpoint(object(), body))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(calls, [], msg="an over-budget selection was sent anyway")
        for expected in ("12 records", "characters"):
            self.assertIn(expected, caught.exception.detail)

    def test_thirteen_records_is_refused_even_though_the_text_would_fit(self):
        """The count cap is its own guard, not a side effect of the char budget."""
        endpoint, calls = self._runner(10)
        body = type("B", (), {"factIds": ["f%d" % i for i in range(13)]})()
        with self.assertRaises(self._HTTPException) as caught:
            asyncio.run(endpoint(object(), body))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("at most 12", caught.exception.detail)
        self.assertEqual(calls, [])

    def test_one_record_is_refused(self):
        endpoint, calls = self._runner(400)
        with self.assertRaises(self._HTTPException) as caught:
            asyncio.run(endpoint(object(), type("B", (), {"factIds": ["f0"]})()))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(calls, [])

    def test_a_people_draft_is_formatted_through_the_schema(self):
        result, calls = self._draft(2, 400, category="People")
        self.assertEqual(result["mergedText"], "formatted")


class DedupUnderSetupTests(unittest.TestCase):
    """Deduplicate moved from the top-level tab rail into the Setup page.

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

    def test_the_dedup_controls_live_inside_the_setup_page(self):
        """Containment, not adjacency — being near Setup is not being in it."""
        start = self.html.index('<div id="page-setup"')
        depth, end = 0, None
        for match in re.finditer(r"<div\b|</div>", self.html[start:]):
            depth += 1 if match.group(0) != "</div>" else -1
            if depth == 0:
                end = start + match.end()
                break
        self.assertIsNotNone(end, msg="could not find the end of the setup page")
        setup = self.html[start:end]
        for control in ("dedup-max-cluster", "dedup-threshold", "dedup-category",
                        "dedup-scan-btn", "dedup-status", "dedup-clusters"):
            self.assertIn(control, setup,
                          msg=f"{control} is not inside the Setup page")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
