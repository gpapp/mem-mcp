"""Tests for the compressed Qdrant snapshots in ``backup.py``.

``backup.py`` cannot be imported here — it pulls in ``httpx`` and ``common``,
neither of which is installed — so the two pure helpers are lifted out of the
real source with ``ast`` and exec'd, the same technique
``test_embedding_reliability.py`` uses. These tests therefore exercise the
shipping code rather than a copy, and a rename makes the loader fail loudly
instead of going quietly blind.

Compression was added because a chunked long record repeats its whole payload
across every chunk, so the raw snapshot is mostly near-duplicate JSON. Two
things are easy to get wrong and both would fail only at restore time, on a
savepoint that took a full export to produce:

- Qdrant validates the uploaded multipart filename and rejects anything that
  does not end in ``.snapshot``. Sending ``ea_memories.snapshot.gz`` would be
  refused with no useful message, after the collection was already dropped.
- Deciding whether to decompress by manifest version would strand every
  savepoint taken before compression existed. The suffix is what is on disk
  and it is the only thing that cannot lie.
"""

import ast
import gzip
import io
import os
import tempfile
import unittest

BACKUP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.py")

_FUNCTIONS = ("_open_snapshot", "_upload_name")


def _load(namespace_extra):
    """Exec the helpers we care about out of the real backup.py."""
    with open(BACKUP_PY, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    found = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    missing = set(_FUNCTIONS) - found
    if missing:
        raise AssertionError(f"backup.py no longer defines {sorted(missing)}")
    chunks = [
        ast.get_source_segment(source, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCTIONS
    ]

    namespace = {"gzip": gzip, "os": os}
    namespace.update(namespace_extra)
    exec("\n\n".join(chunks), namespace)  # noqa: S102 - our own source
    return namespace


def _pack(payload, name=None):
    """gzip exactly the way backup.py does: GzipFile over an open file handle.

    ``filename=""`` matters — GzipFile otherwise records the target's basename
    in the header, which would make two exports of identical data differ and
    quietly break the diff-the-snapshot idea.
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(payload)
    return buffer.getvalue()


class SnapshotCompressionTests(unittest.TestCase):
    def setUp(self):
        self.payload = b"".join(
            b'{"text":"the same long passage repeated many times","vector":[0.1,0.2]}'
            for _ in range(500)
        )

    def _write(self, directory, name, compress):
        path = os.path.join(directory, name)
        if compress:
            with open(path, "wb") as handle:
                with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as gz:
                    gz.write(self.payload)
        else:
            with open(path, "wb") as handle:
                handle.write(self.payload)
        return path

    def test_compressed_snapshot_round_trips_to_the_original_bytes(self):
        ns = _load({})
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "ea_memories.snapshot.gz", compress=True)
            with ns["_open_snapshot"](path) as handle:
                self.assertEqual(handle.read(), self.payload)

    def test_uncompressed_snapshot_still_opens(self):
        """Savepoints predating compression are still on disk and still valid."""
        ns = _load({})
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "ea_memories.snapshot", compress=False)
            with ns["_open_snapshot"](path) as handle:
                self.assertEqual(handle.read(), self.payload)

    def test_gzip_actually_shrinks_a_chunked_payload(self):
        # Guards the premise of the change: if it ever stopped paying, the
        # added restore-path complexity would be unjustified.
        with tempfile.TemporaryDirectory() as tmp:
            plain = self._write(tmp, "a.snapshot", compress=False)
            packed = self._write(tmp, "a.snapshot.gz", compress=True)
            self.assertLess(
                os.path.getsize(packed),
                os.path.getsize(plain) / 4,
                "repeating payloads should compress heavily",
            )

    def test_upload_name_is_rebuilt_not_derived_from_the_path(self):
        """Qdrant rejects any multipart filename that is not *.snapshot."""
        ns = _load({})
        self.assertEqual(
            ns["_upload_name"]("/backups/20260101-030000/ea_memories.snapshot.gz", "ea_memories"),
            "ea_memories.snapshot",
        )
        # Also correct for an old uncompressed savepoint, so the two formats
        # produce byte-identical requests to Qdrant.
        self.assertEqual(
            ns["_upload_name"]("/backups/20250101-030000/ea_memories.snapshot", "ea_memories"),
            "ea_memories.snapshot",
        )

    def test_upload_name_does_not_leak_the_directory(self):
        ns = _load({})
        name = ns["_upload_name"]("/var/lib/mem-mcp/backup/x/diary.snapshot.gz", "diary")
        self.assertEqual(name, "diary.snapshot")
        self.assertNotIn("/", name)

    def test_open_snapshot_is_decided_by_suffix_not_content(self):
        # A plain file whose *bytes* start with the gzip magic must still be
        # read as plain, because the decision has to be knowable from the
        # manifest and the filename alone.
        ns = _load({})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "weird.snapshot")
            with open(path, "wb") as handle:
                handle.write(b"\x1f\x8b not really gzip " + b"tail")
            with ns["_open_snapshot"](path) as handle:
                self.assertEqual(handle.read(), b"\x1f\x8b not really gzip tail")

    def test_gzip_stream_helper_is_deterministic(self):
        # Two exports of identical data must produce identical bytes, so a diff
        # between savepoints means the data changed. This is why backup.py
        # passes mtime=0, and why the GzipFile is handed a fileobj rather than
        # a path.
        self.assertEqual(_pack(self.payload), _pack(self.payload))


# ---------------------------------------------------------------------------
# Change fingerprint — a scheduled run over an unchanged vault is a no-op
# ---------------------------------------------------------------------------
_FINGERPRINT_FUNCTIONS = (
    "_digest_line", "fingerprint_matches", "_vault_digest", "latest_fingerprint",
)


def _load_fingerprint(namespace_extra=None):
    """Lift the fingerprint helpers, which need ``hashlib`` and ``_EXPORT_PAGE``."""
    with open(BACKUP_PY, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    found = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    missing = set(_FINGERPRINT_FUNCTIONS) - found
    if missing:
        raise AssertionError(f"backup.py no longer defines {sorted(missing)}")
    chunks = [
        ast.get_source_segment(source, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in _FINGERPRINT_FUNCTIONS
    ]
    namespace = {"hashlib": __import__("hashlib"), "_EXPORT_PAGE": 500}
    namespace.update(namespace_extra or {})
    exec("\n\n".join(chunks), namespace)  # noqa: S102 - our own source
    return namespace


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows


class _FakeSession:
    """Only the two query shapes ``_vault_digest`` asks a session for."""

    def __init__(self, nodes, relationships):
        self._nodes = nodes
        self._relationships = relationships

    def run(self, query, **kwargs):
        return _FakeResult(self._nodes if "MATCH (n)" in query else self._relationships)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, nodes, relationships):
        self._nodes = nodes
        self._relationships = relationships

    def session(self):
        return _FakeSession(self._nodes, self._relationships)


_NODES = [
    {"eid": "4:1", "labels": ["Fact"], "id": "f1", "name": "A fact", "body": "body text"},
    {"eid": "4:2", "labels": ["DiaryEntry"], "id": "d1", "name": "A note", "body": "long body"},
]
_RELATIONSHIPS = [{"src": "f1", "rel_type": "FOR_CLIENT", "dst": "c1"}]


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.fp = _load_fingerprint()

    def digest(self, nodes=None, relationships=None):
        return self.fp["_vault_digest"](_FakeDriver(
            list(_NODES) if nodes is None else nodes,
            list(_RELATIONSHIPS) if relationships is None else relationships,
        ))

    def test_two_reads_of_an_unchanged_vault_are_equal(self):
        first, second = self.digest(), self.digest()
        self.assertEqual(first, second)
        self.assertNotEqual(first["digest"], "")
        self.assertTrue(self.fp["fingerprint_matches"](first, second))

    def test_the_counts_ride_along_for_the_log_and_the_comparison(self):
        counts = self.digest()
        self.assertEqual(counts["nodes"], 2)
        self.assertEqual(counts["relationships"], 1)
        self.assertEqual(counts["Fact"], 1)
        self.assertEqual(counts["DiaryEntry"], 1)
        self.assertEqual(counts["Client"], 0)

    def test_a_body_edit_changes_the_fingerprint(self):
        edited = [dict(_NODES[0], body="body text, corrected")]
        self.assertFalse(self.fp["fingerprint_matches"](self.digest(), self.digest(edited)))

    def test_a_same_length_edit_changes_the_fingerprint(self):
        # Length is not content: fixing a typo keeps the length, so a digest
        # over counts and sizes alone would call this "no change".
        edited = [dict(_NODES[0], body="bodx text")]
        self.assertFalse(self.fp["fingerprint_matches"](self.digest(), self.digest(edited)))

    def test_a_reclassify_that_only_moves_edges_changes_the_fingerprint(self):
        # The scope classifier rewrites FOR_CLIENT / IN_CONTEXT edges without
        # touching a single node property. A digest over nodes alone would skip
        # the savepoint that was supposed to capture the new scope.
        moved = [{"src": "f1", "rel_type": "RELEVANT_TO", "dst": "c1"}]
        self.assertFalse(self.fp["fingerprint_matches"](self.digest(), self.digest(relationships=moved)))

    def test_an_unreadable_fingerprint_never_matches(self):
        real = self.digest()
        self.assertFalse(self.fp["fingerprint_matches"]({}, {}))
        self.assertFalse(self.fp["fingerprint_matches"]({}, real))
        self.assertFalse(self.fp["fingerprint_matches"](real, {}))

    def test_the_digest_line_cannot_be_ambiguous(self):
        line = self.fp["_digest_line"]
        self.assertNotEqual(line("ab", "c"), line("a", "bc"))
        self.assertNotEqual(line("a", "b"), line("a", "b", ""))

    def test_a_failed_savepoint_never_provides_the_baseline(self):
        # Its manifest was written before the snapshot, so the fingerprint it
        # carries describes a vault that was never captured.
        self.fp["list_savepoints"] = lambda: [
            {"id": "failed", "status": "failed"},
            {"id": "ok", "status": "complete"},
        ]
        self.fp["_read_manifest"] = lambda entry_id: (
            {"fingerprint": {"digest": "baseline"}} if entry_id == "ok" else
            {"fingerprint": {"digest": "live-but-never-captured"}}
        )
        self.assertEqual(self.fp["latest_fingerprint"](), {"digest": "baseline"})

    def test_a_savepoint_from_before_the_feature_reads_as_unknown(self):
        self.fp["list_savepoints"] = lambda: [{"id": "old", "status": "complete"}]
        self.fp["_read_manifest"] = lambda entry_id: {"counts": {"nodes": 1}}
        self.assertEqual(self.fp["latest_fingerprint"](), {})
        # ...and unknown therefore takes a savepoint instead of skipping one.
        self.assertFalse(self.fp["fingerprint_matches"]({}, self.digest()))


class ScheduledSkipTests(unittest.TestCase):
    """The comparison lives in the scheduler, and nowhere else.

    The loop sleeps until the configured hour and is not drivable here, so the
    call site is pinned as source: the property is *where* the comparison sits,
    which no helper test can see. The scheduled path compares and skips; the
    manual path (``start_backup`` -> ``run_backup``) never compares, because an
    explicit request always takes a savepoint.
    """

    @classmethod
    def setUpClass(cls):
        with open(BACKUP_PY, "r", encoding="utf-8") as handle:
            cls.source = handle.read()
        tree = ast.parse(cls.source)
        cls.segments = {
            node.name: ast.get_source_segment(cls.source, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_the_scheduled_loop_skips_before_taking_a_savepoint(self):
        loop = self.segments["scheduled_backup_loop"]
        self.assertTrue("vault_fingerprint()" in loop, loop[:400])
        self.assertTrue("latest_fingerprint()" in loop, loop[:400])
        self.assertTrue("fingerprint_matches(" in loop, loop[:400])
        self.assertTrue("backup: skipped" in loop, loop[:400])
        self.assertLess(
            loop.index("fingerprint_matches("),
            loop.index('run_backup("scheduled"'),
            msg="the comparison must happen before the savepoint is taken",
        )

    def test_a_manual_run_never_consults_the_fingerprint(self):
        for name in ("run_backup", "start_backup"):
            self.assertFalse(
                "fingerprint_matches(" in self.segments[name],
                msg=f"{name} compares fingerprints; only the scheduled loop "
                    f"may skip — a manual run is an explicit request",
            )


if __name__ == "__main__":
    unittest.main()
