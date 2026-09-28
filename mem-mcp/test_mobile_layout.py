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


class LayoutInlineStyleTests(unittest.TestCase):
    """An inline style beats a stylesheet rule of any specificity."""

    def test_no_layout_container_carries_an_inline_style(self):
        for cls in ("memories-layout", "diary-layout", "graph-layout"):
            with self.subTest(container=cls):
                self.assertIsNone(
                    re.search(r'class="%s"[^>]*\bstyle=' % cls, SOURCE),
                    f".{cls} has an inline style, which overrides the mobile "
                    f"media query no matter what the stylesheet says",
                )

    def test_the_graph_layout_flex_rule_lives_in_the_stylesheet(self):
        self.assertRegex(CSS, r"\.graph-layout\s*\{[^}]*display:\s*flex")


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
        _assert_declares(self, self.tablet, "#graph-container", "height: 55vh",
                         "70vh is most of a phone screen")
        _assert_declares(self, self.tablet, "#graph-container", "min-height: 320px",
                         "a 500px floor overflows a short viewport")

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


if __name__ == "__main__":
    unittest.main()
