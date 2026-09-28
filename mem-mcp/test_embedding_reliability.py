"""Regression tests for the embedding call against Ollama.

``common.py`` cannot be imported here: it pulls in httpx, numpy, the Neo4j
driver and Qdrant, none of which are installed. Instead the embedding block is
lifted out of the real source with ``ast`` and exec'd against stubs, so these
tests exercise the code that ships rather than a copy of it. If a helper is
renamed, the loader raises and the suite says so instead of going quietly blind.

A 500 from ``/api/embeddings`` is what killed ``diary_save_entry`` in
production. A bare traceback said only "500", which cannot distinguish a
missing model from an out-of-memory load from a route the server no longer
serves, so these tests pin three properties:

- the Ollama error body reaches the log and the raised message;
- a plausibly transient failure is retried, and a persistent one falls back
  from the legacy ``/api/embeddings`` route to ``/api/embed``;
- a 4xx is not retried, because a bad model name cannot fix itself.
"""

import ast
import asyncio
import os
import re
import unittest

COMMON_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "common.py")

_FUNCTIONS = (
    "_ollama_detail",
    "_embed_once",
    "_is_input_too_long",
    "_truncate_for_embed",
    "_ollama_model_matches",
    "get_embedding",
)
_ASSIGNMENTS = (
    "_EMBEDDING_CACHE",
    "EMBED_CACHE_MAX",
    "EMBED_RETRIES",
    "EMBED_RETRY_BACKOFF",
    "EMBED_MAX_CHARS",
    "_EMBED_RETRY_STATUS",
    "_EMBED_INPUT_ERRORS",
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text

    @property
    def is_error(self):
        return self.status_code >= 400

    @property
    def text(self):
        if self._text is not None:
            return self._text
        if self._payload is None:
            return ""
        return str(self._payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTPError(Exception):
    """Stands in for httpx.HTTPError, which the retry loop catches."""


class Recorder:
    """Collects POSTs and the log records the code under test emits.

    ``debugs``/``warnings``/``errors`` are kept apart on purpose, because the
    levels are a contract, not cosmetics. Embedding is the high-volume path, so
    per-attempt detail is DEBUG; an over-budget input is a WARNING because the
    stored vector is then lossy; a terminal failure is an ERROR because it is the
    only signal that write was refused.
    """

    def __init__(self):
        self.posts = []
        self.warnings = []
        self.debugs = []
        self.errors = []
        self.slept = 0.0


def _load(recorder, responder, *, retries=None, backoff=0.0, max_chars=None):
    """Exec the real embedding block against stubs. Returns a namespace."""
    with open(COMMON_PY, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    wanted = set(_FUNCTIONS)
    chunks = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            chunks.append(ast.get_source_segment(source, node))
            continue
        # Plain `X = ...` and annotated `X: T = ...` both count; the cache dict
        # is annotated, the tunables are not.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in _ASSIGNMENTS for t in targets):
                chunks.append(ast.get_source_segment(source, node))
    missing = wanted - {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if missing:
        raise AssertionError(f"common.py no longer defines {sorted(missing)}")

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            # Copy the body. get_embedding reuses one dict across attempts and
            # only overwrites the text field, so recording the reference would
            # make every logged request show the final value and hide the shrink.
            recorder.posts.append((url, dict(json) if json else json))
            result = responder(url, json, len(recorder.posts))
            if isinstance(result, Exception):
                raise result
            return result

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            self._c = FakeClient()

        def __aenter__(self):
            return self._c.__aenter__()

        def __aexit__(self, *exc):
            return self._c.__aexit__(*exc)

    class FakeHttpx:
        AsyncClient = FakeAsyncClient
        HTTPError = FakeHTTPError

    class FakeLogger:
        def warning(self, msg, *a, **kw):
            recorder.warnings.append(str(msg))

        def debug(self, msg, *a, **kw):
            recorder.debugs.append(str(msg))

        def error(self, msg, *a, **kw):
            recorder.errors.append(str(msg))

    async def _sleep(seconds):
        recorder.slept += seconds

    namespace = {
        "os": os,
        "httpx": FakeHttpx,
        "logger": FakeLogger(),
        "asyncio": type("A", (), {"sleep": staticmethod(_sleep)})(),
        "List": list,
        "EMBED_MODEL": "nomic-embed-text",
        "EMBED_CACHE_MAX": 2048,
        "OLLAMA_URL": "http://ollama:11434",
        "HTTP_TIMEOUT": 300.0,
    }
    exec("\n\n".join(chunks), namespace)  # noqa: S102 - executing our own source
    # Overrides are applied after exec so the real os.getenv defaults are
    # computed first and then deliberately replaced.
    if retries is not None:
        namespace["EMBED_RETRIES"] = retries
    namespace["EMBED_RETRY_BACKOFF"] = backoff
    if max_chars is not None:
        namespace["EMBED_MAX_CHARS"] = max_chars
    return namespace


VEC = [0.1, 0.2, 0.3]
LEGACY = "/api/embeddings"
MODERN = "/api/embed"


def _ok_for(url, body=None, n=1):
    if url.endswith(LEGACY):
        return FakeResponse(200, {"embedding": list(VEC)})
    return FakeResponse(200, {"embeddings": [list(VEC)]})


def _paths(recorder):
    return [url.rsplit("11434", 1)[-1] for url, _ in recorder.posts]


class EmbeddingCallTests(unittest.TestCase):
    def _run(self, responder, **kw):
        recorder = Recorder()
        ns = _load(recorder, responder, **kw)
        try:
            vector = asyncio.run(ns["get_embedding"]("hello"))
        except Exception as exc:  # noqa: BLE001 - the point is to inspect failures
            return recorder, None, exc
        return recorder, vector, None

    # -- the happy paths ---------------------------------------------------
    def test_single_request_on_success(self):
        recorder, vector, err = self._run(_ok_for)
        self.assertIsNone(err)
        self.assertEqual(vector, VEC)
        self.assertEqual(len(recorder.posts), 1)
        self.assertEqual(_paths(recorder), [LEGACY])

    def test_result_is_cached_per_model_and_text(self):
        # One namespace, so both calls share the same _EMBEDDING_CACHE.
        recorder = Recorder()
        ns = _load(recorder, _ok_for)
        asyncio.run(ns["get_embedding"]("hello"))
        self.assertEqual(len(recorder.posts), 1)
        asyncio.run(ns["get_embedding"]("hello"))
        self.assertEqual(len(recorder.posts), 1, "second call must hit the cache")

    def test_both_response_shapes_are_accepted(self):
        """A server that switches routes must not change the vector we get."""
        for url in (LEGACY, MODERN):
            with self.subTest(url=url):
                recorder, vector, err = self._run(lambda u, j, n, url=url: (
                    FakeResponse(200, {"embedding": list(VEC)}) if u.endswith(url)
                    else FakeResponse(404, {"error": "gone"})
                ))
                self.assertIsNone(err, err)
                self.assertEqual(vector, VEC)

    # -- transient failures ------------------------------------------------
    def test_transient_500_is_retried_and_then_succeeds(self):
        calls = {"n": 0}

        def responder(url, body, n):
            calls["n"] += 1
            return FakeResponse(500, {"error": "model loading"}) if calls["n"] == 1 else _ok_for(url)

        recorder, vector, err = self._run(responder, retries=2)
        self.assertIsNone(err, err)
        self.assertEqual(vector, VEC)
        self.assertEqual(_paths(recorder), [LEGACY, LEGACY], "must retry the same route first")

    def test_ollama_error_body_reaches_the_debug_log(self):
        """A recovered failure still explains itself, at DEBUG not WARNING."""
        calls = {"n": 0}

        def responder(url, body, n):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(500, {"error": "model requires more system memory"})
            return _ok_for(url)

        recorder, _, _ = self._run(responder, retries=1)
        self.assertTrue(
            any("requires more system memory" in d for d in recorder.debugs),
            f"Ollama's reason never reached the debug log: {recorder.debugs}",
        )
        self.assertEqual(recorder.warnings, [], "a recovered embed must not warn")

    def test_per_attempt_detail_never_warns(self):
        """Embedding is the high-volume path; it must not spam WARNING.

        Production runs at LOG_LEVEL=WARNING, so a degraded Ollama would emit a
        line per call and bury the chat traffic that level exists to surface.
        """
        calls = {"n": 0}

        def responder(url, body, n):
            calls["n"] += 1
            if calls["n"] < 4:
                return FakeResponse(503, {"error": "server busy"})
            return _ok_for(url)

        recorder, vector, err = self._run(responder, retries=2)
        self.assertIsNone(err, err)
        self.assertEqual(vector, VEC)
        self.assertEqual(recorder.warnings, [], f"embedding warned: {recorder.warnings}")

    def test_transport_error_is_retried(self):
        calls = {"n": 0}

        def responder(url, body, n):
            calls["n"] += 1
            if calls["n"] <= 2:
                return FakeHTTPError("connection reset")
            return _ok_for(url)

        recorder, vector, err = self._run(responder, retries=2)
        self.assertIsNone(err, err)
        self.assertEqual(vector, VEC)
        self.assertEqual(len(recorder.posts), 3)

    # -- fallback across routes -------------------------------------------
    def test_persistent_failure_falls_back_to_the_modern_route(self):
        def responder(url, body, n):
            return FakeResponse(500, {"error": "not implemented"}) if url.endswith(LEGACY) else _ok_for(url)

        recorder, vector, err = self._run(responder, retries=1)
        self.assertIsNone(err, err)
        self.assertEqual(vector, VEC)
        self.assertIn(MODERN, _paths(recorder))

    def test_removed_legacy_route_moves_straight_to_modern(self):
        """A 404 is not transient, so retrying it would only waste time."""
        def responder(url, body, n):
            return FakeResponse(404, {"error": "not found"}) if url.endswith(LEGACY) else _ok_for(url)

        recorder, vector, err = self._run(responder, retries=3)
        self.assertIsNone(err, err)
        self.assertEqual(_paths(recorder), [LEGACY, MODERN])

    def test_modern_route_receives_input_not_prompt(self):
        def responder(url, body, n):
            return FakeResponse(404, {"error": "gone"}) if url.endswith(LEGACY) else _ok_for(url)

        recorder, _, _ = self._run(responder)
        modern = [b for u, b in recorder.posts if u.endswith(MODERN)]
        self.assertTrue(modern and "input" in modern[0], modern)

    # -- terminal failures -------------------------------------------------
    def test_missing_model_is_not_retried_and_says_so(self):
        recorder, _, err = self._run(
            lambda url, body, n: FakeResponse(404, {"error": 'model "nomic-embed-text" not found'}),
            retries=3,
        )
        self.assertIsInstance(err, RuntimeError, "must not surface a raw httpx error")
        self.assertIn("not found", str(err))
        self.assertIn("ollama pull", str(err), "the message should name the fix")
        self.assertLessEqual(len(recorder.posts), 4, "a 4xx must not be hammered")

    def test_total_failure_names_the_model_and_keeps_ollamas_reason(self):
        recorder, _, err = self._run(
            lambda url, body, n: FakeResponse(500, {"error": "out of memory"}),
            retries=0,
        )
        self.assertIsInstance(err, RuntimeError)
        message = str(err)
        self.assertIn("nomic-embed-text", message)
        self.assertIn("out of memory", message)

    def test_total_failure_is_logged_once_as_an_error(self):
        """ERROR, exactly once, and never a WARNING.

        Both routes are tried, so a naive implementation would report the same
        failure twice and a per-attempt implementation once per retry.
        """
        recorder, _, err = self._run(
            lambda url, body, n: FakeResponse(500, {"error": "out of memory"}),
            retries=2,
        )
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(len(recorder.errors), 1, f"expected one error: {recorder.errors}")
        self.assertIn("out of memory", recorder.errors[0])
        self.assertIn("ollama pull", recorder.errors[0])
        self.assertEqual(recorder.warnings, [], f"a failure must not warn: {recorder.warnings}")

    def test_malformed_success_body_does_not_raise_keyerror(self):
        _, _, err = self._run(
            lambda url, body, n: FakeResponse(200, {"data": "nope"}),
            retries=0,
        )
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("no embedding in response", str(err))

    def test_non_json_body_is_reported_clearly(self):
        _, _, err = self._run(
            lambda url, body, n: FakeResponse(200, None, text="<html>gateway</html>"),
            retries=0,
        )
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("non-JSON", str(err))

    def test_empty_ollama_error_body_does_not_produce_a_bare_500(self):
        _, _, err = self._run(
            lambda url, body, n: FakeResponse(500, None, text=""),
            retries=0,
        )
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("empty response body", str(err))


class EmbedDetailTests(unittest.TestCase):
    def _detail(self, response):
        ns = {"getattr": getattr}
        with open(COMMON_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        segment = next(
            ast.get_source_segment(source, n)
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_ollama_detail"
        )
        scope = {}
        exec(segment, scope)  # noqa: S102
        return scope["_ollama_detail"](response)

    def test_json_error_field_is_preferred(self):
        self.assertEqual(
            self._detail(FakeResponse(500, {"error": "model not found"})),
            "model not found",
        )

    def test_json_without_error_falls_back_to_raw_text(self):
        self.assertEqual(self._detail(FakeResponse(500, {"foo": "bar"})), "{'foo': 'bar'}")

    def test_plain_text_body_is_returned(self):
        self.assertEqual(self._detail(FakeResponse(502, None, text="upstream down")), "upstream down")

    def test_empty_body_is_labelled(self):
        self.assertEqual(self._detail(FakeResponse(500, None, text="  ")), "(empty response body)")


class OversizedInputTests(unittest.TestCase):
    """Ollama answers an over-long input with a 500, not a 4xx.

    That is what killed ``diary_save_entry`` in production a second time: the
    retry loop dutifully re-sent the same oversized text five times, then failed
    with a message that did not mention length at all. These pin the properties
    that make the call succeed instead.
    """

    TOO_LONG = "the input length exceeds the context length"

    def _run_text(self, text, responder, **kw):
        recorder = Recorder()
        ns = _load(recorder, responder, **kw)
        try:
            return recorder, ns, asyncio.run(ns["get_embedding"](text)), None
        except Exception as exc:  # noqa: BLE001
            return recorder, ns, None, exc

    def test_text_over_the_budget_is_truncated_before_sending(self):
        recorder, _, _, err = self._run_text("x" * 20000, _ok_for, max_chars=1000)
        self.assertIsNone(err, err)
        sent = recorder.posts[0][1]["prompt"]
        self.assertLessEqual(len(sent), 1000)
        self.assertTrue(
            any("truncating" in w for w in recorder.warnings),
            f"an over-budget embed must warn: {recorder.warnings}",
        )

    def test_truncation_keeps_head_and_tail(self):
        # A transcription puts the subject first and the conclusions last, and a
        # search query is far more likely to match the tail.
        text = "HEADMARKER" + ("-" * 5000) + "TAILMARKER"
        recorder, _, _, err = self._run_text(text, _ok_for, max_chars=1000)
        self.assertIsNone(err, err)
        sent = recorder.posts[0][1]["prompt"]
        self.assertIn("HEADMARKER", sent)
        self.assertIn("TAILMARKER", sent)

    def test_short_text_is_sent_verbatim(self):
        recorder, _, _, err = self._run_text("a short note", _ok_for, max_chars=1000)
        self.assertIsNone(err, err)
        self.assertEqual(recorder.posts[0][1]["prompt"], "a short note")

    def test_too_long_response_shrinks_the_input_and_succeeds(self):
        """The budget is a guess at the real window, so the reply can disagree."""
        def responder(url, body, n):
            payload = body.get("prompt") or body.get("input") or ""
            if len(payload) > 400:
                return FakeResponse(500, {"error": self.TOO_LONG})
            return _ok_for(url, body, n)

        recorder, _, vector, err = self._run_text("y" * 3000, responder, max_chars=1000, retries=3)
        self.assertIsNone(err, err)
        self.assertEqual(vector, VEC)
        sizes = [len(b.get("prompt") or b.get("input") or "") for _, b in recorder.posts]
        self.assertTrue(sizes[0] > sizes[-1], f"input never shrank: {sizes}")

    def test_too_long_does_not_spend_the_retry_budget(self):
        """Re-sending the same bytes cannot clear a length error."""
        recorder, _, _, err = self._run_text(
            "z" * 5000, lambda url, body, n: FakeResponse(500, {"error": self.TOO_LONG}),
            max_chars=1000, retries=2,
        )
        self.assertIsInstance(err, RuntimeError)
        # A shrink loop, not a backoff loop: bounded by the halvings, and the
        # two routes only add one more attempt each.
        self.assertLessEqual(len(recorder.posts), 8, f"length error was retried: {len(recorder.posts)} posts")

    def test_input_too_long_survives_the_retry_and_fallback_routes(self):
        # The 500 would otherwise be read as retryable and burn the backoff.
        recorder, _, _, err = self._run_text(
            "w" * 5000, lambda url, body, n: FakeResponse(500, {"error": self.TOO_LONG}),
            max_chars=1000, retries=1,
        )
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("exceeds the context length", str(err))
        self.assertEqual(recorder.slept, 0.0, "a length error must not sleep between attempts")
        self.assertEqual(_paths(recorder)[-1], MODERN, "should still try the other route")

    def test_length_error_is_recognised_not_treated_as_transient(self):
        ns = _load(Recorder(), _ok_for)
        for detail in ("HTTP 500: the input length exceeds the context length", "maximum context"):
            self.assertTrue(ns["_is_input_too_long"](detail), detail)
        for detail in ("HTTP 500: out of memory", "HTTP 404: model not found", ""):
            self.assertFalse(ns["_is_input_too_long"](detail), detail)

    def test_cached_key_is_the_original_text_not_the_truncated_one(self):
        # Otherwise a search whose query truncates onto a stored fact's prefix
        # would return that fact's vector as its own answer.
        recorder = Recorder()
        ns = _load(recorder, _ok_for, max_chars=1000)
        long_text = "q" * 20000
        asyncio.run(ns["get_embedding"](long_text))
        asyncio.run(ns["get_embedding"](long_text))
        self.assertEqual(len(recorder.posts), 1, "the long text must still be cached")
        self.assertIn(("nomic-embed-text", long_text), ns["_EMBEDDING_CACHE"])


_HERE = os.path.dirname(os.path.abspath(__file__))

# Every path that persists a fact or diary entry. Each must embed before it
# touches Neo4j, so a failed embed leaves the graph and the vector store
# agreeing on the old text.
WRITE_PATHS = (
    ("fact_manager.py", "db_add_memory"),
    ("fact_manager.py", "db_update_memory"),
    ("diary_manager.py", "db_save_diary"),
    ("diary_manager.py", "db_update_diary"),
)

# Reads, ownership checks and the initial ``MATCH ... RETURN`` are not writes.
_MUTATES = re.compile(r"\b(MERGE|CREATE|DELETE|DETACH|SET\b|REMOVE)\b")


def _function_source(name, body):
    lines = body.split("\n")
    start = next(i for i, line in enumerate(lines) if re.match(rf"async def {name}\(", line))
    try:
        end = next(i for i in range(start + 1, len(lines)) if re.match(r"async def ", lines[i]))
    except StopIteration:
        end = len(lines)
    return "\n".join(lines[start:end])


class WriteOrderingGuardTests(unittest.TestCase):
    """A failed embed must never leave a half-written record.

    These paths cannot run here — they need Neo4j, Qdrant and Ollama — so the
    invariant is asserted against the source. ``db_update_diary`` had the two
    steps the other way round, which left Neo4j holding text the vector store
    could no longer find; nothing about that failure is loud.
    """

    def _read(self, module):
        with open(os.path.join(_HERE, module), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_embed_precedes_the_neo4j_write(self):
        for module, func in WRITE_PATHS:
            with self.subTest(f"{module}::{func}"):
                body = _function_source(func, self._read(module))
                hits = [
                    body.find(needle)
                    for needle in ("_upsert_fact_points(", "_upsert_diary_points(")
                    if body.find(needle) != -1
                ]
                self.assertTrue(hits, f"{func} never embeds")
                embed = min(hits)
                # A read-only session.run (ownership check) is not a write.
                writes = [
                    m.start()
                    for m in re.finditer(r"\.run\(", body)
                    if _MUTATES.search(body[m.start():body.find(")", m.start()) + 400])
                ]
                if not writes:
                    self.skipTest(f"{func} writes to Neo4j through a helper, not an inline query")
                self.assertLess(
                    embed, writes[0],
                    f"{func} writes to Neo4j before it embeds — a failed embed would "
                    f"leave the two stores describing different text",
                )

    def test_no_write_path_embeds_without_an_upsert(self):
        """A bare get_embedding whose result is then discarded."""
        for module, func in WRITE_PATHS:
            with self.subTest(f"{module}::{func}"):
                body = _function_source(func, self._read(module))
                self.assertNotIn(
                    "await get_embedding(", body,
                    f"{func} embeds directly; route it through the _upsert_* helper so "
                    f"the chunk family and the delete-then-upsert order stay correct",
                )


if __name__ == "__main__":
    unittest.main()

class OllamaModelMatchTests(unittest.TestCase):
    """The boot-time model check must not re-pull what is already installed.

    Ollama reports a tagless model as ``name:latest`` (and an untagged one as
    ``name:any``) while the configuration says just ``name``. Set membership
    therefore never matched, and every restart re-downloaded a 274MB embedder --
    23 times in the week this was found.
    """

    def setUp(self):
        self._matches = _load(Recorder(), _ok_for)["_ollama_model_matches"]

    def test_a_tagless_config_name_matches_the_latest_tag(self):
        # The exact case from the log: nomic-embed-text vs nomic-embed-text:latest
        self.assertTrue(self._matches({"nomic-embed-text:latest"}, "nomic-embed-text"))

    def test_a_tagless_config_name_matches_the_any_tag(self):
        self.assertTrue(self._matches({"llama3:any"}, "llama3"))

    def test_an_exact_tagged_name_still_matches(self):
        self.assertTrue(self._matches({"qwen3.5:2b"}, "qwen3.5:2b"))
        self.assertTrue(self._matches({"gemma4:e2b"}, "gemma4:e2b"))

    def test_a_different_tag_is_not_the_same_model(self):
        # 0.8b and 2b are genuinely different models; matching them would leave
        # the vault embedded with the wrong one.
        self.assertFalse(self._matches({"qwen3.5:2b"}, "qwen3.5:0.8b"))
        self.assertFalse(self._matches({"gemma4:e2b"}, "gemma4:4b"))

    def test_a_different_model_is_not_a_match(self):
        self.assertFalse(self._matches({"nomic-embed-text:latest"}, "qwen3.5:2b"))

    def test_a_missing_model_is_not_a_match(self):
        self.assertFalse(self._matches(set(), "nomic-embed-text"))

    def test_an_empty_name_never_matches(self):
        self.assertFalse(self._matches({"nomic-embed-text:latest"}, ""))
        self.assertFalse(self._matches({"nomic-embed-text:latest"}, "   "))
        self.assertFalse(self._matches({"nomic-embed-text:latest"}, None))

    def test_a_colonless_installed_name_matches_a_tagless_wanted(self):
        # Defensive: a proxy in front of Ollama could report a bare name.
        self.assertTrue(self._matches({"nomic-embed-text"}, "nomic-embed-text"))

    def test_the_real_installed_set_from_the_production_log(self):
        installed = {
            "nomic-embed-text:latest", "qwen3.5:2b", "qwen3.5:0.8b",
            "nomic-ea:latest", "gemma4:e2b",
        }
        for wanted in ("nomic-embed-text", "qwen3.5:2b", "qwen3.5:0.8b", "gemma4:e2b"):
            self.assertTrue(self._matches(installed, wanted), wanted)
        self.assertFalse(self._matches(installed, "gemma4:e4b"))

    def test_ensure_ollama_models_actually_calls_the_helper(self):
        """Pin the call site.

        The tests above exercise the helper directly, so re-injecting the old
        ``if model in installed`` at the call site leaves all nine green --
        verified by doing exactly that. The download bug lives in the call
        site, so the call site is what has to be asserted.
        """
        with open(COMMON_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        segment = next(
            ast.get_source_segment(source, n)
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "ensure_ollama_models"
        )
        self.assertIn("_ollama_model_matches(installed, model)", segment)
        # The old form must not survive anywhere in the function.
        self.assertNotRegex(segment, r"if\s+model\s+in\s+installed\s*:")
