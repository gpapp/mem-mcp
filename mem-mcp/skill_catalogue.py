"""
skill_catalogue.py – read the `skills/` directory without a web framework.

Stdlib-only by design, like `matching_utils.py`, `live_notes.py` and
`scoped_ids.py`: `mcp_skills.py` imports fastmcp and `memory`, so nothing that
lives there can be called by a suite on a box with no fastmcp and no DB driver.
Discovery and front-matter parsing are the parts worth testing, so they live
here and both call sites — the `find_skills` tool and `register_skills` — read
the directory through this module rather than through `os.listdir`.

The front-matter parser is deliberately a flat `key: value` reader, not a YAML
implementation. A skill's front matter is two keys written by whoever added the
skill; the alternative is a PyYAML dependency to read two lines, and a parser
that silently accepts nesting it will never be asked to handle.
"""
import os
import re

# `---` on its own line, the block, then `---`. Non-greedy so the block ends at
# the first closing fence rather than swallowing the whole document.
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
                           re.DOTALL)
_KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*)$")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def default_skills_dir() -> str:
    """The `skills/` directory beside this module.

    Resolved from this file's own location, so a call site never builds the
    path itself and the two call sites cannot drift.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def parse_front_matter(text: str) -> dict:
    """Return the flat `key: value` pairs at the top of a skill document.

    Only the first block is read, and only single-line values: a folded
    (`>`) or literal (`|`) scalar is left out rather than half-parsed, which
    makes the caller's fallback fire instead of a description that reads as
    the character `>`.

    Quoted values lose their matching outer quotes. A key repeated in the
    block keeps its **first** value, because a second `description:` is a
    mistake and taking the last one would make the output depend on where the
    mistake was typed.
    """
    if not text:
        return {}
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}
    parsed = {}
    for line in match.group(1).splitlines():
        pair = _KEY_VALUE.match(line.strip())
        if not pair:
            continue
        key, value = pair.group(1).strip(), pair.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        if key in parsed or not value or value in (">", "|", ">-", "|-"):
            continue
        parsed[key] = value
    return parsed


def _heading_fallback(text: str) -> str:
    """The first Markdown heading, as a description of last resort.

    A skill without front matter still opens with `# Skill: Name`, and that
    line says far more than an empty string or the filename does.
    """
    match = _HEADING.search(text or "")
    if not match:
        return ""
    heading = re.sub(r"^[Ss]kill\s*:\s*", "", match.group(1)).strip()
    return heading


def describe_skill(path: str) -> dict:
    """Read one skill file into `{name, description, path}`.

    `name` is the front-matter name when the file declares one and the
    filename stem otherwise, so the name a caller passes to
    `get_skill_workflow` is always the name of a file that exists. That
    matters: `get_skill_workflow` resolves the argument as a filename, so a
    front-matter name that disagreed with the filename would produce a
    catalogue entry nothing could load.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {"name": stem, "description": stem, "path": path}
    meta = parse_front_matter(text)
    description = meta.get("description") or _heading_fallback(text) or stem
    return {"name": stem, "description": description, "path": path}


def list_skills(directory: str = "") -> list:
    """Every `*.md` in the skills directory, name-sorted, with descriptions.

    Sorted rather than `os.listdir` order: discovery output that reshuffles
    between two identical calls is noise in a client's first request, and the
    order a filesystem returns is not stable across deploys.
    """
    target = directory or default_skills_dir()
    if not os.path.isdir(target):
        return []
    names = sorted(f for f in os.listdir(target) if f.endswith(".md"))
    return [describe_skill(os.path.join(target, f)) for f in names]
