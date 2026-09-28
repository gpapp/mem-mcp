"""Regression tests for mem-mcp/chunking.py.

Dependency-light on purpose: chunking.py imports nothing beyond os/re/uuid, so
the suite runs without the DB drivers, the same way test_matching_regressions
does. The property that matters most is coverage — a splitter that drops or
reorders text makes a record *less* findable than the single truncated vector it
replaced, which is the opposite of the point.
"""

import os
import re
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunking
from chunking import (
    CHUNK_MAX,
    CHUNK_OVERLAP,
    CHUNK_TARGET_CHARS,
    build_chunk_payloads,
    chunk_point_id,
    chunk_text_of,
    effective_target,
    is_chunk_of,
    merge_by_parent,
    needs_chunking,
    parent_of,
    split_chunks,
    strip_chunk_meta,
)


class _Point:
    def __init__(self, point_id, score=0.0, payload=None):
        self.id = point_id
        self.score = score
        self.payload = payload


class ChunkingTestBase(unittest.TestCase):
    """Asserts the three invariants every split must preserve."""

    def assert_split_preserves_text(self, text, target=None, max_chunks=None):
        plan = chunking.plan_chunks(text, target, max_chunks)
        body, chunks, parts = plan["body"], plan["chunks"], plan["parts"]

        # Nothing is discarded or invented by the split.
        self.assertEqual("".join(parts), body, "split lost or altered characters")
        # Parts tile the source in order: no gap (a gap is unretrievable text)
        # and no overlap of their own (which would double-count the text).
        position = 0
        for part in parts:
            self.assertEqual(body[position:position + len(part)], part)
            position += len(part)
        self.assertEqual(position, len(body), "parts do not reach the end of the source")
        # Every chunk is a real slice of the source, so a chunk can always be
        # traced back to the record it came from.
        for index, chunk in enumerate(chunks):
            self.assertIn(chunk, body, f"chunk {index} is not a substring of the source")
        # The overlap is exactly the previous part's tail — no inserted glue.
        self.assertEqual(len(chunks), len(parts))
        for index in range(1, len(parts)):
            expected = (parts[index - 1][-CHUNK_OVERLAP:] if CHUNK_OVERLAP else "") + parts[index]
            self.assertEqual(chunks[index], expected)
        # A chunk wider than the embed ceiling gets head+tail truncated by the
        # embedder, so its middle would be unreachable — the very thing chunking
        # is meant to prevent.
        for index, chunk in enumerate(chunks):
            self.assertLessEqual(len(chunk), chunking.EMBED_CEILING + CHUNK_OVERLAP,
                                 f"chunk {index} is past the embed ceiling")
        self.assertLessEqual(len(chunks), plan["limit"])
        return chunks


