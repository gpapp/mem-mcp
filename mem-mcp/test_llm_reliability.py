"""Tests for the Ollama chat path (`get_llm_response`).

Why these exist
---------------
A reclassify returned 503 and was read as "out of memory loading the model". It
was neither: `get_llm_response` had a hardcoded `httpx.AsyncClient(timeout=60.0)`
and no exception handling at all, so a chat that took longer than 60s raised a
bare `httpx.ReadTimeout` and wrote *nothing*. The log showed the request line,
then a 60.07s gap, then the next request — which is the timeout firing, with no
record of it. Meanwhile `gui.py` converted every `RuntimeError` into a 503
without logging the `detail`, so the real reason reached neither the log file
nor stdout.

The tests below are behavioural, not source-shape checks, because the defect
was the *absence* of a log line. A source assertion can only confirm a string is
present; here the fake client actually raises, and the assertion is that the
failure is recorded and that the message reaches the caller.

`common.py` cannot be imported without the DB drivers, so the real function is
lifted out with `ast.get_source_segment` and exec'd against stubs — this tests
the shipping code, not a copy of it.
"""

import ast
import asyncio
import os
import unittest

COMMON_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "common.py")
GUI_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")

_FUNCTION = "get_llm_response"
_ASSIGNMENTS = ("LLM_TIMEOUT", "LLM_CONNECT_TIMEOUT", "SEARCH_LLM_TIMEOUT")


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class FakeTimeout:
    """httpx.Timeout is a value object; the real one is not importable here."""

    def __init__(self, read, connect=None):
        self.read = read
        self.connect = connect

    def __repr__(self):
        return f"Timeout(read={self.read!r}, connect={self.connect!r})"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, is_error=False):
        self.status_code = status_code
        self._payload = payload
        self._text = text
        self._is_error = is_error

    @property
    def text(self):
        if self._text is not None:
            return self._text
        import json as _json
        return _json.dumps(self._payload or {})

    @property
    def is_error(self):
        return self._is_error

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeTimeoutException(Exception):
    """Stands in for httpx.TimeoutException; the code only catches by name."""


class FakeHTTPError(Exception):
    """Stands in for httpx.HTTPError, the base of TimeoutException."""


class Recorder:
    def __init__(self):
        self.posts = []
        self.client_timeouts = []
        self.warnings = []
        self.errors = []


class FakeLogger:
    def __init__(self, rec):
        self._rec = rec

    def warning(self, msg, *a, **kw):
        self._rec.warnings.append(str(msg))

    def error(self, msg, *a, **kw):
        self._rec.errors.append(str(msg))

    def debug(self, msg, *a, **kw):
        pass


