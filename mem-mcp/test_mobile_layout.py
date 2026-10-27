"""Source-level checks for the mobile layout in templates/dashboard.html.

There is no browser in this environment, so nothing here can assert that the
page *looks* right. What it can do is pin the properties that a regression
would silently undo, each of which is invisible to py_compile and to the
Python suites:

  * an inline `style` on a layout container outranks any media query, so one
    re-added attribute silently disables the whole mobile block;
  * the three page layouts are fixed-width columns, and a 360px viewport
    cannot fit them;
  * the iOS zoom guard is a bare font-size that is easy to drop;
  * the per-pane `100vh` containers trap the page scroll on touch.
"""

import os
import re
import unittest

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "dashboard.html")

# The file is CRLF; normalise to \n so the patterns below stay readable.
SOURCE = open(TEMPLATE, "rb").read().decode("utf-8").replace("\r\n", "\n")
CSS = re.search(r"<style>(.*?)</style>", SOURCE, re.S).group(1)


def _strip_comments(css):
    """Drop /* ... */ before parsing.

    A comment sitting above a rule is picked up as the rule's selector by a
    naive `[^{}]+` match, which then keys the declaration map on a sentence
    instead of on `.alpha-btn`.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _media_block(width):
    """The body of one `@media (max-width: Npx)` block."""
    m = re.search(r"@media \(max-width: %dpx\) \{" % width, CSS)
    assert m, f"no @media (max-width: {width}px) block"
    start = m.end()
    depth = 1
    i = start
    while depth:
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
        i += 1
    return CSS[start:i - 1]


def _base_css():
    """The stylesheet with every @media block removed.

    `_declarations(CSS)` includes the media blocks, so a declaration that
    appears in both the desktop rule and a media query is indistinguishable
    from one that is desktop-only. A test that re-injects a defect into the
    desktop rule needs the desktop rules alone, or the mobile copy satisfies
    the assertion and the guard reports green on the bug.
    """
    out = CSS
    while True:
        m = re.search(r"@media[^{]*\{", out)
        if not m:
            return out
        start = m.end()
        depth = 1
        i = start
        while depth:
            if out[i] == "{":
                depth += 1
            elif out[i] == "}":
                depth -= 1
            i += 1
        out = out[:m.start()] + out[i:]


def _declarations(block):
    """selector -> concatenated declarations, for every rule in a block.

    Grouped selectors have to be expanded. `.memories-sidebar, .diary-sidebar
    { width: auto }` is the natural way to write the rule, and a parser that
    only looks for `selector {` misses it entirely -- which is how this
    suite's first version reported three columns as unrestyled while the
    stylesheet was correct.
    """
    out = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _strip_comments(block)):
        for selector in (s.strip() for s in selectors.split(",")):
            if selector and not selector.startswith("@"):
                out.setdefault(selector, []).append(body)
    return out


def _decls(block_map, selector):
    """Every declaration for a selector in a media block, joined.

    Returns "" when the selector is absent, so a single assertTrue covers both
    "not restyled" and "restyled wrongly". Deliberately not assertIn against
    the map: a 20-key dict in a failure message is unreadable, and one of
    these assertions already failed that way.
    """
    return " ".join(block_map.get(selector, []))


def _assert_declares(case, block_map, selector, declaration, why):
    merged = _decls(block_map, selector)
    case.assertTrue(
        declaration in merged,
        f"{selector} must declare `{declaration}` below the breakpoint ({why}); got: {merged.strip()!r}",
    )


class StackedPaneVisibilityTests(unittest.TestCase):
    """A stacked pane that stays a scroll container collapses to nothing.

    Every desktop scroll pane is a flex item with a ZERO flex basis --
    `flex: 1`, or `flex: 1 1 0` with an explicit `min-height: 0` -- and each
    lives inside a layout that becomes a column of `height: auto` below the
    breakpoint. A pane that is still `overflow-y: auto` is sized from that
    zero basis, so the auto-height parent resolves against zero and the
    content renders into a box with no height. There is no error and no
    overflow to see: the tab simply looks empty on a phone.

    This is not hypothetical, and it has now bitten twice, one level apart.
    The memories pane was released and the diary pane was not. Then the
    diary pane was released and `#diary-dates-list` -- the date and
    search-results list, carrying `flex: 1 1 0` and `min-height: 0` -- was
    not, so searching returned nothing visible while the entries pane
    looked fine. Fixing the one pane you happened to be looking at is not
    evidence the others are fine, so the derived test below walks the whole
    class rather than a list of instances.
    """

    _PANES = (".memories-main", ".diary-main")
    # Not detail panes, but the same trap: these are the scrollable lists
    # inside the two sidebars.
    _LISTS = (".memories-list-scroll", "#diary-dates-list")

    def setUp(self):
        self.tablet = _declarations(_media_block(900))

    def test_every_stacked_pane_is_released(self):
        for selector in self._PANES + self._LISTS:
            with self.subTest(selector=selector):
                merged = _decls(self.tablet, selector)
                # Assert presence first. _decls returns "" for an absent
                # selector, and every assertNotIn below would pass vacuously
                # on it -- which is exactly what happened when the diary
                # release line was deleted to test this guard.
                self.assertTrue(
                    merged.strip(),
                    f"{selector} is not restated below the breakpoint at all, "
                    f"so it keeps its full-width zero-basis scroll container",
                )
                self.assertNotIn(
                    "overflow-y: auto", merged,
                    f"{selector} keeps its own scroll below the breakpoint, so "
                    f"a zero flex-basis in a height:auto column collapses it; got: {merged.strip()!r}",
                )

    def test_no_stacked_pane_keeps_a_zero_flex_basis(self):
        # flex: 1 and flex: 1 1 0% both mean basis 0. flex: 0 0 auto is the
        # only form that lets the pane contribute its content height.
        for selector in self._PANES + self._LISTS:
            with self.subTest(selector=selector):
                merged = _decls(self.tablet, selector)
                self.assertTrue(merged.strip(), f"{selector} is not restated")
                for zero_basis in ("flex: 1;", "flex: 1 1 0;", "flex: 1 1 0%"):
                    self.assertNotIn(
                        zero_basis, merged,
                        f"{selector} re-applies a zero flex-basis below the "
                        f"breakpoint; got: {merged.strip()!r}",
                    )

    def test_the_sidebars_are_the_only_bounded_scroll_region(self):
        """The inner lists are released, so their column must scroll.

        With `.memories-list-scroll` and `#diary-dates-list` content-sized,
        a `max-height` on the column does nothing on its own -- the content
        would spill out of an un-scrolling box. Each column is the bounded
        scroll region instead, which is also why there is only one scroll
        area per column rather than a nested one.

        The diary has two bounded columns now: the entry list, and the
        calendar rail above it. A single `.diary-sidebar` entry would leave
        one of them unbounded, and an unbounded column of stacked entries
        pushes the detail pane off the bottom of the screen.
        """
        for selector in (".memories-list-col", ".diary-sidebar", ".diary-list-col"):
            with self.subTest(selector=selector):
                merged = _decls(self.tablet, selector)
                self.assertTrue(merged.strip(), f"{selector} is not restated")
                self.assertIn(
                    "max-height", merged,
                    f"{selector} is bounded, which only helps if it scrolls",
                )
                self.assertIn(
                    "overflow-y: auto", merged,
                    f"{selector} is bounded but cannot scroll, so the list "
                    f"inside it is clipped instead of reachable",
                )

    def test_every_zero_basis_scroll_container_is_released(self):
        """Belt and braces: derive the panes from the stylesheet, not a list.

        A hardcoded list of panes is a list that goes stale the moment a tab
        is added -- and it already was stale once. The first version of this
        derived only `*-main` selectors, so it passed while the diary's date
        and search-results list was still collapsing. That list is not a
        detail pane; it is the list the user searches, and it carries the
        same `flex: 1 1 0` plus an explicit `min-height: 0`.

        The real class is therefore "a scroll container with a zero flex
        basis", and that is what this derives: any full-width selector that
        declares vertical scrolling *and* `flex: 1` / `flex: 1 1 0`. Each
        one must be restated below the breakpoint, or it collapses to zero
        height inside the auto-height column and silently renders nothing.
        """
        css = _strip_comments(CSS)
        base = _declarations(css)
        # _declarations maps a selector to a LIST of declaration blocks, so
        # test membership with the joining helper rather than `in`, which
        # would be an exact-element list test and silently never match.
        zero_basis = ("flex: 1;", "flex: 1 1 0;", "flex: 1 1 0%")
        trapped = set()
        for selector in base:
            decls = _decls(base, selector)
            if "overflow-y: auto" not in decls:
                continue
            if any(basis in decls for basis in zero_basis):
                trapped.add(selector)
        self.assertTrue(
            trapped,
            "no zero-basis scroll container is left -- this test is "
            "stale, or the panes were all restated at full width",
        )
        for selector in sorted(trapped):
            with self.subTest(selector=selector):
                merged = _decls(self.tablet, selector)
                self.assertTrue(
                    merged.strip(),
                    f"{selector} is a full-width zero-basis scroll container "
                    f"but the mobile block never mentions it, so it renders "
                    f"into a box with no height",
                )


class StatusWidgetLayoutTests(unittest.TestCase):
    """The status widget sits on the bottom edge of the left rail of four pages.

    There is no browser here, so what is pinned is the set of ways it can go
    wrong silently: pinned to nothing (a rail that is not a flex column, or a
    widget with no `margin-top: auto`), pinned to a rail that is not the left
    one, or — the one that costs the whole tab — a strip added *below* the
    layout, which pushes the page past `height: calc(100vh - 140px)` and makes
    a three-pane reading screen scroll.

    The mobile half is the same zero-basis trap as every other pane, so it is
    checked through the same derived rule rather than by listing the widget.
    """

    # The left rail of each page: categories, calendar, graph categories and
    # the live session list. The live rail is an <aside>, so the markup capture
    # below matches either tag -- an assumption of <div> silently returned "" and
    # would have reported a present widget as missing.
    _RAILS = (
        ".memories-sidebar", ".diary-sidebar", ".graph-sidebar", ".live-sidebar",
    )
    # Markup and stylesheet spell this differently; both are pinned.
    _WIDGET = ".srv-status"
    _WIDGET_CLASS = 'class="srv-status"'

    def setUp(self):
        self.base = _declarations(CSS)
        self.tablet = _declarations(_media_block(900))

    def test_the_widget_is_a_child_of_each_left_rail(self):
        """Not a sibling strip, and not a child of the scroller.

        A widget inside the scrolled element scrolls out of sight on a long
        list, which for the memories rail is the moment it is least wanted.
        """
        for rail in self._RAILS:
            with self.subTest(rail=rail):
                body = self._rail_markup(rail)
                self.assertTrue(body, msg=f"{rail} not found in the template")
                self.assertIn(self._WIDGET_CLASS, body,
                              msg=f"the status widget is not inside {rail}")

    def _rail_markup(self, rail):
        cls = rail[1:]
        # The closing tag is bound by the *opening* line's indentation and its
        # tag name, via backreferences. A fixed indent cannot work: the graph
        # rail is nested one level deeper, and the diary rail contains a
        # multi-line child div whose own `</div>` would otherwise end the
        # capture early. The tag name is matched rather than assumed because the
        # live rail is an <aside>: `<div class="live-sidebar">` never matches,
        # and an empty capture reads as "the widget is missing" on a template
        # that has it.
        m = re.search(
            r'^( *)<(div|aside) class="%s"[^>]*>(.*?)\n\1</\2>' % re.escape(cls),
            SOURCE, re.S | re.M,
        )
        return m.group(3) if m else ""

    def test_each_rail_is_a_flex_column_so_the_widget_can_pin(self):
        """Without `display: flex; flex-direction: column` there is no free
        space for `margin-top: auto` to push against, and the widget renders
        directly under the content instead of on the bottom edge."""
        for rail in self._RAILS:
            with self.subTest(rail=rail):
                decls = _decls(self.base, rail)
                self.assertTrue(decls.strip(), msg=f"{rail} has no desktop rule")
                self.assertIn("display: flex", decls)
                self.assertIn("flex-direction: column", decls)
                self.assertIn("overflow: hidden", decls,
                              msg=f"{rail} is not hidden, so a pinned widget "
                                  f"cannot be relied on and the rail may grow")

    def test_the_widget_takes_no_flex_space_of_its_own(self):
        decls = _decls(self.base, self._WIDGET)
        self.assertTrue(decls.strip(), msg="the widget has no desktop rule")
        self.assertIn("flex: 0 0 auto", decls,
                      msg="a growing widget steals the rail from the list above it")
        self.assertIn("margin-top: auto", decls,
                      msg="without this the widget is not on the bottom edge")

    def test_no_strip_is_added_below_any_layout(self):
        """`calc(100vh - 140px)` leaves no room for a footer under the panes."""
        for layout in (".memories-layout", ".diary-layout", ".live-layout"):
            with self.subTest(layout=layout):
                # Bound to the opening line's own indent, so each layout's own
                # closing tag ends the capture rather than whichever `</div>`
                # happens to come first.
                m = re.search(
                    r'^( *)<div class="%s">(.*?)\n\1</div>' % re.escape(layout[1:]),
                    SOURCE, re.S | re.M,
                )
                self.assertTrue(m, msg=f"{layout} markup not found")
                after = SOURCE.split(m.group(0), 1)[1]
                # The next thing in the page must be another page, not a strip
                # belonging to this layout.
                following = after.lstrip().split("\n", 1)[0]
                self.assertTrue(
                    following.strip().startswith("</div>") or "page-" in following,
                    msg=f"{layout} is followed by {following[:60]!r}, which "
                        f"looks like a sibling strip below the panes",
                )

    def test_the_widget_is_not_a_zero_basis_scroll_container(self):
        """The same trap as the panes, in miniature: it must not scroll."""
        decls = _decls(self.base, self._WIDGET)
        for basis in ("flex: 1;", "flex: 1 1 0;", "flex: 1 1 0%"):
            self.assertNotIn(basis, decls)
        self.assertNotIn("overflow-y: auto", decls,
                         msg="the widget scrolls instead of the rail it is pinned to")

    def test_the_memories_rail_scrolls_on_its_list_not_itself(self):
        """The rail became `overflow: hidden` so the widget can pin.

        The scroller moved to `#categories-sidebar`, and it has to move with a
        non-zero basis: a zero basis inside the stacked auto-height column
        resolves against nothing, which is the empty-tab bug in miniature.
        """
        rail = _decls(self.base, ".memories-sidebar")
        self.assertNotIn("overflow-y: auto", rail,
                         msg="the rail is the scroller again, so the widget scrolls away")
        chips = _decls(self.base, "#categories-sidebar")
        self.assertTrue(chips.strip(), msg="#categories-sidebar has no rule at all")
        self.assertIn("overflow-y: auto", chips)
        for basis in ("flex: 1 1 0;", "flex: 1 1 0%", "flex: 1;"):
            self.assertNotIn(basis, chips,
                             msg="#categories-sidebar is a zero-basis scroller")
        self.assertIn("min-height: 0", chips)

    def test_the_graph_rail_scrolls_on_its_list_not_itself(self):
        """The graph rail took the same shape as the memories rail.

        Its chips column is the scroller, on a non-zero basis, and the rail
        itself is `overflow: hidden` — otherwise the newly pinned widget
        scrolls away with the chips on a vault with many categories, and on a
        phone a zero-basis scroller inside the stacked column resolves against
        nothing at all.
        """
        rail = _decls(_declarations(_base_css()), ".graph-sidebar")
        self.assertNotIn("overflow-y: auto", rail,
                         msg="the graph rail is the scroller again, so the "
                             "status widget scrolls away with the chips")
        chips = _decls(self.base, "#graph-categories-sidebar")
        self.assertTrue(chips.strip(), msg="#graph-categories-sidebar has no rule at all")
        self.assertIn("overflow-y: auto", chips)
        for basis in ("flex: 1 1 0;", "flex: 1 1 0%", "flex: 1;"):
            self.assertNotIn(basis, chips,
                             msg="#graph-categories-sidebar is a zero-basis scroller")
        self.assertIn("min-height: 0", chips)

    def test_the_category_chips_do_not_stretch_to_fill_the_rail(self):
        """The one assertion missing when this broke, and the reason it passed.

        The rail became a flex column so the status widget could pin to its
        bottom edge, and the chip list was made the scroller with
        `flex: 1 1 auto`. A *growing* chip list is handed the leftover column
        height — and `.chips-container` is a wrapping **row** flex container
        whose default `align-items: stretch` matches every chip to its line's
        cross size, so the chips themselves became full-height blocks filling
        the rail. The category count is small and constant; a rail that tall is
        the chip list, not the data.

        The previous test checked the basis was not zero and said nothing about
        growth, so `1 1 auto` passed a test written for `0 1 auto`. Both halves
        are asserted now: shrinkable, and not growable.
        """
        chips = _decls(self.base, "#categories-sidebar")
        self.assertTrue(chips.strip(), msg="#categories-sidebar has no rule at all")
        grow = re.search(r"flex:\s*(\d+)", chips)
        self.assertIsNotNone(grow, msg="#categories-sidebar has no flex shorthand")
        self.assertEqual(
            grow.group(1), "0",
            msg="the chip list may shrink and scroll but must not claim the "
                "rail's free space -- the widget's margin-top:auto is the only "
                f"thing that wants it; got flex: {grow.group(0)}",
        )
        # Belt and braces: if the list is ever shorter than the space it was
        # given, the lines must pack at the top rather than stretch to fill.
        self.assertIn("align-content: flex-start", chips,
                      msg="without this the wrapped chip lines stretch to the "
                          "container height even when the container is taller")
        # And the chips themselves must not be stretched by the default
        # `align-items: stretch` on their wrapping row container.
        self.assertNotIn("align-items: stretch", chips)

    def test_stacked_the_widget_still_renders(self):
        """Below the breakpoint the rail is a bounded scroller, so the widget
        is at the end of its content rather than pinned. That is acceptable;
        being *inside* a `height: 0` box is not, and `flex: 0 0 auto` is what
        rules that out in a column."""
        self.assertNotIn("display: none", _decls(self.tablet, self._WIDGET))
        self.assertIn("flex: 0 0 auto", _decls(self.base, self._WIDGET))


class LayoutInlineStyleTests(unittest.TestCase):
    """An inline style beats a stylesheet rule of any specificity."""

    @classmethod
    def setUpClass(cls):
        # Everything from the layout breakpoint onwards is the mobile sheet, so
        # a declaration has to appear *above* it to be overridable: searched over
        # the whole stylesheet, the mobile `#graph-container { height: 62vh }`
        # satisfies a check that the desktop 70vh is still there.
        cls.desktop = CSS[:CSS.index("@media (max-width: 900px)")]

    # Every box whose mobile rules could be outranked: the five laid out by
    # `class`, and the graph canvas, which is addressed by `id`. The attribute
    # pattern is carried per row because a single `class=` form would not match
    # that last one.
    _CONTAINERS = (
        (r'class="memories-layout"', ".memories-layout"),
        (r'class="diary-layout"', ".diary-layout"),
        (r'class="graph-layout"', ".graph-layout"),
        (r'class="graph-sidebar"', ".graph-sidebar"),
        (r'class="graph-main"', ".graph-main"),
        (r'id="graph-container"', "#graph-container"),
        (r'class="graph-detail-pane"', ".graph-detail-pane"),
    )

    # One test covers the group: "this container carries an inline style" is a
    # single property, asserted here over every container that could hide a
    # mobile rule behind one.
    def test_no_layout_container_carries_an_inline_style(self):
        for pattern, label in self._CONTAINERS:
            with self.subTest(container=label):
                self.assertIsNone(
                    re.search(pattern + r"[^>]*\bstyle=", SOURCE),
                    f"{label} has an inline style, which overrides the mobile "
                    f"media query no matter what the stylesheet says",
                )

    # The desktop declarations the mobile block has to be able to override, and
    # which therefore have to come from the stylesheet. `#graph-container` is
    # in the list because an inline `height: 70vh` outranked the mobile 62vh
    # rule at every specificity, and vis.js sizes its canvas once from that
    # box -- so the rule that never applied was the rule deciding how much room
    # the user has to pan.
    _FROM_STYLESHEET = (
        (r"\.graph-layout\s*\{[^}]*display:\s*flex", ".graph-layout",
         "display: flex", "the mobile block's column override has to beat a side-by-side row"),
        (r"#graph-container\s*\{[^}]*(?<![\w-])height\s*:", "#graph-container",
         "height:", "the mobile 62vh share has to override the desktop 70vh floor"),
    )

    # One test covers the group: "this desktop declaration lives in the
    # stylesheet" is a single property, asserted per row over the elements whose
    # mobile rules would otherwise be inert.
    def test_the_graph_desktop_sizes_come_from_the_stylesheet(self):
        for pattern, label, declaration, why in self._FROM_STYLESHEET:
            with self.subTest(selector=label):
                self.assertRegex(
                    self.desktop, pattern,
                    f"{label} must declare `{declaration}` in the desktop sheet "
                    f"so the mobile block can override it: {why}",
                )


class GraphDetailPaneTests(unittest.TestCase):
    """The graph's read-only detail panel for the selected Fact / DiaryEntry.

    - The pane is a flex sibling to the RIGHT of the canvas (filters left,
      graph centre, details right), so revealing it still narrows the canvas —
      which is why openInGraph defers its focus until the width-only
      ResizeObserver refit has landed.
    - A definite flex basis, not `flex: 1`: a zero basis in an auto-height
      column resolves against no available space and the pane renders empty
      (the mobile-pane trap, desktop edition).
    - `hidden` on a styled element loses to any `display` declaration, so the
      stylesheet must carry the explicit `[hidden]` rule.
    - The panel renders the *record* (from the Memories/Diary stores), not the
      vis node — the node only carries a truncated name.
    """

    def setUp(self):
        self.desktop = _declarations(_base_css())
        self.tablet = _declarations(_media_block(900))

    def _panel_segment(self):
        start = SOURCE.index("function showGraphItemPanel(")
        end = SOURCE.index("async function saveEdit", start)
        return SOURCE[start:end]

    def _function(self, name):
        # Bounded by the NEAREST top-level declaration, async or not: a plain
        # `\n  function ` marker runs past an intervening `async function`
        # and pulls the next function's body into the slice.
        start = SOURCE.index("function %s(" % name)
        ends = []
        for marker in ("\n  function ", "\n  async function "):
            try:
                ends.append(SOURCE.index(marker, start + 1))
            except ValueError:
                pass
        return SOURCE[start:min(ends)]

    def test_the_pane_sits_to_the_right_of_the_canvas(self):
        # Column order is DOM order in a flex row: filters left, canvas
        # centre, details right. The `class="..."` strings are HTML-only —
        # the stylesheet spells these selectors with a leading dot.
        sidebar = SOURCE.index('class="graph-sidebar"')
        main = SOURCE.index('class="graph-main"')
        pane = SOURCE.index('id="graph-detail-pane"')
        self.assertLess(sidebar, main, "the category rail stays on the left")
        self.assertLess(main, pane,
                        "the detail pane sits to the right of the canvas")
        # `pane` indexes the id attribute, which is already past `<aside ` —
        # search backwards for the opening tag, not forwards from here.
        tag = SOURCE.rindex('<aside', 0, pane)
        self.assertRegex(SOURCE[tag:tag + 120], r'\bhidden',
                         "the pane must ship hidden")

    def test_the_pane_has_a_definite_basis_and_scroll_of_its_own(self):
        _assert_declares(
            self, self.desktop, ".graph-detail-pane", "flex: 0 0 auto",
            "a zero flex basis in an auto-height column renders the pane empty")
        _assert_declares(
            self, self.desktop, ".graph-detail-pane", "overflow-y: auto",
            "a long record must scroll inside the pane, not the page")

    def test_the_hidden_attribute_beats_the_display_declaration(self):
        hidden = _decls(self.desktop, ".graph-detail-pane[hidden]")
        self.assertIn(
            "display: none", hidden,
            "`hidden` loses to any other display declaration; without the "
            "explicit rule the pane can never be closed")

    def test_the_graph_page_spans_the_full_width(self):
        # `.page` caps three-column reading layouts at 1100px; the graph tab
        # holds a 200px rail, the canvas and a 300px pane, so it joins
        # memories and diary in overriding the cap.
        self.assertIn(
            "max-width: none", _decls(self.desktop, "#page-graph"),
            "#page-graph must override the .page cap the way "
            "#page-memories/#page-diary do")

    def test_a_click_on_a_record_opens_the_panel(self):
        init = self._function("initNetwork")
        self.assertRegex(
            init, r'(?s)network\.on\("click".*?showGraphItemPanel\(params\.nodes\[0\]\)',
            "a plain node click must open the detail panel")
        self.assertRegex(
            init,
            r'(?s)network\.on\("click".*?params\.nodes\.length === 0.*?hideGraphItemPanel\(\)',
            "an empty-canvas click must close it")

    def test_open_in_graph_shows_the_panel_before_the_focus_animation(self):
        seg = self._function("openInGraph")
        self.assertIn("showGraphItemPanel(id);", seg)
        self.assertLess(
            seg.index("showGraphItemPanel(id);"), seg.index("network.focus("),
            "the pane reveal — and the ResizeObserver refit it triggers — must "
            "land before the animated focus, or the refit overrides the focus")

    def test_show_on_map_opens_the_panel_in_both_branches(self):
        seg = self._function("showOnMap")
        self.assertEqual(
            seg.count("showGraphItemPanel(id);"), 2,
            "both the already-on-map and the just-added branches must open "
            "the panel")

    def test_the_panel_renders_the_record_not_the_node(self):
        seg = self._panel_segment()
        self.assertIn("memories.find", seg)
        self.assertIn("api.get('memories/'", seg,
            "a fact missing from the loaded list must be fetched, not skipped")
        self.assertIn("ensureDiaryEntries()", seg)
        self.assertIn("api.get('diary/'", seg,
            "a diary entry missing from the cached list must be fetched")
        self.assertIn("_graphPanelToken", seg,
            "a stale fetch must not overwrite a newer click's pane")

    def test_the_footer_open_button_routes_by_record_kind(self):
        seg = self._function("openGraphPanelRecord")
        self.assertRegex(seg, r"if \(r\.isDiary\).*?selectDiaryEntry\(r\.id\)")
        self.assertIn("jumpToMemory(r.id)", seg)


class GraphCategoryScopeTests(unittest.TestCase):
    """The graph rail's category chips must count the current scope.

    The chips mirror the memories rail's counts, which are scoped through
    memPassesScope; the graph rail counted the whole vault, so a scoped
    session saw totals it could not reach. The defect had two halves: the
    render function counted gross AND _cfApply never re-rendered the rail,
    so fixing only the counter would have left the chips stale on every
    scope change — a helper tested in isolation is not a test of its call
    site. One subtlety the first test pins: the chip LIST stays vault-wide
    (it seeds the first-visit init, and a scoped list would leave later
    scope switches no chip to re-enable) while the counts are scoped.
    """

    def _render_fn(self):
        start = SOURCE.index("function renderGraphCategorySidebar(")
        end = SOURCE.index("function setCategory(", start)
        return SOURCE[start:end]

    def _cf_apply(self):
        start = SOURCE.index("async function _cfApply(")
        end = SOURCE.index("async function onClientFilterChange(", start)
        return SOURCE[start:end]

    def test_fact_counts_run_through_the_scope_predicate(self):
        seg = self._render_fn()
        # One regex on purpose: the list takes EVERY memory
        # (categories.add) while the count takes only the scoped ones
        # (memPassesScope). Asserting them separately could pass on a
        # version that scoped the list too — the silent-chip regression.
        self.assertRegex(
            seg,
            r"memories\.forEach\(m => \{\s*categories\.add\(m\.category\);\s*"
            r"if \(memPassesScope\(m\)\) counts\[m\.category\]")
        self.assertFalse(
            "counts[m.category] = (counts[m.category] || 0) + 1; });" in seg,
            msg="the unscoped forEach must not return")

    def test_diary_counts_run_through_the_scope_predicate(self):
        seg = self._render_fn()
        self.assertIn("diaryEntries.filter(memPassesScope)", seg)
        self.assertFalse("counts['Diary'] = diaryEntries.length;" in seg,
                         msg="the gross diary count must not return")

    def test_the_chip_list_stays_vault_wide(self):
        seg = self._render_fn()
        self.assertIn("categories.add(m.category);", seg)
        self.assertIn("diaryEntries && diaryEntries.length > 0", seg)
        self.assertIn("categories.add('Diary');", seg)

    def test_a_scope_change_re_renders_the_graph_chips(self):
        seg = self._cf_apply()
        self.assertIn("renderGraphCategorySidebar();", seg)


class MobileBreakpointTests(unittest.TestCase):
    def setUp(self):
        self.tablet = _declarations(_media_block(900))
        self.phone_body = _media_block(640)
        self.phone = _declarations(self.phone_body)

    def test_every_fixed_width_column_collapses(self):
        # The layouts are 180+280+flex, flex+340+250 and 200+flex+300. Anything
        # left at its desktop width overflows a 360px viewport -- the diary's
        # two rails are 590px on their own.
        for selector in (
            ".memories-sidebar", ".memories-list-col", ".diary-sidebar",
            ".diary-list-col", ".graph-sidebar", ".graph-detail-pane",
        ):
            with self.subTest(selector=selector):
                _assert_declares(
                    self, self.tablet, selector, "width: auto",
                    "a fixed-width column does not fit a phone",
                )

    def test_the_three_layouts_stack(self):
        for selector in (".memories-layout", ".diary-layout", ".graph-layout"):
            with self.subTest(selector=selector):
                _assert_declares(
                    self, self.tablet, selector, "flex-direction: column",
                    "the side-by-side columns must stack",
                )

    def test_the_per_pane_viewport_height_is_released(self):
        # calc(100vh - Npx) + overflow-y:auto on every pane means the page
        # itself cannot scroll on touch.
        for selector in (".memories-layout", ".diary-layout", ".graph-layout"):
            with self.subTest(selector=selector):
                _assert_declares(
                    self, self.tablet, selector, "height: auto",
                    "the fixed-height pane traps the page scroll",
                )
        merged = " ".join(self.tablet[".memories-main"])
        self.assertTrue("overflow-y: visible" in merged, merged)

    def test_ios_zoom_guard(self):
        # Under 16px iOS zooms on focus and the zoom cannot be undone, so the
        # form never recovers its layout.
        _assert_declares(
            self, self.tablet, "input", "font-size: 16px",
            "iOS zooms on focus below 16px and cannot be undone",
        )
        for selector in ("textarea", "select"):
            with self.subTest(selector=selector):
                _assert_declares(self, self.tablet, selector, "font-size: 16px",
                                 "same zoom guard as input")

    def test_body_uses_clip_not_hidden_for_horizontal_overflow(self):
        # `overflow-x: hidden` makes body a scroll container, which unsticks
        # the sticky nav. `clip` does not create one.
        merged = _decls(self.tablet, "body")
        self.assertTrue("overflow-x: clip" in merged, merged)
        self.assertFalse("overflow-x: hidden" in merged, merged)

    def test_tabs_scroll_sideways_instead_of_wrapping(self):
        _assert_declares(self, self.tablet, ".tabs", "overflow-x: auto",
                         "five tab buttons do not fit a phone")
        _assert_declares(self, self.tablet, ".tab-btn", "white-space: nowrap",
                         "a wrapped tab is a two-line target")

    def test_chip_rails_go_horizontal(self):
        # The graph sidebar sets flex-direction: column inline, so this needs
        # !important to win; the memories rail is flex-wrap by default.
        _assert_declares(self, self.tablet, ".chips-container",
                         "flex-direction: row !important",
                         "the inline column wins without !important")
        _assert_declares(self, self.tablet, ".chips-container", "flex-wrap: nowrap",
                         "a wrapping rail is a tall column on a phone")

    def test_graph_canvas_is_bounded_on_a_phone(self):
        # 70vh with min-height 500px is most of a phone's screen, and the
        # canvas keeps whatever width it was constructed at unless vis is
        # told to refit.
        # The exact values are pinned in GraphSizeOnMobileTests, which also
        # pins the flex basis; asserting them here as well would just be two
        # tests that have to be edited together.
        _assert_declares(self, self.tablet, "#graph-container", "height:",
                         "the desktop 70vh is most of a phone screen")
        _assert_declares(self, self.tablet, "#graph-container", "min-height:",
                         "the desktop 500px floor overflows a short viewport")

    def test_touch_targets_are_grown_on_a_phone(self):
        _assert_declares(self, self.phone, ".alpha-btn", "width: 36px",
                         "28px is below the reliable thumb size")
        _assert_declares(self, self.phone, ".item-menu-btn", "min-height: 36px",
                         "the per-card menu button is tiny on touch")


class GraphTouchTests(unittest.TestCase):
    def test_long_press_opens_the_context_menu(self):
        # `oncontext` is a right-click. Without `hold` the build-graph menu is
        # unreachable without a mouse.
        self.assertIn('network.on("hold"', SOURCE)
        self.assertIn("showContextMenu(nativeEvent, params.nodes[0])", SOURCE)

    def test_context_menu_is_clamped_to_its_measured_size(self):
        # The old Math.min(x, innerWidth - 200) assumed a >=200px viewport:
        # on a phone the menu ran off the right edge, and a tap in the left
        # margin produced a negative left.
        self.assertNotIn("window.innerWidth - 200", SOURCE)
        self.assertNotIn("window.innerHeight - 200", SOURCE)
        self.assertIn("contextMenu.offsetWidth", SOURCE)
        self.assertIn("contextMenu.offsetHeight", SOURCE)
        self.assertRegex(SOURCE, r"left\s*=\s*Math\.max\(")
        self.assertRegex(SOURCE, r"top\s*=\s*Math\.max\(")

    def test_context_menu_is_measured_after_it_is_in_the_document(self):
        # offsetWidth is 0 on a detached element, so the clamp would use 0 and
        # pin the menu to the right edge.
        append = SOURCE.index("document.body.appendChild(contextMenu);")
        measure = SOURCE.index("contextMenu.offsetWidth")
        self.assertLess(append, measure)

    def test_the_canvas_refits_when_its_container_resizes(self):
        # vis.js sizes its canvas once, at construction. Stacking the layout
        # below 900px leaves the canvas at the old width, i.e. clipped.
        self.assertIn("graphResizeObserver", SOURCE)
        self.assertIn("network.redraw()", SOURCE)
        self.assertIn("network.fit(", SOURCE)

    def test_the_observer_is_detached_before_the_network_is_destroyed(self):
        # rebuildNetwork() replaces the canvas; a live observer would keep
        # fitting a destroyed one. The teardown moved into destroyNetwork() so
        # that loadGraph() could use it too, and this follows the property to
        # wherever it lives rather than pinning a function body -- the order is
        # what matters, not which function holds it.
        idx = SOURCE.index("function destroyNetwork()")
        body = SOURCE[idx:idx + 600]
        self.assertIn("graphResizeObserver.disconnect()", body)
        self.assertLess(
            body.index("graphResizeObserver.disconnect()"),
            body.index("network.destroy()"),
            "disconnecting first means no callback can fire against a network "
            "that is being torn down",
        )


class GraphSizeOnMobileTests(unittest.TestCase):
    """The graph canvas has to be given a real height on a phone.

    Two distinct defects produced a narrow band, and neither is visible in
    the markup:

    * the container was sized by an inline style, so the mobile height rule
      never applied and the desktop 70vh / 500px floor stood;
    * `flex: 1` is a *zero* flex basis. In the stacked auto-height column
      the mobile block creates, that resolves against no available space --
      the same trap as the zero-basis scroll panes, which had already
      produced two separate "the tab is just empty" bugs.

    So the height is stated rather than inherited, and the basis is pinned
    to content.
    """

    @classmethod
    def setUpClass(cls):
        cls.tablet = _declarations(_media_block(900))
        cls.base = _declarations(CSS)

    def test_the_container_is_not_a_zero_flex_basis_on_mobile(self):
        _assert_declares(
            self, self.tablet, "#graph-container", "flex: 0 0 auto",
            "flex:1 is a zero basis, which resolves against nothing once "
            "the layout is a stacked auto-height column")

    def test_the_container_gets_a_usable_share_of_the_screen(self):
        _assert_declares(
            self, self.tablet, "#graph-container", "height: 62vh",
            "the desktop 70vh leaves no room once controls are stacked above it")
        _assert_declares(
            self, self.tablet, "#graph-container", "min-height: 420px",
            "a 62vh share of a short screen is not enough to pan a graph in")

    def test_the_sidebar_is_bounded_so_it_cannot_push_the_graph_off_screen(self):
        _assert_declares(
            self, self.tablet, ".graph-sidebar", "max-height: 28vh",
            "stacked above the graph, an unbounded sidebar of category chips "
            "leaves only a band of canvas")
        _assert_declares(
            self, self.tablet, ".graph-sidebar", "overflow-y: auto",
            "a bound that cannot scroll clips instead of scrolling")

    def test_the_desktop_sizes_still_come_from_the_stylesheet(self):
        for selector in (".graph-sidebar", ".graph-main", "#graph-container"):
            with self.subTest(selector=selector):
                self.assertTrue(
                    selector in self.base,
                    f"{selector} must be a stylesheet rule so the mobile "
                    f"block can override it; got: {sorted(self.base)}",
                )

class ClientFilterOnMobileTests(unittest.TestCase):
    """The client filter is a child of the tab bar, not of the nav or a sidebar.

    It is invisible on a phone for a reason no amount of restyling the
    control itself would fix: `.tabs` becomes a horizontal scroller below
    900px, and the filter is the *last* flex child of that rail, sitting
    after every tab button off the right edge of the screen. The
    scrollbar is hidden, so nothing advertises that it is there -- the
    markup is present and the control works, it is simply unreachable.

    The fix is to stop the bar being a scroller, wrap it, and give the
    filter a full-width row of its own. Both halves matter, and the second
    is the one that is easy to miss: `.cf-panel` is `position: absolute`,
    so inside a scroller it is clipped to the rail and opens as a sliver.
    """

    @classmethod
    def setUpClass(cls):
        cls.tablet = _declarations(_media_block(900))
        cls.base = _declarations(CSS)

    def test_the_filter_is_a_child_of_the_tab_bar_in_the_markup(self):
        """Pin the structure the bug depends on.

        If the filter is ever moved out of `.tabs`, the wrapping rules
        below become inert rather than broken, and the panel rule
        (position: static) is no longer needed. This test is what makes
        that change visible instead of silent.
        """
        tabs = SOURCE.split('<div class="tabs">')[1].split("</div>\n")[0]
        self.assertIn('id="cf-wrap"', tabs,
                      "the client filter should still live inside the tab bar")
        self.assertIn("cf-bar", tabs,
                      "its wrapper needs the .cf-bar class for the mobile row")

    def test_the_tab_bar_stops_being_a_horizontal_scroller(self):
        _assert_declares(
            self, self.tablet, ".tabs", "flex-wrap: wrap",
            "otherwise the filter is the last thing in a scroller off-screen")
        _assert_declares(
            self, self.tablet, ".tabs", "overflow-x: visible",
            "an absolutely positioned panel is clipped by a scroller")

    def test_the_filter_gets_a_full_width_row_of_its_own(self):
        _assert_declares(
            self, self.tablet, ".cf-bar", "order: 99",
            "puts the filter after the tabs on its own wrapped line")
        _assert_declares(
            self, self.tablet, ".cf-bar", "width: 100%",
            "a full-width row rather than a sliver beside the last tab")
        _assert_declares(
            self, self.tablet, ".cf-bar", "margin-left: 0",
            "the desktop auto margin would fight the wrap")

    def test_the_panel_is_in_flow_so_it_is_not_clipped(self):
        _assert_declares(
            self, self.tablet, ".cf-panel", "position: static",
            "absolute positioning inside the rail would clip it")
        _assert_declares(
            self, self.tablet, ".cf-panel", "max-height: 50vh",
            "an unbounded list would run off the bottom of a phone screen")

    def test_the_control_and_its_items_are_reachable_by_thumb(self):
        _assert_declares(
            self, self.tablet, ".cf-btn", "min-height: 40px",
            "the desktop button is a few pixels tall")
        # A grouped rule is expanded by _declarations, so each selector is
        # its own key -- asking for ".cf-item, .cf-ctx" finds nothing and
        # reports a rule that is present.
        for selector in (".cf-item", ".cf-ctx"):
            _assert_declares(
                self, self.tablet, selector, "min-height: 40px",
                "the list items are the actual tap targets")

    def test_the_desktop_rule_survives(self):
        """The mobile fix must not have removed the desktop placement."""
        _assert_declares(
            self, self.base, ".cf-bar", "margin-left: auto",
            "the filter belongs to the right of the tab bar on desktop")


class DiaryColumnOrderTests(unittest.TestCase):
    """The diary tab is three columns, and only one order of them is the design.

    `.diary-layout` is a flex *row*, so the DOM order is the visual order:
    the calendar and search rail on the left, then the filtered entry list, then
    the entry being read taking all the remaining width. Nothing errors if the
    columns are swapped or the detail pane is pinned narrow -- the tab still
    renders, and still selects an entry -- it is simply not the screen that was
    asked for, and there is no browser here to see the difference.

    So these pin the three things a reorder would silently undo: which element
    each column is, what order they sit in, and that the detail pane is the
    flexible one rather than one of the fixed rails.

    The order below is **left to right**. This test asserted the opposite once
    (detail, list, rail) and failed on the shipped template for a long time
    before anyone asked whether the template or the test was right: the answer
    was that the template had been swapped by hand on purpose. A test that
    disagrees with the shipped layout is not a failing regression detector, it
    is a green suite covering a screen nobody ships.
    """

    # Left to right, and deliberately not `sorted()`-ed anywhere: the whole point
    # is the order, so it is spelled out and compared against itself.
    COLUMNS = ('class="diary-sidebar"', 'class="diary-list-col"', 'class="diary-main"')

    @classmethod
    def setUpClass(cls):
        cls.base = _declarations(CSS)
        layout = SOURCE.split('<div class="diary-layout">')[1].split('<div id="page-graph"')[0]
        cls.layout = layout

    def test_the_three_columns_are_in_the_intended_order(self):
        positions = []
        for column in self.COLUMNS:
            idx = self.layout.find(column)
            self.assertNotEqual(idx, -1, f"{column} is not in the diary layout")
            positions.append(idx)
        self.assertEqual(
            positions, sorted(positions),
            "the diary layout must read calendar rail, then entry list, then "
            "the entry being read -- a flex row renders DOM order left to right",
        )

    def test_the_rail_holds_search_above_the_calendar(self):
        self.assertIn(
            'id="diary-month-dates"', self.layout,
            "the month grid has to be in the left-hand rail",
        )
        # Search is a filter on the list, so it goes above the calendar: the
        # rail is the top-left of the screen and the field should be the
        # thing already under the cursor there.
        self.assertLess(
            self.layout.find('id="diary-search-input"'),
            self.layout.find('id="diary-month-dates"'),
            "search sits above the calendar in the rail",
        )

    def test_the_entry_list_is_wider_than_the_calendar_rail(self):
        """The list is what the user reads at; the rail is a control surface."""
        def width(selector):
            merged = _decls(self.base, selector)
            found = re.search(r"width:\s*(\d+)px", merged)
            self.assertIsNotNone(found, f"{selector} has no fixed width: {merged!r}")
            return int(found.group(1))

        self.assertGreater(
            width(".diary-list-col"), width(".diary-sidebar"),
            "the filtered entry list was asked to be wider than the rail",
        )

    def test_the_detail_pane_takes_the_leftover_width(self):
        merged = _decls(self.base, ".diary-main")
        self.assertIn("flex: 1", merged, merged)
        # A flex item's default `min-width: auto` refuses to shrink below its
        # content, so one long transcription would widen the detail pane and
        # squeeze the rails instead of wrapping.
        self.assertIn("min-width: 0", merged, merged)

    def test_the_pickers_come_before_the_entry_on_a_phone(self):
        """Stacked, DOM order is visual order, so `order` has to state it.

        The stacked column reads rail, entry list, then the entry being read,
        which is also how the desktop row reads. The two currently agree -- the
        rail is the first column in the DOM too -- so these rules are a no-op
        on the markup as it stands. They are pinned anyway, because the only
        failure that would make a phone unusable is someone reordering the
        columns on desktop and taking the stacked layout with them.
        """
        tablet = _declarations(_media_block(900))
        for selector in (".diary-sidebar", ".diary-list-col", ".diary-main"):
            with self.subTest(selector=selector):
                merged = _decls(tablet, selector)
                self.assertTrue(
                    "order:" in merged,
                    f"{selector} is not re-ranked on a phone, so the stacked "
                    f"column stops reading in the order the desktop row does; "
                    f"got: {merged.strip()!r}",
                )

    def test_the_list_column_holds_the_scroller_not_the_rail(self):
        """`#diary-dates-list` is the zero-basis scroller the mobile block releases.

        It moved out of `.diary-sidebar` and into `.diary-list-col`. If it were
        still in the rail, the rail's bounded `max-height` scroll region and
        the list's own released scroller would nest, and the entry list would
        be bounded twice.
        """
        # Both regions have to be *bounded*, and the order matters: the rail is
        # the first column, so a tail slice from `class="diary-sidebar"` to the
        # end of the layout swallows the list column -- and `#diary-dates-list`
        # is legitimately in there. That is why this test failed on a template
        # that was already correct: the assertion was reading a region three
        # times larger than the rail and calling it the rail. `_column` cuts at
        # whichever of the other two columns comes next.
        # Sorted by position, then each column is the slice between its own
        # opening tag and the next column's. Only the *following* boundaries may
        # be consulted -- a column that happens to be first has no earlier
        # sibling to stop at, which is exactly what made the old tail slice
        # reach past the rail into the list.
        # COLUMNS carries the opening tags verbatim because the order test
        # compares against them; strip the wrapper to get the class names back.
        names = [column.split('"')[1] for column in self.COLUMNS]
        boundaries = sorted(
            (self.layout.index(f'class="{name}"'), name) for name in names
        )

        def _column(name):
            at = next(i for i, n in boundaries if n == name)
            end = next((i for i, _ in boundaries if i > at), len(self.layout))
            return self.layout[at:end]

        list_col = _column("diary-list-col")
        self.assertIn('id="diary-dates-list"', list_col,
                      "the entry list column is where the scroller lives")
        self.assertNotIn('id="diary-dates-list"', _column("diary-sidebar"),
                         "the rail must not hold the list's scroller as well")


class DiaryFillsThePageTests(unittest.TestCase):
    """The diary tab has to use the whole screen, and two separate rules can
    silently take that away again.

    `.page` is `max-width: 1100px; margin: 0 auto` — right for a form page,
    wrong for a three-pane reading screen, which is why `#page-memories` has
    always overridden it and `#page-diary` did not. That leaves a gutter on
    both sides.

    The second is the `calc(100vh - Npx)` on the pane. N is the chrome above
    the pane, and the chrome includes this page's own padding — so the two
    defects compound: widen the page but leave N alone and the row is now
    *shorter* than the space it has, showing a dead band at the bottom. There
    is no browser here to see either, so they are pinned from the stylesheet.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = _declarations(CSS)

    def test_the_diary_page_is_not_capped_or_centred(self):
        merged = _decls(self.base, "#page-diary")
        self.assertTrue(
            "max-width: none" in merged,
            "#page-diary still inherits `.page`'s 1100px cap, so the three "
            f"columns are squeezed into the middle of the screen; got: {merged.strip()!r}",
        )
        self.assertTrue(
            "margin: 0 auto" not in merged,
            f"#page-diary re-centres itself; got: {merged.strip()!r}",
        )

    def test_the_pane_height_budget_matches_the_page_padding(self):
        """The subtracted constant must be the same chrome the page actually has.

        `.memories-layout` and `.diary-layout` sit under identical chrome —
        same nav, same tab rail, and the same page padding — so their budgets
        must be equal. They were not: the diary one subtracted 180px, sized for
        the 1.5rem page padding that full-bleed removed, which is where the
        remaining band of dead space came from.
        """
        def budget(selector):
            merged = _decls(self.base, selector)
            found = re.search(r"height:\s*calc\(100vh\s*-\s*(\d+)px\)", merged)
            self.assertIsNotNone(found, f"{selector} has no 100vh budget: {merged!r}")
            return int(found.group(1))

        self.assertEqual(
            budget(".diary-layout"), budget(".memories-layout"),
            "the diary and memory panes sit under the same chrome, so a "
            "different subtracted constant means one of them is not filling "
            "the page",
        )

    def test_the_diary_and_memory_pages_pad_alike(self):
        """The budget equality above is only meaningful if the padding matches."""
        diary = _decls(self.base, "#page-diary")
        memories = _decls(self.base, "#page-memories")
        pad = re.search(r"padding:\s*([^;]+);", diary)
        self.assertIsNotNone(pad, diary)
        self.assertEqual(
            pad.group(1).strip(),
            re.search(r"padding:\s*([^;]+);", memories).group(1).strip(),
            "the two full-bleed pages must pad alike, or the shared 100vh "
            "budget above is comparing two different chromes",
        )