class SplitInvariantsTests(ChunkingTestBase):
    CASES = {
        "repeated sentence": "para one. " * 400,
        "blob with no separator": "x" * 25000,
        "very long": "y" * 100000,
        "sentences but no newlines": "One sentence here. Another follows. " * 2000,
        "paragraphs": "\n\n".join(f"Paragraph {i}. " + "word " * 300 for i in range(20)),
        "transcript": "\n\n".join(f"[{i}] Gergely: " + "discussion " * 200 for i in range(20)),
        "lines only": "\n".join(f"line {i} with some text " * 50 for i in range(60)),
        "unicode and em dash": "\n\n".join(f"Ünïcödé bödy {i} — em dash. " * 200 for i in range(15)),
        "single long line": "z" * 7000,
        "trailing separator": "alpha bravo charlie " * 1000 + "\n\n",
        "leading separator": "\n\n" + "alpha bravo charlie " * 1000,
        "mixed block sizes": "A" * 5000 + "\n\n" + "B" * 5000 + "\n" + "C." * 2000,
        "crlf": "\r\n".join(f"row {i} value " * 60 for i in range(100)),
        "markdown": "\n\n".join(f"## H{i}\n- a {i}\n\nSome prose {i}. " * 150 for i in range(20)),
        "whitespace only": "   \n\n  ",
        "short": "hi",
    }

    def test_every_shape_preserves_text(self):
        for name, text in self.CASES.items():
            with self.subTest(case=name):
                self.assert_split_preserves_text(text)

    def test_chunks_stay_within_cap(self):
        for name, text in self.CASES.items():
            with self.subTest(case=name):
                self.assertLessEqual(len(split_chunks(text)), CHUNK_MAX)

    def test_long_record_reaches_every_chapter(self):
        """A 20-section transcript must be reachable in every section.

        This is the actual user-facing promise: the query 'what was said about
        section 17' has to find section 17. A splitter that returned only the
        first few sections would pass a naive length check and fail here.
        """
        text = "\n\n".join(f"SECTION-{i} " + "filler " * 200 for i in range(20))
        chunks = split_chunks(text)
        self.assertGreater(len(chunks), 1)
        for index in range(20):
            self.assertTrue(
                any(f"SECTION-{index} " in chunk for chunk in chunks),
                f"section {index} is not in any chunk",
            )

    def test_empty_and_whitespace_inputs(self):
        for value in ("", "   ", "\n\n\n", None):
            with self.subTest(value=repr(value)):
                self.assertEqual(split_chunks(value or ""), [])

    def test_short_text_is_returned_verbatim(self):
        text = "A short fact about the invoice."
        self.assertEqual(split_chunks(text), [text])
        self.assertFalse(needs_chunking(text))

    def test_threshold_is_the_target_not_the_embed_ceiling(self):
        # Splitting below CHUNK_TARGET_CHARS produces chunks too small to carry
        # a distinct meaning, so it must not happen even though it would fit.
        self.assertFalse(needs_chunking("x" * CHUNK_TARGET_CHARS))
        self.assertTrue(needs_chunking("x" * (CHUNK_TARGET_CHARS + 1)))
        self.assertGreater(chunking.EMBED_CEILING, CHUNK_TARGET_CHARS)

    def test_explicit_target_overrides_config(self):
        text = "word " * 2000
        self.assertEqual(len(split_chunks(text, target=100000)), 1)
        self.assertGreater(len(split_chunks(text, target=500, max_chunks=500)), 4)

    def test_overlap_keeps_the_previous_tail(self):
        text = "\n\n".join(f"para {i} " + "filler " * 300 for i in range(10))
        chunks = split_chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[1][:CHUNK_OVERLAP], chunks[0][-CHUNK_OVERLAP:])

    def test_cap_widens_chunks_instead_of_dropping_the_tail(self):
        """The obvious way to honour the cap loses the end of the document.

        Keeping the first N chunks and letting the last one absorb the rest
        produces a chunk far past the embed ceiling, which the embedder then
        truncates — so the tail is unreachable again, silently. The target is
        widened instead.
        """
        text = "\n\n".join(f"part {i} " + "filler " * 2000 for i in range(30))
        chunks = split_chunks(text)
        # Coverage first: every part is in some chunk.
        for index in range(30):
            self.assertTrue(any(f"part {index} " in chunk for chunk in chunks))
        # And no chunk is so wide that the embedder truncates its middle, which
        # is the only way a cap could quietly reintroduce the original problem.
        for chunk in chunks:
            self.assertLessEqual(len(chunk), chunking.EMBED_CEILING + CHUNK_OVERLAP,
                                 "a chunk past the embed ceiling loses its middle")
        # The call budget is still respected for an ordinary-size record.
        self.assertLessEqual(len(split_chunks("x " * 40000)), CHUNK_MAX)

    def test_crlf_is_normalised_to_lf(self):
        chunks = split_chunks("\r\n".join(f"row {i} " * 200 for i in range(30)))
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertNotIn("\r", chunk)


