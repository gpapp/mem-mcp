"""
migrate_vault_user.py — move everything belonging to one vault user to another.

Usage:
    python migrate_vault_user.py --from OLD_USER --to NEW_USER            # dry run
    python migrate_vault_user.py --from OLD_USER --to NEW_USER --apply

Options:
    --from USER_ID      The vault to move. Required.
    --to USER_ID        The vault to move it into. Required.
    --apply             Actually write. Without it this only reports.
    --reconcile         Also run sync_qdrant_scope() afterwards (default on;
                        --no-reconcile turns it off).
    --env FILE          Load environment from .env file (default: .env)

Dry run is the default, and that is the opposite of the other scripts here on
purpose. ``reindex_chunks.py`` rebuilds vectors it can rebuild; this moves the
only copy of somebody's data between two owners, and there is no undo.

What a "user" spans
-------------------
The username is not a profile row, it is the partition key of four stores, and a
move that touches three of them leaves a vault that looks empty in one place and
full in another:

===========================  ===============================================
Neo4j                       :Fact, :DiaryEntry, :Client, :Context (``userId``),
                             plus a ``:User {id}`` hub node
Qdrant                      ``userId`` in the ``ea_memories`` and ``ea_diary``
                             payloads
SQLite (sessions.db)        ``psks.user_id`` — the access keys
htpasswd / credentials      deliberately NOT touched
===========================  ===============================================

``:Category`` is global — it carries no ``userId`` — and is never rewritten.

Why this is not a ``SET n.userId`` loop
--------------------------------------
``Client.id`` and ``Context.id`` are derived from the username::

    client_id  = uuid5(NAMESPACE_DNS, f"client_{user_id}_{name.strip().lower()}")
    context_id = uuid5(NAMESPACE_DNS,
                       f"context_{user_id}_{client_id}_{name.strip().lower()}")

Rewriting ``userId`` alone moves the data and leaves those ids stale, and the
damage is not visible in the vault you just moved: the next ``db_create_client``
with an existing name derives a *different* uuid, so the vault ends up with two
Client nodes of the same name. That reads as the scope filter being broken.
``scoped_ids.py`` owns the derivation so this script and ``db_create_client``
cannot disagree about it.

What is deliberately left behind
--------------------------------
* ``DiaryEntry.id`` is also user-derived but is left alone. Chunk 0 of a Qdrant
  family *is* the record id, so re-deriving it means re-embedding every chunk of
  every entry — a large operation with real data-loss risk, bought to avoid a
  collision that needs the same timestamp to the stored resolution.
* Live sessions are **revoked**, not re-pointed. A session that was mid-write
  when the vault changed hands would otherwise land its write in the wrong place.
* The old account's htpasswd entry and credentials row stay, so it can still sign
  in and will find an empty vault.

Order of operations
-------------------
Qdrant is rewritten **before** Neo4j. That is not cosmetic: the reconciliation
pass reads Qdrant filtered by ``userId``, so a Neo4j-first migration would find
nothing to repair. Scope ids are retargeted first, then ``userId``, both while
the points are still reachable by the old value.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from qdrant_client.models import (
    Filter,
    FieldCondition,
    MatchValue,
)

# The payload keys ``retarget_qdrant_scope`` ever writes, in the order they are
# packed into a group key. A tuple rather than a dict so points sharing a patch
# hash together, and a list so an absent value is distinct from a present one.
PATCH_KEYS = ("clientId", "contextId")

from common import COLLECTION_NAME, DIARY_COLLECTION

class DestinationOccupied(ValueError):
    """The destination is not empty, discovered once the writes have begun.

    Its own type rather than a bare ``ValueError`` because the handler in ``run``
    turns it into a clean refusal, and a bare ``except ValueError`` around the
    whole of ``perform_move`` would also swallow a failure from the credential
    half — *after* Qdrant and Neo4j are fully rewritten, and with no ``FAILED:``
    line. That is the exact failure the abort message exists to prevent,
    reachable by a different route.
    """


HERE = Path(__file__).resolve().parent
DEFAULT_ENV = HERE.parent / ".env"

# Scroll page size, and the point at which the accumulated retarget patches are
# flushed. It is a *threshold*, not a cap: the queue survives a page boundary and
# only points that actually need a patch accumulate, so one flush can carry up to
# 2 * QDRANT_BATCH - 1 ids across its groups. Harmless, but do not rely on it as
# a request bound.
QDRANT_BATCH = 200


def _load_env(env_file: str) -> None:
    p = Path(env_file)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Reading the current state
# ---------------------------------------------------------------------------

async def read_graph(neo4j_driver, user_id: str) -> dict:
    """Counts and the Client/Context rows of one vault.

    The two count queries are written out inline rather than held in a
    module-level tuple: a lifted copy of this function is exec'd by the test
    suite without the module body, so a name it does not own is a NameError in
    the harness rather than a finding about the code.
    """
    out = {"facts": 0, "diary": 0, "clients": [], "contexts": []}
    with neo4j_driver.session() as s:
        for key, query in (
            ("facts", "MATCH (n:Fact) WHERE n.userId = $u RETURN count(n) AS n"),
            ("diary", "MATCH (n:DiaryEntry) WHERE n.userId = $u RETURN count(n) AS n"),
        ):
            record = s.run(query, u=user_id).single()
            out[key] = (record["n"] if record else 0) or 0
        out["clients"] = [dict(r) for r in s.run(
            "MATCH (c:Client) WHERE c.userId = $u RETURN c.id AS id, c.name AS name",
            u=user_id)]
        out["contexts"] = [dict(r) for r in s.run(
            "MATCH (ctx:Context) WHERE ctx.userId = $u "
            "RETURN ctx.id AS id, ctx.name AS name, ctx.clientId AS clientId",
            u=user_id)]
    return out


async def read_points(qdrant, user_id: str) -> int:
    """How many Qdrant points belong to one user, across both collections."""
    total = 0
    for collection in (COLLECTION_NAME, DIARY_COLLECTION):
        offset = None
        while True:
            points, offset = await qdrant.scroll(
                collection_name=collection,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
                scroll_filter=Filter(must=[
                    FieldCondition(key="userId", match=MatchValue(value=user_id))
                ]),
            )
            total += len(points)
            if offset is None:
                break
    return total


async def read_known_scope_ids(neo4j_driver, user_id: str):
    """``{"client": set(...), "context": set(...)}`` — kept apart on purpose.

    Merged into one set, a Context whose ``clientId`` wrongly holds a *context*
    id tests as present, so the one corruption this check exists to catch reports
    as clean. The two namespaces have to stay distinguishable to be worth
    looking at.
    """
    out = {"client": set(), "context": set()}
    with neo4j_driver.session() as s:
        for key, query in (
            ("client", "MATCH (c:Client) WHERE c.userId = $u RETURN c.id AS id"),
            ("context", "MATCH (ctx:Context) WHERE ctx.userId = $u "
                        "RETURN ctx.id AS id"),
        ):
            for row in s.run(query, u=user_id):
                if row["id"]:
                    out[key].add(row["id"])
    return out


async def read_old_user_node_present(neo4j_driver, user_id: str) -> bool:
    """Whether the source's ``:User`` hub node is still there.

    It is left behind on purpose — the account itself is not this tool's
    business — but it is then an ownerless node, so the report says so rather
    than leaving the operator to wonder.
    """
    with neo4j_driver.session() as s:
        row = s.run("MATCH (u:User {id: $u}) RETURN count(u) AS n",
                    u=user_id).single()
    return bool(row and row["n"])


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

async def _flush(qdrant, collection: str, groups: dict) -> int:
    """Write the accumulated patches, one request per distinct patch.

    ``set_payload`` takes either a single payload applied to a list of point ids,
    or a list of per-point payloads. The first form needs no pydantic model at
    all, and the number of *distinct* patches is the number of distinct
    (client, context) pairs actually present — a handful in any real vault, and
    never more than the vault has scope assignments.
    """
    written = 0
    for key, ids in groups.items():
        if not ids:
            continue
        # Only the keys that actually change. ``set_payload`` writes whatever it
        # is handed, so carrying the absent half of the group key through as
        # ``None`` would write a null over a real clientId — the opposite of
        # leaving it alone.
        payload = {name: value for name, value in zip(PATCH_KEYS, key)
                   if value is not None}
        await qdrant.set_payload(collection_name=collection, payload=payload,
                                 points=ids)
        written += len(ids)
    groups.clear()
    return written


async def retarget_qdrant_scope(qdrant, user_id: str, client_map: dict,
                                context_map: dict) -> int:
    """Point every scope id at its new value, for one user's points.

    ``clientName`` / ``contextName`` need no rewrite — a name is not derived from
    the username — so this touches ids only. Runs while the points are still
    reachable by the old ``userId``.

    Grouped by patch rather than one ``PointStruct`` per point, and that is not a
    style choice. ``PointStruct`` is a pydantic model whose ``vector`` field is
    **required**; ``set_payload`` ignores the value entirely, so an earlier
    version that omitted it passed the entire test suite and then raised a
    ValidationError inside the container on the first real point. The id-list
    form is what ``client_manager`` already uses in production, so it needs no
    model to be constructed correctly and cannot be version-sensitive.
    """
    patched = 0
    for collection in (COLLECTION_NAME, DIARY_COLLECTION):
        groups: dict = {}
        queued = 0
        offset = None
        while True:
            points, offset = await qdrant.scroll(
                collection_name=collection,
                limit=QDRANT_BATCH,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                scroll_filter=Filter(must=[
                    FieldCondition(key="userId", match=MatchValue(value=user_id))
                ]),
            )
            for point in points:
                payload = point.payload or {}
                patch = []
                if payload.get("clientId") in client_map:
                    patch.append(client_map[payload["clientId"]])
                else:
                    patch.append(None)
                if payload.get("contextId") in context_map:
                    patch.append(context_map[payload["contextId"]])
                else:
                    patch.append(None)
                if not any(patch):
                    continue
                key = tuple(patch)
                groups.setdefault(key, []).append(point.id)
                queued += 1
            # A flush *threshold*, not a cap: `queued` counts every point that
            # needs a patch while a group persists across pages, so one request
            # can carry up to 2 * QDRANT_BATCH - 1 ids in total across its
            # groups. Harmless, but do not rely on it as a request bound.
            if queued >= QDRANT_BATCH:
                patched += await _flush(qdrant, collection, groups)
                queued = 0
            if offset is None:
                break
        patched += await _flush(qdrant, collection, groups)
    return patched


async def move_qdrant_user(qdrant, old_user: str, new_user: str) -> int:
    """Re-own one user's points. ``set_payload`` merges, so nothing else moves."""
    moved = 0
    for collection in (COLLECTION_NAME, DIARY_COLLECTION):
        offset = None
        while True:
            points, offset = await qdrant.scroll(
                collection_name=collection,
                limit=QDRANT_BATCH,
                offset=offset,
                with_payload=False,
                with_vectors=False,
                scroll_filter=Filter(must=[
                    FieldCondition(key="userId", match=MatchValue(value=old_user))
                ]),
            )
            if points:
                await qdrant.set_payload(
                    collection_name=collection,
                    payload={"userId": new_user},
                    points=[p.id for p in points])
                moved += len(points)
            if offset is None:
                break
    return moved


