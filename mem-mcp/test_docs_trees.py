"""The docs' module trees and suite counts cannot drift from the directory.

`README.md` and `CLAUDE.md` are hand-maintained lists of the same things the
package directory already has — every non-test module, and the number of
dependency-light regression suites. Nothing fails when they drift (AGENTS.md
has recorded this class of drift more than once), so this suite asserts the
invariants:

- every module on disk is named in both docs' trees, and every name in the
  trees is backed by a module on disk;
- the suite count both docs claim equals the number of `test_*.py` files.
"""

import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOCS = ("README.md", "CLAUDE.md")


def _tree_names(doc):
    names = set()
    for line in (ROOT / doc).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("├──", "└──")):
            names.update(t[:-3] for t in re.findall(r"([A-Za-z_]+\.py)", stripped))
    return names


def _package_modules():
    return sorted(p.name[:-3] for p in HERE.glob("*.py") if not p.name.startswith("test_"))


def _suite_file_count():
    return len([p for p in HERE.glob("test_*.py")])


def _claimed_suite_count(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    match = re.search(r"(\d+) dependency-light regression suites", text)
    return int(match.group(1)) if match else None


class DocTreeDriftTests(unittest.TestCase):
    def test_every_package_module_is_named_in_both_docs(self):
        disk = set(_package_modules())
        for doc in DOCS:
            named = _tree_names(doc)
            missing = [m for m in sorted(disk) if m not in named]
            self.assertEqual(missing, [], f"{doc} tree is missing modules: {missing}")

    def test_no_tree_entry_names_a_module_that_is_not_on_disk(self):
        disk = set(_package_modules())
        for doc in DOCS:
            named = _tree_names(doc)
            dangling = [n for n in sorted(named) if n not in disk]
            self.assertEqual(dangling, [], f"{doc} tree names modules not on disk: {dangling}")

    def test_claimed_suite_counts_match_the_directory(self):
        actual = _suite_file_count()
        for doc in DOCS:
            claimed = _claimed_suite_count(doc)
            self.assertIsNotNone(claimed, f"{doc} makes no 'N dependency-light regression suites' claim")
            self.assertEqual(claimed, actual, f"{doc} claims {claimed} suites, the directory has {actual}")


if __name__ == "__main__":
    unittest.main()
