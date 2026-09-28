"""
backup.py — Savepoints for the memory vault.

A savepoint is a self-contained directory under ``BACKUP_DIR``:

    <BACKUP_DIR>/<id>/
        manifest.json          human-readable inventory + counters
        neo4j.jsonl            one JSON object per line: nodes then relationships
        qdrant-<name>.snapshot  one binary Qdrant collection snapshot per collection

Why the two stores are dumped differently
-----------------------------------------
Qdrant gets its own binary snapshot API, so vectors and payloads round-trip
byte-exactly with no serialization step to get wrong. Neo4j Community has no
equivalent: ``neo4j-admin database backup`` is Enterprise-only and
``dump``/``load`` require stopping the database, so it is unusable as a daily
job from inside the app container. The graph is therefore exported over Bolt and
re-created on restore. That is only lossless if temporal property values are
type-tagged on the way out — a bare ``str()`` fallback writes a perfectly valid
Neo4j *string* where a ``ZonedDateTime`` used to be, and the corruption does not
surface until something queries a date. See ``_encode_value``.

Ordering matters. Qdrant is snapshotted *first*, then Neo4j is exported, so a
fact written in between exists in Neo4j but not in the vector snapshot. The
lifespan reconciliation chain repairs that direction by re-embedding; the
opposite order would strand a Neo4j fact with no vector and nothing to rebuild
it from.
"""