def move_graph(neo4j_driver, old_user: str, new_user: str, client_map: dict,
               context_map: dict, context_parents: dict) -> dict:
    """Move the graph half, in one transaction.

    Order matters inside it: a Context stores its parent's id in a property, so
    the Client rows have to be re-identified before the Context rows are read
    for rewriting. All of it is UNSET-free and id-preserving — a Fact's id is a
    uuid4 and does not depend on the user at all.
    """

    def _work(tx):
        counts = {}
        # Re-checked here, not only in run(): between that check and this write
        # somebody can have saved a record into the destination, and a move into
        # a non-empty vault interleaves two sets of the same labels. Inside the
        # transaction it is a read that cannot be raced.
        occupied = tx.run(
            "MATCH (n) WHERE n.userId = $new "
            "AND (n:Fact OR n:DiaryEntry OR n:Client OR n:Context) "
            "RETURN count(n) AS n",
            new=new_user).single()["n"]
        if occupied:
            # The message is this precise because it is reached *after* the Qdrant
            # half has already been rewritten. Saying "nothing was written" here
            # would be false, and an operator who believes it stops looking at a
            # vault that is genuinely half-moved.
            raise DestinationOccupied(
                "the destination gained records while the migration was being "
                "prepared. The Qdrant half has already been rewritten and the "
                "graph half has not, so this vault is mid-move: resolve the "
                "destination and re-run, which is re-runnable and converges")

        client_rows = [{"oldId": o, "newId": n} for o, n in client_map.items()]
        ctx_rows = [{"oldId": o, "newId": n, "newParent": context_parents.get(o)}
                    for o, n in context_map.items()]
        # RETURN count(...) rather than reporting len(rows): a row whose MATCH
        # matched nothing is a silent no-op, and reporting the plan instead of
        # the result would hide exactly that.
        counts["clients"] = tx.run(
            "UNWIND $rows AS row "
            "MATCH (c:Client {id: row.oldId, userId: $old}) "
            "SET c.id = row.newId, c.userId = $new RETURN count(c) AS n",
            rows=client_rows, old=old_user, new=new_user).single()["n"]
        counts["contexts"] = tx.run(
            "UNWIND $rows AS row "
            "MATCH (ctx:Context {id: row.oldId, userId: $old}) "
            "SET ctx.id = row.newId, ctx.userId = $new, ctx.clientId = row.newParent "
            "RETURN count(ctx) AS n",
            rows=ctx_rows, old=old_user, new=new_user).single()["n"]
        for label, key in (("Fact", "facts"), ("DiaryEntry", "diary")):
            result = tx.run(
                f"MATCH (n:{label}) WHERE n.userId = $old SET n.userId = $new "
                "RETURN count(n) AS n",
                old=old_user, new=new_user)
            counts[key] = result.single()["n"]
        # The hub node for the destination is created by db_create_client on
        # demand; MERGE it here so a vault that has Clients but has never had one
        # created through the UI is not left without an owner node.
        tx.run("MERGE (u:User {id: $new})", new=new_user).consume()
        return counts

    with neo4j_driver.session() as s:
        return s.execute_write(_work)


