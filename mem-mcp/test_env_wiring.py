"""Every documented knob must actually reach the container.

`.env.example` and `docker-compose.yml` are two files with an implicit contract
between them, and nothing enforced it. Docker Compose only passes variables that
appear in a service's `environment:` block, so a knob documented in
`.env.example` but absent from that block is unreachable: it sits at its code
default forever, and editing `.env` does nothing at all.

That is exactly what happened here. 24 documented variables were unreachable,
including `MEM_LLM_TIMEOUT` -- documented in AGENTS.md as the fix for the 60s
chat timeout, and genuinely untunable, because the only way to change it would
have been to add it to the compose block, which nobody had done. A knob that
cannot be set is worse than an undocumented one: the documentation is a claim
about the system that is false.

The failure mode is silent, which is the argument for a test. There is no error
when a variable is dropped from the compose file -- the app starts normally, the
default applies, and the discrepancy is only visible by diffing two files that
have no structural relationship. So this suite derives the set from
`.env.example` rather than hardcoding it, which means a newly documented
variable is covered the moment it is documented.
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
ENV_EXAMPLE = os.path.join(_ROOT, ".env.example")
COMPOSE = os.path.join(_ROOT, "docker-compose.yml")

# A line in .env.example that declares a variable the operator can set.
_DECL = re.compile(r"^(MEM_[A-Z0-9_]+|LLM_[A-Z0-9_]+|OLLAMA_[A-Z0-9_]+|"
                   r"LOG_LEVEL|BASE_URL|NEO_PASS|HTPASSWD_[A-Z0-9_]+)=", re.M)
# Either interpolated from the host .env, or assigned a literal value.
_INTERPOLATED = re.compile(r"\$\{([A-Z0-9_]+)")
_ASSIGNED = re.compile(r"^\s+-\s+([A-Z0-9_]+)=", re.M)


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _declared():
    return set(_DECL.findall(_read(ENV_EXAMPLE)))


def _reachable():
    compose = _read(COMPOSE)
    return set(_INTERPOLATED.findall(compose)) | set(_ASSIGNED.findall(compose))


TEMPLATES = os.path.join(_HERE, "templates")
# Every key get_gui() builds: _get_auth_context()'s four, plus the two it adds.
DASHBOARD_CTX = {
    "AUTH_USER": "u", "AUTH_PASS": "p", "AUTH_BASE64": "dTpw",
    "MCP_URL": "/mem-mcp/mcp", "BASE_URL": "/mem-mcp",
    "MERGE_MAX_CLUSTER": 12,
}
# `{{ NAME }}` where NAME is a bare identifier. Anything else between the
# doubled braces is an EXPRESSION, and Jinja parses those in comments too.
_JINJA_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)


class DashboardRenderTests(unittest.TestCase):
    """The dashboard must actually render.

    `MERGE_MAX_CLUSTER` reached the template, `DedupUnderSetupTests` asserted
    the context key, and the input's max attribute was correct -- and the app
    still would not start. `get_gui` calls `_render("dashboard", **ctx)`, and a
    comment explaining how to avoid Jinja syntax contained `{{...}}`, which Jinja
    parses as an expression; the ellipsis made it a TemplateSyntaxError and the
    whole page failed to render. 431 tests were green.

    Nothing here is visible to py_compile, because the template is not Python.
    The check that was missing is the simplest one available: hand the file to
    the same engine the server uses and see that it comes out.
    """

    def test_the_dashboard_renders_with_the_context_get_gui_builds(self):
        try:
            from jinja2 import Environment, FileSystemLoader
        except ImportError:
            self.skipTest("jinja2 not installed; the lexical guard below still runs")

        env = Environment(loader=FileSystemLoader(TEMPLATES))
        try:
            out = env.get_template("dashboard.html").render(**DASHBOARD_CTX)
        except Exception as exc:  # noqa: BLE001 -- the point is that none escape
            self.fail(f"dashboard.html does not render: "
                      f"{type(exc).__name__}: {exc}")
        self.assertTrue(len(out) > 1000, msg="rendered page is implausibly small")

    def test_merge_max_cluster_reaches_the_rendered_page(self):
        """The knob must be substituted, not left as literal braces."""
        try:
            from jinja2 import Environment, FileSystemLoader
        except ImportError:
            self.skipTest("jinja2 not installed")
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        out = env.get_template("dashboard.html").render(**DASHBOARD_CTX)
        self.assertIn('Number("12")', out,
                      msg="MERGE_MAX_CLUSTER did not reach the client-side cap")
        self.assertNotIn("{{MERGE_MAX_CLUSTER}}", out,
                         msg="MERGE_MAX_CLUSTER reached the page unsubstituted")

    def test_no_doubled_braces_holding_an_expression(self):
        """Dependency-free guard, so it runs even without jinja2.

        A bare `{{...}}` in prose reads as harmless -- it is in a comment, or in
        a string literal, or it is obviously talking about the syntax rather
        than using it. Jinja does not distinguish: it lexes every `{{` in the
        file, so any of those takes the page down. Only bare identifiers are
        allowed between the braces.
        """
        for name in sorted(os.listdir(TEMPLATES)):
            if not name.endswith(".html"):
                continue
            src = _read(os.path.join(TEMPLATES, name))
            for expr in _JINJA_EXPR.findall(src):
                if not re.fullmatch(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*", expr):
                    line = src[:src.find("{{" + expr)].count("\n") + 1
                    self.fail(
                        f"{name}:{line} has {{{{{expr.strip()}}}}} between doubled "
                        f"braces, which Jinja evaluates as an expression. Only a "
                        f"bare identifier may appear there -- never an ellipsis, "
                        f"not even inside a comment."
                    )


class ModelRoleRoutingTests(unittest.TestCase):
    """Which chat model each role actually calls.

    The four chat roles are deliberately not one knob: extraction is
    high-volume and mechanical, judgement is low-volume and consequential, and
    they measured best with different models on this host. Every role that does
    not pass `model=` silently inherits LLM_QUERY_MODEL, so a role can be moved
    onto the wrong model by someone adding an argument, or by someone deleting
    one, with no error anywhere -- the call still succeeds, it just quietly
    produces worse output.

    A test of the helper is not a test of the call site: `EXTRACT_MODEL` being
    correct in common.py says nothing about whether diary_manager actually
    passes it. So these assert on the call sites.
    """

    def _call_sites(self, filename, needle):
        """The get_llm_response(...) calls in a module, as source text."""
        path = os.path.join(_HERE, filename)
        with open(path, "r", encoding="utf-8") as handle:
            src = handle.read().replace("\r\n", "\n")
        return [m.group() for m in re.finditer(
            r"get_llm_response\((?:[^()]|\([^()]*\))*\)", src)]

    def test_the_extraction_roles_pass_the_extract_model(self):
        """Diary keywords and people names are extraction roles.

        These were the two that measured granite's win: 99.0% keyword precision
        with 0 invented terms against nemotron's 79.7% with 5, and people-name
        F1 0.938 against 0.920, at 2.3-4.2x the speed.
        """
        for module, label in (("diary_manager.py", "diary extractors"),
                              ("fact_manager.py", "search rewrite")):
            calls = self._call_sites(module, "EXTRACT_MODEL")
            self.assertTrue(calls, msg=f"{label}: no get_llm_response calls found")
            self.assertTrue(
                any("model=EXTRACT_MODEL" in c for c in calls),
                msg=(f"{label}: no call passes model=EXTRACT_MODEL, so it inherits "
                     f"LLM_QUERY_MODEL and the measured extraction win is lost.\n"
                     f"calls found:\n" + "\n".join(calls)))

    def test_the_extraction_roles_pass_it_on_every_extraction_call(self):
        """Both diary extractors must pass it, not just one.

        Half a split is the failure that survives review: the keyword call is
        moved and the people call is missed, and both look correct in the diff.
        """
        # Match the CALL, not the prompt's definition. Finding the constant's
        # name finds its assignment first -- which is a prompt string hundreds
        # of lines from the call that uses it, so the first version of this test
        # failed against correct code.
        for system_name in ("_KEYWORD_EXTRACT_SYSTEM", "_PEOPLE_EXTRACT_SYSTEM"):
            calls = [c for c in self._call_sites("diary_manager.py", system_name)
                     if f"system={system_name}" in c]
            self.assertTrue(calls, msg=f"no call passes {system_name}")
            for call in calls:
                self.assertIn("model=EXTRACT_MODEL", call,
                              msg=(f"the {system_name} call does not pass "
                                   f"model=EXTRACT_MODEL, so it inherits "
                                   f"LLM_QUERY_MODEL"))

    def test_merge_review_uses_a_judgement_model(self):
        """Duplicate adjudication goes to the scope model, not the extract model.

        Measured: on a genuine duplicate nemotron answered "merge" and granite
        answered "review". Merging is the expensive mistake here and never
        merging is the recoverable one, so this role does not get the faster
        model just because it is faster.
        """
        calls = self._call_sites("mcp_tools.py", "SCOPE_MODEL")
        self.assertTrue(
            any("model=mem.SCOPE_MODEL" in c for c in calls),
            msg=("the merge-review call does not pass a judgement model; "
                 "it inherits LLM_QUERY_MODEL"))

    def test_the_split_does_not_silently_collapse(self):
        """EXTRACT_MODEL must be able to differ, and must default sanely.

        If this read `EXTRACT_MODEL = LLM_QUERY_MODEL` with no env override,
        every call-site test above would still pass while the split did nothing.
        """
        with open(os.path.join(_HERE, "common.py"), encoding="utf-8") as handle:
            common = handle.read()
        self.assertTrue('os.getenv("MEM_EXTRACT_MODEL")' in common,
                        msg="MEM_EXTRACT_MODEL is not read from the environment")
        self.assertTrue("or LLM_QUERY_MODEL" in common,
                        msg="EXTRACT_MODEL has no default, so an unset knob crashes")


class EnvWiringTests(unittest.TestCase):
    def setUp(self):
        self.declared = _declared()
        self.reachable = _reachable()

    def test_the_two_files_are_actually_parsed(self):
        """Self-check: a regex that stops matching must not make this go green.

        Both suites that derive their targets from source have shipped a
        derived-from-the-wrong-class test that passed while the bug was live.
        Assert the inputs are non-trivial before trusting any conclusion drawn
        from them.
        """
        self.assertGreater(len(self.declared), 30,
                           msg=f".env.example yielded only {self.declared} -- "
                               f"the declaration pattern no longer matches the file")
        self.assertGreater(len(self.reachable), 30,
                           msg=f"docker-compose.yml yielded only {self.reachable} -- "
                               f"the environment-block pattern no longer matches the file")

    def test_every_documented_variable_reaches_the_container(self):
        unreachable = sorted(self.declared - self.reachable)
        self.assertEqual(
            unreachable, [],
            msg=(
                "these variables are documented in .env.example but never passed by "
                "docker-compose.yml, so setting them has no effect and they are "
                "permanently stuck at their code default:\n  "
                + "\n  ".join(unreachable)
            ),
        )

    def test_the_chat_timeout_knobs_are_wired(self):
        """Named explicitly, because these are the ones this suite was written for.

        `MEM_LLM_TIMEOUT` shipped in 514e184 as the fix for a hardcoded 60s
        Ollama timeout, and was untunable until this was caught. A future
        deletion of it should name the incident rather than fail anonymously.
        """
        for name in ("MEM_LLM_TIMEOUT", "MEM_LLM_CONNECT_TIMEOUT",
                     "MEM_SEARCH_LLM_TIMEOUT", "MEM_LLM_LOG_CHARS"):
            with self.subTest(var=name):
                self.assertIn(name, self.reachable,
                              msg=f"{name} is not passed to the container, so it "
                                  f"cannot be tuned and the documented default is "
                                  f"not the effective one")

    def test_a_bare_interpolation_never_feeds_a_getenv_that_has_no_empty_fallback(self):
        """The hazard is real, but only for one of the two `os.getenv` forms.

        `os.getenv(NAME, "fallback")` returns the empty string when the variable
        is present-but-empty, so a bare `${NAME}` in compose would defeat the
        code's own default. `os.getenv(NAME) or "fallback"` treats empty as
        absent and is safe. The first attempt at this test asserted that *every*
        bare interpolation is a crash, which was false -- `LLM_QUERY_MODEL`,
        `MEM_SCOPE_MODEL` and `BASE_URL` are all the safe `or` form, and
        demanding `:-` defaults for them would have been a false alarm.

        So the condition is derived instead: find the two-argument form in the
        modules that read configuration, and require those to carry a compose
        default -- unless the fallback literal is itself `""`, in which case an
        empty value is what the code asked for and there is nothing to defeat.
        (`BASE_URL` is that case: it defaults to "", meaning "no path prefix".)
        """
        unsafe = set()
        for module in ("common.py", "chunking.py", "diary_manager.py",
                       "fact_manager.py", "migrate_client_context.py",
                       "client_manager.py"):
            path = os.path.join(_HERE, module)
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            # `os.getenv(NAME, "default")` -- the two-argument form. A leading
            # "or" anywhere in the statement makes the default reachable for an
            # empty value, so those are not unsafe; neither is a fallback that
            # is itself the empty string.
            for match in re.finditer(
                r"os\.getenv\(\s*[\"']?([A-Z][A-Z0-9_]*)[\"']?\s*,\s*([\"']?)([^\"')]*)\2\s*\)",
                source,
            ):
                name, _, default = match.group(1), match.group(2), match.group(3)
                line = source[source.rfind("\n", 0, match.start()) + 1:
                              source.find("\n", match.end())]
                if re.search(r"\bor\b", line):
                    continue  # empty value falls through to the fallback
                if default.strip() == "":
                    continue  # empty is the intended value
                unsafe.add(name)

        self.assertTrue(unsafe, msg=(
            "no two-argument os.getenv found -- the pattern no longer matches, so "
            "this test would pass vacuously"))

        compose = _read(COMPOSE)
        secrets = {"NEO_PASS", "MEM_SESSION_SECRET"}
        bare = sorted(
            name for name in unsafe - secrets
            if f"${{{name}}}" in compose
        )
        self.assertEqual(
            bare, [],
            msg=("these are read with os.getenv(NAME, default) and interpolated in "
                 "compose without a ':-' fallback, so a host .env that sets them to "
                 "an empty value silently defeats the code's own default:\n  "
                 + "\n  ".join(bare)),
        )

    def test_the_embed_ceiling_is_wired_and_matches_its_documented_default(self):
        """`common.py` and `chunking.py` must agree, and compose must not fork it.

        `EMBED_MAX_CHARS` sizes the truncation and `chunking.EMBED_CEILING`
        sizes the chunk parts from the same variable. A default in compose that
        disagreed with either one would size every chunk for a ceiling the
        embedder refuses -- the exact failure the shared default was introduced
        to prevent.
        """
        compose = _read(COMPOSE)
        self.assertIn("MEM_EMBED_MAX_CHARS", self.reachable)
        self.assertIn(
            "MEM_EMBED_MAX_CHARS=${MEM_EMBED_MAX_CHARS:-8000}", compose,
            msg="the compose default for the embed ceiling must stay 8000, the "
                "measured window of the embedder this host actually serves",
        )

    def test_the_model_variables_are_interpolated_not_hardcoded(self):
        """The model choice is the operator's, and it depends on their VRAM.

        A hardcoded model in the compose file would silently override .env --
        the failure would look like "I changed the env and nothing happened",
        which is indistinguishable from a caching problem.
        """
        compose = _read(COMPOSE)
        for name in ("LLM_QUERY_MODEL", "MEM_SCOPE_MODEL", "MEM_EMBEDDER_MODEL"):
            with self.subTest(var=name):
                self.assertIn(f"${{{name}", compose,
                              msg=f"{name} is not interpolated from .env")


if __name__ == "__main__":
    unittest.main()
