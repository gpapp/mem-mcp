"""
test_vault_migration.py — the vault-move path, and the id derivation it rests on.

Two subjects in one file, because the second is worthless without the first:
``scoped_ids`` is a pure re-derivation of what ``db_create_client`` mints, and
its whole job is to agree with a function it cannot see. Splitting them would
leave a suite that passes while the two copies of the formula have drifted —
which is the failure this module exists to prevent.

``migrate_vault_user.py`` cannot be imported here: it needs ``qdrant_client``
and ``common``, neither of which is installed. Its Qdrant and graph functions
are lifted with ``ast.get_source_segment`` and driven against fakes, the same
technique ``test_embedding_reliability.py`` uses on ``common.py``. They are
*called* rather than read, because the properties worth guarding are
behavioural: the scope retarget has to reach every point of a chunk family, and
has to run before ``userId`` moves. Neither is visible in the shape of the code.
"""

import ast
import asyncio
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

import sessions

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scoped_ids import (  # noqa: E402
    client_id_for,
    context_id_for,
    plan_id_remap,
)

SOURCE_USER = "vault-alpha"
TARGET_USER = "vault-beta"


def _read(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _lift(module_name, function_name, namespace):
    """Pull one function out of a module and exec it against ``namespace``.

    Lifted rather than imported because the module needs qdrant_client, which is
    not installed. A *copy* of the logic would test nothing; this runs the
    shipping source.
    """
    source = _read(module_name)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            scope = dict(namespace)
            exec(compile(segment, f"{module_name}:{function_name}", "exec"), scope)
            return scope[function_name]
    raise AssertionError(f"{function_name} not found in {module_name}")


# ---------------------------------------------------------------------------
# Stand-ins for the qdrant_client models and the app's collection names
# ---------------------------------------------------------------------------

class _Model:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        return f"{type(self).__name__}({self.__dict__})"


class Filter(_Model):
    pass


class FieldCondition(_Model):
    pass


class MatchValue(_Model):
    pass


class FakeQdrant:
    """Enough of AsyncQdrantClient for scroll and set_payload.

    ``scroll`` honours the ``userId`` equality filter, because that filter is
    the whole reason the migration has an order: a point that has already been
    re-owned is invisible to the scope retarget.
    """

    def __init__(self, data=None):
        self.data = data if data is not None else {"ea_memories": {}, "ea_diary": {}}
        self.payload_writes = []
        self.payload_batches = []
        # How many points the *other* vault holds. Separate from ``data`` because
        # the migration reads the destination's count in the preflight, and the
        # state that matters — a destination whose Neo4j was emptied while its
        # vectors survived — has points without any payload rows here.
        self.destination_points = 0

    def _matches(self, payload, scroll_filter):
        if scroll_filter is None:
            return True
        for condition in (scroll_filter.must or []):
            if payload.get(condition.key) != condition.match.value:
                return False
        return True

    async def set_payload(self, collection_name, payload=None, points=None):
        """Mirrors the call shape ``client_manager`` uses in production.

        A list of point ids plus **one** payload applied to all of them. The
        per-point form needs a pydantic model whose ``vector`` field is
        required, which is a runtime error the suite cannot see; this form
        cannot be version-sensitive, so a permissive stub here no longer hides
        anything. Rejects a per-point payload outright rather than accepting it,
        so the shape cannot drift back.
        """
        if not isinstance(payload, dict):
            raise TypeError(
                "set_payload takes one payload and a list of point ids; a "
                "per-point payload list needs a PointStruct, whose 'vector' "
                "field is required and is ignored here")
        self.payload_batches.append((collection_name, len(points)))
        for pid in points:
            self.data[collection_name].setdefault(pid, {}).update(payload)
            self.payload_writes.append((collection_name, pid, dict(payload)))

    def written_ids(self):
        return [pid for _c, pid, _p in self.payload_writes]

    def with_pages(self, page_size):
        """Paginate the way Qdrant does, which is the assumption under test.

        Qdrant's ``offset`` is *the id to start reading from* — a cursor over the
        collection's own point order. The filter is applied per point during that
        iteration, so it is not an index into the filtered result.

        The distinction is load-bearing for ``move_qdrant_user``, which mutates
        the very filter it is paginating over. Under Qdrant's model the cursor
        still lands correctly because the unfiltered order did not change; under
        a filtered-index model the result set shrinks underneath it and every
        point past the first page is silently dropped. Modelling it wrongly would
        have made the test demand a "fix" for correct code, so it is modelled
        from the API's contract rather than from what would be convenient.
        """
        self.page_size = page_size
        return self

    async def scroll(self, collection_name, limit=1000, offset=None,
                     with_payload=False, with_vectors=False, scroll_filter=None):
        size = getattr(self, "page_size", None) or limit
        everything = list(self.data[collection_name].items())
        start = offset or 0
        window = everything[start:start + size]
        rows = [(pid, p) for pid, p in window if self._matches(p, scroll_filter)]
        cursor = start + size if start + size < len(everything) else None
        if with_payload:
            return [_Model(id=pid, payload=dict(p)) for pid, p in rows], cursor
        return [_Model(id=pid, payload=None) for pid, _ in rows], cursor


def _qdrant_namespace():
    return {
        "Filter": Filter,
        "FieldCondition": FieldCondition,
        "MatchValue": MatchValue,
        "COLLECTION_NAME": "ea_memories",
        "DIARY_COLLECTION": "ea_diary",
        "QDRANT_BATCH": 200,
    }


class DerivationTests(unittest.TestCase):
    """The formula itself, pinned so a change has to be a deliberate one.

    Written out longhand rather than recomputed with uuid5: a test that built
    the expected value with the same call would pass on a formula that had
    changed, which is the one thing this needs to catch.
    """

    def test_the_two_ids_are_the_documented_uuid5(self):
        import uuid
        self.assertEqual(
            client_id_for("someone", "Acme Holdings"),
            str(uuid.uuid5(uuid.NAMESPACE_DNS, "client_someone_acme holdings")))
        self.assertEqual(
            context_id_for("someone", "cid", "Phase One"),
            str(uuid.uuid5(uuid.NAMESPACE_DNS, "context_someone_cid_phase one")))

    def test_the_inputs_are_all_three_of_them(self):
        # The username, the name and — for a context — the parent id. Each is a
        # distinct field in the f-string, so dropping one is a behaviour change
        # that leaves the ids looking perfectly well-formed.
        self.assertNotEqual(client_id_for("one", "Acme"),
                            client_id_for("two", "Acme"))
        self.assertNotEqual(client_id_for("u", "acme"),
                            client_id_for("u", "acme_uk"))
        self.assertNotEqual(context_id_for("u", "parent-a", "phase"),
                            context_id_for("u", "parent-b", "phase"))

    def test_both_names_are_normalised_identically(self):
        # Both functions normalise. Covering only one let `.strip()` be dropped
        # from context_id_for and the whole suite stay green.
        self.assertEqual(client_id_for("u", "  ACME  "), client_id_for("u", "acme"))
        self.assertEqual(context_id_for("u", "cid", "  Phase One  "),
                         context_id_for("u", "cid", "phase one"))

class ClientManagerCallSiteTests(unittest.TestCase):
    """A test of the helper is not a test of its call site.

    ``client_id_for`` agreeing with the migration is worth nothing if
    ``db_create_client`` still mints its own. This is the same reasoning behind
    ``OllamaModelMatchTests`` and ``WriteOrderingGuardTests`` in
    ``test_embedding_reliability.py``.
    """

    def setUp(self):
        self.source = _read("client_manager.py")

    def test_create_client_calls_the_shared_derivation(self):
        self.assertIn("client_id = client_id_for(user_id, name)", self.source)

    def test_create_context_calls_the_shared_derivation(self):
        self.assertIn("context_id = context_id_for(user_id, client_id, name)",
                      self.source)

    def test_no_inline_uuid5_is_left_behind(self):
        # An AST walk, not a substring search, for the same reason its sibling
        # in MigrationScriptTests is one: `from uuid import uuid5` followed by
        # `uuid5(...)` sails straight past `assertNotIn("uuid.uuid5", source)`,
        # so the guard would pass on exactly the drift it exists to catch.
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Call):
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", ""))
                self.assertNotEqual(name, "uuid5",
                                    "the derivation must come from scoped_ids")
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("uuid", [a.name for a in node.names],
                                 "client_manager must not import uuid")


class ScopedIdsImportTests(unittest.TestCase):
    """Standard library only, so the derivation is callable without a database.

    ``matching_utils`` has the same rule and the same reason: a module that
    cannot be imported on a plain box cannot be tested on one, and the failure
    mode is the whole suite erroring at collection rather than one test failing.
    """

    def test_it_imports_only_the_uuid_module(self):
        imported = set()
        for node in ast.walk(ast.parse(_read("scoped_ids.py"))):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= {"uuid"},
                        f"scoped_ids gained a dependency: {sorted(imported)}")


