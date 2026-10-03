"""
scoped_ids.py — how a Client/Context id is derived, and how to re-derive it.

Standard library only (``uuid`` and nothing else), deliberately, so this module
can be imported and *called* on a machine with no Neo4j, no Qdrant and no web
framework. That is the same constraint ``matching_utils.py`` works under and for
the same reason: the interesting half of a scope migration is a pure function,
and a pure function that cannot be imported here cannot be tested here.

Why the derivation lives here rather than in ``client_manager.py``
----------------------------------------------------------------
``client_manager.db_create_client`` computed its id as::

    uuid5(NAMESPACE_DNS, f"client_{user_id}_{name.strip().lower()}")

A user rename or a vault migration therefore has to reproduce that formula
*exactly* — off by one normalisation step and the migrated nodes carry ids that
nothing will ever generate again, while ``db_create_client`` goes on minting new
ones from the formula. The result is two Client nodes with the same name in one
vault, which is indistinguishable from the scope filter being broken.

So the formula lives here, ``client_manager`` calls it, and the migration calls
the same function. There is no second copy to drift.

What is deliberately NOT here
----------------------------
``DiaryEntry.id`` is also user-derived (``diary_{user_id}_{timestamp}``) and is
deliberately left alone when a vault moves. It cannot realistically collide — it
would need the same timestamp to the resolution of the stored value — whereas
re-deriving it would change the record's primary key, and chunk 0 of a Qdrant
family *is* that key. Rewriting it means re-embedding every chunk of every entry
to keep the vector store attached to the graph, which is a large, lossy-risk
operation bought for nothing. See ``migrate_vault_user.py``.
"""

import uuid

__all__ = [
    "client_id_for",
    "context_id_for",
    "plan_id_remap",
    "IdRemap",
]