class PointIdentityTests(unittest.TestCase):
    def test_chunk_zero_keeps_the_record_id(self):
        self.assertEqual(chunk_point_id("fact-1", 0), "fact-1")
        self.assertEqual(chunk_point_id("fact-1", -3), "fact-1")

    def test_derived_ids_are_stable_and_distinct(self):
        first = chunk_point_id("fact-1", 1)
        self.assertEqual(first, chunk_point_id("fact-1", 1))
        self.assertNotEqual(first, chunk_point_id("fact-1", 2))
        self.assertNotEqual(first, chunk_point_id("fact-2", 1))
        # The namespace is a pinned literal, not a fresh uuid4. Regenerating it
        # would give every previously chunked record a new set of ids, and the
        # old points would survive as orphans that nothing can reach or delete.
        self.assertEqual(chunking.CHUNK_NS, uuid.UUID("6f1c8b2a-3d54-4e97-9a10-5b7d0e2c8f43"))

    def test_parent_of_prefers_the_payload(self):
        self.assertEqual(parent_of("derived-uuid", {"parentId": "fact-1"}), "fact-1")
        self.assertEqual(parent_of("derived-uuid", {}), "derived-uuid")
        self.assertEqual(parent_of("fact-1", None), "fact-1")
        self.assertEqual(parent_of("derived", {"parentId": None}), "derived")
        # An int id from a Qdrant response must still stringify.
        self.assertEqual(parent_of(12345, {"parentId": 99}), "99")

    def test_is_chunk_of_distinguishes_the_primary_point(self):
        self.assertFalse(is_chunk_of("fact-1", {"parentId": "fact-1"}))
        self.assertTrue(is_chunk_of("derived", {"parentId": "fact-1"}))
        self.assertFalse(is_chunk_of("fact-1", {}))
        self.assertFalse(is_chunk_of("fact-1", None))


class PayloadShapeTests(unittest.TestCase):
    BASE = {"text": "", "name": "T", "userId": "u", "category": "General"}

    def long_text(self):
        return "\n\n".join(f"Section {i} " + "content " * 400 for i in range(12))

    def test_unchunked_record_is_byte_identical_to_before(self):
        payload = {"text": "short", "name": "T", "userId": "u"}
        self.assertEqual(
            build_chunk_payloads("fact-1", "short", dict(payload)),
            [{"id": "fact-1", "payload": payload}],
        )

    def test_chunked_record_shape(self):
        text = self.long_text()
        points = build_chunk_payloads("fact-1", text, dict(self.BASE, text=text))
        self.assertGreater(len(points), 1)
        self.assertEqual(points[0]["id"], "fact-1")
        self.assertEqual(len({p["id"] for p in points}), len(points))
        for index, point in enumerate(points):
            payload = point["payload"]
            self.assertEqual(payload["parentId"], "fact-1")
            self.assertEqual(payload["chunkIndex"], index)
            self.assertEqual(payload["chunkCount"], len(points))
            self.assertEqual(point["id"], chunk_point_id("fact-1", index))

    def test_every_chunk_carries_the_full_payload(self):
        """A chunk missing its filter keys is invisible to every filter.

        Qdrant filters on category, client, context, and keywords. If only chunk
        0 carried them, a query scoped to a client would silently miss
        everything except the opening passage of a long record.
        """
        text = self.long_text()
        base = dict(self.BASE, text=text, client="Deutsche Bank (DB)",
                    context="Atlas Migration", keywords=["k1", "k2"], userId="u1")
        points = build_chunk_payloads("fact-1", text, base)
        for index, point in enumerate(points):
            payload = point["payload"]
            for key in ("name", "userId", "category", "client", "context", "keywords"):
                self.assertEqual(payload.get(key), base[key], f"chunk {index} lost {key}")

    def test_only_chunk_zero_carries_the_full_text(self):
        text = self.long_text()
        points = build_chunk_payloads("fact-1", text, dict(self.BASE, text=text))
        self.assertEqual(points[0]["payload"]["text"], text)
        self.assertNotIn("chunkText", points[0]["payload"])
        for point in points[1:]:
            self.assertNotIn("text", point["payload"])
            self.assertTrue(point["payload"]["chunkText"])

    def test_diary_uses_content_as_the_full_text_key(self):
        text = self.long_text()
        points = build_chunk_payloads("d-1", text, {"content": text, "userId": "u"})
        self.assertEqual(points[0]["payload"]["content"], text)
        for point in points[1:]:
            self.assertNotIn("content", point["payload"])
            self.assertTrue(point["payload"]["chunkText"])

    def test_rewriting_is_idempotent(self):
        text = self.long_text()
        first = build_chunk_payloads("fact-1", text, dict(self.BASE, text=text))
        second = build_chunk_payloads("fact-1", text, dict(self.BASE, text=text))
        self.assertEqual([p["id"] for p in first], [p["id"] for p in second])

    def test_shortening_a_record_does_not_leak_stale_chunks(self):
        """A point that vanished from the family must not linger in the payload.

        The writer deletes the old family before upserting, but build_chunk_payloads
        has to at least stop advertising a higher chunkCount than it produces —
        a stale chunkCount is what makes a reader think points are missing.
        """
        long_text = self.long_text()
        long_points = build_chunk_payloads("fact-1", long_text, dict(self.BASE, text=long_text))
        short_points = build_chunk_payloads("fact-1", "now short", dict(self.BASE, text="now short"))
        self.assertEqual(len(short_points), 1)
        self.assertNotIn("chunkCount", short_points[0]["payload"])
        self.assertGreater(len(long_points), 1)

    def test_strip_chunk_meta_removes_only_chunking_keys(self):
        payload = {"text": "t", "parentId": "x", "chunkIndex": 3, "chunkCount": 9, "chunkText": "c"}
        self.assertEqual(strip_chunk_meta(payload), {"text": "t"})

    def test_existing_chunk_keys_in_a_base_payload_are_dropped(self):
        text = self.long_text()
        stale = dict(self.BASE, text=text, parentId="other", chunkIndex=7, chunkCount=99)
        points = build_chunk_payloads("fact-1", text, stale)
        self.assertEqual(points[0]["id"], "fact-1")
        self.assertEqual(points[0]["payload"]["parentId"], "fact-1")
        self.assertEqual(points[0]["payload"]["chunkIndex"], 0)

    def test_chunk_text_of(self):
        self.assertEqual(chunk_text_of({"text": "full"}), "full")
        self.assertEqual(chunk_text_of({"content": "diary"}), "diary")
        self.assertEqual(chunk_text_of({"chunkText": "part"}), "part")
        # A passage wins over an empty full-text slot.
        self.assertEqual(chunk_text_of({"text": "", "chunkText": "part"}), "part")
        self.assertEqual(chunk_text_of({}), "")
        self.assertEqual(chunk_text_of(None), "")


