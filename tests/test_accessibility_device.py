"""Unit tests for AccessibilityDevice — the V3 (no-PC) Device implementation.

These run with **no Android and no device**: a pure-Python `FakeBridge` stands
in for the Kotlin `ReactorAccessibilityService`, returning canned node JSON in
the exact contract the real service emits. That lets us pin down the two things
P1 must get right before any on-device work:

  1. Field mapping — a11y node JSON -> `UiNode` (docs/V3_PLAN.md §3 table).
  2. Selector semantics — `find`/`find_all` behave identically to `U2Device`
     (exact text/desc/id/class; substring for *Contains; AND across criteria;
     `find` returns the first match in tree order).

The gesture methods are checked at the unit level too: we assert the Python
`Device` API (pixel coords, seconds) is translated to the bridge primitives
(pixel coords, milliseconds) correctly, and that `input_text` maps to a
field-replacing set-text.
"""

import json
import unittest

from insta_reactor.device.accessibility_device import AccessibilityDevice
from insta_reactor.device.base import UiNode


# A small but realistic slice of an Instagram clips-viewer tree, in the depth-
# first pre-order the real service emits. Uses the clean, non-obfuscated view
# ids the P0 dump confirmed (like_button, reel_share_item_view, ...).
SAMPLE_NODES = [
    {"cls": "android.widget.FrameLayout", "text": "", "desc": "",
     "id": "com.instagram.android:id/clips_viewer_view_pager",
     "bounds": [0, 0, 1080, 2400], "clickable": False, "scrollable": True},
    {"cls": "android.widget.TextView", "text": "modern.aphorism", "desc": "",
     "id": "com.instagram.android:id/clips_author_username",
     "bounds": [40, 120, 400, 180], "clickable": True},
    {"cls": "android.widget.Button", "text": "", "desc": "Like",
     "id": "com.instagram.android:id/like_button",
     "bounds": [960, 1000, 1060, 1100], "clickable": True},
    {"cls": "android.widget.Button", "text": "", "desc": "Comment",
     "id": "com.instagram.android:id/comment_button",
     "bounds": [960, 1120, 1060, 1220], "clickable": True},
    {"cls": "android.widget.TextView", "text": "LMAOO 💀", "desc": "",
     "id": "com.instagram.android:id/row_comment_textview",
     "bounds": [80, 1500, 700, 1560], "clickable": False},
    {"cls": "android.widget.TextView", "text": "same energy fr", "desc": "",
     "id": "com.instagram.android:id/row_comment_textview",
     "bounds": [80, 1580, 700, 1640], "clickable": False},
]


class FakeBridge:
    """Pure-Python stand-in for ReactorAccessibilityService. Returns canned
    node JSON and records every gesture call for assertions."""

    def __init__(self, nodes=None, package="com.instagram.android", size=(1080, 2400)):
        self._nodes = SAMPLE_NODES if nodes is None else nodes
        self._package = package
        self._size = size
        self.calls: list[tuple] = []

    def currentPackage(self):
        return self._package

    def windowSize(self):
        return list(self._size)

    def nodesJson(self):
        return json.dumps(self._nodes)

    def tap(self, x, y):
        self.calls.append(("tap", x, y)); return True

    def longPress(self, x, y, durationMs):
        self.calls.append(("longPress", x, y, durationMs)); return True

    def swipe(self, x1, y1, x2, y2, durationMs):
        self.calls.append(("swipe", x1, y1, x2, y2, durationMs)); return True

    def inputText(self, text):
        self.calls.append(("inputText", text)); return True

    def pressBack(self):
        self.calls.append(("pressBack",)); return True

    def pressHome(self):
        self.calls.append(("pressHome",)); return True

    def startApp(self, package):
        self.calls.append(("startApp", package)); return True


class TestFieldMapping(unittest.TestCase):
    def setUp(self):
        self.dev = AccessibilityDevice(FakeBridge())

    def test_node_maps_all_uinode_fields(self):
        like = self.dev.find(resource_id="com.instagram.android:id/like_button")
        self.assertIsInstance(like, UiNode)
        self.assertEqual(like.clazz, "android.widget.Button")
        self.assertEqual(like.desc, "Like")
        self.assertEqual(like.text, "")
        self.assertEqual(like.resource_id, "com.instagram.android:id/like_button")
        self.assertEqual(like.bounds, (960, 1000, 1060, 1100))
        self.assertTrue(like.clickable)

    def test_center_computed_from_bounds(self):
        like = self.dev.find(resource_id="com.instagram.android:id/like_button")
        self.assertEqual(like.center, (1010, 1050))

    def test_blank_fields_are_empty_string_not_none(self):
        author = self.dev.find(descContains="")  # first node
        self.assertEqual(author.desc, "")
        self.assertIsInstance(author.text, str)

    def test_current_package_and_window_size(self):
        self.assertEqual(self.dev.current_package(), "com.instagram.android")
        self.assertEqual(self.dev.window_size(), (1080, 2400))