def client_id_for(user_id: str, name: str) -> str:
    """The stable id of a Client node owned by ``user_id``.

    Note that the username is part of the input: the same client name under two
    vaults yields two ids. That is correct — they are two different nodes in two
    different vaults — and it is exactly why a vault migration must re-derive.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS,
                          f"client_{user_id}_{name.strip().lower()}"))


def context_id_for(user_id: str, client_id: str, name: str) -> str:
    """The stable id of a Context node under ``client_id``.

    Takes the *client id*, not the client name, so it must be called with the
    parent's **new** id when the parent is itself being re-identified.
    """
    return str(uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"context_{user_id}_{client_id}_{name.strip().lower()}"))


class IdRemap:
    """The old-id -> new-id tables for one vault move, plus what it would break.

    Built by :func:`plan_id_remap` from plain dicts so it can be constructed in a
    test with no database anywhere in sight.
    """

    def __init__(self, clients: dict, contexts: dict, context_parents: dict,
                 unchanged_clients: list, unchanged_contexts: list,
                 destination_occupied: bool, duplicate_names=()):
        # old id -> new id
        self.clients = clients
        self.contexts = contexts
        # old context id -> the id its parent Client will have. Separate from
        # ``contexts`` because the two values are not interchangeable: a Context
        # stores its parent's id in a property, and handing the caller the new
        # *context* id there would point every Context at a sibling that does
        # not exist. Kept as its own map rather than derived at the call site,
        # because the two maps have the same shape and the same key type.
        self.context_parents = context_parents
        # Ids that come out the same, i.e. rows that do not need writing.
        self.unchanged_clients = unchanged_clients
        self.unchanged_contexts = unchanged_contexts
        # True when the destination already owns a node with one of these names,
        # which would produce two same-named nodes in one vault after the move.
        self.destination_occupied = destination_occupied
        # Names that appear twice *in the source*. Separate from the above
        # because it is a different fault with the same consequence: two source
        # rows with one name derive one id, so the move would leave two Client
        # nodes sharing an id. Checking only the destination cannot see it.
        self.duplicate_names = list(duplicate_names)

    def client_rows(self) -> dict:
        """Every source Client id -> its destination id, changed or not.

        ``clients`` alone is not enough to hand to ``move_graph``. A row whose id
        comes out unchanged — a hand-written or legacy id that does not embed the
        username — still needs its ``userId`` rewritten, or it stays behind in
        the source vault while everything around it moves. That case is
        unreachable through the UI, which is exactly why it is worth covering
        rather than assuming.
        """
        rows = dict(self.clients)
        for row_id in self.unchanged_clients:
            rows.setdefault(row_id, row_id)
        return rows

    def context_rows(self) -> dict:
        """Every source Context id -> its destination id, changed or not."""
        rows = dict(self.contexts)
        for row_id in self.unchanged_contexts:
            rows.setdefault(row_id, row_id)
        return rows

    def context_parent_rows(self) -> dict:
        """Every source Context id -> the id its parent Client will have.

        Complete by construction: ``context_parents`` is filled for every row,
        changed or not, precisely so this needs no default. The two earlier
        shapes — defaulting to the parent, and defaulting to the context's own id
        — each wrote a wrong ``Context.clientId`` for some input, and the second
        produced a node pointing at itself.
        """
        return dict(self.context_parents)

    @property
    def client_count(self) -> int:
        return len(self.clients)

    @property
    def context_count(self) -> int:
        return len(self.contexts)

    @property
    def changes_anything(self) -> bool:
        """False when every id would come out unchanged.

        That is the signature of a same-user move (``--from X --to X``), which
        would otherwise report a tidy success having done nothing at all.
        """
        return bool(self.clients or self.contexts)


def plan_id_remap(source_user: str, target_user: str, clients, contexts,
                  target_clients=(), target_contexts=()) -> IdRemap:
    """Work out every id change a move from ``source_user`` to ``target_user``.

    ``clients`` / ``contexts`` are iterables of ``{"id", "name"}`` dicts (and for
    contexts, ``clientId``) belonging to the source vault. ``target_clients`` /
    ``target_contexts`` are the destination's, used only to detect a collision —
    the move is refused by the caller rather than silently producing a
    duplicate.

    Contexts are re-derived against the **new** parent id, so a context whose
    parent is itself being renamed comes out right in one pass.
    """
    target_client_names = {(c.get("name") or "").strip().lower()
                           for c in target_clients}
    target_context_names = {(c.get("name") or "").strip().lower()
                            for c in target_contexts}

    client_map = {}
    unchanged_clients = []
    occupied = False
    duplicates = []
    seen_client_names = set()
    for row in clients:
        name = (row.get("name") or "").strip()
        new_id = client_id_for(target_user, name)
        if name.strip().lower() in target_client_names:
            occupied = True
        key = name.strip().lower()
        if key in seen_client_names:
            duplicates.append(name)
        seen_client_names.add(key)
        if new_id == row.get("id"):
            unchanged_clients.append(row.get("id"))
            continue
        client_map[row.get("id")] = new_id

    context_map = {}
    context_parents = {}
    unchanged_contexts = []
    seen_contexts = set()
    for row in contexts:
        name = (row.get("name") or "").strip()
        old_parent = row.get("clientId")
        new_parent = client_map.get(old_parent, old_parent)
        new_id = context_id_for(target_user, new_parent, name)
        if name.strip().lower() in target_context_names:
            occupied = True
        # Keyed by parent as well as name, because two contexts of one name under
        # *different* clients are two different nodes with two different ids —
        # only a same-name, same-parent pair actually collides.
        context_key = (old_parent, name.strip().lower())
        if context_key in seen_contexts:
            duplicates.append(f"{name} (under one client)")
        seen_contexts.add(context_key)
        # Recorded unconditionally, and that is the point: a Context stores its
        # parent's id in a *property*, and a row whose own id comes out unchanged
        # can still have a stale parent pointer. Populating this only inside the
        # changed branch left `context_parent_rows()` to default a missing entry
        # to the context's own id, so `SET ctx.clientId` wrote the context's id
        # into its own parent field — a Context pointing at itself, which reads as
        # valid and is checked by nothing.
        context_parents[row.get("id")] = new_parent
        if new_id == row.get("id"):
            unchanged_contexts.append(row.get("id"))
            continue
        context_map[row.get("id")] = new_id

    return IdRemap(client_map, context_map, context_parents, unchanged_clients,
                   unchanged_contexts, occupied, duplicates)