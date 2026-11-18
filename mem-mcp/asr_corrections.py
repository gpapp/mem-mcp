"""Pure helpers for correcting ASR mishears, scoped per client.

asr-mcp mishears the same terms over and over -- ``Deutche Bank`` for
``Deutsche Bank``, ``1dp`` for ``OneDP``. A correction is a single
``wrong phrase -> right phrase`` pair, and this module is the whole matching
engine. It imports **nothing** from the app or from a database driver, for the
same reason ``matching_utils.py``, ``sessions.py`` and ``live_notes.py`` are
stdlib-only: the rules decide what gets written into somebody's vault, so they
have to be *called* by a test on a machine with no Neo4j and no Ollama rather
than lifted out of a module that needs one.

Five invariants are worth stating before the code:

1. **Matching is case-insensitive on word boundaries.** ``Deutche Bank`` must
   be found in ``deutche bank`` and in ``DEUTCHE BANK``; ``TIO`` must not be
   found inside ``STATION`` and ``Tim`` must not touch ``timeout``.
2. **Longest rule first, at each position.** With ``TIO -> TO`` and
   ``Southern TIO -> TO``, ``Southern TIO`` is one replacement, not
   ``Southern TO``. Without this the short rule consumes the head of the long
   one and the long rule can never fire.
3. **One non-overlapping pass, and it never chains.** Each character of the
   input is considered once; a replacement is emitted and the scan continues
   *after* it, so ``a -> b`` / ``b -> c`` turns ``a`` into ``b``, never ``c``.
   Chaining is how a dictionary quietly rewrites a document into something
   nobody said.
4. **A rule whose right side contains its wrong side is refused.** ``Bank ->
   Deutsche Bank`` is a loop wearing a correction's clothes, and the first
   application would produce a string the rule matches again. The check is
   word-boundary aware, so ``TO -> Toronto`` is allowed: ``TO`` is not a word
   inside ``Toronto``.
5. **The stored right side is emitted verbatim.** The rule carries the
   canonical spelling, so ``deutche bank`` becomes ``Deutsche Bank``. There is
   deliberately no case guessing -- a heuristic that "preserved" the match's
   case would turn ``1dp`` into ``Onedp`` and need a second rule to fix it.
"""

import re

__all__ = [
    "CorrectionRuleError",
    "apply_corrections",
    "compile_rules",
    "find_corrections",
    "normalise_rule",
]

# A raw rule is a pair (or a row dict); a compiled rule is this triple.
CompiledRule = tuple[str, str, "re.Pattern[str]"]

_WHITESPACE = re.compile(r"\s+")


class CorrectionRuleError(ValueError):
    """A rule that must not be applied: empty, or its right side loops."""


def _is_word_char(ch):
    return ch.isalnum() or ch == "_"


def _phrase_pattern(phrase):
    """Compile a phrase to a case-insensitive pattern on word boundaries.

    Whitespace *inside* the phrase is matched as ``\\s+``, so a name split by a
    line break or an odd number of spaces in the transcript still matches. The
    boundaries are added only where the phrase actually starts or ends on a
    word character: ``\\b`` before punctuation would assert a transition that
    may not exist.
    """
    tokens = [re.escape(token) for token in phrase.split()]
    body = r"\s+".join(tokens)
    prefix = r"\b" if _is_word_char(phrase[0]) else ""
    suffix = r"\b" if _is_word_char(phrase[-1]) else ""
    return re.compile(prefix + body + suffix, re.IGNORECASE)


def normalise_rule(wrong, right):
    """Validate one rule and return its ``(wrong, right)`` spelling.

    Whitespace is stripped and collapsed, so a rule pasted from a diary entry
    with a stray newline behaves like the same rule typed normally. Raises
    :class:`CorrectionRuleError` for an empty side or for a rule whose right
    side contains its wrong side as a word.
    """
    clean_wrong = _WHITESPACE.sub(" ", str(wrong or "").strip())
    clean_right = _WHITESPACE.sub(" ", str(right or "").strip())
    if not clean_wrong:
        raise CorrectionRuleError("A correction needs a wrong phrase.")
    if not clean_right:
        raise CorrectionRuleError(
            "The correction for %r needs a right phrase." % clean_wrong
        )
    if _phrase_pattern(clean_wrong).search(clean_right):
        raise CorrectionRuleError(
            "The correction %r -> %r is a loop: its right side contains its "
            "wrong side." % (clean_wrong, clean_right)
        )
    return clean_wrong, clean_right


def _pair(item):
    """Accept a 2-tuple/2-list or a row dict with ``wrong``/``right`` keys."""
    if isinstance(item, dict):
        return item.get("wrong"), item.get("right")
    return item[0], item[1]


def compile_rules(rules):
    """Validate, de-duplicate and order raw rules into compiled ones.

    Duplicates on the same wrong phrase keep the **first** rule: storage refuses
    a duplicate ``(client, wrong)``, so reaching this branch means two sources
    were merged, and failing every save over a cosmetic conflict would be worse
    than picking one deterministically. Ordering is longest-wrong-first, then
    alphabetical, so the result does not depend on the order handed in.
    """
    seen = {}
    for item in rules or []:
        wrong, right = _pair(item)
        clean_wrong, clean_right = normalise_rule(wrong, right)
        seen.setdefault(clean_wrong.lower(), (clean_wrong, clean_right))
    ordered = sorted(seen.values(), key=lambda wr: (-len(wr[0]), wr[0].lower()))
    return [(wrong, right, _phrase_pattern(wrong)) for wrong, right in ordered]


def _compiled(rules):
    """Pass a compiled list through, compile anything else."""
    items = list(rules or [])
    if not items:
        return []
    first = items[0]
    if isinstance(first, (tuple, list)) and len(first) == 3:
        return items
    return compile_rules(items)


def _matches(text, compiled):
    """Every replacement the rules make in ``text``, left to right.

    A ``for``/``else`` per character: the first rule that matches at the cursor
    wins (the list is longest-first), the match is recorded, and the cursor
    jumps past it. That jump is what makes the pass non-overlapping and free of
    chaining -- the replacement is never re-examined.
    """
    found = []
    cursor = 0
    length = len(text)
    while cursor < length:
        for wrong, right, pattern in compiled:
            match = pattern.match(text, cursor)
            if match:
                found.append(
                    {
                        "wrong": wrong,
                        "right": right,
                        "found": text[match.start():match.end()],
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
                cursor = match.end()
                break
        else:
            cursor += 1
    return found


def apply_corrections(text, rules):
    """Return ``(corrected_text, changes)`` for ``text``.

    ``changes`` is one dict per replacement -- ``wrong``, ``right``, the
    ``found`` substring, and its ``start``/``end`` offsets in the *original*
    text -- so a preview can show what changed without re-deriving it. With no
    rules, or nothing matched, the input is returned unchanged and the list is
    empty.
    """
    source = text or ""
    found = _matches(source, _compiled(rules))
    if not found:
        return source, []
    pieces = []
    last = 0
    for change in found:
        pieces.append(source[last:change["start"]])
        pieces.append(change["right"])
        last = change["end"]
    pieces.append(source[last:])
    return "".join(pieces), found


def find_corrections(text, rules):
    """Return the changes the rules *would* make, without changing anything.

    The count is ``len(find_corrections(...))``; each entry also carries the
    offsets, which is what a retroactive preview shows as a sample.
    """
    return _matches(text or "", _compiled(rules))
