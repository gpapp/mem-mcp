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

    def test_a_client_id_is_the_documented_uuid5(self):
        import uuid
        self.assertEqual(
            client_id_for("someone", "Acme Holdings"),
            str(uuid.uuid5(uuid.NAMESPACE_DNS, "client_someone_acme holdings")))

    def test_a_context_id_is_the_documented_uuid5(self):
        import uuid
        self.assertEqual(
            context_id_for("someone", "cid", "Phase One"),
            str(uuid.uuid5(uuid.NAMESPACE_DNS, "context_someone_cid_phase one")))

    def test_a_context_name_is_normalised_the_same_way_a_client_is(self):
        # The client path had this and the context path did not, so removing
        # `.strip()` from context_id_for passed the whole suite. Both normalise;
        # a test covering only one leaves the other free to drift into producing
        # a different id for a padded name.
        self.assertEqual(context_id_for("u", "cid", "  Phase One  "),
                         context_id_for("u", "cid", "phase one"))

    def test_a_client_name_is_normalised_the_same_way(self):
        self.assertEqual(client_id_for("u", "  ACME  "),
                         client_id_for("u", "acme"))

    def test_the_username_is_part_of_the_id(self):
        self.assertNotEqual(client_id_for("one", "Acme"),
                            client_id_for("two", "Acme"))

    def test_name_normalisation_is_case_and_space_insensitive(self):
        self.assertEqual(client_id_for("u", "  ACME  "), client_id_for("u", "acme"))

    def test_two_names_sharing_a_prefix_do_not_collide(self):
        # "client_u_acme" and "client_u_acme_uk" are different strings, but a
        # derivation that joined the fields with a fixed separator would fold
        # one into the other.
        self.assertNotEqual(client_id_for("u", "acme"), client_id_for("u", "acme_uk"))

    def test_a_context_depends_on_its_parents_id(self):
        self.assertNotEqual(context_id_for("u", "parent-a", "phase"),
                            context_id_for("u", "parent-b", "phase"))


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
    """The pure planner: what changes, what does not, and what must be refused."""

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

    def test_every_client_is_re_identified(self):
        self.assertEqual(self.remap.client_count, 2)

    def test_the_new_id_is_what_a_create_would_mint(self):
        # The point of the whole module: the moved id has to be
        # indistinguishable from one db_create_client would have produced in the
        # new vault, or the next same-named client becomes a second node.
        #
        # Membership is asserted before the lookup on purpose. Indexing the map
        # directly reports a wrong derivation as a KeyError from an omitted
        # entry — and an omitted entry is the *expected* consequence of a
        # derivation that ignores the username, since the id then comes out
        # unchanged. The guard bites either way, but as a KeyError it reads like
        # a broken fixture rather than a broken formula.
        for row in self.clients:
            self.assertIn(row["id"], self.remap.clients,
                          "a client whose id changes must be in the remap")
            self.assertEqual(self.remap.clients[row["id"]],
                             client_id_for(TARGET_USER, row["name"]))

    def test_a_context_is_derived_from_its_parents_new_id(self):
        # Deriving it from the OLD parent is the subtle half: the parent is
        # renamed in the same pass, so a context built from the old id is an id
        # nothing will ever generate again.
        expected = context_id_for(
            TARGET_USER, client_id_for(TARGET_USER, "Acme Holdings"), "Phase One")
        self.assertEqual(list(self.remap.contexts.values()), [expected])

    def test_the_parent_each_context_points_at_is_reported(self):
        # Deliberately a separate map from `contexts`. Both have the same shape
        # and the same key type, so handing the caller the new *context* id where
        # the *parent client* id belongs points every Context at a sibling that
        # does not exist — and reads as correct, because the value is a real id
        # of the right shape.
        old_context_id = self.contexts[0]["id"]
        self.assertEqual(self.remap.context_parents[old_context_id],
                         client_id_for(TARGET_USER, "Acme Holdings"))

    def test_the_parent_map_is_not_the_context_map(self):
        old_context_id = self.contexts[0]["id"]
        self.assertNotEqual(self.remap.context_parents[old_context_id],
                            self.remap.contexts[old_context_id])

    def test_a_same_user_move_is_reported_as_a_no_op(self):
        # Otherwise the script prints a tidy success having done nothing, which
        # is the failure changes_anything exists to catch.
        same = plan_id_remap(SOURCE_USER, SOURCE_USER, self.clients, self.contexts)
        self.assertFalse(same.changes_anything)
        self.assertEqual(same.clients, {})
        self.assertEqual(same.contexts, {})

    def test_an_unchanged_id_is_left_out_of_the_map(self):
        same = plan_id_remap(SOURCE_USER, SOURCE_USER, self.clients, self.contexts)
        self.assertEqual(sorted(same.unchanged_clients),
                         sorted(c["id"] for c in self.clients))

    def test_an_occupied_destination_name_is_detected(self):
        clash = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                              self.contexts,
                              target_clients=[{"name": "acme holdings"}])
        self.assertTrue(clash.destination_occupied)

    def test_an_unrelated_destination_name_is_not_a_clash(self):
        free = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                             self.contexts,
                             target_clients=[{"name": "Gamma GmbH"}])
        self.assertFalse(free.destination_occupied)

    def test_a_clashing_context_name_is_also_detected(self):
        clash = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                              self.contexts,
                              target_contexts=[{"name": "Phase One"}])
        self.assertTrue(clash.destination_occupied)

    def test_duplicate_source_client_names_are_reported(self):
        # Two same-named Clients in the source derive one id, so the move would
        # leave two nodes sharing it. `destination_occupied` cannot see this: it
        # only inspects the target.
        doubled = self.clients + [dict(self.clients[0])]
        remap = plan_id_remap(SOURCE_USER, TARGET_USER, doubled, self.contexts)
        self.assertEqual(remap.duplicate_names, ["Acme Holdings"])

    def test_duplicate_source_context_names_are_reported(self):
        # Same collision one level down, and it needs the parent in the key:
        # two contexts of one name under *different* clients are two different
        # nodes with two different ids.
        other = {"id": client_id_for(SOURCE_USER, "Beta SE"), "name": "Beta SE"}
        twin = dict(self.contexts[0])
        twin["id"] = context_id_for(SOURCE_USER, other["id"], "Phase One")
        twin["clientId"] = other["id"]
        clash = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                              self.contexts + [twin])
        self.assertEqual(clash.duplicate_names, [])

        same_parent = dict(self.contexts[0])
        same_parent["id"] = "a-different-hand-written-id"
        dupes = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients,
                              self.contexts + [same_parent])
        self.assertEqual(len(dupes.duplicate_names), 1)

    def test_the_row_accessors_cover_every_source_id(self):
        # `clients` alone omits any row whose id comes out unchanged, and
        # move_graph must still rewrite those: it is handed client_rows(), not
        # clients. A same-user plan is the only thing that populates the
        # unchanged set, since a real rename changes every derived id — so that
        # is what exercises the union.
        same = plan_id_remap(SOURCE_USER, SOURCE_USER, self.clients,
                             self.contexts)
        self.assertEqual(same.clients, {})
        self.assertEqual(same.unchanged_clients,
                         [c["id"] for c in self.clients])
        self.assertEqual(set(same.client_rows()),
                         {c["id"] for c in self.clients})
        self.assertEqual(set(same.context_rows()),
                         {c["id"] for c in self.contexts})
        # Values, not just key sets. A self-referential parent — the map mapping
        # each context id to *itself* — passes every key-set assertion and writes
        # a Context that points at itself.
        self.assertEqual(same.context_parent_rows(),
                         {c["id"]: c["clientId"] for c in self.contexts})

    def test_the_parent_map_never_points_a_context_at_itself(self):
        for source, target in ((SOURCE_USER, TARGET_USER),
                               (SOURCE_USER, SOURCE_USER)):
            remap = plan_id_remap(source, target, self.clients, self.contexts)
            for context_id, parent in remap.context_parent_rows().items():
                self.assertNotEqual(
                    context_id, parent,
                    "a Context whose id is unchanged but whose parent is being "
                    "re-identified must still get the new parent id, not its own")

    def test_an_unchanged_context_still_has_its_parent_rewritten(self):
        # The defect this pair exists for. A context stored with an id already
        # derived for the destination user comes out of the remap unchanged, and
        # its `clientId` property is then never rewritten — so it keeps pointing
        # at the old client while that client is re-identified.
        old_acme = client_id_for(SOURCE_USER, "Acme Holdings")
        new_acme = client_id_for(TARGET_USER, "Acme Holdings")
        # Stored with an id already derived for the *destination* user, but a
        # clientId property still pointing at the source's client — which is the
        # inconsistent state that produces the self-referential write.
        rows = [{"id": context_id_for(TARGET_USER, new_acme, "Phase One"),
                 "name": "Phase One", "clientId": old_acme}]
        remap = plan_id_remap(SOURCE_USER, TARGET_USER, self.clients, rows)
        self.assertEqual(remap.contexts, {}, "precondition: the id is unchanged")
        only = next(iter(remap.context_parent_rows()))
        self.assertEqual(remap.context_parent_rows()[only], new_acme,
                         "the stale parent pointer must be rewritten")

    def test_the_row_accessors_include_the_changed_rows(self):
        rows = self.remap.client_rows()
        for row in self.clients:
            self.assertEqual(rows[row["id"]],
                             client_id_for(TARGET_USER, row["name"]))
        self.assertEqual(set(self.remap.context_rows()),
                         set(self.remap.contexts))

    def test_a_vault_with_no_clients_has_nothing_to_remap(self):
        empty = plan_id_remap(SOURCE_USER, TARGET_USER, [], [])
        self.assertFalse(empty.changes_anything)
        self.assertFalse(empty.destination_occupied)


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
        # keys. A retarget that stops at chunk 0 leaves the family split, and
        # the symptom is a long record that filters differently from itself.
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

    def test_both_scope_ids_are_rewritten(self):
        qdrant = self._fixture()
        self.assertEqual(self._retarget(qdrant), 4)
        payload = qdrant.data["ea_memories"]["fact-1"]
        self.assertEqual(payload["clientId"], self.new_client)
        self.assertEqual(payload["contextId"], self.new_context)

    def test_every_chunk_of_a_family_is_rewritten(self):
        qdrant = self._fixture()
        self._retarget(qdrant)
        for pid in ("fact-1", "fact-1#1", "fact-1#2"):
            self.assertEqual(qdrant.data["ea_memories"][pid]["clientId"],
                             self.new_client, f"{pid} was left behind")

    def test_names_are_left_alone(self):
        # A name is not derived from the username, so rewriting it would be a
        # gratuitous write of every scoped point in the vault.
        qdrant = self._fixture()
        self._retarget(qdrant)
        self.assertEqual(qdrant.data["ea_memories"]["fact-1"]["clientName"],
                         "Acme Holdings")

    def test_the_record_text_survives(self):
        qdrant = self._fixture()
        self._retarget(qdrant)
        self.assertEqual(qdrant.data["ea_memories"]["fact-1"]["text"], "body")

    def test_an_unscoped_point_is_not_written_at_all(self):
        qdrant = self._fixture()
        self._retarget(qdrant)
        self.assertNotIn("fact-2", qdrant.written_ids())

    def test_another_users_point_is_never_touched(self):
        # The fixture gives another vault a point carrying this vault's client
        # id; ownership is the userId filter, and the filter is what stops a
        # migration rewriting a stranger's payload.
        qdrant = self._fixture()
        self._retarget(qdrant)
        self.assertEqual(qdrant.data["ea_diary"]["other-1"]["clientId"],
                         self.old_client)

    def test_it_never_builds_a_per_point_payload(self):
        # The container's PointStruct is a pydantic model with a **required**
        # vector field, and set_payload ignores the value. An earlier version
        # omitted it, the permissive stub accepted that, and the whole suite
        # passed — then the container raised a ValidationError on the first real
        # point. The fake now refuses the per-point form outright, and this
        # asserts the module never imports the model at all.
        qdrant = self._fixture()
        self._retarget(qdrant)
        # An AST check, not assertNotIn: the module docstring explains *why* the
        # per-point form is avoided, so a substring guard fails on the
        # explanation — the assertIn-matches-the-comment lesson again.
        for node in ast.walk(ast.parse(_read(_MIGRATE))):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn("qdrant_client.models",
                                 [a.name for a in node.names],
                                 "the per-point payload form needs a pydantic "
                                 "model whose required 'vector' field "
                                 "set_payload ignores")
            if isinstance(node, ast.Call):
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", ""))
                self.assertNotEqual(name, "PointStruct")
        for _collection, pid, payload in qdrant.payload_writes:
            self.assertIn(payload, ({}, {"clientId": self.new_client},
                                    {"contextId": self.new_context},
                                    {"clientId": self.new_client,
                                     "contextId": self.new_context}))

    def test_an_id_that_is_not_in_the_map_is_left_alone(self):
        # A dangling clientId from a deleted client is already broken; blanking
        # it would hide that instead of surfacing it.
        qdrant = FakeQdrant({"ea_memories": {"f": {"userId": SOURCE_USER,
                                                   "clientId": "gone"}},
                             "ea_diary": {}})
        self.assertEqual(self._retarget(qdrant), 0)
        self.assertEqual(qdrant.data["ea_memories"]["f"]["clientId"], "gone")


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
    which labels move, that ids and the parent pointer move with them, and that
    nothing is re-identified that does not need it. The query text itself is
    linted by ``test_cypher_safety.py``, which scans this module too.
    """

    def setUp(self):
        self.move_graph = _module_namespace("move_graph")["move_graph"]
        self.driver = FakeDriver()
        self.client_map = {"old-client": "new-client"}
        self.context_map = {"old-ctx": "new-ctx"}
        self.parents = {"old-ctx": "new-client"}

    def _go(self):
        self.move_graph(self.driver, SOURCE_USER, TARGET_USER, self.client_map,
                        self.context_map, self.parents)
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
        self.assertEqual(params["old"], SOURCE_USER)
        self.assertEqual(params["new"], TARGET_USER)

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
        self.assertIn(":Context {id:", labelled[1])

    def test_facts_and_diary_move_by_user_id_only(self):
        # The record id is a uuid4 (facts) or timestamp-derived (diary) and is
        # not a function of the user, so nothing else about them may be written.
        self._go()
        fact, _ = self._only("MATCH (n:Fact)")
        diary, _ = self._only("MATCH (n:DiaryEntry)")
        self.assertIn("WHERE n.userId = $old SET n.userId = $new", fact)
        self.assertIn("WHERE n.userId = $old SET n.userId = $new", diary)
        self.assertNotIn("n.id", fact)
        self.assertNotIn("n.id", diary)

    def test_the_returned_counts_are_per_label(self):
        counts = self.move_graph(self.driver, SOURCE_USER, TARGET_USER,
                                 self.client_map, self.context_map, self.parents)
        self.assertEqual(counts["facts"], 7)
        self.assertEqual(counts["diary"], 3)
        self.assertEqual(counts["clients"], 1)
        self.assertEqual(counts["contexts"], 1)

    def test_the_reported_client_count_is_what_the_match_rewrote(self):
        # Not len(client_map). A row whose MATCH matched nothing is a silent
        # no-op, and reporting the plan instead of the result would hide exactly
        # that — the whole reason the statements RETURN a count.
        self.driver.counts["Client"] = 0
        counts = self.move_graph(self.driver, SOURCE_USER, TARGET_USER,
                                 self.client_map, self.context_map, self.parents)
        self.assertEqual(counts["clients"], 0)
        self.assertEqual(counts["contexts"], 1)

    def test_a_non_empty_destination_aborts_the_whole_transaction(self):
        # Re-checked inside the transaction, not only before it: a record saved
        # into the destination in between would otherwise be interleaved with the
        # arriving ones, and the move would exit 0 having merged two vaults.
        self.driver.counts["__bare__"] = 4
        with self.assertRaises(ValueError):
            self.move_graph(self.driver, SOURCE_USER, TARGET_USER,
                            self.client_map, self.context_map, self.parents)
        self.assertEqual(self.driver.transactions, 1)

    def test_the_destination_gets_a_user_node(self):
        self._go()
        _, params = self._only("MERGE (u:User")
        self.assertEqual(params["new"], TARGET_USER)

    def test_no_node_is_deleted(self):
        # The old account is left intact by design, and nothing else may go.
        for query, _ in self._go():
            self.assertNotIn("DELETE", query.upper())

    def test_no_user_node_is_created_for_the_source(self):
        self._go()
        for query, _ in self._go():
            self.assertNotIn("MERGE (u:User {id: $old})", query)

    def test_category_nodes_are_never_rewritten(self):
        # :Category is global — it carries no userId at all — so a migration
        # touching it would re-file one vault's categories under another.
        for query, _ in self._go():
            self.assertNotIn(":Category", query)

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
        ns[name] = _lift_class(_MIGRATE, name, ns)
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

    def _ids(self):
        return (client_id_for(TARGET_USER, "Acme Holdings"),
                context_id_for(TARGET_USER,
                               client_id_for(TARGET_USER, "Acme Holdings"),
                               "Phase One"))

    def _run(self, payload, client_ids, context_ids, **kwargs):
        # client_ids / context_ids are required, not defaulted: with empty
        # defaults a caller that forgot them got a graph with no scope nodes at
        # all and every payload id read as dangling — the signature failed open.
        graph = FakeGraph(clients=[{"id": i, "name": "n"} for i in client_ids],
                          contexts=[{"id": i, "name": "n"} for i in context_ids],
                          **kwargs)
        qdrant = FakeQdrant({"ea_memories": {"f": dict({"userId": TARGET_USER},
                                                      **payload)},
                             "ea_diary": {}})
        return asyncio.run(self.verify(qdrant, graph, SOURCE_USER, TARGET_USER))

    def test_a_healthy_payload_reports_nothing_dangling(self):
        client_id, context_id = self._ids()
        report = self._run({"clientId": client_id, "contextId": context_id},
                           client_ids=[client_id], context_ids=[context_id])
        self.assertEqual(report["dangling_scope_payloads"], 0)

    def test_a_client_id_pointing_at_a_context_id_is_dangling(self):
        # The regression: merged into one set this tests as present.
        client_id, context_id = self._ids()
        report = self._run({"clientId": context_id},
                           client_ids=[client_id], context_ids=[context_id])
        self.assertEqual(report["dangling_client_payloads"], 1)
        self.assertEqual(report["dangling_scope_payloads"], 1)

    def test_a_context_id_pointing_at_a_client_id_is_dangling(self):
        client_id, context_id = self._ids()
        report = self._run({"contextId": client_id},
                           client_ids=[client_id], context_ids=[context_id])
        self.assertEqual(report["dangling_context_payloads"], 1)

    def test_each_kind_is_counted_separately(self):
        client_id, context_id = self._ids()
        report = self._run({"clientId": context_id, "contextId": client_id},
                           client_ids=[client_id], context_ids=[context_id])
        self.assertEqual(report["dangling_client_payloads"], 1)
        self.assertEqual(report["dangling_context_payloads"], 1)
        self.assertEqual(report["dangling_scope_payloads"], 2)

    def test_records_left_in_the_source_are_counted(self):
        client_id, _ = self._ids()
        graph = FakeGraph(facts=4, diary=2, clients=[{"id": client_id, "name": "n"}])
        qdrant = FakeQdrant({"ea_memories": {}, "ea_diary": {}})
        report = asyncio.run(self.verify(qdrant, graph, SOURCE_USER, TARGET_USER))
        self.assertEqual(report["old_left_in_graph"], 7)

    def test_the_leftover_user_node_is_reported(self):
        client_id, _ = self._ids()
        qdrant = FakeQdrant({"ea_memories": {}, "ea_diary": {}})
        self.assertTrue(asyncio.run(
            self.verify(qdrant, FakeGraph(user_node=True), SOURCE_USER,
                        TARGET_USER))["source_user_node_left"])
        self.assertFalse(asyncio.run(
            self.verify(qdrant, FakeGraph(user_node=False), SOURCE_USER,
                        TARGET_USER))["source_user_node_left"])


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
    """

    EMPTY_DESTINATION = {"facts": 0, "diary": 0, "clients": [], "contexts": []}

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

    def _restore_streams(self):
        for redirect in reversed(self._redirects):
            redirect.__exit__(None, None, None)

    def output(self) -> str:
        return self._sinks[0].getvalue() + self._sinks[1].getvalue()

    def _read_graph(self, clients=None, destination=None):
        """Build the per-user ``read_graph`` stand-in.

        Source vault populated, destination empty, keyed by the argument.

        run() reads the same graph twice — once per user — so a single fixed
        answer would make the destination look occupied and the move would be
        refused before any of the behaviour under test is reached.

        The real ``plan_id_remap`` is used rather than a stub: ``run`` imports it
        inside its own body, so a namespace entry cannot reach it, and forcing one
        would mean stripping the import out of the segment under test.
        """
        if clients is None:
            client = client_id_for(SOURCE_USER, "Acme Holdings")
            clients = [{"id": client, "name": "Acme Holdings"}]

        async def _read(_driver, user_id):
            if user_id == SOURCE_USER:
                return {"facts": 3, "diary": 1, "clients": list(clients),
                        "contexts": []}
            return dict(self.EMPTY_DESTINATION if destination is None
                        else destination)
        return _read

    def _restore(self):
        if self._saved is None:
            sys.modules.pop("common", None)
        else:
            sys.modules["common"] = self._saved

    # The fixture source holds 3 facts, 1 diary entry, 1 client, 0 contexts and
    # 0 Qdrant points (the qdrant fake is empty) and 0 access keys, so a clean
    # run reports exactly those numbers at the destination. Getting this wrong
    # makes every test in the class fail on the completeness check instead of on
    # what it is about.
    KEYS_BEFORE = 2

    def _read_points(self, qdrant):
        """Point count per user, defaulting to an empty vault.

        A closure over the fake rather than the lifted function, so a test can
        hand the destination a non-empty vector store — the state a stopped
        ``sync_orphans`` leaves behind, and the one the completeness comparison
        would otherwise mis-report.
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

    def _drive(self, verify_report, apply=True, source=None, target=None,
               clients=None, destination=None, qdrant=None):
        import argparse
        ns = _module_namespace(
            extra={
                "read_points": self._read_points(qdrant),
                "read_graph": self._read_graph(clients, destination),
                "_load_env": lambda *a, **k: None,
                "_report": lambda *a, **k: None,
                "_count_keys": lambda *a, **k: self.KEYS_BEFORE,
                "perform_move": _async_value({"scope_payloads": 1,
                                              "qdrant_points": 0, "facts": 3,
                                              "diary": 1, "clients": 1,
                                              "contexts": 0,
                                              "psks": self.KEYS_BEFORE}),
                "verify": _async_value(verify_report),
            })
        run = _lift(_MIGRATE, "run", ns)
        return asyncio.run(run(argparse.Namespace(
            source=source or SOURCE_USER, target=target or TARGET_USER,
            apply=apply, reconcile=False, env="/nonexistent")))

    def test_a_clean_run_exits_zero(self):
        self.assertEqual(self._drive(self._healthy()), 0)
        self.assertIn("Done.", self.output())

    def test_records_left_in_the_source_exit_non_zero(self):
        report = self._healthy()
        report["old_left_in_graph"] = 3
        self.assertEqual(self._drive(report), 1)

    def test_a_dangling_payload_exits_non_zero(self):
        report = self._healthy()
        report["dangling_scope_payloads"] = 2
        report["dangling_client_payloads"] = 1
        report["dangling_context_payloads"] = 1
        self.assertEqual(self._drive(report), 1)

    def test_points_left_in_the_source_exit_non_zero(self):
        # Qdrant is the half a count in the graph cannot see: a point whose
        # userId never moved is invisible to read_graph entirely.
        report = self._healthy()
        report["old_points"] = 5
        self.assertEqual(self._drive(report), 1)

    def test_records_missing_at_the_destination_exit_non_zero(self):
        # "Nothing was left behind" and "everything arrived" are different
        # claims. Only the first was being checked, so a move that lost records
        # while clearing the source printed "Done".
        self.assertEqual(self._drive(self._healthy(new_facts=1)), 1)

    def test_diary_missing_at_the_destination_exits_non_zero(self):
        self.assertEqual(self._drive(self._healthy(new_diary=0)), 1)

    def test_points_missing_at_the_destination_exit_non_zero(self):
        self.assertEqual(self._drive(self._healthy(new_points=2)), 1)

    def test_a_dry_run_never_calls_perform_move(self):
        calls = []
        import argparse
        ns = _module_namespace(
            "read_points",
            extra={
                "read_graph": self._read_graph(),
                "_load_env": lambda *a, **k: None,
                "_report": lambda *a, **k: None,
                "_count_keys": lambda *a, **k: self.KEYS_BEFORE,
                "perform_move": lambda *a, **k: calls.append(1),
                "verify": _async_value(self._healthy()),
            })
        run = _lift(_MIGRATE, "run", ns)
        code = asyncio.run(run(argparse.Namespace(
            source=SOURCE_USER, target=TARGET_USER, apply=False, reconcile=False,
            env="/nonexistent")))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [], "a dry run must not reach the writes")

    def test_keys_that_did_not_move_exit_non_zero(self):
        # The SQLite store is the one verification cannot see. Run against a
        # different MEM_SESSION_DIR than the app, transfer_psks opens a
        # different database, moves nothing, and everything else reports clean.
        import argparse
        ns = _module_namespace(
            "read_points",
            extra={
                "read_graph": self._read_graph(),
                "_load_env": lambda *a, **k: None,
                "_report": lambda *a, **k: None,
                "_count_keys": lambda *a, **k: 2,
                "perform_move": _async_value({"scope_payloads": 1,
                                              "qdrant_points": 0, "facts": 3,
                                              "diary": 1, "clients": 1,
                                              "contexts": 0, "psks": 0}),
                "verify": _async_value(self._healthy()),
            })
        run = _lift(_MIGRATE, "run", ns)
        code = asyncio.run(run(argparse.Namespace(
            source=SOURCE_USER, target=TARGET_USER, apply=True, reconcile=False,
            env="/nonexistent")))
        self.assertEqual(code, 1)

    def test_the_occupancy_abort_is_a_clean_refusal_not_a_traceback(self):
        # The one refusal only discoverable after the writes begin. It has to
        # read like every other one, and it must not claim nothing was written:
        # the Qdrant half is already rewritten by that point.
        import argparse
        # The exception has to be raised as the *same class object* the handler
        # catches: _module_namespace execs the class afresh each call, so
        # raising one built from a different namespace escapes the handler and the
        # test reports a traceback instead of a clean refusal.
        ns = _module_namespace("read_points")
        abort = ns["DestinationOccupied"]

        def _boom(*_a, **_k):
            raise abort("the destination gained records; the Qdrant half has "
                        "already been rewritten")
        ns.update({
            "read_graph": self._read_graph(),
            "_load_env": lambda *a, **k: None,
            "_report": lambda *a, **k: None,
            "_count_keys": lambda *a, **k: self.KEYS_BEFORE,
            "perform_move": _boom,
            "verify": _async_value(self._healthy()),
        })
        run = _lift(_MIGRATE, "run", ns)
        code = asyncio.run(run(argparse.Namespace(
            source=SOURCE_USER, target=TARGET_USER, apply=True, reconcile=False,
            env="/nonexistent")))
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", self.output())
        self.assertIn("already been rewritten", self.output(),
                      "the refusal must say the Qdrant half is already done")
        self.assertNotIn("Traceback", self.output())

    def test_the_abort_message_does_not_claim_nothing_was_written(self):
        # A message is text, so a source assertion is the right tool — the same
        # reasoning as the LLM prompt sentences in test_matching_regressions.
        # Nothing else in the suite can see it, and it is load-bearing: the
        # Qdrant half IS rewritten when this fires.
        # The AST, not a slice of the file. Both of the narrower versions of this
        # check failed on text that is not the message: the explanatory comment
        # quotes the phrase to say why it is wrong, and the dry-run banner
        # further down legitimately says "Nothing was written". Only the string
        # literal handed to ValueError is the message.
        message = None
        for node in ast.walk(ast.parse(_read(_MIGRATE))):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                for arg in node.exc.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and "destination gained records" in arg.value:
                        message = arg.value
        self.assertIsNotNone(message, "the occupancy abort must raise a reason")
        self.assertFalse(
            "nothing was written" in message.lower(),
            "the abort message must not claim nothing was written — the Qdrant "
            "half has already been rewritten by this point")
        self.assertIn("already been rewritten", message,
                      "the abort must say the Qdrant half is already done")

    def test_a_failure_from_the_credential_half_is_not_a_clean_refusal(self):
        """The reason ``DestinationOccupied`` is its own type.

        A bare ``except ValueError`` around ``perform_move`` would report this as
        a refusal — *after* Qdrant and Neo4j are fully rewritten, with no
        ``FAILED:`` line and no statement that the vault is mid-move. So it has
        to propagate, and the test says so by asserting the raise.
        """
        import argparse
        ns = _module_namespace(extra={
            "read_graph": self._read_graph(),
            "read_points": self._read_points(None),
            "_load_env": lambda *a, **k: None,
            "_report": lambda *a, **k: None,
            "_count_keys": lambda *a, **k: self.KEYS_BEFORE,
            "perform_move": _boom_with(ValueError("PSK row has an empty "
                                                   "user_id")),
            "verify": _async_value(self._healthy()),
        })
        run = _lift(_MIGRATE, "run", ns)
        with self.assertRaises(ValueError):
            asyncio.run(run(argparse.Namespace(
                source=SOURCE_USER, target=TARGET_USER, apply=True,
                reconcile=False, env="/nonexistent")))
        self.assertNotIn("mid-move", self.output(),
                         "the credential failure must not borrow the "
                         "occupancy message")

    def test_a_same_user_move_is_refused(self):
        self.assertEqual(self._drive(self._healthy(), source=SOURCE_USER,
                                     target=SOURCE_USER), 1)

    def _refuses(self, destination=None, qdrant=None, what=""):
        """Drive a run whose destination is non-empty in exactly one way."""
        if qdrant is not None:
            qdrant.destination_points = 1
        code = self._drive(self._healthy(), destination=destination,
                           qdrant=qdrant)
        self.assertEqual(code, 1, f"a destination {what} was not refused")
        self.assertIn("the destination is not empty", self.output())

    def test_a_contexts_only_source_is_not_moved_as_empty(self):
        # The "nothing to move" check has to count scope nodes. Without this term
        # a source whose only content is Context nodes is reported as empty and
        # refused, while a source with the same number of clients is not.
        import argparse
        ctx = context_id_for(SOURCE_USER,
                              client_id_for(SOURCE_USER, "Acme Holdings"),
                              "Phase One")
        async def _read(_driver, user_id):
            if user_id == SOURCE_USER:
                return {"facts": 0, "diary": 0, "clients": [],
                        "contexts": [{"id": ctx, "name": "Phase One",
                                      "clientId": client_id_for(
                                          SOURCE_USER, "Acme Holdings")}]}
            return dict(self.EMPTY_DESTINATION)
        ns = _module_namespace(extra={
            "read_graph": _read,
            "read_points": self._read_points(None),
            "_load_env": lambda *a, **k: None,
            "_report": lambda *a, **k: None,
            "_count_keys": lambda *a, **k: self.KEYS_BEFORE,
            "perform_move": _async_value({"scope_payloads": 0, "qdrant_points": 0,
                                          "facts": 0, "diary": 0, "clients": 0,
                                          "contexts": 1,
                                          "psks": self.KEYS_BEFORE}),
            "verify": _async_value(self._healthy(new_facts=0, new_diary=0)),
        })
        code = asyncio.run(_lift(_MIGRATE, "run", ns)(
            argparse.Namespace(source=SOURCE_USER, target=TARGET_USER,
                               apply=True, reconcile=False,
                               env="/nonexistent")))
        self.assertEqual(code, 0)
        self.assertIn("Done.", self.output())

    def test_a_records_only_source_is_not_refused_as_a_no_op(self):
        # Records with no Client/Context nodes at all — clients are created on
        # demand, so this is an ordinary vault — and the move does real work. The
        # "every id would come out unchanged" guard used to refuse it, because
        # there are no ids to change.
        # clients=[] is the whole point: with a client present the ids change and
        # any no-op guard is satisfied, so the test would pass on the defect.
        self.assertEqual(self._drive(self._healthy(new_facts=3), clients=[]), 0)
        self.assertIn("Done.", self.output())

    def test_a_destination_with_facts_is_refused(self):
        self._refuses(destination={"facts": 1, "diary": 0, "clients": [],
                                   "contexts": []}, what="with facts")

    def test_a_destination_with_diary_is_refused(self):
        self._refuses(destination={"facts": 0, "diary": 2, "clients": [],
                                   "contexts": []}, what="with diary entries")

    def test_a_destination_with_clients_is_refused(self):
        self._refuses(destination={"facts": 0, "diary": 0,
                                   "clients": [{"id": "c", "name": "n"}],
                                   "contexts": []}, what="with clients")

    def test_a_destination_with_only_contexts_is_refused(self):
        # The term the in-transaction guard also has, and the preflight used not
        # to: a contexts-only destination passed the preflight, had its whole
        # Qdrant half rewritten, and then aborted with a message saying it had
        # gained records while being prepared. It had them all along.
        self._refuses(destination={"facts": 0, "diary": 0, "clients": [],
                                   "contexts": [{"id": "x", "name": "n"}]},
                      what="with contexts only")

    def test_a_destination_with_only_vector_points_is_refused(self):
        # A destination whose Neo4j was emptied while its vector points survived
        # is what a stopped sync_orphans leaves behind. Letting it through made
        # the completeness comparison report a healthy move as dirty and tell the
        # operator to stop using a vault that was fine.
        self._refuses(qdrant=FakeQdrant({"ea_memories": {}, "ea_diary": {}}),
                      what="with surviving Qdrant points")

    def test_an_empty_destination_is_still_accepted(self):
        # So the five refusals above are about the condition and not the harness.
        self.assertEqual(self._drive(self._healthy()), 0)

    def test_a_duplicate_source_name_is_refused(self):
        # Two Client rows of one name derive one id, so the move would leave two
        # nodes sharing it — and the destination check cannot see it, because it
        # only inspects the target.
        duplicate = {"id": "a-second-row-same-name", "name": "Acme Holdings"}
        self.assertEqual(self._drive(self._healthy(), clients=[
            {"id": client_id_for(SOURCE_USER, "Acme Holdings"),
             "name": "Acme Holdings"}, duplicate]), 1)

    def test_an_empty_source_is_refused(self):
        # read_graph reports an empty vault, so there is nothing to move.
        import argparse
        # The empty-vault case: read_points and read_graph both zero, so the
        # "nothing to move" refusal fires. _count_keys is stubbed because the
        # real one touches SQLite, and on this path it is never reached.
        run = _lift(_MIGRATE, "run", _module_namespace(extra={
            "_load_env": lambda *a, **k: None,
            "_report": lambda *a, **k: None,
            "_count_keys": lambda *a, **k: 0,
            "read_graph": _async_value({"facts": 0, "diary": 0, "clients": [],
                                        "contexts": []}),
            "read_points": _async_value(0),
        }))
        code = asyncio.run(run(argparse.Namespace(
            source=SOURCE_USER, target=TARGET_USER, apply=False, reconcile=False,
            env="/nonexistent")))
        self.assertEqual(code, 1)


def _boom_with(exc):
    """A callable that raises ``exc``, for the failure-path tests."""
    def _boom(*_a, **_k):
        raise exc
    return _boom


def _async_value(value):
    async def _get(*_a, **_k):
        return value
    return _get


class MigrationScriptTests(unittest.TestCase):
    """Properties of the script that are decisions rather than behaviour.

    Everything here is about what the tool must *not* do, which is why it is
    asserted rather than assumed: a migration that quietly also rewrites the
    credentials table, or that is one flag away from writing without a dry run,
    is a different tool from the one that was reviewed.
    """

    def setUp(self):
        self.source = _read("migrate_vault_user.py")

    def test_dry_run_is_the_default(self):
        self.assertIn('parser.add_argument("--apply", action="store_true"', self.source)
        self.assertIn("if not args.apply:", self.source)

    def test_both_users_are_required_arguments(self):
        self.assertIn('parser.add_argument("--from", dest="source", required=True',
                      self.source)
        self.assertIn('parser.add_argument("--to", dest="target", required=True',
                      self.source)

    def test_a_same_user_move_is_refused(self):
        self.assertIn("source and destination are the same user", self.source)

    def test_a_non_empty_destination_is_refused(self):
        self.assertIn("the destination is not empty", self.source)

    def test_an_occupied_name_is_refused(self):
        self.assertIn("remap.destination_occupied", self.source)

    def test_an_empty_source_is_refused(self):
        self.assertIn("has nothing to move", self.source)

    def test_keys_are_transferred_through_the_sessions_module(self):
        # The SQL lives next to the schema that declares the column.
        self.assertIn("sessions.transfer_psks(", self.source)

    def test_sessions_are_revoked_rather_than_moved(self):
        self.assertIn("sessions.delete_sessions_for_user(", self.source)

    def _called_names(self):
        """Every function name the script actually calls.

        An AST walk, not a substring search, and the distinction is the whole
        point of these tests: the module docstring names htpasswd, the
        credentials table and the uuid5 formula *because it documents what the
        tool does not touch*. A substring guard fails on that explanation, so it
        is either deleted (losing the documentation) or watered down until it
        asserts nothing.
        """
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
            elif isinstance(node, ast.ImportFrom):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        return names

    def test_the_account_itself_is_never_touched(self):
        # Deliberately checked against calls and imports rather than text: the
        # docstring names every one of these to say it leaves them alone.
        called = self._called_names()
        for forbidden in ("delete_credentials", "create_credentials",
                          "htpasswd", "google_identities", "revoke_psk",
                          "user_id_taken", "verify_account"):
            self.assertFalse(
                forbidden in called,
                f"the migration must not reach {forbidden}, but it does")

    def test_run_delegates_the_writes_to_perform_move(self):
        # So the ordering lives in one testable function instead of inline in
        # run(), where a test could only read it. PerformMoveOrderTests drives it.
        self.assertTrue("await perform_move(qdrant, neo4j_driver" in self.source,
                        "run() must delegate the writes")
        # Counted from the AST, so the `def` line is not counted as a call. A
        # regex over the text would have to exclude the definition by hand, and
        # getting that wrong fails open.
        called = {}
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called[node.func.id] = called.get(node.func.id, 0) + 1
        for stage in ("retarget_qdrant_scope", "move_qdrant_user",
                      "move_graph", "move_credentials"):
            self.assertEqual(
                called.get(stage), 1,
                f"{stage} must be called from perform_move, and nowhere else")

    def test_the_id_derivation_is_imported_not_reimplemented(self):
        # The docstring quotes the formula it delegates to, so this is an AST
        # check: no Call anywhere resolves to uuid5.
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) \
                    else getattr(node.func, "id", "")
                self.assertNotEqual(
                    name, "uuid5",
                    "the derivation must come from scoped_ids, not be inlined")

    def test_diary_entry_ids_are_not_rewritten(self):
        # Rewriting them would orphan every chunk family, because chunk 0 of a
        # Qdrant family *is* the record id.
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertFalse(
                    "DiaryEntry {id:" in node.value,
                    "the migration must not address a diary entry by id")

    def test_it_reports_a_verification_pass_at_the_end(self):
        self.assertTrue("await verify(qdrant, neo4j_driver" in self.source,
                        "the run must end with a verification pass")

    def test_the_verification_pass_looks_for_dangling_scope_ids(self):
        # The failure this whole exercise is about is a payload pointing at an id
        # no node has, which no count anywhere would reveal.
        self.assertTrue("dangling_scope_payloads" in self.source,
                        "verification must count dangling scope payloads")


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