def move_credentials(old_user: str, new_user: str) -> dict:
    """Hand the access keys over, then end the old account's live sessions.

    Sessions are revoked rather than re-pointed: a session open across the move
    would keep writing under whichever vault it started with, and the two stores
    would only disagree until it happened to.
    """
    import sessions

    sessions.init_db()
    keys = sessions.transfer_psks(old_user, new_user)
    ended = sessions.delete_sessions_for_user(old_user)
    return {"psks": keys, "sessions_revoked": ended}


async def perform_move(qdrant, neo4j_driver, old_user, new_user, remap) -> dict:
    """Every write, in the only order that works. Returns what each one did.

    This is a function rather than a run of statements in ``run`` so the order is
    something a test can *drive* with fakes. Asserted from source text it only
    proves two ``await``s are on the right lines, and it stays green through the
    refactor that hoists them into a helper and reverses them there.

    Qdrant goes first, and within it the scope retarget goes before the
    ``userId`` move. Both facts come from the same place: every read here is a
    scroll filtered by the *old* ``userId``, so once a point has been re-owned
    it is invisible to the step that still has work to do on it. The wrong order
    is not an error — it is a silent success that patches nothing.
    """
    report = {}
    # client_rows() rather than remap.clients: a row whose id comes out unchanged
    # still needs its userId rewritten, or it stays behind in the source vault.
    report["scope_payloads"] = await retarget_qdrant_scope(
        qdrant, old_user, remap.clients, remap.contexts)
    report["qdrant_points"] = await move_qdrant_user(qdrant, old_user, new_user)
    report.update(move_graph(neo4j_driver, old_user, new_user,
                             remap.client_rows(), remap.context_rows(),
                             remap.context_parent_rows()))
    report.update(move_credentials(old_user, new_user))
    return report


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