class LiveNotesLayoutTests(unittest.TestCase):
    """The Live notes tab is the fourth full-bleed reading screen.

    It repeats every trap the other three have already paid for: a page cap
    that starves the panes, a 100vh budget that must match the chrome, a
    zero-basis pane that collapses when stacked, and an inline display on a
    layout container that outranks the media query.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = _declarations(_base_css())
        cls.mobile = _declarations(_media_block(900))

    def test_the_live_page_is_not_capped(self):
        merged = _decls(self.base, "#page-live")
        self.assertTrue("max-width: none" in merged,
                        f"#page-live still inherits the 1100px page cap; got: {merged.strip()!r}")
        self.assertTrue("margin: 0 auto" not in merged,
                        f"#page-live re-centres itself; got: {merged.strip()!r}")

    def test_the_live_pane_budget_matches_the_other_reading_screens(self):
        def budget(selector):
            merged = _decls(self.base, selector)
            found = re.search(r"height:\s*calc\(100vh\s*-\s*(\d+)px\)", merged)
            self.assertIsNotNone(found, f"{selector} has no 100vh budget: {merged!r}")
            return int(found.group(1))
        self.assertEqual(budget(".live-layout"), budget(".memories-layout"),
                         "live notes sits under the same chrome as memories, so a "
                         "different subtracted constant leaves a dead band")

    def test_the_live_layout_has_no_inline_display(self):
        self.assertNotIn('<div class="live-layout" style=', SOURCE,
                         "an inline display on .live-layout outranks the mobile block")
        self.assertNotIn('<div id="live-pane-transcription" class="live-panel active" style=',
                         SOURCE)

    def test_the_live_columns_stack_and_release_their_zero_bases(self):
        self.assertIn("flex-direction: column", _decls(self.mobile, ".live-layout"))
        self.assertIn("width: auto", _decls(self.mobile, ".live-sidebar"))
        for selector in (".live-workspace", ".live-panel", ".live-panel-scroll"):
            self.assertIn("flex: 0 0 auto", _decls(self.mobile, selector),
                          f"{selector} keeps its zero flex basis when stacked")
        self.assertIn("overflow-y: visible", _decls(self.mobile, ".live-panel-scroll"))

    def test_the_live_tab_and_state_are_wired(self):
        self.assertIn('data-tab="live"', SOURCE)
        self.assertIn('id="page-live"', SOURCE)
        self.assertIn("activeLiveTranscriptionId: activeLiveTranscriptionId", SOURCE)
        self.assertIn("liveTab: liveTab", SOURCE)
        self.assertIn("if (LIVE_TABS.indexOf(state.liveTab) !== -1)", SOURCE)


class GraphHeightIsDefiniteTests(unittest.TestCase):
    """The graph container's height must not be derived from its content.

    "The page is resized constantly" was a feedback loop with three edges, and
    this is the one that remains once the JS is right. vis-network's
    `autoResize` defaults to true, so vis installs its own ResizeObserver on
    its frame and calls `Canvas.setSize()` on every size change. `setSize()`
    writes the canvas backing store from `clientWidth`/`clientHeight` and sets
    the frame's height to `100%` -- a percentage, which resolves to `auto`
    when the container's height is content-derived. It is, when the container
    is a zero-basis flex item in an auto-height column: `flex: 1` is
    `flex-basis: 0%`, so the container's height comes from the canvas inside
    it, and each pass reads `clientHeight` and writes it back as
    `Math.round(clientHeight * devicePixelRatio)`. At 1x that is a fixed point
    and the bug is invisible; on a HiDPI display it is a ~1.25x growth per
    pass, forever.

    The mobile rule already states the height (`flex: 0 0 auto; height: 62vh`)
    and does not loop. The desktop rule is the one that did.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = _declarations(_base_css())

    def test_the_desktop_container_is_not_a_zero_flex_basis(self):
        _assert_declares(
            self, self.base, "#graph-container", "flex: 0 0 auto",
            "flex:1 is a zero basis in an auto-height column, which makes the "
            "container's height content-derived -- and the content is the vis "
            "canvas, which is what grows without bound")

    def test_the_desktop_container_states_its_height(self):
        _assert_declares(
            self, self.base, "#graph-container", "height: 70vh",
            "a definite height is what lets the frame's `height: 100%` resolve "
            "to a real pixel value instead of falling back to the canvas")

    def test_the_desktop_container_cannot_spill(self):
        _assert_declares(
            self, self.base, "#graph-container", "overflow: hidden",
            "the belt to the braces: even if a future change makes the height "
            "content-derived again, the canvas clips instead of growing the page")
