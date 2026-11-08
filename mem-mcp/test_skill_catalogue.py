"""
Tests for skill discovery.

`skill_catalogue` is stdlib-only on purpose, so it is *imported and called*
here rather than lifted out of its source — the parsing rules are the whole
subject and a source assertion would say nothing about them. `mcp_skills`
imports fastmcp and cannot be imported on this box, so the one thing that has
to be checked there — that resources are derived from the directory instead of
hand-registered — is read from its source, and the assertion is scoped to a
function rather than to the whole file.
"""
import ast
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import skill_catalogue  # noqa: E402


def _skills_file(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _function_source(source, name):
    """The source of one module-level function, located through the AST.

    Slicing between two `def` lines runs to EOF when a decorator or a blank
    gap intervenes, and a comment quoting the forbidden shape is not that
    shape — so the segment comes from the tree, not from the text.
    """
    tree = ast.parse(source)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


class ParseFrontMatterTests(unittest.TestCase):

    def test_flat_pairs_are_read(self):
        text = ("---\n"
                "name: search-and-recall\n"
                "description: How to search the vault.\n"
                "---\n"
                "# Skill: Search\n")
        parsed = skill_catalogue.parse_front_matter(text)
        self.assertEqual(parsed.get("name"), "search-and-recall")
        self.assertEqual(parsed.get("description"), "How to search the vault.")

    def test_quoted_values_lose_their_quotes(self):
        text = "---\ndescription: \"A quoted: description\"\n---\n# Heading\n"
        self.assertEqual(skill_catalogue.parse_front_matter(text)["description"],
                         "A quoted: description")

    def test_a_document_without_front_matter_yields_nothing(self):
        self.assertEqual(
            skill_catalogue.parse_front_matter("# Skill: X\n\nbody\n"), {})

    def test_front_matter_must_open_the_document(self):
        text = "\n---\ndescription: late\n---\n# Heading\n"
        self.assertEqual(skill_catalogue.parse_front_matter(text), {},
                         msg="a block after the first line is body text, and "
                             "reading it would make any horizontal rule a "
                             "metadata block")

    def test_a_folded_scalar_is_left_out_rather_than_half_parsed(self):
        parsed = skill_catalogue.parse_front_matter(
            "---\ndescription: >\n  wrapped over lines\n---\n# Heading\n")
        self.assertNotIn("description", parsed,
                         msg="returning '>' as a description is worse than "
                             "letting the heading fallback fire")

    def test_a_repeated_key_keeps_the_first_value(self):
        parsed = skill_catalogue.parse_front_matter(
            "---\ndescription: first\ndescription: second\n---\n# H\n")
        self.assertEqual(parsed["description"], "first",
                         msg="a duplicate is a mistake, and taking the last "
                             "one makes the answer depend on where it was typed")

    def test_blank_lines_and_comments_are_skipped(self):
        parsed = skill_catalogue.parse_front_matter(
            "---\n\nname: x\n# a comment, not a pair\n---\n# H\n")
        self.assertEqual(parsed, {"name": "x"})


class DescribeSkillTests(unittest.TestCase):

    def _write(self, text):
        import tempfile
        handle, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_the_name_is_always_the_filename_stem(self):
        path = self._write("---\nname: something-else\n---\n# H\n")
        entry = skill_catalogue.describe_skill(path)
        self.assertEqual(entry["name"],
                         os.path.splitext(os.path.basename(path))[0],
                         msg="get_skill_workflow resolves the name as a "
                             "filename, so a catalogue name that disagreed "
                             "would name a skill nothing can load")

    def test_the_description_comes_from_front_matter(self):
        path = self._write("---\ndescription: From the metadata\n---\n# H\n")
        self.assertEqual(skill_catalogue.describe_skill(path)["description"],
                         "From the metadata")

    def test_a_missing_description_falls_back_to_the_heading(self):
        path = self._write("# Skill: Vault Orientation\n\nbody\n")
        self.assertEqual(skill_catalogue.describe_skill(path)["description"],
                         "Vault Orientation",
                         msg="a skill is discoverable the moment its .md "
                             "exists, remembered metadata or not")

    def test_with_neither_metadata_nor_heading_the_stem_is_used(self):
        path = self._write("just prose, no heading\n")
        entry = skill_catalogue.describe_skill(path)
        self.assertTrue(entry["description"],
                        msg="discovery must never answer with an empty "
                            "description")


class ShippedSkillsTests(unittest.TestCase):
    """The properties of the real `skills/` directory."""

    def test_every_skill_has_a_non_empty_description(self):
        entries = skill_catalogue.list_skills()
        self.assertTrue(entries, msg="the skills directory resolved to nothing")
        for entry in entries:
            with self.subTest(skill=entry["name"]):
                self.assertTrue(entry["description"].strip())

    def test_every_skill_file_is_listed(self):
        directory = skill_catalogue.default_skills_dir()
        on_disk = sorted(f[:-3] for f in os.listdir(directory)
                         if f.endswith(".md"))
        self.assertEqual([e["name"] for e in skill_catalogue.list_skills()],
                         on_disk,
                         msg="discovery must not depend on a list someone "
                             "maintains by hand")

    def test_the_listing_is_sorted(self):
        names = [e["name"] for e in skill_catalogue.list_skills()]
        self.assertEqual(names, sorted(names),
                         msg="listdir order reshuffles between deploys, and "
                             "that is noise in a client's first request")

    def test_a_missing_directory_lists_nothing(self):
        self.assertEqual(
            skill_catalogue.list_skills(os.path.join(HERE, "no-such-dir")), [])


class RegistrationSourceTests(unittest.TestCase):
    """`mcp_skills` cannot be imported here, so the wiring is read."""

    def test_resources_are_derived_from_the_directory(self):
        body = _function_source(_skills_file("mcp_skills.py"), "register_skills")
        self.assertIn("skill_catalogue.list_skills()", body,
                      msg="a new .md must need no registration code")
        self.assertIn('f"skill://{skill[\'name\']}"', body)

    def test_no_skill_is_hand_registered_by_uri(self):
        body = _function_source(_skills_file("mcp_skills.py"), "register_skills")
        self.assertNotIn('@mcp.resource("skill://', body,
                         msg="a hand-registered URI would be registered twice "
                             "by the loop, and the second one is the one that "
                             "wins or the one that raises")

    def test_every_shipped_skill_is_addressable(self):
        body = _function_source(_skills_file("mcp_skills.py"), "register_skills")
        self.assertIn("skill_catalogue.list_skills()", body,
                      msg="the loop is what makes every file addressable; "
                          "without it a skill has a name and no URI")
        source = _skills_file("mcp_skills.py")
        self.assertIn('@mcp.prompt("process-directory")', source,
                      msg="the process-transcription prompt points at this "
                          "skill, and it had neither a resource nor a prompt")

    def test_the_reader_factory_binds_its_path(self):
        source = _skills_file("mcp_skills.py")
        factory = _function_source(source, "_skill_reader")
        self.assertIn("read.__doc__ = description", factory)
        self.assertIn("read.__name__", factory,
                      msg="every resource must have a distinct name, or the "
                          "second registration is a duplicate")


class SearchAndRecallSkillTests(unittest.TestCase):
    """A skill is prompt text, so pin the claims that must not drift.

    Every sentence here is one a reader acts on, and none of them is visible in
    any other test: deleting "scope prioritises instead of filtering" leaves a
    perfectly valid Markdown file that teaches the wrong thing, and no code
    path notices. Same reasoning as `ScopePromptTests` — the file is the
    product, so the file is what gets asserted.
    """

    def setUp(self):
        with open(os.path.join(skill_catalogue.default_skills_dir(),
                               "search-and-recall.md"), encoding="utf-8") as fh:
            self.text = fh.read()

    def _has(self, needle, why):
        self.assertTrue(needle in self.text, msg=why)

    def test_scope_prioritises_and_never_filters(self):
        self._has("Scope prioritises; it never filters",
                  "a reader who believes scope filters will conclude an empty "
                  "result means the record is not filed under that client")

    def test_score_is_never_a_threshold(self):
        self._has("never be thresholded",
                  "`score` is a ranking value, not a similarity; treating it "
                  "as a gate drops the records the escape hatch exists for")
        self._has("`confidence`", "confidence is the gate and must be named")
        self._has("`evidence`", "evidence is what says whether it is the same record")

    def test_unconfirmed_matches_must_not_be_acted_on(self):
        self._has("not the same record",
                  "an unconfirmed near miss is how a second record for one "
                  "person gets created and never reconciled")

    def test_find_patterns_is_scoped_to_what_it_answers(self):
        self._has("what recurs",
                  "`find_patterns` is the tool most likely to be asked a "
                  "question it does not answer")

    def test_a_hit_is_the_whole_record(self):
        self._has("always the whole record",
                  "chunked records return complete text; a reader who assumes "
                  "a fragment will join two hits that share an id")

    def test_disagreement_is_reported(self):
        self._has("vault disagrees with what the user just said",
                  "a dated record contradicting a recollection is the most "
                  "valuable thing this vault produces, and only if it is said")


if __name__ == "__main__":
    unittest.main()