import asyncio
import gzip
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from common import (
    BACKUP_DIR,
    COLLECTION_NAME,
    DIARY_COLLECTION,
    HTTP_TIMEOUT,
    QDRANT_URL,
    claim_maintenance,
    logger,
    publish_db_event,
    release_maintenance,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKUP_ENABLED = os.getenv("MEM_BACKUP_ENABLED", "1") == "1"
BACKUP_KEEP = max(1, int(os.getenv("MEM_BACKUP_KEEP", "14")))
BACKUP_HOUR = min(23, max(0, int(os.getenv("MEM_BACKUP_HOUR", "3"))))
BACKUP_MINUTES = min(59, max(0, int(os.getenv("MEM_BACKUP_MINUTES", "0"))))

MANIFEST_NAME = "manifest.json"
GRAPH_NAME = "neo4j.jsonl"
GRAPH_NAME_GZ = "neo4j.jsonl.gz"
MANIFEST_VERSION = 2

# Rows per Bolt page. The neo4j container caps the heap at 1g, so pages stay
# small enough that a busy vault cannot push the export out of memory.
_EXPORT_PAGE = 500
_RESTORE_PAGE = 500
_DELETE_PAGE = 2000

_CYPHER_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Neo4j temporal/spatial python type -> the Cypher constructor that rebuilds it.
# Keyed by class name so this module never has to import neo4j.
_VALUE_ENCODERS = {
    "DateTime": "datetime",
    "LocalDateTime": "localdatetime",
    "Date": "date",
    "Time": "time",
    "LocalTime": "localtime",
    "Duration": "duration",
}

_BACKUP_JOBS: dict = {}
_JOB_HISTORY = 8


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Property encoding
# ---------------------------------------------------------------------------
def _encode_value(value):
    """Make a Neo4j property value JSON-safe without losing its type.

    Scalars, lists and dicts pass through. Anything the driver hands back as a
    Neo4j-specific class becomes ``{"__t": <ctor>, "v": <iso text>}`` so the
    restore side can rebuild it with the right Cypher constructor. An
    unrecognized object is refused rather than silently stringified.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _encode_value(v) for k, v in value.items()}
    ctor = _VALUE_ENCODERS.get(type(value).__name__)
    if ctor:
        return {"__t": ctor, "v": str(value)}
    raise TypeError(f"cannot serialize Neo4j value of type {type(value).__name__!r}")


def _decode_props(props: dict) -> tuple:
    """Split a property map into (plain, typed). ``typed`` maps prop -> ctor."""
    plain = {}
    typed = {}
    for key, value in (props or {}).items():
        if isinstance(value, dict) and value.get("__t") in _VALUE_ENCODERS.values():
            plain[key] = value.get("v")
            typed[key] = value["__t"]
        else:
            plain[key] = value
    return plain, typed


def _key_field(props: dict) -> str:
    """Nodes are addressed by business key. Category nodes only have a name."""
    return "id" if props.get("id") is not None else "name"


def _node_key(props: dict) -> object:
    return props.get(_key_field(props))


# ---------------------------------------------------------------------------
# Savepoint listing
# ---------------------------------------------------------------------------
def _safe_name(entry_id: str) -> str:
    """Reject anything that is not a plain savepoint directory name."""
    candidate = str(entry_id or "").strip()
    if not candidate or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", candidate):
        raise ValueError("invalid savepoint id")
    if candidate in {".", ".."} or candidate.startswith("."):
        raise ValueError("invalid savepoint id")
    return candidate


def savepoint_path(entry_id: str) -> str:
    return os.path.join(BACKUP_DIR, _safe_name(entry_id))


def _read_manifest(entry_id: str):
    path = os.path.join(savepoint_path(entry_id), MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def list_savepoints() -> list:
    """Every readable savepoint, newest first."""
    try:
        entries = os.listdir(BACKUP_DIR)
    except OSError as exc:
        logger.warning(f"backup: cannot list {BACKUP_DIR}: {exc}")
        return []

    found = []
    for name in entries:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) or name.startswith("."):
            continue
        manifest = _read_manifest(name)
        if not manifest:
            continue
        found.append({
            "id": name,
            "created_at": manifest.get("created_at"),
            "reason": manifest.get("reason"),
            "status": manifest.get("status"),
            "counts": manifest.get("counts", {}),
            "collections": manifest.get("collections", []),
            "bytes": manifest.get("bytes", 0),
        })
    found.sort(key=lambda item: (item.get("created_at") or "", item["id"]), reverse=True)
    return found


def prune_savepoints(keep: int = BACKUP_KEEP) -> list:
    """Delete all but the newest ``keep`` completed savepoints."""
    complete = [e for e in list_savepoints() if e.get("status") == "complete"]
    stale = complete[max(0, keep):]
    removed = []
    for entry in stale:
        path = savepoint_path(entry["id"])
        try:
            for name in os.listdir(path):
                os.remove(os.path.join(path, name))
            os.rmdir(path)
            removed.append(entry["id"])
            logger.info(f"backup: pruned old savepoint {entry['id']}")
        except OSError as exc:
            logger.warning(f"backup: could not prune {entry['id']}: {exc}")
    return removed


# ---------------------------------------------------------------------------
# Qdrant snapshots
# ---------------------------------------------------------------------------
async def _qdrant_snapshot(collection: str, dest_dir: str) -> dict:
    """Create a collection snapshot and stream it to ``dest_dir``."""
    before = set()
    try:
        response = await httpx.AsyncClient(timeout=HTTP_TIMEOUT).get(
            f"{QDRANT_URL}/collections/{collection}/snapshots"
        )
        if response.status_code == 200:
            before = {s.get("name") for s in response.json().get("result", [])}
    except Exception as exc:
        logger.debug(f"backup: pre-listing {collection} snapshots failed: {exc}")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        created = await client.post(f"{QDRANT_URL}/collections/{collection}/snapshots",
                                    json={"priority": "snapshot"})
        if created.status_code not in (200, 201):
            raise RuntimeError(f"Qdrant snapshot for {collection} failed: HTTP {created.status_code}")
        try:
            listed = (await client.get(f"{QDRANT_URL}/collections/{collection}/snapshots"))
            entries = listed.json().get("result", []) if listed.status_code == 200 else []
        except Exception as exc:
            logger.debug(f"backup: snapshot listing for {collection} failed: {exc}")
            entries = []
        fresh = [s for s in entries if s.get("name") not in before] or entries
        if not fresh:
            raise RuntimeError(f"Qdrant produced no snapshot for {collection}")
        snapshot = max(fresh, key=lambda s: s.get("creation_time") or 0)
        name = snapshot.get("name")

        target = os.path.join(dest_dir, f"qdrant-{collection}.snapshot")
        size = 0
        async with client.stream(
            "GET", f"{QDRANT_URL}/collections/{collection}/snapshots/{name}"
        ) as stream:
            if stream.status_code != 200:
                raise RuntimeError(
                    f"Qdrant snapshot download for {collection} failed: HTTP {stream.status_code}"
                )
            with open(target, "wb") as handle:
                async for chunk in stream.aiter_bytes():
                    handle.write(chunk)
                    size += len(chunk)
        # The snapshot now lives in our savepoint; drop the copy on the server
        # so repeated runs cannot fill the qdrant volume.
        try:
            await client.delete(f"{QDRANT_URL}/collections/{collection}/snapshots/{name}")
        except Exception as exc:
            logger.debug(f"backup: server snapshot cleanup for {collection} failed: {exc}")
    return {"collection": collection, "file": os.path.basename(target), "bytes": size}


async def _qdrant_restore(collection: str, source: str) -> None:
    """Replace a collection with the contents of a savepoint snapshot."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        dropped = await client.delete(f"{QDRANT_URL}/collections/{collection}")
        if dropped.status_code not in (200, 204, 404):
            raise RuntimeError(f"Qdrant drop of {collection} failed: HTTP {dropped.status_code}")
        with open(source, "rb") as handle:
            uploaded = await client.put(
                f"{QDRANT_URL}/collections/{collection}/snapshots/upload",
                params={"priority": "snapshot"},
                files={"file": (os.path.basename(source), handle, "application/octet-stream")},
            )
        if uploaded.status_code not in (200, 201):
            raise RuntimeError(f"Qdrant snapshot upload for {collection} failed: HTTP {uploaded.status_code}")


# ---------------------------------------------------------------------------
# Neo4j export
# ---------------------------------------------------------------------------
def _export_graph(neo4j_driver, dest_dir: str) -> dict:
    """Page the whole graph into a gzipped JSONL file. Blocking — call in a thread."""
    counts = {"nodes": 0, "relationships": 0}
    path = os.path.join(dest_dir, GRAPH_NAME_GZ)
    with neo4j_driver.session() as session, gzip.open(path, "wt", encoding="utf-8") as handle:

        def emit(record):
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")

        offset = 0
        while True:
            rows = list(session.run(
                """
                MATCH (n)
                RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props
                ORDER BY eid
                SKIP $offset LIMIT $limit
                """,
                offset=offset, limit=_EXPORT_PAGE,
            ))
            if not rows:
                break
            for row in rows:
                props = _encode_value(row["props"] or {})
                labels = list(row["labels"] or [])
                emit({
                    "r": "n",
                    "l": labels,
                    "kf": _key_field(props),
                    "k": _node_key(props),
                    "p": props,
                })
                counts["nodes"] += 1
            offset += len(rows)
            if len(rows) < _EXPORT_PAGE:
                break

        # Relationships resolve endpoints by business key, never by elementId:
        # elementIds are reassigned by any dump/load or store copy, so keying on
        # them produces a graph whose edges point at nothing after a restore.
        offset = 0
        seen = {}
        while True:
            rows = list(session.run(
                """
                MATCH (a)-[r]->(b)
                RETURN elementId(a) AS aeid, labels(a) AS alabels, properties(a) AS aprops,
                       elementId(b) AS beid, labels(b) AS blabels, properties(b) AS bprops,
                       type(r) AS relType, properties(r) AS rprops
                ORDER BY aeid, beid, relType
                SKIP $offset LIMIT $limit
                """,
                offset=offset, limit=_EXPORT_PAGE,
            ))
            if not rows:
                break
            for row in rows:
                a_eid, b_eid = row["aeid"], row["beid"]
                for eid, labels, props in ((a_eid, row["alabels"], row["aprops"]),
                                           (b_eid, row["blabels"], row["bprops"])):
                    if eid not in seen:
                        encoded = _encode_value(props or {})
                        seen[eid] = {
                            "l": list(labels or []),
                            "kf": _key_field(encoded),
                            "k": _node_key(encoded),
                        }
                a_ref, b_ref = seen[a_eid], seen[b_eid]
                if not a_ref["l"] or not b_ref["l"] or a_ref["k"] is None or b_ref["k"] is None:
                    continue  # unaddressable endpoint; cannot be restored faithfully
                emit({
                    "r": "r",
                    "al": a_ref["l"][0], "akf": a_ref["kf"], "ak": a_ref["k"],
                    "tl": row["relType"],
                    "bp": _encode_value(row["rprops"] or {}),
                    "bl": b_ref["l"][0], "bkf": b_ref["kf"], "bk": b_ref["k"],
                })
                counts["relationships"] += 1
            offset += len(rows)
            if len(rows) < _EXPORT_PAGE:
                break
    return {"counts": counts, "file": GRAPH_NAME_GZ, "bytes": os.path.getsize(path)}


def _iter_graph_records(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _restore_nodes(neo4j_driver, records: list) -> int:
    """Recreate nodes one label at a time with a static, parameterized query."""
    created = 0
    for label in sorted({r["l"][0] for r in records}):
        if not _CYPHER_IDENT_RE.match(label):
            logger.warning(f"backup: skipping node label {label!r} — not a valid Cypher identifier")
            continue
        by_key_field = {}
        for record in records:
            if record["l"][0] == label:
                by_key_field.setdefault(record["kf"], []).append(record)
        for key_field, group in by_key_field.items():
            if not _CYPHER_IDENT_RE.match(key_field):
                logger.warning(f"backup: skipping key field {key_field!r} for {label}")
                continue
            typed_props = {}
            for start in range(0, len(group), _RESTORE_PAGE):
                rows = []
                for record in group[start:start + _RESTORE_PAGE]:
                    plain, typed = _decode_props(record["p"])
                    rows.append({"k": record["k"], "p": plain})
                    for name, ctor in typed.items():
                        typed_props.setdefault((label, name), set()).add(ctor)
                with neo4j_driver.session() as session:
                    session.run(
                        f"""
                        UNWIND $rows AS row
                        MERGE (n:{label} {{{key_field}: row.k}})
                        SET n = row.p
                        """,
                        rows=rows,
                    )
                created += len(rows)

            # Temporal values were flattened to ISO text by the generic SET above.
            # Re-apply them with the real constructor, one static query per
            # (label, property) pair, so a DateTime does not come back a string.
            for (prop_label, prop_name), ctors in typed_props.items():
                ctor = sorted(ctors)[0]
                if not _CYPHER_IDENT_RE.match(prop_name) or not _CYPHER_IDENT_RE.match(ctor):
                    continue
                query = f"MATCH (n:{prop_label}) WHERE n.{prop_name} IS NOT NULL SET n.{prop_name} = {ctor}(n.{prop_name})"
                logger.info(f"backup: restoring {prop_label}.{prop_name} as {ctor}()")
                with neo4j_driver.session() as session:
                    session.run(query)
    return created


def _restore_relationships(neo4j_driver, records: list) -> int:
    """Recreate edges with APOC, which handles the arbitrary relationship types
    the app creates from user input. One static query per endpoint pair."""
    groups = {}
    for record in records:
        rel_type = record.get("tl")
        if not rel_type or not _CYPHER_IDENT_RE.match(rel_type):
            continue
        key = (record["al"], record["akf"], record["bl"], record["bkf"])
        groups.setdefault(key, []).append(record)

    created = 0
    for (a_label, a_key_field, b_label, b_key_field), group in groups.items():
        if not all(_CYPHER_IDENT_RE.match(v) for v in (a_label, a_key_field, b_label, b_key_field)):
            logger.warning(f"backup: skipping relationship group {group[0].get('tl')!r} — unsafe identifier")
            continue
        for start in range(0, len(group), _RESTORE_PAGE):
            rows = []
            for record in group[start:start + _RESTORE_PAGE]:
                plain, _ = _decode_props(record.get("bp") or {})
                rows.append({"ak": record["ak"], "bp": plain, "bk": record["bk"]})
            with neo4j_driver.session() as session:
                session.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (a:{a_label} {{{a_key_field}: row.ak}})
                    MATCH (b:{b_label} {{{b_key_field}: row.bk}})
                    CALL apoc.create.relationship(a, $relType, row.bp, b) YIELD rel
                    RETURN count(rel) AS created
                    """,
                    relType=group[0].get("tl"),
                    rows=rows,
                )
            created += len(rows)
    return created


def _wipe_graph(neo4j_driver) -> int:
    """Detach-delete every node in bounded batches (the heap is capped at 1g)."""
    removed = 0
    while True:
        with neo4j_driver.session() as session:
            row = session.run(
                "MATCH (n) WITH n LIMIT $limit DETACH DELETE n RETURN count(n) AS removed",
                limit=_DELETE_PAGE,
            ).single()
        batch = int(row["removed"]) if row and row["removed"] else 0
        removed += batch
        if batch < _DELETE_PAGE:
            break
    return removed


def _restore_graph(neo4j_driver, path: str) -> dict:
    """Blocking — call in a thread. Wipe then replay nodes before edges."""
    nodes = [r for r in _iter_graph_records(path) if r.get("r") == "n"]
    rels = [r for r in _iter_graph_records(path) if r.get("r") == "r"]
    removed = _wipe_graph(neo4j_driver)
    created_nodes = _restore_nodes(neo4j_driver, nodes)
    created_rels = _restore_relationships(neo4j_driver, rels)
    return {
        "removed": removed,
        "nodes": created_nodes,
        "relationships": created_rels,
        "declared_nodes": len(nodes),
        "declared_relationships": len(rels),
    }


def _read_graph_file(entry_dir: str, manifest: dict) -> str:
    name = manifest.get("graph", {}).get("file") or GRAPH_NAME_GZ
    return os.path.join(entry_dir, name if name.endswith(".gz") else GRAPH_NAME)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
async def _reconcile_after_restore() -> None:
    """Re-run the startup repair chain so the two stores agree again."""
    from fact_manager import sync_orphans
    from migrate_client_context import restore_scope_links, sync_qdrant_scope

    for name, call in (
        ("sync_qdrant_scope", sync_qdrant_scope()),
        ("restore_scope_links", restore_scope_links()),
        ("sync_orphans", sync_orphans()),
    ):
        try:
            await call
            logger.info(f"backup: post-restore {name} completed")
        except Exception as exc:
            logger.warning(f"backup: post-restore {name} failed: {exc}")


async def run_backup(reason: str = "manual") -> dict:
    """Create one savepoint. Qdrant first, then Neo4j — see the module docstring."""
    from common import get_neo4j, get_qdrant

    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j is not available")
    await get_qdrant()

    entry_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = savepoint_path(entry_id)
    os.makedirs(dest, exist_ok=True)
    started = time.perf_counter()

    manifest = {
        "version": MANIFEST_VERSION,
        "id": entry_id,
        "created_at": _utcnow(),
        "reason": reason,
        "status": "incomplete",
        "counts": {},
        "collections": [],
        "bytes": 0,
    }
    manifest_path = os.path.join(dest, MANIFEST_NAME)

    def _flush():
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)

    _flush()
    try:
        for collection in (COLLECTION_NAME, DIARY_COLLECTION):
            info = await _qdrant_snapshot(collection, dest)
            manifest["collections"].append(info)
            manifest["bytes"] += info["bytes"]
            _flush()
            logger.info(f"backup: {collection} snapshot {info['bytes']} bytes")

        graph = await asyncio.to_thread(_export_graph, neo4j_driver, dest)
        manifest["graph"] = graph
        manifest["counts"] = graph["counts"]
        manifest["bytes"] += graph["bytes"]
        manifest["status"] = "complete"
        manifest["duration_s"] = round(time.perf_counter() - started, 2)
        _flush()
        logger.info(
            f"backup: savepoint {entry_id} complete — "
            f"{graph['counts']['nodes']} nodes, {graph['counts']['relationships']} relationships"
        )
        prune_savepoints()
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        _flush()
        logger.exception(f"backup: savepoint {entry_id} failed: {exc}")
        raise
    return manifest


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
async def run_restore(entry_id: str, include_vectors: bool = True) -> dict:
    """Overwrite the live stores with the contents of a savepoint.

    ``include_vectors`` defaults to True: restoring the graph without the vector
    store would leave every restored fact unsearchable until the next reindex.
    """
    from common import get_neo4j, get_qdrant

    entry_dir = savepoint_path(entry_id)
    manifest = _read_manifest(_safe_name(entry_id))
    if not manifest:
        raise ValueError(f"Savepoint {entry_id!r} has no readable manifest")
    if manifest.get("status") != "complete":
        raise ValueError(f"Savepoint {entry_id!r} is {manifest.get('status')}, not complete")
    graph_path = _read_graph_file(entry_dir, manifest)
    if not os.path.isfile(graph_path):
        raise ValueError(f"Savepoint {entry_id!r} is missing its graph export")

    neo4j_driver = get_neo4j()
    if not neo4j_driver:
        raise RuntimeError("Neo4j is not available")
    await get_qdrant()

    logger.warning(f"backup: restoring savepoint {entry_id} — this overwrites current data")
    summary = {"id": entry_id, "collections": [], "graph": {}}

    if include_vectors:
        for info in manifest.get("collections", []):
            collection = info.get("collection")
            source = os.path.join(entry_dir, info.get("file") or "")
            if not collection or not os.path.isfile(source):
                logger.warning(f"backup: savepoint {entry_id} has no snapshot for {collection!r}")
                continue
            await _qdrant_restore(collection, source)
            summary["collections"].append(collection)
            logger.warning(f"backup: restored Qdrant collection {collection}")

    summary["graph"] = await asyncio.to_thread(_restore_graph, neo4j_driver, graph_path)
    logger.warning(f"backup: restored {summary['graph']['nodes']} nodes and "
                   f"{summary['graph']['relationships']} relationships")

    await _reconcile_after_restore()
    return summary


# ---------------------------------------------------------------------------
# Job registry (mirrors the reclassification job pattern)
# ---------------------------------------------------------------------------
def _public_job(job) -> dict:
    if not job:
        return {"state": "idle", "total": 0, "done": 0, "message": None,
                "started_at": None, "finished_at": None, "error": None}
    return {k: v for k, v in job.items() if k != "task"}


def _prune_jobs(keep: int = _JOB_HISTORY) -> None:
    if len(_BACKUP_JOBS) <= keep:
        return
    finished = sorted(
        (job.get("finished_at") or job.get("started_at") or "", user_id)
        for user_id, job in _BACKUP_JOBS.items() if job.get("state") != "running"
    )
    for _, user_id in finished[:len(_BACKUP_JOBS) - keep]:
        _BACKUP_JOBS.pop(user_id, None)


def _start_job(user_id: str, operation: str, coro_factory) -> dict:
    running = _BACKUP_JOBS.get(user_id)
    if running and running.get("state") == "running":
        return {"started": False, "job": _public_job(running)}
    if not claim_maintenance(user_id, operation):
        return {"started": False, "job": _public_job(running), "conflict": "maintenance"}

    job = {"state": "running", "operation": operation, "total": 0, "done": 0,
           "message": "Starting", "started_at": _utcnow(), "finished_at": None, "error": None}
    # Prune after inserting so the eviction count accounts for the new entry.
    _BACKUP_JOBS[user_id] = job
    _prune_jobs(keep=_JOB_HISTORY - 1)

    async def _runner():
        try:
            job["message"] = "Working"
            result = await coro_factory(job)
            job["result"] = result
            job["state"] = "done"
            job["message"] = "Complete"
            await publish_db_event(user_id, f"{operation}_done", {"id": result.get("id")})
        except Exception as exc:
            logger.exception(f"backup: {operation} for {user_id} failed: {exc}")
            job.update(state="error", error=str(exc), message="Failed")
        finally:
            job["finished_at"] = _utcnow()
            release_maintenance(user_id)

    job["task"] = asyncio.create_task(_runner())
    return {"started": True, "job": _public_job(job)}


def start_backup(user_id: str, reason: str = "manual") -> dict:
    """Start a savepoint as a background job. False + conflict if one is running."""
    return _start_job(user_id, "backup", lambda job: run_backup(reason))


def start_restore(user_id: str, entry_id: str, include_vectors: bool = True) -> dict:
    """Start a restore as a background job. False + conflict if one is running."""
    _safe_name(entry_id)  # validate before taking the lock
    return _start_job(user_id, "restore", lambda job: run_restore(entry_id, include_vectors))


def get_backup_status(user_id: str) -> dict:
    return _public_job(_BACKUP_JOBS.get(user_id))


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def seconds_until_next_run(now: datetime = None) -> float:
    """Seconds until the next scheduled savepoint, so the loop can sleep once."""
    now = now or datetime.now()
    target = now.replace(hour=BACKUP_HOUR, minute=BACKUP_MINUTES, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


def backup_config() -> dict:
    """What the UI needs to explain the schedule to the user."""
    return {
        "enabled": BACKUP_ENABLED,
        "hour": BACKUP_HOUR,
        "minute": BACKUP_MINUTES,
        "keep": BACKUP_KEEP,
        "directory": BACKUP_DIR,
    }


async def scheduled_backup_loop() -> None:
    """Sleep until the configured hour, save, repeat. Runs for the process life."""
    logger.warning(
        f"backup: scheduler active — daily at {BACKUP_HOUR:02d}:{BACKUP_MINUTES:02d} "
        f"into {BACKUP_DIR} (keep {BACKUP_KEEP})"
    )
    while True:
        try:
            await asyncio.sleep(seconds_until_next_run())
            if not BACKUP_ENABLED:
                logger.info("backup: skipped — MEM_BACKUP_ENABLED=0")
                continue
            result = await run_backup("scheduled")
            logger.warning(f"backup: scheduled savepoint {result['id']} written")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"backup: scheduled run failed: {exc}")
            await asyncio.sleep(600)