class PlanTests(unittest.TestCase):
    """The pure planner: what changes, what does not, and what must be refused.

    ``plan_id_remap`` is the one part of a move that needs no database, which is
    why it can be tested exhaustively here and why ``scoped_ids`` is
    standard-library-only.
    """

    def setUp(self):
        self.clients = [
            {"id": client_id_for(SOURCE_USER, "Acme Holdings"), "name": "Acme Holdings"},
            {"id": client_id_for(SOURCE_USER, "Beta SE"), "name": "Beta SE"},
        ]
        self.acme = client_id_for(SOURCE_USER, "Acme Holdings")
        self.contexts = [{
            "id": context_id_for(SOURCE_USER, self.acme, "Phase One"),
            "name": "Phase One",
            "clientId": self.acme,
        }]
        self.remap = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                                   self.contexts)

    def test_a_moved_id_is_the_one_a_create_would_mint(self):
        # The point of the whole module: the moved id has to be
        # indistinguishable from one db_create_client would have produced in the
        # new vault, or the next same-named client becomes a second node.
        #
        # Membership is asserted before the lookup on purpose. Indexing the map
        # directly reports a wrong derivation as a KeyError from an omitted entry
        # — and an omitted entry is the *expected* consequence of a derivation
        # that ignores the username, since the id then comes out unchanged. The
        # guard bites either way, but as a KeyError it reads like a broken
        # fixture rather than a broken formula.
        for row in self.clients:
            self.assertIn(row["id"], self.remap.clients,
                          "a client whose id changes must be in the remap")
            self.assertEqual(self.remap.clients[row["id"]],
                             client_id_for(TARGET_USER, row["name"]))

    def test_a_context_is_derived_from_its_parents_new_id(self):
        # Deriving it from the OLD parent is the subtle half: the parent is
        # renamed in the same pass, so a context built from the old id is an id
        # nothing will ever generate again.
        self.assertEqual(list(self.remap.contexts.values()),
                         [context_id_for(TARGET_USER,
                                         client_id_for(TARGET_USER, "Acme Holdings"),
                                         "Phase One")])

    def test_a_parent_is_never_the_context_itself(self):
        # The two maps have the same shape and the same key type, so a
        # self-referential parent is silent and writes a node pointing at itself.
        # `verify()` reads Qdrant payloads only and cannot see it.
        for context_id, parent in self.remap.context_parent_rows().items():
            self.assertNotEqual(context_id, parent)
        self.assertEqual(
            list(self.remap.context_parent_rows().values()),
            [client_id_for(TARGET_USER, "Acme Holdings")])

    def test_a_same_user_move_is_a_no_op_that_still_reaches_the_rewrite(self):
        # ``changes_anything`` exists so a no-op cannot report success — and the
        # *unchanged* rows must still be handed to move_graph, because a row whose
        # id comes out unchanged is a pre-existing inconsistency that still needs
        # its userId rewritten.
        same = plan_id_remap(SOURCE_USER, SOURCE_USER, self.clients,
                             self.contexts)
        self.assertFalse(same.changes_anything)
        self.assertEqual(same.clients, {})
        self.assertEqual(same.contexts, {})
        self.assertEqual(set(same.client_rows()), {c["id"] for c in self.clients})
        self.assertEqual(set(same.context_rows()), {c["id"] for c in self.contexts})
        # Values, not just key sets: a self-referential parent passes every
        # key-set assertion.
        self.assertEqual(same.context_parent_rows(),
                         {c["id"]: c["clientId"] for c in self.contexts})

    def test_the_row_accessors_include_the_changed_rows(self):
        for row in self.clients:
            self.assertEqual(self.remap.client_rows()[row["id"]],
                             client_id_for(TARGET_USER, row["name"]))
        self.assertEqual(set(self.remap.context_rows()),
                         set(self.remap.contexts))

    def test_an_unchanged_context_still_has_its_parent_rewritten(self):
        # A Context stored with an id already derived for the destination user
        # comes out of the remap unchanged, and its `clientId` property is then
        # never rewritten — so it keeps pointing at the old client while that
        # client is re-identified.
        old_acme = client_id_for(SOURCE_USER, "Acme Holdings")
        new_acme = client_id_for(TARGET_USER, "Acme Holdings")
        rows = [{"id": context_id_for(TARGET_USER, new_acme, "Phase One"),
                 "name": "Phase One", "clientId": old_acme}]
        remap = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients, rows)
        self.assertEqual(remap.contexts, {}, "precondition: the id is unchanged")
        self.assertEqual(list(remap.context_parent_rows().values()), [new_acme],
                         "the stale parent pointer must be rewritten")

    def test_a_name_the_destination_already_holds_is_refused(self):
        # `destination_occupied` only inspects the target, and the Context key is
        # the name alone here because a destination Context hangs off a client
        # this source has not got.
        for label, kwargs in (
                ("client", {"target_clients": [{"name": "acme holdings"}]}),
                ("context", {"target_contexts": [{"name": "Phase One"}]}),
                ("unrelated", {"target_clients": [{"name": "Gamma GmbH"}]})):
            with self.subTest(collision=label):
                occupied = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                                         self.contexts, **kwargs)
                self.assertEqual(occupied.destination_occupied,
                                 label != "unrelated")

    def test_a_name_the_source_repeats_is_refused(self):
        # Two same-named rows derive one id, so the move would leave two nodes
        # sharing it — and `destination_occupied` cannot see it, because it only
        # inspects the target.
        twin_client = dict(self.clients[0])
        twin_context = dict(self.contexts[0])
        twin_context["id"] = "a-second-row-same-name"
        with self.subTest(dupe="client"):
            self.assertEqual(
                plan_id_remap(SOURCE_USER, TARGET_USER, self.clients + [twin_client],
                              self.contexts).duplicate_names, ["Acme Holdings"])
        with self.subTest(dupe="context"):
            self.assertEqual(
                len(plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                                  self.contexts + [twin_context]
                                  ).duplicate_names), 1)

    def test_a_context_under_another_client_is_not_a_duplicate(self):
        # Keyed by parent as well as name: two contexts of one name under
        # *different* clients are two different nodes with two different ids.
        other = {"id": client_id_for(SOURCE_USER, "Beta SE"), "name": "Beta SE"}
        twin = {"id": context_id_for(SOURCE_USER, other["id"], "Phase One"),
                "name": "Phase One", "clientId": other["id"]}
        remap = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                              self.contexts + [twin])
        self.assertEqual(remap.duplicate_names, [])
        self.assertEqual(remap.context_count, 2)

    def test_a_vault_with_no_clients_has_nothing_to_remap(self):
        empty = plan_id_remap(SOURCE_USER, TARGET_USER, [], [])
        self.assertFalse(empty.changes_anything)
        self.assertFalse(empty.destination_occupied)
        self.assertEqual(empty.duplicate_names, [])

class RetargetScopeTests(unittest.TestCase):
    """Called against a fake client, because the properties are behavioural."""

    def setUp(self):
        ns = _module_namespace("_flush", "retarget_qdrant_scope")
        self.retarget = ns["retarget_qdrant_scope"]
        self.old_client = client_id_for(SOURCE_USER, "Acme Holdings")
        self.new_client = client_id_for(TARGET_USER, "Acme Holdings")
        self.old_context = context_id_for(SOURCE_USER, self.old_client, "Phase One")
        self.new_context = context_id_for(TARGET_USER, self.new_client, "Phase One")
        self.client_map = {self.old_client: self.new_client}
        self.context_map = {self.old_context: self.new_context}

    def _fixture(self):
        # A chunked record: chunk 0 plus two siblings sharing the same scope
        # keys. A retarget that stops at chunk 0 leaves the family split, and the
        # symptom is a long record that filters differently from itself.
        return FakeQdrant({
            "ea_memories": {
                "fact-1": {"userId": SOURCE_USER, "clientId": self.old_client,
                           "contextId": self.old_context,
                           "clientName": "Acme Holdings", "text": "body"},
                "fact-1#1": {"userId": SOURCE_USER, "clientId": self.old_client,
                             "contextId": self.old_context, "chunkIndex": 1},
                "fact-1#2": {"userId": SOURCE_USER, "clientId": self.old_client,
                             "contextId": self.old_context, "chunkIndex": 2},
                "fact-2": {"userId": SOURCE_USER, "text": "unscoped"},
            },
            "ea_diary": {
                "diary-1": {"userId": SOURCE_USER, "clientId": self.old_client,
                            "clientName": "Acme Holdings"},
                "other-1": {"userId": "vault-gamma", "clientId": self.old_client},
            },
        })

    def _retarget(self, qdrant):
        return asyncio.run(self.retarget(qdrant, SOURCE_USER, self.client_map,
                                         self.context_map))

    def test_only_the_ids_are_touched(self):
        # Every chunk of a family, and nothing else on the point. A name is not
        # derived from the username, so rewriting it would be a gratuitous write
        # of every scoped point in the vault; the text must survive too.
        qdrant = self._fixture()
        self.assertEqual(self._retarget(qdrant), 4)
        for pid in ("fact-1", "fact-1#1", "fact-1#2"):
            self.assertEqual(qdrant.data["ea_memories"][pid]["clientId"],
                             self.new_client, f"{pid} was left behind")
        payload = qdrant.data["ea_memories"]["fact-1"]
        self.assertEqual(payload["contextId"], self.new_context)
        self.assertEqual(payload["clientName"], "Acme Holdings")
        self.assertEqual(payload["text"], "body")

    def test_points_that_need_no_patch_are_not_written(self):
        # The unscoped point, and another vault's point — which carries this
        # vault's client id in the fixture. Ownership is the userId filter, and
        # the filter is what stops a migration rewriting a stranger's payload.
        qdrant = self._fixture()
        self._retarget(qdrant)
        self.assertNotIn("fact-2", qdrant.written_ids())
        self.assertEqual(qdrant.data["ea_diary"]["other-1"]["clientId"],
                         self.old_client)

    def test_an_id_that_is_not_in_the_map_is_left_alone(self):
        # A dangling clientId from a deleted client is already broken; blanking it
        # would hide that instead of surfacing it.
        qdrant = FakeQdrant({"ea_memories": {"f": {"userId": SOURCE_USER,
                                                   "clientId": "gone"}},
                             "ea_diary": {}})
        self.assertEqual(self._retarget(qdrant), 0)
        self.assertEqual(qdrant.data["ea_memories"]["f"]["clientId"], "gone")

    def test_it_never_builds_a_per_point_payload(self):
        # The container's PointStruct is a pydantic model with a **required**
        # vector field, and set_payload ignores the value. An earlier version
        # omitted it, the permissive stub accepted that, and the whole suite
        # passed — then the container raised a ValidationError on the first real
        # point. The fake now refuses the per-point form outright.
        #
        # An AST check, not assertNotIn: the module docstring explains *why* the
        # per-point form is avoided, so a substring guard fails on the
        # explanation — the assertIn-matches-the-comment lesson again.
        self._retarget(self._fixture())
        for node in ast.walk(ast.parse(_read(_MIGRATE))):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("PointStruct", [a.name for a in node.names])
            if isinstance(node, ast.Call):
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", ""))
                self.assertNotEqual(name, "PointStruct")

