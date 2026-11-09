"""The meeting-prep brief: the pure layout, and the wiring that feeds it.

`briefing.py` is stdlib-only with zero imports, which is the whole reason it is
a separate module from `brief_manager.py` -- the layout rules are the part
worth testing, and this suite can import and *call* them on a box with no
database driver, no fastapi and no httpx.

The manager half is pinned rather than driven: it reaches four managers and a
model, so what matters there is the shape of the calls -- that the fallback is
rendered before the model is asked, that a model failure is caught rather than
raised, and that the brief a reader gets when Ollama is down is still a brief.
"""

import ast
import os
import unittest

import briefing


def _source(*parts):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, *parts), "r", encoding="utf-8") as handle:
        return handle.read()


def _function_body(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            if segment:
                return segment
    raise AssertionError(f"{name} not found")


def _fact(name, category, text="", **extra):
    return {"id": f"f-{name}", "name": name, "category": category,
            "text": text or f"{name} does something.", **extra}


class GroupingTests(unittest.TestCase):
    """Categories render in a fixed order, and one bucket is alphabetical."""

    def test_categories_come_out_in_the_declared_order(self):
        grouped = briefing.group_facts_by_category([
            _fact("Zeta", "Tool"), _fact("Alpha", "People"), _fact("Beta", "Project"),
        ])
        self.assertEqual(list(grouped), ["Project", "People", "Tool"])

    def test_an_unknown_category_sorts_after_the_known_ones(self):
        # A vault that grows a category must not reshuffle every existing
        # section; new material goes at the end.
        grouped = briefing.group_facts_by_category([
            _fact("A", "Zebra"), _fact("B", "Project"),
        ])
        self.assertEqual(list(grouped), ["Project", "Zebra"])

    def test_a_bucket_is_alphabetical_by_name(self):
        grouped = briefing.group_facts_by_category([
            _fact("Charlie", "People"), _fact("Alice", "People"), _fact("Bob", "People"),
        ])
        self.assertEqual([f["name"] for f in grouped["People"]],
                         ["Alice", "Bob", "Charlie"])

    def test_an_empty_vault_is_an_empty_mapping(self):
        self.assertEqual(briefing.group_facts_by_category([]), {})


class SummariseTests(unittest.TestCase):
    def _scope(self, **extra):
        return {"id": "c1", "name": "Deutsche Bank (DB)", "active": True,
                "contexts": [{"id": "x1", "name": "DB AI Adoption"}], **extra}

    def test_the_counts_describe_what_was_gathered(self):
        brief = briefing.summarise(
            self._scope(),
            [{"text": "send the doc", "status": "open"},
             {"text": "book the room", "status": "open", "overdue": True}],
            [_fact("A", "People"), _fact("B", "Technology")],
            [{"id": "d1", "date": "2026-05-01", "name": "Kickoff"}],
        )
        self.assertEqual(brief["overdue_count"], 1)
        self.assertEqual(brief["fact_count"], 2)
        self.assertEqual(brief["meeting_count"], 1)
        self.assertEqual(brief["project_count"], 1)

    def test_where_names_the_client_and_its_projects(self):
        brief = briefing.summarise(self._scope(), [], [], [])
        self.assertIn("**Deutsche Bank (DB)**", brief["where"][0])
        self.assertIn("DB AI Adoption", brief["where"][1])

    def test_an_inactive_client_is_labelled(self):
        brief = briefing.summarise(self._scope(active=False), [], [], [])
        self.assertIn("inactive", brief["where"][0])

    def test_no_projects_is_stated_rather_than_omitted(self):
        # Silence would read as "this client has projects and none were
        # relevant"; the brief says which it is.
        brief = briefing.summarise({"name": "SAP SE", "active": True, "contexts": []},
                                   [], [], [])
        self.assertIn("_none recorded_", brief["where"][1])

    def test_an_unnamed_scope_still_renders(self):
        brief = briefing.summarise({}, [], [], [])
        self.assertEqual(brief["name"], "this engagement")


class RenderTests(unittest.TestCase):
    def _brief(self, **extra):
        base = briefing.summarise(
            {"name": "EPAM", "active": True, "contexts": [{"name": "PPC"}]},
            [], [], [])
        base.update(extra)
        return base

    def test_an_empty_section_says_so_instead_of_vanishing(self):
        text = briefing.render(self._brief())
        self.assertIn(briefing.SECTION_MEETINGS, text)
        self.assertIn(briefing.EMPTY, text)

    def test_actions_split_overdue_from_outstanding(self):
        brief = self._brief(actions=[
            {"text": "send the deck", "status": "open", "dueAt": "2026-05-01",
             "overdue": True},
            {"text": "book the room", "status": "open", "dueAt": "2026-06-01"},
        ], overdue_count=1)
        text = briefing.render(brief)
        self.assertIn("### Overdue", text)
        self.assertIn("### Outstanding", text)
        self.assertLess(text.index("send the deck"), text.index("book the room"))

    def test_overdue_is_named_not_just_coloured(self):
        brief = self._brief(actions=[
            {"text": "send the deck", "status": "open", "dueAt": "2026-05-01",
             "overdue": True}], overdue_count=1)
        self.assertIn("OVERDUE", briefing.render(brief))

    def test_the_gaps_section_is_omitted_when_there_are_no_gaps(self):
        brief = self._brief()
        self.assertNotIn(briefing.SECTION_GAPS,
                         briefing.render(brief, gaps_list=[]))

    def test_the_document_starts_with_the_name(self):
        self.assertTrue(briefing.render(self._brief()).startswith("# Brief — EPAM"))


class GapsTests(unittest.TestCase):
    def test_nothing_recorded_is_said_rather_than_implied(self):
        brief = briefing.summarise({"name": "X", "contexts": []}, [], [], [])
        said = " ".join(briefing.gaps(brief)).lower()
        self.assertIn("no meetings", said)
        self.assertIn("no open commitments", said)
        self.assertIn("no facts", said)

    def test_an_engagement_that_is_on_track_says_so(self):
        # "Nothing is overdue" is information, and the difference between it and
        # silence is the difference between a calm reader and a worried one.
        brief = briefing.summarise(
            {"name": "X", "contexts": [{"name": "P"}]},
            [{"text": "a", "status": "open", "dueAt": "2099-01-01"}],
            [_fact("A", "People")], [{"id": "d1", "date": "2026-05-01"}])
        said = " ".join(briefing.gaps(brief))
        self.assertIn("No commitments are overdue", said)


class PolishTests(unittest.TestCase):
    def test_a_real_answer_is_kept(self):
        answer = "# Brief — EPAM\n\n" + ("The steering committee meets on Thursday. " * 4)
        self.assertEqual(briefing.polish(answer, "fallback"), answer.strip() + "\n")

    def test_an_empty_answer_falls_back(self):
        self.assertEqual(briefing.polish("", "fallback"), "fallback")

    def test_a_stub_answer_falls_back(self):
        # The floor is deliberately low -- it catches "OK" and "Here is the
        # brief:", not a terse brief.
        self.assertEqual(briefing.polish("Here is the brief:", "fallback"), "fallback")

    def test_the_result_always_ends_with_one_newline(self):
        out = briefing.polish("# Brief — EPAM\n\n" + ("Sentence. " * 12), "fallback")
        self.assertTrue(out.endswith("\n"))
        self.assertFalse(out.endswith("\n\n"))


class PromptTests(unittest.TestCase):
    def _brief(self):
        return briefing.summarise(
            {"name": "EPAM", "active": True, "contexts": [{"name": "PPC"}]},
            [{"text": "send the deck", "status": "open", "dueAt": "2026-05-01",
              "overdue": True}],
            [_fact("Thomas Kraikauer", "People")],
            [{"id": "d1", "date": "2026-05-01", "name": "Kickoff"}])

    def test_every_section_heading_reaches_the_model(self):
        prompt = briefing.build_prompt(self._brief())
        for heading in (briefing.SECTION_WHERE, briefing.SECTION_ACTIONS,
                        briefing.SECTION_MEETINGS, briefing.SECTION_KNOWLEDGE,
                        briefing.SECTION_GAPS):
            self.assertIn(heading, prompt)

    def test_the_material_is_the_rendered_brief(self):
        brief = self._brief()
        prompt = briefing.build_prompt(brief)
        self.assertIn("MATERIAL", prompt)
        self.assertIn("send the deck", prompt)
        self.assertIn("Thomas Kraikauer", prompt)

    def test_the_system_prompt_forbids_invention(self):
        # A prompt is text, and nothing else notices when a sentence carrying a
        # rule is softened. This is the sentence the whole brief rests on.
        self.assertIn("invented detail", briefing.SYSTEM_PROMPT)


class ManagerSourceTests(unittest.TestCase):
    """The wiring, which no behavioural test here can reach."""

    def setUp(self):
        self.manager = _source("brief_manager.py")

    def test_the_fallback_is_rendered_before_the_model_is_asked(self):
        body = _function_body(self.manager, "db_build_brief")
        self.assertLess(body.index("briefing.render("), body.index("get_llm_response("),
                        msg="the plain brief is the thing served when the model "
                            "fails, so it has to exist before the call")

    def test_a_model_failure_is_caught_not_raised(self):
        body = _function_body(self.manager, "db_build_brief")
        call = body.index("get_llm_response(")
        # The try/except must bracket the call; a 503 on the way into a meeting
        # is worse than an unpolished brief.
        self.assertLess(body.index("try:"), call)
        self.assertGreater(body.index("except Exception"), call)

    def test_the_brief_is_cached_on_the_vault_fingerprint(self):
        body = _function_body(self.manager, "db_build_brief")
        self.assertIn("vault_fingerprint", self.manager)
        self.assertIn("_CACHE.get(key)", body)

    def test_the_brief_reads_only(self):
        # A brief that writes would put a maintenance lock in front of somebody
        # opening a meeting.
        for writer in ("db_save_diary(", "db_create_action_item(", "db_update_",
                       "db_add_memory("):
            self.assertNotIn(writer, self.manager,
                             msg=f"{writer} has no place in a read path")


class RouteWiringTests(unittest.TestCase):
    def test_both_brief_routes_exist(self):
        source = _source("gui.py")
        self.assertIn('@web_app.get("/api/clients/{client_id}/brief"', source)
        self.assertIn('@web_app.get("/api/contexts/{context_id}/brief"', source)

    def test_the_tool_is_registered(self):
        source = _source("mcp_tools.py")
        self.assertIn('monitor_mcp_tool("client_brief"', source)
        self.assertIn("async def client_brief(", source)

    def test_an_unknown_client_is_refused_rather_than_guessed(self):
        body = _function_body(_source("mcp_tools.py"), "client_brief")
        self.assertIn("No client matches", body,
                      msg="the brief names the client at the top; a wrong one "
                          "is read out loud")

    def test_the_skill_is_shipped(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "skills", "meeting-prep.md")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("---"))
        self.assertIn("name: meeting-prep", text)
        self.assertIn("client_brief(", text,
                      msg="a skill that does not name the tool will be done by hand")

    def test_the_panel_offers_the_brief(self):
        # A feature reachable only by hand-written HTTP is a feature the
        # consultant who asked for it will never open.
        import test_auth_guard

        source = _source("templates", "dashboard.html")
        self.assertIn("async function showScopeBrief(", source)
        # The dashboard is JavaScript, so it is lifted with the JS extractor
        # rather than the AST one -- `ast.parse` on HTML raises on the first
        # typographic dash in the markup.
        panel = test_auth_guard._js_function_source("templates/dashboard.html",
                                                    "toggleScopeItems")
        self.assertIn("showScopeBrief(", panel,
                      msg="the button has to be on the panel, not only in a menu")
        self.assertIn("briefHtml +", panel,
                      msg="the brief is offered even when the panel has no items")


if __name__ == "__main__":
    unittest.main()
