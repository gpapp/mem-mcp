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
import re
import unittest

COMMON_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "common.py")
GUI_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")

_FUNCTION = "get_llm_response"
_HELPERS = ("_llm_excerpt",)
_ASSIGNMENTS = ("LLM_TIMEOUT", "LLM_CONNECT_TIMEOUT", "SEARCH_LLM_TIMEOUT", "LLM_LOG_CHARS")


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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == _FUNCTION or node.name in _HELPERS
        ):
            chunks.append(ast.get_source_segment(source, node))
            found.add(node.name)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in _ASSIGNMENTS for t in targets):
                chunks.append(ast.get_source_segment(source, node))
    if _FUNCTION not in found:
        raise AssertionError(f"common.py no longer defines {_FUNCTION}")
    for name in _HELPERS:
        if name not in found:
            raise AssertionError(f"common.py no longer defines {name}")

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


def _def_segment(source, name):
    """One top-level function, up to the next top-level construct.

    Slicing to the next `\\ndef ` is wrong: a function followed by the
    two-blank-line PEP8 top-level gap has no `\\ndef ` after it, so the
    slice runs to end-of-file. An `assertIn` cannot tell -- it passes on a
    segment far too long -- and only an `assertNotIn` notices, by then
    scanning the rest of the file.
    """
    start = source.find(f"def {name}(")
    if start == -1:
        return ""
    m = re.compile(r"^(?:@|def |class |async def )", re.M).search(source, start + 1)
    return source[start:m.start()] if m else source[start:]


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


class ContentLoggingTests(unittest.TestCase):
    """The request and the answer must be recoverable from the log alone.

    Only the character counts were logged, so a mis-scoped or hallucinating
    answer could not be inspected after the fact — you had to reproduce the call
    to see what the model had actually been shown and said. That is exactly the
    question you want answered when a reclassify produces a wrong link.

    Note this writes entry and meeting text to the log file, which is why
    MEM_LLM_LOG_CHARS=0 turns it off and why the truncation is capped.
    """

    def setUp(self):
        self.rec = Recorder()

    def _excerpt(self, **overrides):
        ns = _load(self.rec, _ok(), **overrides)
        return ns["_llm_excerpt"]

    # --- the excerpt helper -------------------------------------------------
    def test_short_text_is_untouched(self):
        ex = self._excerpt()
        self.assertEqual(ex("hello"), "hello")

    def test_a_prompt_that_fits_is_not_marked_as_truncated(self):
        ex = self._excerpt()
        self.assertNotIn("more chars", ex("a" * 1000))

    def test_long_text_is_capped_at_the_budget(self):
        ex = self._excerpt()
        out = ex("a" * 5000)
        self.assertTrue(out.startswith("a" * 1000))
        self.assertIn("+4000 more chars", out)

    def test_the_elision_marker_reports_the_total_length(self):
        """The first question is always 'how much is there that I can't see'."""
        ex = self._excerpt()
        self.assertIn("+4000 more chars", ex("a" * 5000))

    def test_newlines_are_escaped_so_one_event_stays_one_line(self):
        ex = self._excerpt()
        out = ex("line one\nline two\r\nline three\ttabbed")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertIn("\\n", out)
        self.assertIn("\\t", out)

    def test_backslashes_are_escaped_before_the_whitespace_rules(self):
        ex = self._excerpt()
        self.assertEqual(ex("a\\b"), "a\\\\b")

    def test_a_limit_of_zero_logs_sizes_only(self):
        ex = self._excerpt(LLM_LOG_CHARS=0)
        self.assertEqual(ex("secret content"), "")

    def test_an_explicit_limit_overrides_the_default(self):
        ex = self._excerpt()
        self.assertTrue(ex("a" * 100, 10).startswith("a" * 10))
        self.assertIn("+90 more chars", ex("a" * 100, 10))

    # --- the content actually reaches the log line -------------------------
    def test_the_prompt_reaches_the_log(self):
        ns = _load(self.rec, _ok("x"))
        asyncio.run(ns[_FUNCTION]("Find the client named Deutsche Bank."))
        joined = " ".join(self.rec.warnings)
        self.assertIn("Find the client named Deutsche Bank.", joined)

    def test_the_system_prompt_reaches_the_log(self):
        ns = _load(self.rec, _ok("x"))
        asyncio.run(ns[_FUNCTION]("q", system="You are a scope classifier."))
        joined = " ".join(self.rec.warnings)
        self.assertIn("You are a scope classifier.", joined)

    def test_the_answer_reaches_the_log(self):
        ns = _load(self.rec, _ok('{"client": "Deutsche Bank (DB)"}'))
        asyncio.run(ns[_FUNCTION]("q"))
        self.assertTrue(
            any("Deutsche Bank (DB)" in w for w in self.rec.warnings),
            msg="the model's answer is not in the log, only its length",
        )

    def test_a_huge_prompt_does_not_land_in_the_log_unbounded(self):
        """A 40k-char entry must not put 40k chars on every reclassify line."""
        ns = _load(self.rec, _ok("x"))
        asyncio.run(ns[_FUNCTION]("q" * 40000))
        request_line = next(
            w for w in self.rec.warnings if "Ollama request" in w
        )
        self.assertIn("+39000 more chars", request_line)
        self.assertLess(len(request_line), 1400)

    def test_every_logged_line_stays_single_line(self):
        """One logical event, one line — otherwise grep reads fragments."""
        ns = _load(self.rec, _ok("line one\nline two"))
        asyncio.run(ns[_FUNCTION]("first\nsecond\nthird", system="sys\nprompt"))
        for line in self.rec.warnings:
            self.assertNotIn("\n", line, msg=f"embedded newline in: {line!r}")

    def test_content_logging_can_be_switched_off_end_to_end(self):
        ns = _load(self.rec, _ok("SENSITIVE ANSWER"), LLM_LOG_CHARS=0)
        asyncio.run(ns[_FUNCTION]("SENSITIVE PROMPT"))
        joined = " ".join(self.rec.warnings)
        self.assertNotIn("SENSITIVE PROMPT", joined)
        self.assertNotIn("SENSITIVE ANSWER", joined)
        # the sizes are still there, which is the point of the knob
        self.assertIn("prompt_chars=", joined)
        self.assertIn("content_chars=", joined)


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
        self.assertIn("def _service_unavailable(",
                      source, msg="the 503 logging helper is missing")
        segment = _def_segment(source, "_service_unavailable")
        self.assertIn("error", segment, msg=(
            "the helper must log the reason; a 503 with no logged cause is what "
            "made this failure take a log dive to explain"
        ))
        self.assertIn("HTTPException(status_code=503", segment)

    def test_the_helper_returns_an_exception_rather_than_raising(self):
        """`raise _service_unavailable(e)` needs a return, not a bare raise."""
        source = self._gui_source()
        segment = _def_segment(source, "_service_unavailable")
        self.assertIn("return HTTPException(", segment)