class PaginationTests(unittest.TestCase):
    """Every loop in the migration, at a page boundary.

    The default fake returns one page, so the loops run once and a boundary bug
    is unreachable. These drive them with a cursor-paginating fake and a batch of
    2 against odd and exact-multiple point counts.
    """

    def setUp(self):
        ns = _module_namespace(extra=dict(_qdrant_namespace(), QDRANT_BATCH=2))
        self.retarget = ns["retarget_qdrant_scope"] = _lift(
            _MIGRATE, "retarget_qdrant_scope", _module_namespace("_flush", extra=ns))
        self.move = ns["move_qdrant_user"] = _lift(_MIGRATE, "move_qdrant_user", ns)
        self.old_client = client_id_for(SOURCE_USER, "Acme")
        self.new_client = client_id_for(TARGET_USER, "Acme")
        self.maps = ({self.old_client: self.new_client}, {})

    def _points(self, total, scoped=True):
        data = {}
        for i in range(total):
            payload = {"userId": SOURCE_USER}
            if scoped or i % 2 == 0:
                payload["clientId"] = self.old_client
            data[f"p{i}"] = payload
        return FakeQdrant({"ea_memories": data, "ea_diary": {}}).with_pages(2)

    def test_the_retarget_reaches_every_page(self):
        for total in (1, 2, 3, 5, 6, 7):
            qdrant = self._points(total)
            patched = asyncio.run(self.retarget(qdrant, SOURCE_USER, *self.maps))
            self.assertEqual(patched, total, f"{total} points")
            for i in range(total):
                self.assertEqual(qdrant.data["ea_memories"][f"p{i}"]["clientId"],
                                 self.new_client, f"p{i} of {total}")

    def test_the_retarget_reaches_every_page_with_holes(self):
        # Only every other point carries a scope id, so the flush threshold and
        # the page size disagree — which is the case where a patch can be
        # buffered across a boundary and then lost.
        for total in (3, 5, 6, 9):
            qdrant = self._points(total, scoped=False)
            expected = sum(1 for i in range(total) if i % 2 == 0)
            patched = asyncio.run(self.retarget(qdrant, SOURCE_USER, *self.maps))
            self.assertEqual(patched, expected, f"{total} points")
            for i in range(total):
                # Odd points never had a scope id, so "unchanged" means absent.
                expected_id = self.new_client if i % 2 == 0 else None
                self.assertEqual(
                    qdrant.data["ea_memories"][f"p{i}"].get("clientId"),
                    expected_id, f"p{i} of {total}")

    def test_the_move_reaches_every_page(self):
        for total in (1, 2, 3, 5, 6, 7):
            qdrant = self._points(total, scoped=False)
            moved = asyncio.run(self.move(qdrant, SOURCE_USER, TARGET_USER))
            self.assertEqual(moved, total, f"{total} points")
            for i in range(total):
                self.assertEqual(qdrant.data["ea_memories"][f"p{i}"]["userId"],
                                 TARGET_USER, f"p{i} of {total}")

    def test_points_sharing_a_patch_are_written_in_one_request(self):
        # Grouping by patch is also what keeps the request count sane now that
        # the per-point form is gone.
        qdrant = self._points(6, scoped=True).with_pages(6)
        asyncio.run(self.retarget(qdrant, SOURCE_USER, *self.maps))
        self.assertEqual(len(qdrant.payload_batches), 1)
        self.assertEqual(qdrant.payload_batches[0][1], 6)

    def test_a_grouped_patch_never_nulls_a_key_it_is_not_changing(self):
        """``set_payload`` writes what it is handed, so a carried-through ``None``
        would blank a real value.

        The group key is a fixed-width tuple with ``None`` marking an absent
        half, and the flush has to drop those — otherwise a point with a
        ``clientId`` but no ``contextId`` gets ``contextId: null`` written over
        nothing, and a point whose context *is* being retargeted loses nothing
        but gains a null key that every later filter has to special-case.
        """
        old_client = self.old_client
        old_context = context_id_for(SOURCE_USER, old_client, "Phase One")
        new_client = self.new_client
        new_context = context_id_for(TARGET_USER, new_client, "Phase One")
        qdrant = FakeQdrant({"ea_memories": {
            "with-both": {"userId": SOURCE_USER, "clientId": old_client,
                          "contextId": old_context},
            "client-only": {"userId": SOURCE_USER, "clientId": old_client},
        }, "ea_diary": {}})
        patched = asyncio.run(self.retarget(
            qdrant, SOURCE_USER, {old_client: new_client},
            {old_context: new_context}))
        self.assertEqual(patched, 2)
        both = qdrant.data["ea_memories"]["with-both"]
        self.assertEqual(both["clientId"], new_client)
        self.assertEqual(both["contextId"], new_context)
        only = qdrant.data["ea_memories"]["client-only"]
        self.assertEqual(only["clientId"], new_client)
        self.assertNotIn("contextId", only,
                         "a key that is not changing must not be written at all")

    def test_a_point_needing_no_patch_is_not_written(self):
        qdrant = self._points(2, scoped=True)
        data = qdrant.data["ea_memories"]
        del data["p1"]["clientId"]
        patched = asyncio.run(self.retarget(qdrant, SOURCE_USER, *self.maps))
        self.assertEqual(patched, 1)
        self.assertEqual(qdrant.written_ids(), ["p0"])

    def test_the_group_key_order_matches_the_keys_written(self):
        """The constant and the append order have to agree.

        ``_flush`` turns a group key back into a payload with
        ``zip(PATCH_KEYS, key)``, so a reordered ``PATCH_KEYS`` writes each id
        into the *wrong* payload key — silently, and with no error anywhere. The
        two live in different places (a module constant, and the order of two
        ``append`` calls), so nothing else connects them.
        """
        appends = []
        for node in ast.walk(ast.parse(_read(_MIGRATE))):
            if isinstance(node, ast.AsyncFunctionDef) \
                    and node.name == "retarget_qdrant_scope":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) \
                            and isinstance(inner.func, ast.Attribute) \
                            and inner.func.attr == "append" \
                            and isinstance(inner.func.value, ast.Name) \
                            and inner.func.value.id == "patch" \
                            and not (isinstance(inner.args[0], ast.Constant)
                                     and inner.args[0].value is None):
                        # The `else: patch.append(None)` arms are part of the
                        # fixed-width tuple too, so they are filtered rather than
                        # counted — the key order is what is under test.
                        appends.append(ast.unparse(inner.args[0]))
        self.assertEqual(len(appends), 2,
                         f"expected exactly two patch appends, found {appends}")
        # The first append is guarded on the client key and the second on the
        # context key, so the order they appear in IS the PATCH_KEYS order.
        self.assertEqual(appends, ["client_map[payload['clientId']]",
                                   "context_map[payload['contextId']]"])
        self.assertEqual(_module_constant(_MIGRATE, "PATCH_KEYS"),
                         ("clientId", "contextId"))

    def test_one_request_can_exceed_the_batch_size(self):
        # Documented as a flush threshold rather than a cap, and this pins the
        # consequence: `structs` survives a page boundary and only the points
        # that need a patch accumulate, so a single set_payload can carry up to
        # 2 * BATCH - 1. Asserted so the comment in the module stays true if
        # somebody raises QDRANT_BATCH expecting a request bound.
        # Laid out so the *first* page yields one patch and the second yields two,
        # which is the only arrangement that can exceed the threshold: the flush
        # check runs after a whole page, so the carried-over remainder plus a
        # full page is the maximum. The alternating fixture cannot produce it.
        data = {}
        for i, scoped in enumerate((True, False, True, True)):
            payload = {"userId": SOURCE_USER}
            if scoped:
                payload["clientId"] = self.old_client
            data[f"p{i}"] = payload
        qdrant = FakeQdrant({"ea_memories": data, "ea_diary": {}}).with_pages(2)
        asyncio.run(self.retarget(qdrant, SOURCE_USER, *self.maps))
        sizes = [count for _collection, count in qdrant.payload_batches]
        self.assertTrue(sizes, "nothing was written")
        # Both halves, or the assertion is satisfied by an implementation that
        # flushed one point per request — the exact behaviour the name and the
        # module comment describe would then no longer be reachable from.
        self.assertGreater(max(sizes), 2,
                           "expected a batch to exceed the threshold, so the "
                           "documented bound is not vacuous")
        self.assertLessEqual(max(sizes), 2 * 2 - 1,
                             f"a request carried {max(sizes)} points, above the "
                             "documented 2 * BATCH - 1")


class MoveUserTests(unittest.TestCase):
    def setUp(self):
        ns = _module_namespace("_flush", "retarget_qdrant_scope",
                               "move_qdrant_user")
        self.retarget = ns["retarget_qdrant_scope"]
        self.move = ns["move_qdrant_user"]

    def _fixture(self):
        return FakeQdrant({
            "ea_memories": {"a": {"userId": SOURCE_USER, "text": "x"},
                            "b": {"userId": "vault-gamma", "text": "y"}},
            "ea_diary": {"c": {"userId": SOURCE_USER, "text": "z"}},
        })

    def test_every_point_of_the_source_moves(self):
        qdrant = self._fixture()
        moved = asyncio.run(self.move(qdrant, SOURCE_USER, TARGET_USER))
        self.assertEqual(moved, 2)
        self.assertEqual(qdrant.data["ea_memories"]["a"]["userId"], TARGET_USER)
        self.assertEqual(qdrant.data["ea_diary"]["c"]["userId"], TARGET_USER)

    def test_another_users_points_do_not_move(self):
        qdrant = self._fixture()
        asyncio.run(self.move(qdrant, SOURCE_USER, TARGET_USER))
        self.assertEqual(qdrant.data["ea_memories"]["b"]["userId"], "vault-gamma")

    def test_the_move_does_not_disturb_the_rest_of_the_payload(self):
        qdrant = self._fixture()
        asyncio.run(self.move(qdrant, SOURCE_USER, TARGET_USER))
        self.assertEqual(qdrant.data["ea_memories"]["a"]["text"], "x")

    def test_the_scope_retarget_must_run_before_the_user_id_moves(self):
        # The ordering property, measured rather than asserted. Once userId has
        # moved the old value no longer selects anything, so a retarget run
        # afterwards patches zero points and the vault keeps scope ids matching
        # no node — with no error anywhere.
        old_client = client_id_for(SOURCE_USER, "Acme")
        new_client = client_id_for(TARGET_USER, "Acme")

        async def run(order):
            qdrant = FakeQdrant({"ea_memories": {"f": {
                "userId": SOURCE_USER, "clientId": old_client}},
                "ea_diary": {}})
            # Only the requested order may execute: computing both and picking
            # one would leave the fixture mutated twice and prove nothing.
            if order == "right":
                patched = await self.retarget(qdrant, SOURCE_USER,
                                             {old_client: new_client}, {})
                await self.move(qdrant, SOURCE_USER, TARGET_USER)
            else:
                await self.move(qdrant, SOURCE_USER, TARGET_USER)
                patched = await self.retarget(qdrant, SOURCE_USER,
                                             {old_client: new_client}, {})
            return qdrant, patched

        qdrant, patched = asyncio.run(run("right"))
        self.assertEqual(patched, 1)
        self.assertEqual(qdrant.data["ea_memories"]["f"]["clientId"], new_client)

        qdrant, patched = asyncio.run(run("wrong"))
        self.assertEqual(patched, 0)
        self.assertEqual(qdrant.data["ea_memories"]["f"]["clientId"], old_client)