class MergeByParentTests(unittest.TestCase):
    def test_keeps_the_best_scoring_chunk(self):
        points = [
            _Point("fact-1", 0.4, {"parentId": "fact-1", "chunkIndex": 0}),
            _Point("derived-a", 0.9, {"parentId": "fact-1", "chunkIndex": 3}),
            _Point("derived-b", 0.6, {"parentId": "fact-1", "chunkIndex": 7}),
        ]
        merged = merge_by_parent(points)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged["fact-1"].id, "derived-a")

    def test_unchunked_points_pass_through(self):
        points = [_Point("fact-1", 0.5, {"text": "t"}), _Point("fact-2", 0.8, {"text": "t"})]
        merged = merge_by_parent(points)
        self.assertEqual(sorted(merged), ["fact-1", "fact-2"])
        self.assertEqual(merged["fact-2"].id, "fact-2")

    def test_first_point_wins_a_tie_so_ordering_is_stable(self):
        points = [
            _Point("fact-1", 0.5, {"parentId": "fact-1"}),
            _Point("derived", 0.5, {"parentId": "fact-1"}),
        ]
        self.assertEqual(merge_by_parent(points)["fact-1"].id, "fact-1")

    def test_tolerates_dicts_and_missing_scores(self):
        points = [
            {"id": "d", "score": 0.2, "payload": {"parentId": "f"}},
            {"id": "e", "payload": {"parentId": "f"}},
        ]
        merged = merge_by_parent(points)
        self.assertEqual(list(merged), ["f"])
        self.assertEqual(merge_by_parent([]), {})
        self.assertEqual(merge_by_parent(None), {})