async def verify(qdrant, neo4j_driver, old_user: str, new_user: str) -> dict:
    """Read-only health check of the result."""
    report = {}
    old_graph = await read_graph(neo4j_driver, old_user)
    new_graph = await read_graph(neo4j_driver, new_user)
    report["old_left_in_graph"] = (old_graph["facts"] + old_graph["diary"]
                                   + len(old_graph["clients"])
                                   + len(old_graph["contexts"]))
    report["new_facts"] = new_graph["facts"]
    report["new_diary"] = new_graph["diary"]

    known = await read_known_scope_ids(neo4j_driver, new_user)
    report["dangling_client_payloads"] = 0
    report["dangling_context_payloads"] = 0
    dangling = 0
    for collection in (COLLECTION_NAME, DIARY_COLLECTION):
        offset = None
        while True:
            points, offset = await qdrant.scroll(
                collection_name=collection,
                limit=QDRANT_BATCH,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                scroll_filter=Filter(must=[
                    FieldCondition(key="userId", match=MatchValue(value=new_user))
                ]),
            )
            for point in points:
                payload = point.payload or {}
                if payload.get("clientId") and payload["clientId"] not in known["client"]:
                    dangling += 1
                    report["dangling_client_payloads"] += 1
                if payload.get("contextId") and payload["contextId"] not in known["context"]:
                    dangling += 1
                    report["dangling_context_payloads"] += 1
            if offset is None:
                break
    report["dangling_scope_payloads"] = dangling
    report["source_user_node_left"] = await read_old_user_node_present(
        neo4j_driver, old_user)
    report["new_points"] = await read_points(qdrant, new_user)
    report["old_points"] = await read_points(qdrant, old_user)
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _count_keys(user_id: str) -> int | None:
    """How many access keys the user holds, or None if SQLite cannot be read.

    ``run`` compares this against what ``transfer_psks`` reported, because the
    SQLite store is the one the verification pass cannot see: a migration run
    with a different ``MEM_SESSION_DIR`` from the app opens a *different*
    database, moves zero real keys, and leaves Neo4j and Qdrant both reporting a
    clean move.

    Called only on the ``--apply`` path, deliberately. ``sessions.db_path()``
    *creates* its directory, so counting keys during a dry run writes a database
    and then prints "Nothing was written" — which would be false. Returns None
    rather than raising so an unreadable store is reported as a skipped check
    instead of aborting a migration that has not started.
    """
    try:
        import sessions

        sessions.init_db()
        return len(sessions.list_psks(user_id))
    except Exception as exc:  # noqa: BLE001 - a skipped check, not a failed move
        print(f"WARNING: could not read the access-key store ({exc}); the "
              "check that they all moved will be skipped.", file=sys.stderr)
        return None