class FakeResult:
    """Stands in for a neo4j Result.

    ``consume()`` for a write, ``single()`` for the one RETURN the counts come
    back on, and empty iteration for the read shapes not under test.
    """

    def __init__(self, value=None):
        self._value = value

    def single(self):
        return self._value

    def consume(self):
        return None

    def __iter__(self):
        return iter(())


class FakeTx:
    def __init__(self, driver):
        self.driver = driver

    def run(self, query, **params):
        self.driver.calls.append((" ".join(query.split()), params))
        if "RETURN count(" not in query:
            return FakeResult()
        # The label is whatever follows "MATCH (" — "n" when the pattern is a
        # bare variable carrying an inline label test, "c:Client" and so on.
        # Deriving it by splitting on "MATCH (n:" broke the moment the migration
        # added the destination-occupancy check, which matches a bare (n) and
        # filters on the labels in the WHERE.
        match = re.search(r"MATCH \(([A-Za-z]+)(?::([A-Za-z]+))?", query)
        variable, label = (match.group(1), match.group(2)) if match else ("n", None)
        if label:
            return FakeResult({"n": self.driver.counts.get(label, 0)})
        # A bare (n) with an inline label list: count whichever the driver was
        # told about, so the occupancy guard reads as "empty" by default.
        return FakeResult({"n": self.driver.counts.get("__bare__", 0)})


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_write(self, fn):
        self.driver.transactions += 1
        return fn(FakeTx(self.driver))


class FakeDriver:
    """Records every statement the script issues, in order.

    ``counts`` answers the ``RETURN count(...)`` shapes. ``__bare__`` is what a
    ``MATCH (n)`` with the labels in its WHERE reports, which is how the
    destination-occupancy guard inside the transaction sees an empty destination.
    """

    def __init__(self, counts=None):
        self.counts = counts or {"Fact": 7, "DiaryEntry": 3, "Client": 1,
                                 "Context": 1, "__bare__": 0}
        self.calls = []
        self.transactions = 0

    def session(self):
        return FakeSession(self)

    def queries(self):
        return [q for q, _ in self.calls]


class MoveGraphTests(unittest.TestCase):
    """The Cypher side, driven against a recording driver.

    There is no Neo4j here, so what is asserted is the *shape* of every write:
    which labels move, that ids and the parent pointer move with them, and what
    it does not touch at all. The query text itself is linted by
    ``test_cypher_safety.py``, which scans this module too.
    """

    def setUp(self):
        ns = _module_namespace("move_graph")
        self.move_graph = ns["move_graph"]
        self.DestinationOccupied = ns["DestinationOccupied"]
        self.driver = FakeDriver()
        self.client_map = {"old-client": "new-client"}
        self.context_map = {"old-ctx": "new-ctx"}
        self.parents = {"old-ctx": "new-client"}

    def _go(self):
        self.move_graph(self.driver, SOURCE_USER, TARGET_USER,
                        self.client_map, self.context_map, self.parents)
        return self.driver.calls

    def _only(self, needle):
        found = [(q, p) for q, p in self.driver.calls if needle in q]
        self.assertEqual(len(found), 1,
                         f"expected one {needle} statement, got {len(found)}")
        return found[0]

    def test_a_client_is_rewritten_with_both_its_id_and_its_owner(self):
        self._go()
        query, params = self._only(":Client {id: row.oldId")
        self.assertIn("SET c.id = row.newId, c.userId = $new", query)
        self.assertEqual(params["rows"],
                         [{"oldId": "old-client", "newId": "new-client"}])
        self.assertEqual((params["old"], params["new"]), (SOURCE_USER, TARGET_USER))

    def test_a_context_is_rewritten_with_its_new_parent(self):
        self._go()
        query, params = self._only(":Context {id: row.oldId")
        self.assertIn("ctx.id = row.newId, ctx.userId = $new", query)
        self.assertIn("ctx.clientId = row.newParent", query)
        self.assertEqual(params["rows"],
                         [{"oldId": "old-ctx", "newId": "new-ctx",
                           "newParent": "new-client"}])

    def test_clients_are_rewritten_before_contexts(self):
        # A Context's row carries the parent's new id, so the parent has to be
        # re-identified first or the Context is written against an id that is
        # about to change underneath it.
        self._go()
        labelled = [q for q in self.driver.queries()
                    if ":Client {id:" in q or ":Context {id:" in q]
        self.assertEqual(len(labelled), 2)
        self.assertIn(":Client {id:", labelled[0])

    def test_the_records_move_by_user_id_only(self):
        # A record's id is a uuid4 (facts) or timestamp-derived (diary), neither
        # a function of the user, so nothing else about them may be written.
        self._go()
        for label in ("Fact", "DiaryEntry"):
            query, _ = self._only(f"MATCH (n:{label})")
            self.assertIn("WHERE n.userId = $old SET n.userId = $new", query)
            self.assertNotIn("n.id", query)

    def test_the_reported_counts_are_actual_not_planned(self):
        # RETURN count(...) rather than len(map): a row whose MATCH matched
        # nothing is a silent no-op, and reporting the plan hides exactly that.
        counts = self.move_graph(self.driver, SOURCE_USER, TARGET_USER,
                                 self.client_map, self.context_map, self.parents)
        self.assertEqual(counts, {"facts": 7, "diary": 3, "clients": 1,
                                  "contexts": 1})
        self.driver.counts["Client"] = 0
        self.driver.calls.clear()
        counts = self.move_graph(self.driver, SOURCE_USER, TARGET_USER,
                                 self.client_map, self.context_map, self.parents)
        self.assertEqual(counts["clients"], 0)
        self.assertEqual(counts["contexts"], 1)

    def test_what_it_must_not_touch(self):
        # :Category is global — it carries no userId at all — so a migration that
        # touched it would re-file one vault's categories under another. Nothing is
        # deleted either, because the old account is left intact by design and no
        # node is anybody else's business. And the source gets no :User node.
        for query, _ in self._go():
            self.assertNotIn("DELETE", query.upper())
            self.assertNotIn(":Category", query)
            self.assertNotIn("MERGE (u:User {id: $old})", query)
        _, params = self._only("MERGE (u:User")
        self.assertEqual(params["new"], TARGET_USER)

    def test_a_non_empty_destination_aborts_the_whole_transaction(self):
        # Re-checked inside the transaction, not only before it: a record saved
        # into the destination in between would otherwise be interleaved with the
        # arriving ones, and the move would exit 0 having merged two vaults.
        self.driver.counts["__bare__"] = 4
        with self.assertRaises(self.DestinationOccupied):
            self.move_graph(self.driver, SOURCE_USER, TARGET_USER,
                            self.client_map, self.context_map, self.parents)
        self.assertEqual(self.driver.transactions, 1)

    def test_it_runs_in_a_single_transaction(self):
        # Split across several, a crash between them leaves the graph describing
        # a vault that does not exist.
        self._go()
        self.assertEqual(self.driver.transactions, 1)

