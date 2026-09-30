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

    def test_single_item_reclassify_refuses_before_it_clears(self):
        """Order is the whole guard: the clear is what makes it unrecoverable."""
        for name in ("_reclassify_single_fact", "_reclassify_single_diary"):
            segment = self.segment(name)
            guard = segment.find("_assert_not_manual_scope")
            clear = segment.find("clear_scope_links_async")
            self.assertNotEqual(guard, -1, f"{name} has no manual-scope guard")
            self.assertNotEqual(clear, -1, f"{name} no longer clears its links")
            self.assertLess(guard, clear,
                            f"{name} clears the links before checking whether they were chosen")

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
    """The merge draft's prompt is the one uncapped prompt in the app.

    Everything else that talks to Ollama sends a single window. The merge draft
    json.dumps the full text of every selected record, so its size is
    max_cluster x record length with nothing to stop it. Two separate ceilings
    follow from that and both used to be fiction: num_predict was 900 (below
    what a 4-record merge spends) and max_cluster was advertised up to 20 (a
    prompt that no output budget can fit alongside).
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

    def test_the_output_budget_covers_a_measured_merge(self):
        """900 was below the 2,776 tokens a 4-record merge actually spends.

        The model ran out of budget mid-JSON, never emitted the closing brace,
        and the `re.search(r"\\{.*\\}")` parse found nothing — so the only
        symptom was a 502 with no cause. Assert the literal is gone and the
        knob is used, rather than asserting a number that will drift.
        """
        calls = [ast.unparse(n) for n in ast.walk(self.draft) if isinstance(n, ast.Call)]
        llm = next(c for c in calls if "get_llm_response" in c)
        self.assertIn("num_predict=MERGE_DRAFT_NUM_PREDICT", llm,
                      msg="the draft must use the sized budget, not a literal")
        self.assertNotIn("num_predict=900", llm)
        self.assertFalse("num_predict=900" in self.src,
                         msg="the 900-token budget is back somewhere in gui.py")

    def test_the_budget_default_is_the_measured_value(self):
        """Pin the default, because a wrong default reintroduces the 502."""
        assign = next(
            n for n in self.tree.body
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == "MERGE_DRAFT_NUM_PREDICT" for t in n.targets)
        )
        self.assertIn("4000", ast.unparse(assign.value),
                      msg="measured 2,776 tokens for 4 records; 4000 is the floor")

    def test_an_oversized_selection_is_refused_before_the_llm_call(self):
        """A selection too large to serve is a 400, not an unexplained failure.

        The guard has to run *before* the call. A check placed after it would
        still be a check, and the test would still pass, while the request
        still went out to be truncated.
        """
        self.assertTrue(
            any(isinstance(n, ast.Raise) for n in ast.walk(self.draft)),
            msg="no guard rejects an over-budget selection",
        )
        raise_line, call_line = None, None
        for n in ast.walk(self.draft):
            if isinstance(n, ast.Raise):
                exc = n.exc
                if isinstance(exc, ast.Call) and "HTTPException" in ast.unparse(exc.func):
                    detail = ast.unparse(exc)
                    if "tokens" in detail and "draft" in detail:
                        raise_line = n.lineno
            if isinstance(n, ast.Call) and "get_llm_response" in ast.unparse(n):
                call_line = n.lineno
        self.assertIsNotNone(raise_line, msg="the over-budget guard is missing")
        self.assertIsNotNone(call_line)
        self.assertLess(raise_line, call_line,
                        msg="the guard must run before the LLM call, not after it")

    def test_the_guard_is_not_written_as_a_dead_comparison(self):
        """`if estimated > 0` is a comparison that can never fire.

        This is the same shape as the `$ids` bug: a condition that reads like
        a guard and evaluates to a constant. Derived from the AST, not grepped,
        so a renamed local cannot quietly disarm it.
        """
        compares = [n for n in ast.walk(self.draft) if isinstance(n, ast.Compare)]
        budget_guards = []
        for cmp_node in compares:
            src = ast.unparse(cmp_node)
            if "MERGE_CONTEXT_TOKENS" in src and "MERGE_DRAFT_NUM_PREDICT" in src:
                budget_guards.append(cmp_node)
        self.assertTrue(budget_guards,
                        msg="no comparison combines the prompt estimate, the output "
                            "budget and the context ceiling")
        for cmp_node in budget_guards:
            sides = [cmp_node.left] + list(cmp_node.comparators)
            for side in sides:
                for sub in ast.walk(side):
                    self.assertNotIsInstance(
                        sub, ast.Constant,
                        msg="a literal on either side of the budget comparison makes "
                            "it a constant that never fires")

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
