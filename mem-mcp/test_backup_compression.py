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


if __name__ == "__main__":
    unittest.main()