class PerformMoveOrderTests(unittest.TestCase):
    """The ordering, driven rather than read.

    ``MoveUserTests`` proves the two primitives misbehave in one order. It cannot
    prove the *script* uses the right one: it calls them itself, so it is blind to
    the wiring. ``perform_move`` exists so the wiring is callable, and this drives
    it end to end with fakes.
    """

    def setUp(self):
        # Every collaborator must be in the namespace *before* perform_move is
        # lifted: _lift copies it, so filling it in afterwards leaves
        # perform_move resolving names that do not exist.
        ns = _module_namespace("move_graph", "retarget_qdrant_scope",
                               "move_qdrant_user", "_flush")
        # Stubbed, not lifted: the credential half refuses a same-user move, and
        # the test below needs one in order to populate the unchanged set. That
        # half is covered by RunTests and by test_sessions.PskTransferTests.
        ns["move_credentials"] = lambda *a, **k: {"psks": 0,
                                                   "sessions_revoked": 0}
        _lift_shared(_MIGRATE, "perform_move", ns)
        self.perform_move = ns["perform_move"]
        self.old_client = client_id_for(SOURCE_USER, "Acme Holdings")
        self.new_client = client_id_for(TARGET_USER, "Acme Holdings")
        self.old_context = context_id_for(SOURCE_USER, self.old_client, "Phase One")
        self.new_context = context_id_for(TARGET_USER, self.new_client, "Phase One")
        # Deliberately three *different* values. With an empty dict on both sides
        # the two arguments are the same object, so swapping them at the call
        # site is a literal no-op and the bug this work caught stays invisible —
        # which is exactly what a previous version of this stub did.
        self.remap = _RemapStub(
            clients={self.old_client: self.new_client},
            contexts={self.old_context: self.new_context},
            context_parents={self.old_context: self.new_client})

    def _fixture(self):
        return FakeQdrant({"ea_memories": {"f": {
            "userId": SOURCE_USER, "clientId": self.old_client,
            "contextId": self.old_context, "text": "body"}},
            "ea_diary": {}})

    def test_unchanged_rows_still_reach_the_rewrite(self):
        """The call site for ``client_rows()``, using the real planner.

        ``_RemapStub`` cannot guard this: its ``client_rows()`` returns
        ``dict(self.clients)``, so handing ``perform_move`` ``remap.clients``
        instead is a literal no-op and nothing can tell them apart. A same-user
        plan is what populates the unchanged set, and it is also what makes the
        two accessors differ — so the real ``IdRemap`` is the only thing that
        makes the distinction observable at all.
        """
        clients = [{"id": client_id_for(SOURCE_USER, "Acme Holdings"),
                    "name": "Acme Holdings"}]
        same = plan_id_remap(SOURCE_USER, SOURCE_USER, clients, [])
        self.assertEqual(same.clients, {}, "precondition: nothing changed")
        self.assertEqual(len(same.client_rows()), 1, "precondition: one row")

        driver = FakeDriver()
        asyncio.run(self.perform_move(self._fixture(), driver, SOURCE_USER,
                                      SOURCE_USER, same))
        rows = next(p["rows"] for q, p in driver.calls if ":Client {id:" in q)
        self.assertEqual(rows, [{"oldId": clients[0]["id"],
                                 "newId": clients[0]["id"]}],
                         "an unchanged row must still be re-owned")

    def test_the_parent_pointer_is_written_from_context_parents(self):
        # The call-site guard for the bug this module exists to prevent: handing
        # move_graph `remap.contexts` where it needs `remap.context_parents`
        # points every Context at a sibling that does not exist. MoveGraphTests
        # cannot see it — it calls move_graph directly with its own dict — so the
        # wiring has to be asserted here, where the two are separately supplied.
        driver = FakeDriver()
        asyncio.run(self.perform_move(self._fixture(), driver, SOURCE_USER,
                                      TARGET_USER, self.remap))
        rows = next(p["rows"] for q, p in driver.calls if ":Context {id:" in q)
        self.assertEqual(rows, [{"oldId": self.old_context,
                                 "newId": self.new_context,
                                 "newParent": self.new_client}])

    def test_the_scope_ids_survive_the_whole_move(self):
        qdrant = self._fixture()
        report = asyncio.run(self.perform_move(qdrant, FakeDriver(), SOURCE_USER,
                                               TARGET_USER, self.remap))
        point = qdrant.data["ea_memories"]["f"]
        self.assertEqual(point["userId"], TARGET_USER)
        self.assertEqual(point["clientId"], self.new_client)
        self.assertEqual(point["contextId"], self.new_context)
        self.assertEqual(point["text"], "body")
        self.assertEqual(report["qdrant_points"], 1)

    def test_it_reports_what_each_stage_did(self):
        qdrant = self._fixture()
        report = asyncio.run(self.perform_move(qdrant, FakeDriver(), SOURCE_USER,
                                               TARGET_USER, self.remap))
        for key in ("scope_payloads", "qdrant_points", "facts", "diary",
                    "clients", "contexts"):
            self.assertIn(key, report)


class _RemapStub:
    """The six attributes ``perform_move`` reads, with distinct values.

    Deliberately not a bare namespace: three of the six are methods on the real
    ``IdRemap``, and this stub has to expose the same interface or the test would
    be pinning a different call shape than the code uses.
    """

    def __init__(self, clients, contexts, context_parents):
        self.clients = clients
        self.contexts = contexts
        self.context_parents = context_parents

    def client_rows(self):
        return dict(self.clients)

    def context_rows(self):
        return dict(self.contexts)

    def context_parent_rows(self):
        return dict(self.context_parents)


class FakeGraph(FakeDriver):
    """A Neo4j stand-in that answers the shapes read_graph and verify use."""

    def __init__(self, facts=0, diary=0, clients=(), contexts=(), user_node=False):
        super().__init__()
        self.graph = {"facts": facts, "diary": diary,
                      "clients": [dict(c) for c in clients],
                      "contexts": [dict(c) for c in contexts]}
        self.user_node = user_node

    def _dispatch(self, query, params):
        q = " ".join(query.split())
        if "RETURN c.id AS id, c.name AS name" in q:
            return self.graph["clients"]
        if "RETURN ctx.id AS id, ctx.name AS name, ctx.clientId" in q:
            return self.graph["contexts"]
        if "MATCH (c:Client) WHERE c.userId" in q:
            return [{"id": r["id"]} for r in self.graph["clients"]]
        if "MATCH (ctx:Context) WHERE ctx.userId" in q:
            return [{"id": r["id"]} for r in self.graph["contexts"]]
        if "MATCH (u:User {id:" in q:
            return FakeResult({"n": 1 if self.user_node else 0})
        if "RETURN count(n) AS n" in q:
            if ":Fact" in q:
                return FakeResult({"n": self.graph["facts"]})
            if ":DiaryEntry" in q:
                return FakeResult({"n": self.graph["diary"]})
        return FakeResult()

    def session(self):
        graph = self

        class _Session:
            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                return False

            def run(inner, query, **params):
                graph.calls.append((" ".join(query.split()), params))
                return graph._dispatch(query, params)

            def execute_write(inner, fn):
                graph.transactions += 1
                return fn(FakeTx(graph))
        return _Session()


_MIGRATE = "migrate_vault_user.py"
_MIGRATE_EXCEPTIONS = ("DestinationOccupied",)
# Lifted once and shared, because class *identity* is part of the contract:
# `except DestinationOccupied` only catches the very class that was raised, and
# two execs of the same class body produce two unrelated types. Two namespaces
# built separately therefore miss each other.
_LIFTED_CLASSES = {}


def _module_constant(module_name, name):
    """Read a module-level constant out of the source by AST.

    A hand-written copy in the harness is a duplicated constant that can drift
    from the module it stands in for, and a drift here is invisible: swapping
    the shipping ``PATCH_KEYS`` left the whole suite green because every lifted
    function read the harness's copy. Reading the real one makes that
    unrepresentable rather than merely tested for.
    """
    for node in ast.parse(_read(module_name)).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {module_name}")


def _lift_class(module_name, class_name, namespace):
    """Pull a module-level class definition out and exec it.

    An exception type is part of a function's contract, not decoration: a
    handler that catches a dedicated type cannot swallow an unrelated failure,
    which is the whole reason that type exists. A lifted copy therefore needs it
    in scope or the tests see a NameError instead of the behaviour.
    """
    source = _read(module_name)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            scope = dict(namespace)
            exec(compile(ast.get_source_segment(source, node),
                         f"{module_name}:{class_name}", "exec"), scope)
            return scope[class_name]
    raise AssertionError(f"{class_name} not found in {module_name}")


def _lift_shared(module_name, function_name, scope):
    """Lift a function **into a shared scope**, so order does not matter.

    ``_lift`` copies the namespace it is given, so a lifted function cannot see
    a collaborator lifted afterwards — and the failure is a NameError that reads
    as a bug in the code under test. Exec'ing into the caller's own dict means
    the lifted function's globals *are* that dict, so a second lift is visible to
    the first. The dependencies here are genuinely order-independent
    (``_flush`` is referenced by ``retarget_qdrant_scope`` and defined before it
    in the file, so file order is not call order).
    """
    source = _read(module_name)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            exec(compile(segment, f"{module_name}:{function_name}", "exec"), scope)
            return scope[function_name]
    raise AssertionError(f"{function_name} not found in {module_name}")


def _module_namespace(*names, extra=None):
    """Lift ``names`` out of the migration module, each seeing the others.

    The collaborators have to be in the namespace *before* the caller is exec'd,
    because _lift copies what it is given — filling it in afterwards leaves the
    function resolving names that do not exist, and the failure reads as a bug in
    the harness rather than as a missing stub.
    """
    ns = dict(_qdrant_namespace())
    ns.update(_module_imports(_MIGRATE))
    # Read from the module, not restated here: see _module_constant.
    ns["PATCH_KEYS"] = _module_constant(_MIGRATE, "PATCH_KEYS")
    for name in _MIGRATE_EXCEPTIONS:
        if name not in _LIFTED_CLASSES:
            _LIFTED_CLASSES[name] = _lift_class(_MIGRATE, name, {})
        ns[name] = _LIFTED_CLASSES[name]
    ns.update(extra or {})
    for name in names:
        _lift_shared(_MIGRATE, name, ns)
    return ns


def _module_imports(module_name):
    """The plain stdlib modules ``module_name`` imports at the top level.

    A lifted function carries none of its module's imports, so ``sys.stderr``
    inside it is a NameError in the harness rather than a finding — and it looks
    like the code under test is broken. Derived from the AST rather than listed
    by hand so a new import cannot be forgotten here.
    """
    found = {}
    for node in ast.parse(_read(module_name)).body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found[alias.asname or alias.name.split(".")[0]] = alias.name
    return {name: __import__(target, fromlist=["_"])
            for name, target in found.items()
            if not target.startswith(("common", "qdrant_client", "scoped_ids"))}


class _StubCommon:
    """Stands in for the app module ``run()`` imports inside its own body.

    ``run`` does ``from common import get_qdrant, get_neo4j`` at call time, so a
    namespace entry is not enough — the import has to resolve. Without this the
    harness would have to strip the import out of the segment, which means the
    code under test is no longer the code that ships.
    """

    COLLECTION_NAME = "ea_memories"
    DIARY_COLLECTION = "ea_diary"

    def __init__(self, qdrant, neo4j):
        self.qdrant = qdrant
        self.neo4j = neo4j

    async def get_qdrant(self):
        return self.qdrant

    def get_neo4j(self):
        return self.neo4j


