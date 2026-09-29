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
        """The inner lists are released, so the sidebar must scroll.

        With `.memories-list-scroll` and `#diary-dates-list` content-sized,
        a `max-height` on the sidebar does nothing on its own -- the content
        would spill out of an un-scrolling box. Each sidebar is the bounded
        scroll region instead, which is also why there is only one scroll
        area per sidebar rather than a nested one.
        """
        for selector in (".memories-list-col", ".diary-sidebar"):
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


class LayoutInlineStyleTests(unittest.TestCase):
    """An inline style beats a stylesheet rule of any specificity."""

    def test_no_layout_container_carries_an_inline_style(self):
        for cls in ("memories-layout", "diary-layout", "graph-layout",
                    "graph-sidebar", "graph-main"):
            with self.subTest(container=cls):
                self.assertIsNone(
                    re.search(r'class="%s"[^>]*\bstyle=' % cls, SOURCE),
                    f".{cls} has an inline style, which overrides the mobile "
                    f"media query no matter what the stylesheet says",
                )

    def test_the_graph_layout_flex_rule_lives_in_the_stylesheet(self):
        self.assertRegex(CSS, r"\.graph-layout\s*\{[^}]*display:\s*flex")

    def test_the_graph_container_is_styled_and_carries_no_inline_style(self):
        """`#graph-container` sized the vis canvas from an inline style.

        The mobile block has a height rule for it, and it was inert: an
        inline `height: 70vh` outranks it at every specificity. vis.js
        sizes its canvas once from this box, so the rule that never applied
        was the rule deciding how much room the user has to pan.
        """
        self.assertIsNone(
            re.search(r'id="graph-container"[^>]*\bstyle=', SOURCE),
            "#graph-container has an inline style, so the mobile height "
            "rule for it cannot apply",
        )
        self.assertRegex(
            CSS, r"#graph-container\s*\{[^}]*height:",)


class MobileBreakpointTests(unittest.TestCase):
    def setUp(self):
        self.tablet = _declarations(_media_block(900))
        self.phone_body = _media_block(640)
        self.phone = _declarations(self.phone_body)

    def test_every_fixed_width_column_collapses(self):
        # The layouts are 180+280+flex, 220+flex and 200+flex. Anything left
        # at its desktop width overflows a 360px viewport.
        for selector in (
            ".memories-sidebar", ".memories-list-col", ".diary-sidebar",
            ".graph-sidebar",
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
        # fitting a destroyed one.
        idx = SOURCE.index("function rebuildNetwork()")
        body = SOURCE[idx:idx + 600]
        self.assertIn("graphResizeObserver.disconnect()", body)
        self.assertLess(
            body.index("graphResizeObserver.disconnect()"),
            body.index("network.destroy()"),
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
    after all seven tab buttons off the right edge of the screen. The
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


if __name__ == "__main__":
    unittest.main()