class GraphRenderLoopTests(unittest.TestCase):
    """A resize handler that resizes what it observes is a loop, not a handler.

    "Load graph grows the canvas infinitely" was three defects stacked, none of
    which raises and each of which makes the next one worse. There is no browser
    here, so what is pinned is the shape that turns each pass into another pass.

    - **`#graph-container` derives its height from its own content.**
      `.graph-main` is `flex: 1` inside `.graph-layout`, which is a flex *row*
      with no height of its own — so the container's height comes from the vis
      canvas inside it. A `ResizeObserver` on that container which calls
      `fit()` resizes the canvas, the container, and therefore itself. Every
      pass was slightly larger than the last.
    - **The observer responded to height as well as width.** The reason it
      exists is the mobile block stacking the layout, which changes the *width*
      of a canvas vis sized once at construction. Gating on width keeps that
      case and removes the feedback edge.
    - **The observer called `network.redraw()`, which is not a repaint.**
      In vis-network `redraw()` is `emit("setSize")` plus a draw, and
      `setSize()` rewrites the canvas backing store from the box's own
      `clientWidth`/`clientHeight` -- so the handler read the container and
      wrote back into it, which is the same loop on the axis the width gate
      does not cover. It grew the page by `devicePixelRatio` per pass, so it
      was invisible at 1x and unbounded on a HiDPI display.
    - **Each load left its observer and its network behind.** Six call sites
      reach `loadGraph()`, it never destroyed what was there, and the observer
      reads the *global* `network` — so an observer from load 1 was refitting
      the network from load 6, six observers deep. Teardown happens before the
      load, not only in `rebuildNetwork()`.
    """

    def _function(self, name):
        start = SOURCE.index("function %s(" % name)
        end = SOURCE.index("\n  function ", start)
        return SOURCE[start:end]

    def _observer_body(self):
        """Just the ResizeObserver callback, not the whole of initNetwork.

        initNetwork has *two* resize paths — the observer and the `resize`
        fallback for browsers without ResizeObserver — and they are the same
        shape. Asserting on the whole function let a fix in the fallback branch
        satisfy a check on the observer: the first version of this test passed
        on an observer that had been stripped of its width gate, because
        `lastWidth` and `clientWidth` were still present a few lines below.
        """
        start = SOURCE.index("graphResizeObserver = new ResizeObserver(")
        end = SOURCE.index("graphResizeObserver.observe(container)", start)
        return SOURCE[start:end]

    def setUp(self):
        self.init = self._function("initNetwork")
        self.observer = self._observer_body()
        self.load = self._function("loadGraph")
        self.rebuild = self._function("rebuildNetwork")
        self.destroy = self._function("destroyNetwork")

    def test_the_resize_handler_ignores_height_changes(self):
        self.assertIn("clientWidth", self.observer,
                      msg="the handler must gate on width, the only thing the "
                          "mobile stacking actually changes")
        for height in ("offsetHeight", "contentRect", "clientHeight"):
            self.assertNotIn(height, self.observer,
                             msg=f"reacting to {height} feeds the loop: the "
                                 f"canvas grows the container, which fires this again")

    def test_the_resize_handler_is_deduped_on_the_last_width(self):
        """Without this the handler runs on every notification, not every change.

        `ResizeObserver` delivers an entry on *any* box change, including the
        one it caused, so an ungated `fit()` is a loop even on a container whose
        height is not content-derived.
        """
        self.assertIn("lastWidth", self.observer,
                      msg="no dedupe: the observer fires on its own resize too")
        self.assertIn("width === lastWidth", self.observer,
                      msg="the handler must return early for a width it already fitted")
        self.assertLess(self.observer.index("lastWidth = width"),
                        self.observer.index("network.fit("),
                        msg="the dedupe has to happen before the refit, not after")

    def test_the_fallback_resize_path_is_gated_too(self):
        """Same handler, other branch — it has the same loop in it."""
        start = SOURCE.index("window.addEventListener('resize'")
        body = SOURCE[start:SOURCE.index("\n  function ", start)]
        self.assertIn("width === lastWidth", body,
                      msg="the no-ResizeObserver fallback refits on every event "
                          "with no dedupe, which is the same loop")

    def _fallback_body(self):
        """The `window.resize` branch, sliced the same way as the observer.

        Same shape, same loop -- and the first version of this file let a fix in
        one branch satisfy a check on the other, which is why the existing
        dedupe test re-slices it inline. Reuse the slice rather than duplicating
        it, so a third resize path is one edit away from being covered.
        """
        start = SOURCE.index("window.addEventListener('resize'")
        return SOURCE[start:SOURCE.index("\n  function ", start)]

    def test_the_handler_never_asks_vis_to_resize_the_canvas(self):
        """`network.redraw()` inside a resize observer *is* the loop.

        In vis-network `Network.redraw()` is `emit("setSize")` followed by a
        draw, and `Canvas.setSize()` rewrites the canvas backing store from
        `clientWidth`/`clientHeight` and sets the frame's height to `100%` -- a
        percentage, which resolves to `auto` when the container's height is
        content-derived. It is (see the class docstring), so each pass read
        `clientHeight`, wrote it back as `Math.round(clientHeight *
        devicePixelRatio)`, and handed the result to the next pass. At 1x that
        is a fixed point and the bug is invisible; on a HiDPI display it is a
        ~1.25x growth per pass, which is the "the page is resized constantly"
        report.

        The width gate cannot catch this -- the loop is not gated on width, it is
        gated on *the callback running at all*. `autoResize` defaults to true,
        so vis already resizes the canvas with an observer of its own; all this
        handler owes is the refit.
        """
        for body, where in ((self.observer, "ResizeObserver callback"),
                            (self._fallback_body(), "window resize fallback")):
            self.assertNotIn("redraw", body,
                             msg=f"the {where} calls network.redraw(), which in "
                                 f"vis-network re-derives the canvas size from the "
                                 f"very box this handler observes -- that is the "
                                 f"feedback edge, not a repaint")
            self.assertIn("network.fit(", body,
                          msg=f"the {where} must still refit: autoResize defaults "
                              f"to true and vis sizes its own canvas, so the refit "
                              f"is the only thing left that needs doing here")
    def test_every_load_tears_down_the_previous_graph_first(self):
        """`loadGraph` is not the only entry point that builds a network."""
        self.assertIn("destroyNetwork()", self.load,
                      msg="loadGraph builds a network without releasing the "
                          "previous one, so its observer and physics loop leak "
                          "per reload")
        self.assertLess(self.load.index("destroyNetwork()"),
                        self.load.index("api.get('graph'"),
                        msg="teardown after the await lets two in-flight loads "
                            "both reach initNetwork")

    def test_both_rebuild_paths_use_the_same_teardown(self):
        for name, body in (("rebuildNetwork", self.rebuild),
                           ("loadGraph", self.load)):
            with self.subTest(function=name):
                self.assertIn("destroyNetwork()", body)
        self.assertIn("disconnect()", self.destroy)
        self.assertIn("network.destroy()", self.destroy)
        self.assertIn("graphResizeObserver = null", self.destroy,
                      msg="a stale handle is how a second teardown silently "
                          "skips the observer that is still attached")
        self.assertIn("network = null", self.destroy)

    def test_a_stale_response_cannot_render_over_a_newer_one(self):
        """Two loads in flight both cleared the container, then both drew."""
        self.assertIn("graphLoadToken", SOURCE,
                      msg="no load token: two concurrent /api/graph responses "
                          "both build a network into the same container")
        self.assertIn("++graphLoadToken", self.load)
        self.assertIn("token !== graphLoadToken", self.load,
                      msg="the token is incremented but never checked")