class ConfigTests(unittest.TestCase):
    def test_defaults_are_sane(self):
        self.assertGreaterEqual(chunking.CHUNK_TARGET_CHARS, 200)
        self.assertGreaterEqual(CHUNK_MAX, 1)
        self.assertLessEqual(CHUNK_OVERLAP, 500)
        self.assertLessEqual(CHUNK_TARGET_CHARS, chunking.EMBED_CEILING,
                             "a chunk larger than the embed ceiling is truncated again")

    def test_effective_target_never_returns_less_than_the_configured_target(self):
        self.assertEqual(effective_target("short"), chunking.CHUNK_TARGET_CHARS)
        self.assertEqual(effective_target(""), chunking.CHUNK_TARGET_CHARS)

    def test_effective_target_widens_to_fill_the_cap(self):
        text = "x" * (chunking.CHUNK_TARGET_CHARS * 8)
        widened = effective_target(text)
        # Wide enough that the whole record still fits inside CHUNK_MAX chunks.
        self.assertLessEqual(-(-len(text) // widened), CHUNK_MAX)
        self.assertGreaterEqual(widened, chunking.CHUNK_TARGET_CHARS)


class CallSiteGuardTests(unittest.TestCase):
    """Pin the invariants that only hold if the call sites were wired correctly.

    Nothing can execute these paths here — no Neo4j, no Qdrant — but the two
    ways this feature destroys data are both *silent*: orphan sync deleting the
    live chunks of a healthy record, or a metadata write touching only chunk 0
    so the other chunks silently fall out of every category/scope filter. A
    source-level assertion is the only thing standing between a refactor and
    either outcome, so it is worth the fragility.
    """

    @staticmethod
    def _source(name):
        with open(os.path.join(os.path.dirname(__file__), name), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_no_point_id_reaches_an_id_set_without_going_through_parent_of(self):
        # A raw str(p.id) here would put chunks 1..N-1 into the orphan set, and
        # sync_orphans would delete them on the next boot. Two legitimate forms
        # exist and both are fine: collapsing with parent_of(), and the family
        # lookup itself, which is already filtered on parentId.
        for name in ("fact_manager.py", "diary_manager.py", "reindex_diary_keywords.py"):
            src = self._source(name)
            for match in re.finditer(r"\.add\(([^\n]*p\.id[^\n]*)\)", src):
                collected = match.group(1)
                self.assertIn(
                    "parent_of(", collected,
                    f"{name}: point id added to a set without collapsing to the parent: {collected}",
                )
            # A scroll that collapses must have asked for the parentId payload.
            for match in re.finditer(r"\.scroll\(", src):
                window = src[match.start():match.start() + 400]
                if 'key="parentId"' in window:
                    continue  # family lookup: filtered, so no payload needed
                self.assertIn(
                    'with_payload=["parentId"]', window,
                    f"{name}: a scroll that collapses to the parent must fetch parentId",
                )

    def test_orphan_deletion_goes_through_the_family_helper(self):
        # The orphan sets hold record ids, so a bulk delete by those ids would
        # remove chunk 0 and strand the rest of the family forever.
        # assertFalse, not assertNotIn: the container is a 2500-line file and
        # unittest would echo all of it on failure.
        src = self._source("fact_manager.py")
        for needle in ("PointIdsList(points=list(orphan_fact_qdrant))",
                       "PointIdsList(points=list(orphan_diary_qdrant))"):
            self.assertFalse(needle in src,
                             f"orphans must be deleted per record, found bulk delete: {needle}")

    def test_orphan_comparison_sets_are_built_from_the_parent_id(self):
        src = self._source("fact_manager.py")
        self.assertTrue("qdrant_fact_ids.add(parent_of(p.id, p.payload))" in src,
                        "the fact orphan set must collapse chunks to their record")
        self.assertTrue("qdrant_diary_ids.add(parent_of(p.id, p.payload))" in src,
                        "the diary orphan set must collapse chunks to their entry")

    def test_update_paths_replace_the_whole_family(self):
        # Shortening a text produces fewer chunks; without replace=True the
        # leftover high-index points keep answering for text that is gone.
        for name in ("fact_manager.py", "diary_manager.py"):
            src = self._source(name)
            self.assertTrue("replace=True" in src,
                            f"{name}: update path must replace the family")


if __name__ == "__main__":
    unittest.main(verbosity=2)