class TestSelectorSemantics(unittest.TestCase):
    def setUp(self):
        self.dev = AccessibilityDevice(FakeBridge())

    def test_find_exact_text(self):
        n = self.dev.find(text="LMAOO 💀")
        self.assertIsNotNone(n)
        self.assertEqual(n.bounds[1], 1500)

    def test_find_exact_text_is_not_substring(self):
        # "LMAOO" alone must NOT match the exact-text selector.
        self.assertIsNone(self.dev.find(text="LMAOO"))

    def test_textContains_is_substring(self):
        n = self.dev.find(textContains="energy")
        self.assertIsNotNone(n)
        self.assertEqual(n.text, "same energy fr")

    def test_find_first_in_tree_order(self):
        # Two comment rows share the same id; find() returns the first (topmost).
        n = self.dev.find(resource_id="com.instagram.android:id/row_comment_textview")
        self.assertEqual(n.text, "LMAOO 💀")

    def test_find_all_returns_all_matches(self):
        rows = self.dev.find_all(
            resource_id="com.instagram.android:id/row_comment_textview")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.text for r in rows], ["LMAOO 💀", "same energy fr"])

    def test_find_all_by_classname(self):
        tvs = self.dev.find_all(className="android.widget.TextView")
        self.assertEqual(len(tvs), 3)

    def test_criteria_are_anded(self):
        # class matches 3 nodes, but only one also has this exact text.
        n = self.dev.find(className="android.widget.TextView", text="same energy fr")
        self.assertIsNotNone(n)
        self.assertEqual(n.bounds[1], 1580)
        # A class + text combo that never co-occurs yields nothing.
        self.assertIsNone(
            self.dev.find(className="android.widget.Button", text="same energy fr"))

    def test_no_match_returns_none_and_empty(self):
        self.assertIsNone(self.dev.find(text="nonexistent"))
        self.assertEqual(self.dev.find_all(text="nonexistent"), [])

    def test_exists_helper(self):
        self.assertTrue(self.dev.exists(resource_id="com.instagram.android:id/like_button"))
        self.assertFalse(self.dev.exists(resource_id="com.instagram.android:id/nope"))


class TestActions(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.dev = AccessibilityDevice(self.bridge)

    def test_tap_forwards_integer_coords(self):
        self.dev.tap(100, 200)
        self.assertEqual(self.bridge.calls[-1], ("tap", 100, 200))

    def test_tap_node_uses_center(self):
        like = self.dev.find(resource_id="com.instagram.android:id/like_button")
        self.dev.tap_node(like)
        self.assertEqual(self.bridge.calls[-1], ("tap", 1010, 1050))

    def test_swipe_converts_seconds_to_millis(self):
        self.dev.swipe(10, 20, 30, 40, duration=0.25)
        self.assertEqual(self.bridge.calls[-1], ("swipe", 10, 20, 30, 40, 250))

    def test_swipe_default_duration(self):
        self.dev.swipe(1, 2, 3, 4)
        self.assertEqual(self.bridge.calls[-1][-1], 200)  # 0.2s -> 200ms

    def test_long_press_converts_seconds_to_millis(self):
        self.dev.long_press(5, 6)
        self.assertEqual(self.bridge.calls[-1], ("longPress", 5, 6, 600))

    def test_input_text_forwards_verbatim(self):
        self.dev.input_text("hello 🌊")
        self.assertEqual(self.bridge.calls[-1], ("inputText", "hello 🌊"))

    def test_press_back_and_home_and_start_app(self):
        self.dev.press_back()
        self.dev.press_home()
        self.dev.start_app("com.instagram.android")
        self.assertIn(("pressBack",), self.bridge.calls)
        self.assertIn(("pressHome",), self.bridge.calls)
        self.assertIn(("startApp", "com.instagram.android"), self.bridge.calls)


class TestRobustness(unittest.TestCase):
    def test_null_bridge_rejected(self):
        with self.assertRaises(ValueError):
            AccessibilityDevice(None)

    def test_empty_tree_yields_no_nodes(self):
        dev = AccessibilityDevice(FakeBridge(nodes=[]))
        self.assertEqual(dev.find_all(), [])
        self.assertIsNone(dev.find(text="anything"))

    def test_malformed_json_is_swallowed(self):
        class BadBridge(FakeBridge):
            def nodesJson(self):
                return "{not valid json"
        dev = AccessibilityDevice(BadBridge())
        self.assertEqual(dev.find_all(), [])  # logs + degrades, never crashes

    def test_bad_bounds_default_to_zero(self):
        dev = AccessibilityDevice(FakeBridge(nodes=[
            {"cls": "X", "text": "t", "desc": "", "id": "", "bounds": [1, 2]},
        ]))
        n = dev.find(text="t")
        self.assertEqual(n.bounds, (0, 0, 0, 0))

    def test_dump_hierarchy_returns_json(self):
        dev = AccessibilityDevice(FakeBridge())
        parsed = json.loads(dev.dump_hierarchy())
        self.assertEqual(len(parsed), len(SAMPLE_NODES))


if __name__ == "__main__":
    unittest.main()