class DiarySaveWiringTests(unittest.TestCase):
    """saveDiaryEdit() shipped referring to an undeclared `payload`.

    The ReferenceError was thrown *before* the try block, so there was no toast
    and no request: the button did nothing at all. Nothing about that is
    visible from here — there is no browser — so these assert the properties
    that regress silently, and the same properties for the new-entry path.
    """

    def _function(self, name):
        start = SOURCE.index("async function %s(" % name)
        end = SOURCE.index("\n  function ", start)
        if end < start:
            end = len(SOURCE)
        return SOURCE[start:end]

    def setUp(self):
        self.edit = self._function("saveDiaryEdit")
        self.create = self._function("saveDiary")

    def test_the_edit_path_declares_the_body_it_sends(self):
        self.assertTrue("const payload = {" in self.edit,
                        "saveDiaryEdit builds a payload but never declares one")
        self.assertIn("api.put('diary/' + entryId, payload)", self.edit)

    def test_the_edit_path_did_not_absorb_the_memory_editor(self):
        # A stray `api.post('memories', {... cat, tags ...})` sat here for
        # several commits: none of those names exist in this function.
        self.assertFalse("api.post('memories'" in self.edit,
                         "the memory editor's save call leaked into saveDiaryEdit")
        for orphan in ("category: cat", "tags,", "payload.metadata"):
            self.assertNotIn(orphan, self.edit)

    def test_every_request_in_these_paths_is_inside_a_try(self):
        # The user-visible half of the bug: an error before `try` is silent,
        # because the catch that shows the toast never runs.
        for name, body in (("saveDiaryEdit", self.edit), ("saveDiary", self.create)):
            guarded = body.index("try {")
            first = body.find("await api.")
            self.assertTrue(first != -1, "%s makes no request at all" % name)
            self.assertLess(guarded, first,
                            "%s issues a request before its try block" % name)

    def test_a_failed_save_still_says_so(self):
        for name, body in (("saveDiaryEdit", self.edit), ("saveDiary", self.create)):
            self.assertIn("catch", body)
            self.assertIn("toast(", body,
                          "%s has no toast, so a failure is invisible" % name)

    def test_a_saving_entry_shows_progress(self):
        # A save embeds the text and then runs keyword and people extraction,
        # one LLM call per window, so a long entry is tens of seconds of dead
        # button with no way to tell a slow save from a broken one.
        for name, body in (("saveDiaryEdit", self.edit), ("saveDiary", self.create)):
            self.assertIn("_busy(btn", body,
                          "%s gives no feedback while it saves" % name)

    def test_the_button_is_restored_even_when_the_save_throws(self):
        # The whole point of `finally`. A restore in the success path alone
        # leaves the button permanently disabled after one failure, and the
        # entry then cannot be saved again at all.
        busy = self._function("_busy")
        self.assertTrue("finally {" in busy,
                        "_busy has no finally, so a throw leaves the button disabled")
        guarded = busy.index("finally {")
        self.assertIn("btn.disabled = false", busy)
        self.assertTrue(guarded < busy.rindex("btn.innerHTML = original"),
                        "the button is only restored on the success path")
        self.assertIn("btn.disabled = true", busy)

    def test_both_buttons_pass_themselves_in(self):
        # Without `this` the handler gets no button and _busy is a no-op, which
        # is a silent regression: the code still looks like it guards the save.
        for call in ("saveDiary(this)", "saveDiaryEdit('${entry.id}', this)"):
            self.assertIn(call, SOURCE, "%r does not pass the button" % call)