# ---------------------------------------------------------------------------
# gui.py + dashboard.html: a 409 must say why, on both ends
# ---------------------------------------------------------------------------
class ConflictLoggingTests(unittest.TestCase):
    """The 409 sibling of the 503 rule above, plus the half that makes it reach
    the user at all.

    A 409 on a single-item reclassify is what prompted this: the access log said
    `409 Conflict` and nothing else, and the browser said "Reclassify failed".
    The server had built an actionable message the whole time --
    "Fact ... has a client/project set manually. Clear the client/project first"
    -- and it was discarded at *both* ends independently, so neither the log nor
    the screen could show it.

    Both halves are asserted, because fixing one alone leaves the user exactly
    as informed as before: logging the reason nobody can read, or showing a
    reason the server never emitted.
    """

    TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "dashboard.html")

    def _gui_source(self):
        with open(GUI_PY, "r", encoding="utf-8") as handle:
            return handle.read()

    def _html(self):
        with open(self.TEMPLATE, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_no_bare_409_without_logging_survives(self):
        source = self._gui_source()
        self.assertNotIn("raise HTTPException(status_code=409, detail=str(e))", source)

    def test_every_409_goes_through_the_helper(self):
        self.assertGreater(self._gui_source().count("raise _conflict(e)"), 0,
                           msg="no 409 routes through the logging helper at all")

    def test_the_helper_logs_and_returns_rather_than_raising(self):
        source = self._gui_source()
        self.assertIn("def _conflict(", source,
                      msg="the 409 logging helper is missing")
        segment = _def_segment(source, "_conflict")
        self.assertIn("info(", segment,
                      msg="a 409 with no logged cause is a bare status in the access log")
        self.assertIn("HTTPException(status_code=409", segment)
        self.assertIn("return HTTPException(", segment)

    def test_the_helper_logs_at_info_not_error(self):
        """A refusal is the guard working. ERROR would bury it in real failures."""
        source = self._gui_source()
        segment = _def_segment(source, "_conflict")
        self.assertNotIn(".error(", segment,
                         "a 409 is an expected refusal, not a failure")

    def test_a_rejected_call_carries_the_servers_detail(self):
        """`Promise.reject(r)` handed back a bare Response.

        The reason lives in the JSON body, so `.status` worked and nothing else
        did. The body is consumed lazily, so it cannot be recovered later by a
        caller that did not know to ask for it.
        """
        html = self._html()
        self.assertFalse(
            "Promise.reject(r)" in html,
            "a failed api call is rejecting with the bare Response again, so the "
            "detail in the body is unreachable")
        self.assertIn("async function apiFail(", html)
        self.assertIn("err.detail = detail;", html,
                      "apiFail must attach the parsed detail for callers to read")
        self.assertIn("err.status = r.status;", html,
                      "nine call sites branch on e.status, so it must survive")

    def test_every_api_verb_goes_through_the_shared_failure_path(self):
        """A new verb added with the old idiom silently drops its reason again."""
        html = self._html()
        for verb in ("get", "post", "put", "delete"):
            line = re.search(rf"^\s*{verb}:\s*(.+)$", html, re.M)
            self.assertIsNotNone(line, f"the api object no longer defines {verb}")
            self.assertIn("apiFail(r)", line.group(1),
                          f"api.{verb} does not route its failure through apiFail")

    def test_the_reclassify_toasts_show_the_reason(self):
        html = self._html()
        self.assertNotIn("toast('⚠️ Reclassify failed')", html,
                         "the reclassify handlers still throw the reason away")
        self.assertEqual(html.count("toastApiError(e, 'Reclassify failed')"), 2,
                         "both the fact and the diary reclassify handler must surface it")

    def test_toast_api_error_falls_back_when_there_is_no_detail(self):
        """Preferring the server's text must not make the common case silent."""
        html = self._html()
        start = html.find("function toastApiError(")
        self.assertNotEqual(start, -1, msg="toastApiError is missing")
        end = html.find("\n  }", start)
        segment = html[start:end]
        self.assertIn("e.detail", segment, "the server's detail is never read")
        self.assertIn("fallback", segment, "there is no message for a detail-less failure")


if __name__ == "__main__":
    unittest.main()
