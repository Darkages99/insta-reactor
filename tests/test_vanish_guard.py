"""Vanish / disappearing-mode guard (V3 P3 Fix #2).

Instagram engages vanish mode on an over-scroll at the very bottom of a thread,
which would send our reactions as disappearing messages and trap the scroll loop
in a pull animation. These tests exercise the detector and the scroll abort over
the REAL device path (AccessibilityDevice + ReplayBridge), including a
false-positive check against a real captured chat screen.

NOTE: the vanish indicator STRINGS are a best-effort guess and still need
on-device calibration (see selectors.VANISH_MODE_INDICATORS). These tests lock in
the guard *mechanics*; the exact copy is validated when vanish mode is reproduced.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `replay` importable

from replay.bridge import ReplayBridge, load_screen  # noqa: E402

from insta_reactor.device.accessibility_device import AccessibilityDevice
from insta_reactor.automation.navigator import Navigator


def _nav(nodes) -> tuple[Navigator, ReplayBridge]:
    bridge = ReplayBridge(nodes)
    return Navigator(AccessibilityDevice(bridge), settle=0.0), bridge


def _vanish_node(text="Vanish mode"):
    return {"cls": "android.widget.TextView", "text": text, "desc": "",
            "id": "", "bounds": [100, 2500, 1172, 2600],
            "clickable": False, "scrollable": False, "longClickable": False,
            "enabled": True}


class TestVanishDetection:
    def test_detects_vanish_text(self):
        nav, _ = _nav([_vanish_node("Vanish mode")])
        assert nav.detect_vanish_mode() is True

    def test_detects_disappearing_composer_hint(self):
        nav, _ = _nav([_vanish_node("Disappearing message…")])
        assert nav.detect_vanish_mode() is True

    def test_real_chat_screen_is_not_vanish(self):
        """The detector must NOT fire on a normal captured DM thread."""
        nav, _ = _nav(load_screen("chat"))
        assert nav.detect_vanish_mode() is False


class TestScrollAbortsOnVanish:
    def test_scroll_backs_out_when_vanish_engaged(self):
        # a screen already in vanish mode: scroll must abort immediately and
        # back out (a pressBack), not keep swiping into a deeper pull.
        nav, bridge = _nav([_vanish_node()])
        nav.scroll_thread_to_bottom(max_swipes=6)
        kinds = [c[0] for c in bridge.calls]
        assert "pressBack" in kinds, "should back out of vanish mode"
        # it must NOT have run the full swipe budget before noticing.
        assert kinds.count("swipe") <= 1

    def test_normal_thread_does_not_back_out(self):
        """On a real chat screen the guard never presses Back (no false abort)."""
        nav, bridge = _nav(load_screen("chat"))
        nav.scroll_thread_to_bottom(max_swipes=2)
        assert "pressBack" not in [c[0] for c in bridge.calls]