# The form is rendered twice: once inside a template literal (view mode) and
# once built by `+` concatenation (edit mode, because a gated fragment cannot be
# a template literal nested inside another one -- see the note in
# renderDiaryEditMode). Both spell the same opening tag, so the marker has to
# match both. A literal-only marker finds one copy and reports the other as
# missing, which reads as a regression in code that is correct.
_FORM_OPEN = re.compile(
    r'<div id="dlink-form-(?:\$\{entry\.id\}"|\' \+ entry\.id \+ \'")')


def _diary_link_forms(source):
    """The body of each `dlink-form-${entry.id}` block, as rendered.

    Bounded by the form's own Cancel button, not by the next `</div>`: the
    form nests two divs, so a closing-tag scan truncates it mid-way and every
    assertion below silently reads a body that stops early.
    """
    out = []
    for match in _FORM_OPEN.finditer(source):
        end = source.find(">Cancel</button>", match.end())
        out.append(source[match.start():end + len(">Cancel</button>")
                          if end != -1 else len(source)])
    return out


class DiaryLinkPickerTests(unittest.TestCase):
    """The diary "Link to fact" target was a `<datalist>`.

    A datalist whose options carry the raw UUID as `value` and the name as
    `label` renders as a popup of every memory in the vault -- on this vault a
    thousand rows -- and posts an id the user never saw. The only thing it could
    report was a toast reading "Enter a fact ID", under a placeholder that said
    "Search fact...". It is now the same client-side typeahead the fact pane
    uses, so these assert the *shape* rather than the appearance: a search field
    in both markup copies, a stored picked id, and no population of a datalist.

    The `_fn` helper is local because `DiarySaveWiringTests._function` slices to
    the next `"\n  function "`, which only finds non-async definitions -- and two
    of the functions asserted here are plain ones.
    """

    def _fn(self, name):
        start = -1
        for prefix in ("async function %s(", "function %s("):
            found = SOURCE.find(prefix % name)
            if found != -1:
                start = found
                break
        if start == -1:
            return ""
        end = SOURCE.find("\n  function ", start + 1)
        if end == -1:
            end = SOURCE.find("\n  async function ", start + 1)
        return SOURCE[start:end if end != -1 else len(SOURCE)]

    def test_no_datalist_of_facts_remains(self):
        # Neither of these greps is for the bare id: the comment explaining what
        # this replaced names it too, so a substring guard over the whole file
        # reports green on a file containing the bug -- in its own words.
        self.assertFalse('id="fact-datalist-dropdown"' in SOURCE,
                         "the fact datalist element was left behind")
        # The per-refresh population is the half that costs something: it built
        # one <option> per memory on every loadMemories() call.
        self.assertFalse('<option value="${m.id}">' in SOURCE,
                         "loadMemories still fills a datalist of every fact")

    def test_the_picker_is_a_search_field_in_both_markup_copies(self):
        """The diary form is rendered twice -- view mode and edit mode.

        A guard reading only one copy passes while the other still renders the
        raw input, and which copy is live depends on whether the user is editing
        the entry: not something a single-match source search can see.
        """
        self.assertEqual(SOURCE.count('class="pane-link-results"'), 3,
                         "the fact pane plus both diary forms render a results box")

        # Every one of these is checked *inside* each form, never over the whole
        # file. A file-wide count pins every other contributor to the number --
        # the JS definitions, the diary sidebar's own search input -- so it fails
        # on correct code and would keep passing if one copy regressed.
        forms = _diary_link_forms(SOURCE)
        self.assertEqual(len(forms), 2, "the form is rendered twice: view and edit mode")
        for form in forms:
            # Fold the edit-mode concatenation spelling onto the template one,
            # so the needles below describe ONE shape instead of two. Without
            # this every needle containing an id has to be written twice, and
            # the copy built by concatenation fails all of them.
            folded = form.replace("' + entry.id + '", "${entry.id}")
            for needle in ('<input type="search"', 'id="dlink-q-${entry.id}"',
                           "dlinkSearch(", "dlinkKeys(", 'class="pane-link-results"',
                           'id="dlink-chosen-${entry.id}"'):
                self.assertTrue(needle in folded,
                                "a dlink-form copy is missing %r" % needle)
            self.assertFalse("list=" in form,
                             "a dlink-form copy still binds to a datalist")

    def test_save_links_the_picked_id_not_a_typed_one(self):
        """`saveDiaryLink` read the input's value, so it posted whatever was typed.

        The only correct value is the id the picker stored, and an un-picked form
        must refuse rather than post the query string as a fact id.
        """
        body = self._fn("saveDiaryLink")
        self.assertTrue("_dlinkState(entryId).targetId" in body,
                        "saveDiaryLink is not reading the picker's stored id")
        self.assertFalse("dlink-target-" in body,
                         "saveDiaryLink still reads a raw text input")
        self.assertTrue("if (!targetVal)" in body,
                        "an un-picked form must refuse, not post an empty id")

    def test_candidates_exclude_what_is_already_linked(self):
        """A second MENTIONS edge to the same fact is a duplicate, not a link."""
        body = self._fn("dlinkCandidates")
        self.assertTrue("entry.mentions" in body and "linked.has(m.id)" in body,
                        "already-mentioned facts are still offered as targets")

    def test_the_picker_filters_as_you_type(self):
        """Find-as-you-type is the whole point of replacing the datalist."""
        self.assertTrue('oninput="dlinkSearch(' in SOURCE,
                        "the search field must filter on input, not on submit")
        self.assertTrue("st.results = dlinkCandidates(entryId, st.q)" in self._fn("dlinkSearch"),
                        "typing does not re-run the candidate filter")

    def test_opening_the_form_drops_a_stale_pick(self):
        """The same form element is reused across entries.

        Without the reset, opening entry B's form shows entry A's picked target
        and links the wrong fact -- silently, because the chip renders the
        correct name for whatever was picked.
        """
        self.assertTrue("dlinkReset(entryId)" in self._fn("toggleDiaryLinkForm"),
                        "the diary link form does not re-seed its picker on open")
        self.assertTrue("targetId: null" in self._fn("dlinkReset"),
                        "the reset does not clear the picked target")