def _report(title: str, rows: dict) -> None:
    print(f"\n{title}")
    for key, value in rows.items():
        print(f"  {key:<26} {value}")


async def run(args: argparse.Namespace) -> int:
    _load_env(args.env)

    from common import get_qdrant, get_neo4j
    from scoped_ids import plan_id_remap

    qdrant = await get_qdrant()
    neo4j_driver = get_neo4j()
    if not qdrant or not neo4j_driver:
        print("ERROR: could not connect to Qdrant or Neo4j.", file=sys.stderr)
        return 1

    old_user = args.source.strip()
    new_user = args.target.strip()
    if not old_user or not new_user:
        print("ERROR: --from and --to are both required.", file=sys.stderr)
        return 1
    if old_user == new_user:
        print("ERROR: source and destination are the same user.", file=sys.stderr)
        return 1

    old_graph = await read_graph(neo4j_driver, old_user)
    new_graph = await read_graph(neo4j_driver, new_user)
    old_points = await read_points(qdrant, old_user)
    # Qdrant is consulted too, not only the graph. A destination whose Neo4j was
    # emptied while its vector points survived is exactly what a stopped
    # sync_orphans leaves behind, and comparing the source's point count against
    # a destination total that already included those would report a healthy move
    # as dirty and tell the operator to stop using a vault that is fine.
    destination_points = await read_points(qdrant, new_user)
    # Recorded before the writes so the destination can be checked for
    # completeness afterwards. "Nothing was left behind" and "everything
    # arrived" are different claims, and only the first was being made.
    expected = {"facts": old_graph["facts"], "diary": old_graph["diary"],
                "qdrant_points": old_points}

    _report(f"source: {old_user}", {
        "facts": old_graph["facts"],
        "diary entries": old_graph["diary"],
        "clients": len(old_graph["clients"]),
        "contexts": len(old_graph["contexts"]),
        "qdrant points": old_points,
    })
    _report(f"destination: {new_user}", {
        "facts": new_graph["facts"],
        "diary entries": new_graph["diary"],
        "clients": len(new_graph["clients"]),
        "contexts": len(new_graph["contexts"]),
    })

    if not (old_graph["facts"] or old_graph["diary"] or old_graph["clients"]
            or old_graph["contexts"] or old_points):
        print(f"\nERROR: {old_user} has nothing to move.", file=sys.stderr)
        return 1

    remap = plan_id_remap(old_user, new_user, old_graph["clients"],
                          old_graph["contexts"],
                          new_graph["clients"], new_graph["contexts"])

    # Context is in this list for the same reason it is in the in-transaction
    # check: a destination holding only contexts passed the preflight before,
    # had its whole Qdrant half rewritten, and *then* aborted with a message
    # saying it had gained records while being prepared. It had them all along.
    if (new_graph["facts"] or new_graph["diary"] or new_graph["clients"]
            or new_graph["contexts"] or destination_points):
        print("\nERROR: the destination is not empty. Merging two populated "
              "vaults is a different operation (it has to reconcile ids, "
              "duplicates and scope across both) and is not attempted here.",
              file=sys.stderr)
        return 1
    if remap.destination_occupied:
        print("\nERROR: the destination already has a client or context with one "
              "of these names. Moving would leave two of the same name in one "
              "vault.", file=sys.stderr)
        return 1
    if remap.duplicate_names:
        print("\nERROR: the source already has more than one client with the "
              "same name (" + ", ".join(sorted(set(remap.duplicate_names)))
              + "). They derive one id, so the move would leave two Client "
              "nodes sharing it. Resolve that in the source vault first.",
              file=sys.stderr)
        return 1
    # No `changes_anything` refusal here. Its only reachable False case was a
    # same-user move, which is refused further up, so all it actually caught was
    # a vault holding records but no Client/Context nodes — which is a perfectly
    # ordinary vault (clients are created on demand) and a move that does real
    # work: re-owning every fact, diary entry and Qdrant point.

    _report("id changes", {
        "clients re-identified": remap.client_count,
        "clients unchanged": len(remap.unchanged_clients),
        "contexts re-identified": remap.context_count,
        "contexts unchanged": len(remap.unchanged_contexts),
    })

    if not args.apply:
        print("\nDry run. Nothing was written. Re-run with --apply to move it.")
        return 0

    # Only now, on the writing path: sessions.db_path() creates its directory.
    expected_keys = _count_keys(old_user)

    try:
        report = await perform_move(qdrant, neo4j_driver, old_user, new_user,
                                    remap)
    except DestinationOccupied as exc:
        # The one refusal that can only be discovered once the writes have begun.
        # Handled here rather than left to propagate, so it reads like every other
        # refusal — an ERROR line and a non-zero exit — instead of a traceback
        # that looks like a crash and says nothing about the vault's state.
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    _report("moved", report)

    if args.reconcile:
        from migrate_client_context import sync_qdrant_scope
        result = sync_qdrant_scope()
        if asyncio.iscoroutine(result):
            await result
        print("\nreconciled scope payloads against the graph")

    check = await verify(qdrant, neo4j_driver, old_user, new_user)
    _report("verification", check)

    # A migration that reports a dirty result and exits 0 is the failure mode
    # worth precluding: the operator reads "Done" and stops looking.
    problems = []
    if check["old_left_in_graph"]:
        problems.append(f"{check['old_left_in_graph']} records still belong to "
                        "the source user")
    if check["dangling_scope_payloads"]:
        problems.append(f"{check['dangling_scope_payloads']} Qdrant payloads "
                        "point at a scope id that does not exist")
    if check["old_points"]:
        problems.append(f"{check['old_points']} Qdrant points still belong to "
                        "the source user")
    for label, source_key, dest_key in (
            ("facts", "facts", "new_facts"),
            ("diary entries", "diary", "new_diary"),
            ("Qdrant points", "qdrant_points", "new_points")):
        got = check[dest_key]
        if got != expected[source_key]:
            problems.append(f"{label}: expected {expected[source_key]} at the "
                            f"destination, found {got}")
    if expected_keys is not None and report.get("psks") != expected_keys:
        problems.append(f"{expected_keys} access keys were expected to move, "
                        f"{report.get('psks')} did — check that "
                        "MEM_SESSION_DIR is the app's own")
    if problems:
        print("\nFAILED: " + "; ".join(problems)
              + ".\nThe vault is mid-move: do not use either account until this "
                "is resolved, and re-run once the cause is clear.", file=sys.stderr)
        return 1

    if report["scope_payloads"] == 0 and report["clients"]:
        print("\nNote: no Qdrant payload referenced a client or context id, so "
              "none needed retargeting. That is normal for a vault whose records "
              "carry no scope, and for a re-run of a move whose Qdrant half had "
              "already completed.", file=sys.stderr)
    print("\nDone. Sign in as the destination user; the access keys moved with "
          "the vault, so existing MCP clients keep working.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move everything belonging to one vault user to another.")
    parser.add_argument("--from", dest="source", required=True,
                        help="the vault to move")
    parser.add_argument("--to", dest="target", required=True,
                        help="the vault to move it into")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without this it only reports")
    parser.add_argument("--no-reconcile", dest="reconcile", action="store_false",
                        default=True,
                        help="skip the sync_qdrant_scope() pass afterwards")
    parser.add_argument("--env", default=str(DEFAULT_ENV),
                        help="path to the .env file")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())