class VerifyTests(unittest.TestCase):
    """``verify()`` called against fakes, because the bug it guards is invisible.

    The namespace separation is the whole point: with the Client and Context id
    sets merged into one, a payload whose ``clientId`` wrongly holds a *context*
    id tests as present, so the check reports clean on exactly the corruption it
    exists to catch. Asserting a substring of the source passed on the pre-fix
    code verbatim, which is the ``assertIn``-checks-a-token lesson again.
    """

    def setUp(self):
        ns = _module_namespace("read_graph", "read_points", "read_known_scope_ids",
                               "read_old_user_node_present", "verify")
        self.verify = ns["verify"]
        self.client_id = client_id_for(TARGET_USER, "Acme Holdings")
        self.context_id = context_id_for(TARGET_USER, self.client_id, "Phase One")

    def _run(self, payload, *, healthy=True):
        """Verify a single point carrying ``payload``.

        ``healthy`` puts both scope nodes in the graph, so a payload naming the
        wrong one is the only possible cause of a dangling count.
        """
        graph = FakeGraph(
            clients=[{"id": self.client_id, "name": "n"}] if healthy else [],
            contexts=[{"id": self.context_id, "name": "n"}] if healthy else [],
            user_node=healthy)
        qdrant = FakeQdrant({"ea_memories": {"f": dict({"userId": TARGET_USER},
                                                      **payload)},
                             "ea_diary": {}})
        return asyncio.run(self.verify(qdrant, graph, SOURCE_USER, TARGET_USER))

    def test_a_healthy_payload_reports_nothing_dangling(self):
        report = self._run({"clientId": self.client_id,
                            "contextId": self.context_id})
        self.assertEqual(report["dangling_scope_payloads"], 0)

    def test_each_id_is_checked_against_its_own_namespace(self):
        # Both directions, and counted per kind. Merged into one set these both
        # read as present, which is the regression.
        for key, other in (("clientId", self.context_id),
                           ("contextId", self.client_id)):
            with self.subTest(payload_key=key):
                report = self._run({key: other})
                self.assertEqual(report[f"dangling_{key[:-2].lower()}_payloads"], 1)
                self.assertEqual(report["dangling_scope_payloads"], 1)

    def test_records_and_the_leftover_hub_node_are_reported(self):
        graph = FakeGraph(facts=4, diary=2, user_node=True,
                          clients=[{"id": self.client_id, "name": "n"}])
        report = asyncio.run(self.verify(FakeQdrant({"ea_memories": {},
                                                      "ea_diary": {}}), graph,
                                           SOURCE_USER, TARGET_USER))
        # Facts, diary AND the client: a scope node left behind is a whole client
        # the user cannot see, which is why the count is not just the records.
        self.assertEqual(report["old_left_in_graph"], 7)
        self.assertTrue(report["source_user_node_left"])
        self.assertFalse(asyncio.run(self.verify(
            FakeQdrant({"ea_memories": {}, "ea_diary": {}}),
            FakeGraph(user_node=False), SOURCE_USER, TARGET_USER
        ))["source_user_node_left"])