class FactDedupMenuTests(unittest.TestCase):
    """The per-record dedup entry point: the fact menu's own modal.

    The Service tab's workflow is admin-only and category-wide, so a
    per-record action cannot go through it -- that would put a record's own
    cleanup behind a gate its owner may not hold. These pin the menu entry,
    the scan's filter to the source record, and the merge wiring, because
    each half is one conditional in a template literal and a source-shaped
    assertion cannot tell a wired handler from a rendered label.
    """

    def _between(self, start, end):
        i = SOURCE.index(start)
        j = SOURCE.index(end, i)
        return SOURCE[i:j]

    def test_the_menu_offers_deduplicate_and_gates_it_as_a_writer(self):
        writers = self._between("const _FACT_MENU_WRITERS", "\n")
        self.assertIn("'_factMenuDedup'", writers,
                      msg="Deduplicate is not a writer, so a read-only grantee "
                          "is offered a merge the server refuses")
        menu = self._between("function _showFactMenu(", "function _factMenuEdit(")
        self.assertIn("action: '_factMenuDedup'", menu)

    def test_the_menu_handler_opens_the_modal_for_the_selected_fact(self):
        seg = self._between("function _factMenuDedup()", "async function _factMenuReload(")
        self.assertIn("_factMenuId", seg)
        self.assertIn("openFactDedup(_factMenuId)", seg)

    def test_the_scan_keeps_only_clusters_holding_this_record(self):
        seg = self._between("async function openFactDedup(", "function renderFactDedupClusters(")
        self.assertIn("threshold=0.75", seg)
        self.assertIn("max_cluster=4", seg)
        self.assertIn(".some(m => m.id === id)", seg,
                      msg="the scan does not filter to the source record, so "
                          "every cluster in the category is shown")
        self.assertIn("No duplicates found for this record.", seg)
        self.assertNotIn("loadMemories", seg,
                         msg="scanning must not write -- it is a read")

    def test_merging_confirms_posts_and_refreshes_the_list(self):
        seg = self._between("async function fdMergeCluster(", "// Declared at top of script")
        self.assertIn("confirm(", seg)
        self.assertIn("api.post('duplicates/merge'", seg)
        self.assertIn("closeFactDedup()", seg)
        self.assertIn("loadMemories()", seg)
        self.assertIn("!masterId || !duplicateIds.length || !mergedName || !mergedText", seg)

    def test_the_draft_is_generated_from_the_selected_records_only(self):
        seg = self._between("async function fdGenerateDraft(", "function fdUpdateDraft(")
        self.assertIn("api.post('duplicates/draft', {factIds})", seg)
        self.assertIn("factIds.length < 2", seg)

    def test_the_modal_overlays_and_scrolls_without_scrolling_the_page(self):
        base = _declarations(_base_css())
        self.assertIn("position: fixed", _decls(base, ".fact-dedup-overlay"))
        self.assertIn("overflow-y: auto", _decls(base, ".fact-dedup-overlay"),
                      msg="a tall modal must scroll inside the overlay, not "
                          "push the three-pane layout past its height cap")
        self.assertIn("max-width: 860px", _decls(base, ".fact-dedup-box"))


if __name__ == "__main__":
    unittest.main()