def _load(recorder, responder, **overrides):
    """Exec the real get_llm_response against stubs. Returns a namespace."""
    with open(COMMON_PY, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    chunks = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _FUNCTION:
            chunks.append(ast.get_source_segment(source, node))
            found.add(node.name)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in _ASSIGNMENTS for t in targets):
                chunks.append(ast.get_source_segment(source, node))
    if _FUNCTION not in found:
        raise AssertionError(f"common.py no longer defines {_FUNCTION}")

    class FakeClient:
        def __init__(self, *a, **kw):
            self._timeout = kw.get("timeout")
            recorder.client_timeouts.append(self._timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            recorder.posts.append((url, dict(json) if json else json))
            result = responder(url, json, len(recorder.posts))
            if isinstance(result, Exception):
                raise result
            return result

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            self._c = FakeClient(*a, **kw)

        def __aenter__(self):
            return self._c.__aenter__()

        def __aexit__(self, *exc):
            return self._c.__aexit__(*exc)

    class FakeHttpx:
        AsyncClient = FakeAsyncClient
        Timeout = FakeTimeout
        TimeoutException = FakeTimeoutException
        HTTPError = FakeHTTPError

    import time as _time

    namespace = {
        "os": os,
        "httpx": FakeHttpx,
        "logger": FakeLogger(recorder),
        "asyncio": asyncio,
        "time": _time,
        "LLM_QUERY_MODEL": "qwen3.5:0.8b",
        "OLLAMA_URL": "http://ollama:11434",
    }
    # Defaults are not seeded here: the lifted assignments below are real
    # `float(os.getenv(...))` calls, so they compute the shipping defaults at
    # exec time and would clobber anything passed in. Overrides go on after.
    exec("\n\n".join(chunks), namespace)
    namespace.update(overrides)
    return namespace


def _ok(content="ok"):
    return lambda url, body, n: FakeResponse(
        200, {"message": {"content": content}}
    )


# ---------------------------------------------------------------------------
# The defect: a timeout used to be silent
# ---------------------------------------------------------------------------
class TimeoutLoggingTests(unittest.TestCase):
    """A chat that hits its budget must leave a trace and a usable message."""

    def setUp(self):
        self.rec = Recorder()

    def _timed_out(self):
        ns = _load(self.rec, lambda u, b, n: FakeTimeoutException("read timeout"))
        return ns

    def test_a_timeout_is_logged_at_error(self):
        ns = self._timed_out()
        with self.assertRaises(RuntimeError):
            asyncio.run(ns[_FUNCTION]("a " * 200))
        self.assertTrue(
            self.rec.errors, "a chat timeout wrote no ERROR line at all — this is the defect"
        )

    def test_the_timeout_log_names_the_model_prompt_and_budget(self):
        ns = _load(
            self.rec,
            lambda u, b, n: FakeTimeoutException("read timeout"),
            LLM_QUERY_MODEL="qwen3.5:2b",
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(ns[_FUNCTION]("a " * 200))
        blob = " ".join(self.rec.errors)
        self.assertIn("qwen3.5:2b", blob)
        self.assertIn("prompt_chars", blob)
        self.assertIn("300", blob)

    def test_the_timeout_log_points_at_the_knob(self):
        ns = self._timed_out()
        with self.assertRaises(RuntimeError):
            asyncio.run(ns[_FUNCTION]("x"))
        self.assertIn("MEM_LLM_TIMEOUT", " ".join(self.rec.errors))

    def test_the_raised_error_names_the_model_and_the_knob(self):
        ns = _load(self.rec, lambda u, b, n: FakeTimeoutException("read timeout"))
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(ns[_FUNCTION]("x"))
        detail = str(ctx.exception)
        self.assertIn("qwen3.5:0.8b", detail)
        self.assertIn("MEM_LLM_TIMEOUT", detail)

    def test_a_timeout_is_raised_as_a_runtime_error_not_a_bare_httpx_one(self):
        """RuntimeError is what the API layer turns into a 503 with the detail.

        A bare httpx.ReadTimeout escaped every handler, so the user saw a 500
        with no explanation and the operator saw a log with a hole in it.
        """
        ns = self._timed_out()
        with self.assertRaises(RuntimeError):
            asyncio.run(ns[_FUNCTION]("x"))


# ---------------------------------------------------------------------------
# The regression: the hardcoded 60s
# ---------------------------------------------------------------------------
class TimeoutBudgetTests(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()

    def _budget(self, **overrides):
        ns = _load(self.rec, _ok(), **overrides)
        asyncio.run(ns[_FUNCTION]("x"))
        return self.rec.client_timeouts[0]

    def test_the_read_budget_is_the_configured_default_not_sixty_seconds(self):
        """The literal that caused this was `timeout=60.0` in the source."""
        budget = self._budget()
        self.assertEqual(budget.read, 300.0)

    def test_the_configured_default_is_actually_honoured(self):
        budget = self._budget(LLM_TIMEOUT=45.0)
        self.assertEqual(budget.read, 45.0)

    def test_a_per_call_timeout_overrides_the_default(self):
        """The search rewrite must not inherit the background budget."""
        ns = _load(self.rec, _ok(), LLM_TIMEOUT=300.0)
        asyncio.run(ns[_FUNCTION]("x", timeout=45.0))
        self.assertEqual(self.rec.client_timeouts[0].read, 45.0)

    def test_the_connect_budget_is_short_and_independent(self):
        """A dead Ollama must fail fast rather than burn the full read budget."""
        budget = self._budget(LLM_TIMEOUT=300.0, LLM_CONNECT_TIMEOUT=10.0)
        self.assertEqual(budget.connect, 10.0)
        self.assertLess(budget.connect, budget.read)

    def test_the_source_no_longer_hardcodes_sixty_seconds(self):
        with open(COMMON_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        for node in tree.body:
            if getattr(node, "name", None) == _FUNCTION:
                segment = ast.get_source_segment(source, node)
                self.assertNotIn("60.0", segment, msg=(
                    f"a hardcoded 60s timeout is back in {_FUNCTION}; measured production "
                    f"had a 4420-char prompt take 46s, so a 60s cap is not a safety margin"
                ))
                return
        self.fail(f"{_FUNCTION} not found")


# ---------------------------------------------------------------------------
# The other failure modes that were also silent
# ---------------------------------------------------------------------------
class OtherFailureTests(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()

    def test_a_transport_error_is_logged_and_names_the_pull_fix(self):
        ns = _load(self.rec, lambda u, b, n: FakeHTTPError("connection refused"))
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(ns[_FUNCTION]("x"))
        self.assertIn("ollama pull", str(ctx.exception))
        self.assertTrue(self.rec.errors)

    def test_an_http_error_status_reports_the_body(self):
        body = "model requires more system memory"
        ns = _load(
            self.rec,
            lambda u, b, n: FakeResponse(500, text=body, is_error=True),
        )
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(ns[_FUNCTION]("x"))
        self.assertIn("500", str(ctx.exception))
        self.assertTrue(
            any(body in e for e in self.rec.errors),
            msg="Ollama's own reason is the whole diagnostic and it was dropped",
        )

    def test_a_body_that_is_not_a_chat_message_does_not_raise_keyerror(self):
        ns = _load(self.rec, lambda u, b, n: FakeResponse(200, text="not json"))
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(ns[_FUNCTION]("x"))
        self.assertNotIsInstance(ctx.exception, KeyError)

    def test_an_empty_answer_is_logged_loudly_but_still_returned(self):
        """A model that loaded and produced nothing is the memory-pressure shape.

        It used to return "" in total silence, and callers fell back to the raw
        query or to no keywords, so the vault just quietly got worse. Raising
        here would break those deliberate fallbacks, so the fix is to log it.
        """
        ns = _load(self.rec, _ok(content="   "))
        result = asyncio.run(ns[_FUNCTION]("x"))
        self.assertEqual(result, "")
        self.assertTrue(
            self.rec.errors, "an empty LLM answer must be an ERROR, not silence"
        )


# ---------------------------------------------------------------------------
# The happy path still works
# ---------------------------------------------------------------------------
class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()

    def test_a_normal_call_returns_the_content(self):
        ns = _load(self.rec, _ok("Deutsche Bank (DB)"))
        self.assertEqual(asyncio.run(ns[_FUNCTION]("x")), "Deutsche Bank (DB)")

    def test_the_model_override_is_used(self):
        ns = _load(self.rec, _ok("x"))
        asyncio.run(ns[_FUNCTION]("p", model="gemma4:e2b"))
        _, body = self.rec.posts[0]
        self.assertEqual(body["model"], "gemma4:e2b")

    def test_num_predict_is_forwarded(self):
        ns = _load(self.rec, _ok("x"))
        asyncio.run(ns[_FUNCTION]("p", num_predict=80))
        _, body = self.rec.posts[0]
        self.assertEqual(body["options"]["num_predict"], 80)

    def test_think_blocks_are_stripped(self):
        ns = _load(self.rec, _ok("<think>hmm</think>  Paris  "))
        self.assertEqual(asyncio.run(ns[_FUNCTION]("x")), "Paris")

    def test_the_request_line_reports_the_budget(self):
        """So a log reader can tell a 300s wait from a 45s one."""
        ns = _load(self.rec, _ok("x"))
        asyncio.run(ns[_FUNCTION]("x", timeout=45.0))
        self.assertTrue(
            any("timeout_s=45" in w for w in self.rec.warnings),
            msg=f"no timeout_s in the request lines: {self.rec.warnings}",
        )


# ---------------------------------------------------------------------------
# gui.py: a 503 must say why
# ---------------------------------------------------------------------------
class ServiceUnavailableLoggingTests(unittest.TestCase):
    """Every 503 in gui.py is produced by this helper.

    The reason used to be formatted into an HTTPException and thrown away. The
    access log recorded the status; nothing recorded the cause. The most
    informative message in the codebase — "Another maintenance operation is
    running" — was the one that never got logged.
    """

    def _gui_source(self):
        with open(GUI_PY, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_no_bare_503_without_logging_survives(self):
        source = self._gui_source()
        self.assertNotIn(
            "raise HTTPException(status_code=503, detail=str(e))", source
        )

    def test_every_503_goes_through_the_helper(self):
        count = self._gui_source().count("raise _service_unavailable(e)")
        self.assertGreater(
            count, 0, msg="no 503 routes through the logging helper at all"
        )

    def test_the_helper_logs_before_it_builds_the_response(self):
        source = self._gui_source()
        start = source.find("def _service_unavailable(")
        self.assertNotEqual(start, -1, msg="the 503 logging helper is missing")
        segment = source[start:source.find("\ndef ", start + 10)]
        self.assertIn("error", segment, msg=(
            "the helper must log the reason; a 503 with no logged cause is what "
            "made this failure take a log dive to explain"
        ))
        self.assertIn("HTTPException(status_code=503", segment)

    def test_the_helper_returns_an_exception_rather_than_raising(self):
        """`raise _service_unavailable(e)` needs a return, not a bare raise."""
        source = self._gui_source()
        start = source.find("def _service_unavailable(")
        segment = source[start:source.find("\ndef ", start + 10)]
        self.assertIn("return HTTPException(", segment)


if __name__ == "__main__":
    unittest.main()