class CountKeysTests(unittest.TestCase):
    """``_count_keys`` **called**, because a stub everywhere left it untested.

    ``sessions.db_path()`` creates its directory, so this function has real
    filesystem behaviour and a real failure mode. Stubbed in every ``RunTests``
    case it was never executed at all, which is why reverting it to raise — and
    moving the call back above the ``--apply`` gate — passed the whole suite.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vault-keys-")
        self.saved = os.environ.get("MEM_SESSION_DIR")
        os.environ["MEM_SESSION_DIR"] = self.tmp
        self.addCleanup(self._restore)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.count_keys = _module_namespace("_count_keys")["_count_keys"]

    def _restore(self):
        if self.saved is None:
            os.environ.pop("MEM_SESSION_DIR", None)
        else:
            os.environ["MEM_SESSION_DIR"] = self.saved
        sessions._initialised_path = None

    def test_it_counts_a_real_store(self):
        import sessions as store
        store.init_db()
        store.create_psk("someone", label="a")
        store.create_psk("someone", label="b")
        self.assertEqual(self.count_keys("someone"), 2)
        self.assertEqual(self.count_keys("nobody"), 0)

    def test_it_returns_none_rather_than_raising_on_an_unusable_store(self):
        # An unreadable SQLite is a skipped check, not a failed migration: this
        # must not abort a run that has not started.
        os.environ["MEM_SESSION_DIR"] = os.path.join(self.tmp, "not-a-dir")
        open(os.environ["MEM_SESSION_DIR"], "w").close()
        self.assertIsNone(self.count_keys("someone"))

    def test_a_dry_run_never_calls_it(self):
        # The point of moving it behind --apply: a dry run says "Nothing was
        # written", and creating a database to find that out would make it false.
        # The call list has to be returned and asserted — collecting it and
        # checking only the exit code passes on the exact defect it names.
        with contextlib.redirect_stderr(io.StringIO()):
            code, calls = self._drive_dry()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [],
                         "a dry run must not read or write the access-key store")

    def _drive_dry(self):
        import argparse
        case = RunTests("test_a_clean_run_exits_zero")
        case.setUp()
        try:
            calls = []
            ns = _module_namespace(extra={
                "read_graph": case._read_graph(),
                "read_points": case._read_points(None),
                "_load_env": lambda *a, **k: None,
                "_report": lambda *a, **k: None,
                "_count_keys": lambda *a, **k: calls.append(1),
                "perform_move": lambda *a, **k: calls.append(2),
                "verify": _async_value(case._healthy()),
            })
            return asyncio.run(_lift(_MIGRATE, "run", ns)(
                argparse.Namespace(source=SOURCE_USER, target=TARGET_USER,
                                   apply=False, reconcile=False,
                                   env="/nonexistent"))), calls
        finally:
            case.doCleanups()


class RunTests(unittest.TestCase):
    """``run()`` driven end to end, because its exit code is the deliverable.

    Everything else in this suite reads the script. A migration that reports a
    dirty result and exits 0 is the failure mode this exists to preclude — the
    operator reads "Done" and stops looking — and flipping ``return 1`` to
    ``return 0`` is invisible to a source assertion.

    The dirty conditions and the refusals are each one table-driven test rather
    than one test per row. They share a fixture and a single exit-code
    assertion, and a named failure is not worth twenty method bodies: the
    subTest label carries the same information.
    """

    # The fixture source holds 3 facts, 1 diary entry, 1 client, 0 contexts,
    # 0 Qdrant points (the fake is empty) and 0 access keys, so a clean run
    # reports exactly those numbers at the destination. Getting this wrong makes
    # every test in the class fail on the completeness check instead of on what
    # it is about.
    KEYS_BEFORE = 2
    EMPTY_DESTINATION = {"facts": 0, "diary": 0, "clients": [], "contexts": []}

    # Every condition that must turn a successful move into a non-zero exit, and
    # the report key each one moves. Seven conditions in five rows of AGENTS.md.
    DIRTY = (
        ("records left in the source", {"old_left_in_graph": 3}),
        ("a dangling payload", {"dangling_scope_payloads": 2,
                                "dangling_client_payloads": 1,
                                "dangling_context_payloads": 1}),
        ("points left in the source", {"old_points": 5}),
        ("facts missing at the destination", {"new_facts": 1}),
        ("diary missing at the destination", {"new_diary": 0}),
        ("points missing at the destination", {"new_points": 2}),
        ("access keys that did not move", None),  # handled separately
    )

    # Each way a destination can be non-empty, all of which must be refused
    # before a single write is issued.
    OCCUPIED = (
        ("facts", {"facts": 1, "diary": 0, "clients": [], "contexts": []}, None),
        ("diary entries", {"facts": 0, "diary": 2, "clients": [], "contexts": []}, None),
        ("clients", {"facts": 0, "diary": 0, "clients": [{"id": "c", "name": "n"}],
                     "contexts": []}, None),
        # The term the in-transaction guard also has and the preflight used not
        # to: a contexts-only destination passed the preflight, had its whole
        # Qdrant half rewritten, then aborted claiming it had gained records
        # while being prepared. It had them all along.
        ("contexts only", {"facts": 0, "diary": 0, "clients": [],
                           "contexts": [{"id": "x", "name": "n"}]}, None),
        # A destination whose Neo4j was emptied while its vector points survived
        # is what a stopped sync_orphans leaves behind. Letting it through made
        # the completeness comparison report a healthy move dirty and tell the
        # operator to stop using a vault that was fine.
        ("surviving Qdrant points", None, 1),
    )

    def setUp(self):
        self._saved = sys.modules.get("common")
        self.qdrant = FakeQdrant({"ea_memories": {}, "ea_diary": {}})
        self.graph = FakeGraph(facts=3, diary=1)
        sys.modules["common"] = _StubCommon(self.qdrant, self.graph)
        self.addCleanup(self._restore)
        # run() reports its refusals and its findings on stdout/stderr, which is
        # right for an operator and wrong for a suite: this class's expected
        # output is a line beginning "FAILED:", which reads as a failing test in
        # a combined run. Captured rather than printed; the content is asserted
        # where it matters.
        self._sinks = (io.StringIO(), io.StringIO())
        self._redirects = (contextlib.redirect_stdout(self._sinks[0]),
                           contextlib.redirect_stderr(self._sinks[1]))
        for redirect in self._redirects:
            redirect.__enter__()
        self.addCleanup(self._restore_streams)

    def _restore(self):
        if self._saved is None:
            sys.modules.pop("common", None)
        else:
            sys.modules["common"] = self._saved

    def _restore_streams(self):
        for redirect in reversed(self._redirects):
            redirect.__exit__(None, None, None)

    def output(self) -> str:
        return self._sinks[0].getvalue() + self._sinks[1].getvalue()

    @contextlib.contextmanager
    def capture(self):
        """Fresh output sinks for one case inside a table-driven test.

        Re-running ``setUp`` instead would work on the first case and then
        consume the accumulated cleanups, so the second iteration raises out of
        an empty list — a harness failure that reads as a product failure.
        """
        sinks = (io.StringIO(), io.StringIO())
        entered = (contextlib.redirect_stdout(sinks[0]),
                   contextlib.redirect_stderr(sinks[1]))
        for redirect in entered:
            redirect.__enter__()
        previous = self._sinks
        self._sinks = sinks
        try:
            yield
        finally:
            self._sinks = previous
            for redirect in reversed(entered):
                redirect.__exit__(None, None, None)

    def _read_graph(self, clients=None, destination=None):
        """Build the per-user ``read_graph`` stand-in.

        Source vault populated, destination empty, keyed by the argument — run()
        reads the same graph twice, once per user, so a single fixed answer would
        make the destination look occupied and the move would be refused before
        any of the behaviour under test is reached.

        The real ``plan_id_remap`` is used rather than a stub: ``run`` imports it
        inside its own body, so a namespace entry cannot reach it, and forcing one
        would mean stripping the import out of the segment under test.
        """
        if clients is None:
            clients = [{"id": client_id_for(SOURCE_USER, "Acme Holdings"),
                        "name": "Acme Holdings"}]

        async def _read(_driver, user_id):
            if user_id == SOURCE_USER:
                return {"facts": 3, "diary": 1, "clients": list(clients),
                        "contexts": []}
            return dict(self.EMPTY_DESTINATION if destination is None
                        else destination)
        return _read

    def _read_points(self, qdrant):
        """Point count per user, defaulting to an empty vault.

        A closure over the fake rather than the lifted function, so a test can
        hand the destination a non-empty vector store — the state a stopped
        ``sync_orphans`` leaves behind.
        """
        async def _read(_qdrant, user_id):
            if qdrant is None:
                return 0
            if user_id == SOURCE_USER:
                return (len(qdrant.data["ea_memories"])
                        + len(qdrant.data["ea_diary"]))
            return qdrant.destination_points
        return _read

    def _healthy(self, **overrides):
        report = {"old_left_in_graph": 0, "dangling_scope_payloads": 0,
                  "dangling_client_payloads": 0, "dangling_context_payloads": 0,
                  "old_points": 0, "new_facts": 3, "new_diary": 1,
                  "new_points": 0, "source_user_node_left": True}
        report.update(overrides)
        return report

    def _namespace(self, verify_report, *, read_graph=None, qdrant=None,
                   moved=None, keys=None, extra=None):
        ns = _module_namespace(extra={
            "read_points": self._read_points(qdrant),
            "read_graph": read_graph or self._read_graph(),
            "_load_env": lambda *a, **k: None,
            "_report": lambda *a, **k: None,
            "_count_keys": lambda *a, **k: (self.KEYS_BEFORE if keys is None
                                            else keys),
            "perform_move": _async_value(moved if moved is not None else {
                "scope_payloads": 1, "qdrant_points": 0, "facts": 3, "diary": 1,
                "clients": 1, "contexts": 0, "psks": self.KEYS_BEFORE}),
            "verify": _async_value(verify_report),
            # Merged last so a caller can override a stub — and so the exception
            # class it raises is the same object the handler catches. Two
            # separate _module_namespace() calls produce two *different* classes,
            # and the exception escapes the handler as a traceback.
            **(extra or {}),
        })
        return ns

    def _drive(self, verify_report=None, apply=True, source=None, target=None,
               clients=None, destination=None, qdrant=None):
        if verify_report is None:
            verify_report = self._healthy()
        run = _lift(_MIGRATE, "run", self._namespace(
            verify_report, read_graph=self._read_graph(clients, destination),
            qdrant=qdrant))
        return asyncio.run(run(self._args(source, target, apply)))

    def _args(self, source=None, target=None, apply=True):
        import argparse
        return argparse.Namespace(source=source or SOURCE_USER,
                                  target=target or TARGET_USER,
                                  apply=apply, reconcile=False,
                                  env="/nonexistent")

    def test_a_clean_run_exits_zero(self):
        self.assertEqual(self._drive(), 0)
        self.assertIn("Done.", self.output())

    def test_every_dirty_condition_exits_non_zero(self):
        for label, overrides in self.DIRTY:
            if overrides is None:
                continue
            with self.subTest(condition=label), self.capture():
                report = self._healthy()
                report.update(overrides)
                self.assertEqual(self._drive(report), 1, label)
                self.assertIn("FAILED:", self.output(), label)

    def test_keys_that_did_not_move_exit_non_zero(self):
        # SQLite is the one store verification cannot see. Run against a
        # different MEM_SESSION_DIR than the app, transfer_psks opens a
        # different database, moves nothing, and everything else reports clean.
        run = _lift(_MIGRATE, "run", self._namespace(
            self._healthy(), moved={"scope_payloads": 1, "qdrant_points": 0,
                                    "facts": 3, "diary": 1, "clients": 1,
                                    "contexts": 0, "psks": 0}))
        self.assertEqual(asyncio.run(run(self._args())), 1)
        self.assertIn("MEM_SESSION_DIR", self.output())

    def test_every_way_a_non_empty_destination_is_refused(self):
        for label, destination, points in self.OCCUPIED:
            with self.subTest(destination=label), self.capture():
                qdrant = None
                if points is not None:
                    qdrant = FakeQdrant({"ea_memories": {}, "ea_diary": {}})
                    qdrant.destination_points = points
                self.assertEqual(self._drive(destination=destination,
                                             qdrant=qdrant), 1, label)
                self.assertIn("the destination is not empty", self.output())

    def test_the_other_refusals(self):
        # Same user, an empty source, and a duplicated source name are three
        # arguments or one fixture rather than three method bodies.
        duplicate = {"id": "a-second-row-same-name", "name": "Acme Holdings"}
        cases = (
            ("same user", dict(source=SOURCE_USER, target=SOURCE_USER)),
            ("duplicate source name", dict(clients=[
                {"id": client_id_for(SOURCE_USER, "Acme Holdings"),
                 "name": "Acme Holdings"}, duplicate])),
        )
        for label, kwargs in cases:
            with self.subTest(refusal=label), self.capture():
                self.assertEqual(self._drive(**kwargs), 1, label)
        # An empty source needs its own graph, since the default has content.
        run = _lift(_MIGRATE, "run", self._namespace(
            self._healthy(), read_graph=_async_value(
                {"facts": 0, "diary": 0, "clients": [], "contexts": []})))
        self.assertEqual(asyncio.run(run(self._args())), 1)
        self.assertIn("nothing to move", self.output())

    def test_a_contexts_only_source_is_not_moved_as_empty(self):
        # The "nothing to move" check has to count scope nodes. Without this term
        # a source whose only content is Context nodes is reported as empty, while
        # a source with the same number of clients is not.
        parent = client_id_for(SOURCE_USER, "Acme Holdings")

        async def _read(_driver, user_id):
            if user_id == SOURCE_USER:
                return {"facts": 0, "diary": 0, "clients": [], "contexts": [
                    {"id": context_id_for(SOURCE_USER, parent, "Phase One"),
                     "name": "Phase One", "clientId": parent}]}
            return dict(self.EMPTY_DESTINATION)
        run = _lift(_MIGRATE, "run", self._namespace(
            self._healthy(new_facts=0, new_diary=0), read_graph=_read,
            moved={"scope_payloads": 0, "qdrant_points": 0, "facts": 0,
                   "diary": 0, "clients": 0, "contexts": 1,
                   "psks": self.KEYS_BEFORE}))
        self.assertEqual(asyncio.run(run(self._args())), 0)
        self.assertIn("Done.", self.output())

    def test_a_records_only_source_is_not_refused_as_a_no_op(self):
        # Records with no Client/Context nodes at all — clients are created on
        # demand, so this is an ordinary vault — and the move does real work. The
        # "every id would come out unchanged" guard used to refuse it, because
        # there are no ids to change. clients=[] is the whole point: with a client
        # present the ids change and any no-op guard is satisfied.
        self.assertEqual(self._drive(clients=[]), 0)
        self.assertIn("Done.", self.output())

    def test_a_dry_run_reaches_neither_the_writes_nor_the_key_store(self):
        # Two calls, one assertion each. The key count matters because
        # sessions.db_path() creates its directory, so reading it during a dry run
        # writes a database and then prints "Nothing was written" — which would
        # be false.
        calls = []
        ns = self._namespace(self._healthy())
        ns["_count_keys"] = lambda *a, **k: calls.append("keys")
        ns["perform_move"] = lambda *a, **k: calls.append("move")
        self.assertEqual(asyncio.run(_lift(_MIGRATE, "run", ns)(
            self._args(apply=False))), 0)
        self.assertEqual(calls, [], "a dry run must not read or write anything")

    def test_the_occupancy_abort_is_a_clean_refusal(self):
        # The one refusal only discoverable after the writes begin. It has to read
        # like every other one, and it must not claim nothing was written: the
        # Qdrant half is already rewritten by that point.
        abort = _LIFTED_CLASSES["DestinationOccupied"]

        def _boom(*_a, **_k):
            raise abort("the destination gained records; the Qdrant half has "
                        "already been rewritten")
        ns = self._namespace(self._healthy(), extra={"perform_move": _boom})
        self.assertEqual(asyncio.run(_lift(_MIGRATE, "run", ns)(self._args())), 1)
        self.assertIn("ERROR:", self.output())
        self.assertIn("already been rewritten", self.output(),
                      "the refusal must say the Qdrant half is already done")
        self.assertNotIn("Traceback", self.output())

    def test_the_abort_message_does_not_claim_nothing_was_written(self):
        """The shipped message, not a stub's copy of it.

        A message is text, so a source assertion is the right tool — the same
        reasoning as the LLM prompt sentences in test_matching_regressions.
        Nothing else in the suite can see it, and it is load-bearing: the Qdrant
        half IS rewritten when this fires, so an operator who believed the
        opposite stops looking at a vault that is half-moved.

        Read from the AST, not a slice: two earlier versions of this check failed
        on text that is not the message — the explanatory comment quotes the
        phrase to say why it is wrong, and the dry-run banner further down
        legitimately says "Nothing was written".
        """
        message = None
        for node in ast.walk(ast.parse(_read(_MIGRATE))):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                for arg in node.exc.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and "destination gained records" in arg.value:
                        message = arg.value
        self.assertIsNotNone(message, "the occupancy abort must raise a reason")
        self.assertNotIn("nothing was written", message.lower())
        self.assertIn("already been rewritten", message,
                      "the abort must say the Qdrant half is already done")

    def test_a_failure_from_the_credential_half_is_not_a_clean_refusal(self):
        """The reason ``DestinationOccupied`` is its own type.

        A bare ``except ValueError`` around ``perform_move`` would report this as
        a refusal — *after* Qdrant and Neo4j are fully rewritten, with no
        ``FAILED:`` line and no statement that the vault is mid-move. So it has to
        propagate, and the test says so by asserting the raise.
        """
        ns = self._namespace(self._healthy())
        ns["perform_move"] = _boom_with(ValueError("PSK row has an empty "
                                                   "user_id"))
        with self.assertRaises(ValueError):
            asyncio.run(_lift(_MIGRATE, "run", ns)(self._args()))
        self.assertNotIn("mid-move", self.output(),
                         "the credential failure must not borrow the "
                         "occupancy message")

class MigrationScriptTests(unittest.TestCase):
    """The few properties of the script that are decisions, not behaviour.

    Deliberately short. Everything else this file once asserted here by
    `assertIn` over the source — the refusals, the ordering, the access-key
    handoff, the dangling check — is now driven against fakes in `RunTests`,
    `VerifyTests`, `PerformMoveOrderTests` and `CountKeysTests`, and a substring
    assertion is strictly weaker: it pins a token, not the property. What is left
    here is what no behavioural test can reach.
    """

    def setUp(self):
        self.tree = ast.parse(_read(_MIGRATE))

    def _calls(self):
        names = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                names[node.func.id] = names.get(node.func.id, 0) + 1
        return names

    def test_dry_run_is_the_default_and_both_users_are_required(self):
        # One test because it is one argparse surface: a default anywhere on
        # --from/--to would be a hardcoded vault name, and --apply is the only
        # thing allowed to default to False.
        flags = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "add_argument":
                names = [a.value for a in node.args
                         if isinstance(a, ast.Constant)]
                for flag in names:
                    if flag in ("--from", "--to", "--apply"):
                        flags[flag] = {kw.arg for kw in node.keywords}
        for flag in ("--from", "--to"):
            self.assertIn("required", flags[flag], f"{flag} must be required")
            self.assertNotIn("default", flags[flag],
                             f"{flag} has a default, which hardcodes a vault name")
        self.assertNotIn("default", flags["--apply"],
                         "--apply must be opt-in")

    def test_run_delegates_the_writes_to_perform_move(self):
        # So the ordering lives in one testable function instead of inline in
        # run(), where a test could only read it. PerformMoveOrderTests drives it.
        calls = self._calls()
        for stage in ("retarget_qdrant_scope", "move_qdrant_user", "move_graph",
                      "move_credentials"):
            self.assertEqual(calls.get(stage), 1,
                             f"{stage} must be called from perform_move only")
        self.assertNotIn("retarget_qdrant_scope", _read(_MIGRATE).split("async def run(", 1)[1],
                         "run() must not perform the writes itself")

    def test_the_account_itself_is_never_touched(self):
        # Checked against calls and imports, not text: the module docstring names
        # every one of these to say it leaves them alone, so a substring guard
        # fails on the explanation and is then watered down until it asserts
        # nothing.
        called = set(self._calls())
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                called.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                called.update(a.name for a in node.names)
        for forbidden in ("delete_credentials", "create_credentials", "htpasswd",
                          "google_identities", "revoke_psk", "user_id_taken",
                          "verify_account"):
            self.assertNotIn(forbidden, called,
                             f"the migration must not reach {forbidden}")

def _boom_with(exc):
    """A callable that raises ``exc``, for the failure-path tests."""
    def _boom(*_a, **_k):
        raise exc
    return _boom


def _async_value(value):
    async def _get(*_a, **_k):
        return value
    return _get


class NoVaultNamesCommittedTests(unittest.TestCase):
    """A vault name is the operator's, and must never reach this repository.

    The tool takes both usernames as arguments precisely so that neither is ever
    written down, so the checks are structural: nothing may *default* to a name,
    and the two new modules may not bind a literal that has the shape of one.

    Scoped, deliberately, to the modules this change introduces. Running the
    shape check over ``sessions.py`` and ``client_manager.py`` needs an allowlist
    of ~90 legitimate column names, dict keys and codec names, and it would rot
    on the next schema change — a guard that needs updating whenever unrelated
    code moves is a guard that gets deleted. A name pasted into an existing file
    is caught by the commit diff; the shape of this change is that a name would
    land in a *new* file, so that is where the check belongs.
    """

    NEW_MODULES = ("scoped_ids.py", "migrate_vault_user.py")
    ALL_MODULES = ("scoped_ids.py", "migrate_vault_user.py", "sessions.py",
                   "client_manager.py")

    # Deliberate exceptions in the two new modules: short lowercase tokens that
    # match the shape but are not identities. Kept explicit so that adding one
    # is a decision somebody reads.
    ALLOWED_USERNAME_SHAPED = frozenset({
        # Exported symbol names, listed in scoped_ids.__all__.
        "client_id_for", "context_id_for", "plan_id_remap", "idremap",
        "IdRemap",
        # Cypher label names, which the shape cannot tell from a CamelCase
        # vault name.
        "Fact", "DiaryEntry",
        # Dict keys the Client/Context rows are read and reported by.
        "name", "clients", "contexts", "facts", "diary",
        # Report keys the script prints, all built from the values above.
        "dangling_scope_payloads", "dangling_client_payloads",
        "dangling_context_payloads", "moved", "new_diary", "new_facts",
        "new_points", "old_left_in_graph", "old_points", "psks",
        "qdrant_points", "scope_payloads", "sessions_revoked", "verification",
        "source_user_node_left",
        # argparse vocabulary and dest names.
        "reconcile", "source", "store_false", "store_true", "target",
        # The two halves of the known-scope-id lookup in verify(), which are kept
        # apart precisely so they can be told apart.
        "client", "context",
    })

    # Two shapes, because the username is only lowercased in some paths:
    # `_verify_account` compares the lowercased key, and nothing forces the
    # *stored* spelling to be lowercase. A name written "Some Name" or
    # "Some-Name" is as real as "somename", and a shape that only matched
    # lowercase let both of those through.
    _USERNAME_SHAPES = (
        re.compile(r"^[a-z][a-z0-9_.-]{2,31}$"),
        re.compile(r"^[A-Z][A-Za-z0-9_.-]{2,31}$"),
        re.compile(r"^[A-Z][a-z]+(?:[ ][A-Z][a-z]+){1,3}$"),
    )

    def _username_shaped_literals(self, module_name):
        """Yield ``(lineno, literal)`` for every string constant that looks like
        an account name, anywhere in the module including docstrings.

        Literals *inside an f-string* are excluded, and that is a distinction
        rather than a convenience: a fragment of an interpolation template is
        never a bound value, so ``f"client_{user_id}_{name}"`` is not the vault
        being called "client". Excluding them keeps the guard on the thing it is
        for instead of on the derivation's own spelling.
        """
        tree = ast.parse(_read(module_name))
        template_fragments = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        template_fragments.add(part.value)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in template_fragments:
                    continue
                if any(shape.match(node.value) for shape in self._USERNAME_SHAPES):
                    yield node.lineno, node.value

    def test_a_new_module_binds_no_username_shaped_literal(self):
        for name in self.NEW_MODULES:
            for lineno, literal in self._username_shaped_literals(name):
                self.assertIn(
                    literal, self.ALLOWED_USERNAME_SHAPED,
                    f"{name}:{lineno} binds {literal!r}, which has the shape of "
                    "a vault name. Vault names are the operator's and must not "
                    "be committed.")

    def test_neither_username_argument_has_a_default(self):
        # Scoped to the two flags on purpose: `--env` legitimately has a default
        # path, and a guard broad enough to flag that one would be deleted.
        tree = ast.parse(_read("migrate_vault_user.py"))
        seen = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                continue
            flags = [a.value for a in node.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            for flag in flags:
                if flag in ("--from", "--to"):
                    seen.add(flag)
                    self.assertFalse(
                        any(kw.arg == "default" for kw in node.keywords),
                        f"{flag} has a default, which would hardcode a vault name")
                    self.assertTrue(
                        any(kw.arg == "required" for kw in node.keywords),
                        f"{flag} must be required")
        self.assertEqual(seen, {"--from", "--to"})

    def test_the_usernames_come_only_from_the_arguments(self):
        # No module-level or default-valued identity anywhere in the two new
        # files, so the only path from a vault name into a write is argv.
        for name in self.NEW_MODULES:
            tree = ast.parse(_read(name))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not (isinstance(value, ast.Constant)
                        and isinstance(value.value, str) and value.value):
                    continue
                targets = ([node.target] if isinstance(node, ast.AnnAssign)
                           else node.targets)
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.assertFalse(
                            any(word in target.id.upper()
                                for word in ("USER", "ACCOUNT", "OPERATOR",
                                             "TENANT", "OWNER", "VAULT")),
                            f"{name} binds the string constant {target.id}, "
                            "which reads as a hardcoded identity")

    def test_the_usage_doc_uses_placeholders(self):
        self.assertTrue("--from OLD_USER --to NEW_USER" in _read("migrate_vault_user.py"),
                        "the usage block must use placeholders, not real names")

    def test_the_fixtures_are_the_neutral_placeholders(self):
        # If somebody replaces these with a real vault to make a test easier to
        # read, the name is now in the repository and this fails.
        self.assertEqual((SOURCE_USER, TARGET_USER), ("vault-alpha", "vault-beta"))

    def test_the_shape_check_covers_both_new_modules(self):
        # A guard that silently stopped covering what it claims to cover is worse
        # than no guard. Asserted as a set relation rather than "the files exist":
        # widening NEW_MODULES pulls the pre-existing modules into the shape check
        # and turns the test above red, which is the direction that matters.
        self.assertTrue(set(self.NEW_MODULES) <= set(self.ALL_MODULES))
        self.assertEqual(set(self.NEW_MODULES),
                         {"scoped_ids.py", "migrate_vault_user.py"})


if __name__ == "__main__":
    unittest.main()