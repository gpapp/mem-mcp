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
