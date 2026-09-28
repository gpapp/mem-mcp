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
import unittest

COMMON_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "common.py")

_FUNCTIONS = ("_ollama_detail", "_embed_once", "get_embedding")
_ASSIGNMENTS = (
    "_EMBEDDING_CACHE",
    "EMBED_CACHE_MAX",
    "EMBED_RETRIES",
    "EMBED_RETRY_BACKOFF",
    "_EMBED_RETRY_STATUS",
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
    """Collects POSTs and the warnings the code under test logs."""

    def __init__(self):
        self.posts = []
        self.warnings = []
        self.slept = 0.0


def _load(recorder, responder, *, retries=None, backoff=0.0):
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
            recorder.posts.append((url, json))
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

        def debug(self, *a, **kw):
            pass

        def error(self, *a, **kw):
            pass

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

    def test_ollama_error_body_reaches_the_log(self):
        """The whole point: a bare 500 told us nothing."""
        calls = {"n": 0}

        def responder(url, body, n):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(500, {"error": "model requires more system memory"})
            return _ok_for(url)

        recorder, _, _ = self._run(responder, retries=1)
        self.assertTrue(
            any("requires more system memory" in w for w in recorder.warnings),
            f"Ollama's reason never reached the log: {recorder.warnings}",
        )

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


if __name__ == "__main__":
    unittest.main()
