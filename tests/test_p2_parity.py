"""P2 parity bring-up — the reused engine vs. REAL captured Instagram trees.

Runs the *actual* V3 device path (`AccessibilityDevice`) over a `ReplayBridge`
serving node trees converted from real uiautomator2 captures (`data/calib*/`),
then drives the reused engine (navigator / backend / collector) against them.
This is the device-free half of P2 (docs/V3_PLAN.md §7): it re-verifies selector
strings, state fingerprints, and geometry zones on real data without a phone.

What it does NOT cover (needs the P3 app shell on a physical device): live
gesture fidelity, settle timings against real animations, and a fresh on-device
DM-thread capture. Those are the on-device green-light. Everything here is the
deterministic parity that must hold first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `replay` importable

from replay.bridge import ReplayBridge, load_screen  # noqa: E402

from insta_reactor.device.accessibility_device import AccessibilityDevice
from insta_reactor.config import AppConfig
from insta_reactor.automation.navigator import Navigator
from insta_reactor.automation import selectors as S
from insta_reactor.automation.states import State
from insta_reactor.backends.android import AndroidBackend, _ZONE_TOP, _ZONE_BOTTOM
from insta_reactor.collector.comments import CommentCollector


def _device(screen: str) -> tuple[AccessibilityDevice, ReplayBridge]:
    bridge = ReplayBridge(load_screen(screen))
    return AccessibilityDevice(bridge), bridge


def _backend(screen: str) -> AndroidBackend:
    d, _ = _device(screen)
    return AndroidBackend(AppConfig(), device=d)


# --- state machine: every real screen classifies correctly -------------------
class TestStateParity:
    @pytest.mark.parametrize("screen,expected", [
        ("clips_viewer", State.REEL_VIEWER),
        ("chat", State.CHAT),
        ("comments", State.COMMENTS),
        ("inbox", State.INBOX),
        ("dms", State.INBOX),   # the DM/notes tray also fingerprints as inbox
    ])
    def test_detect_state(self, screen, expected):
        d, _ = _device(screen)
        assert Navigator(d).detect_state() == expected

    def test_reel_viewer_controls_resolve(self):
        """The reel viewer's like/comment/reply selectors hit real nodes."""
        d, _ = _device("clips_viewer")
        nav = Navigator(d)
        assert nav._find_any(S.STATE_FINGERPRINTS["REEL_VIEWER"]) is not None
        assert nav._find_any(S.REEL_REPLY_BAR) is not None       # reply_bar_edittext
        assert nav._find_any(S.OPEN_COMMENTS_BUTTON) is not None  # comment_button/desc


# --- CHAT: reel bubble detection + zone geometry on the real thread ----------
class TestChatReelParity:
    def test_reel_bubbles_found(self):
        d, _ = _device("chat")
        reels = d.find_all(
            resource_id="com.instagram.android:id/reel_share_item_view")
        assert len(reels) == 3            # the real capture holds three
        # REEL_BUBBLE selector list resolves via the navigator helper too.
        assert Navigator(d)._find_any(S.REEL_BUBBLE) is not None

    def test_reel_bubbles_are_incoming_and_left_aligned(self):
        d, _ = _device("chat")
        w, _h = d.window_size()
        for n in d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view"):
            cx, _ = n.center
            assert cx < w / 2, f"reel {n.bounds} should be left-aligned (incoming)"

    def test_zone_filters_reels_by_position(self):
        """Real bubble y-positions exercise fully- vs. loosely-visible bands."""
        be = _backend("chat")
        w, h = be.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)
        fully = be._fully_visible_reels(zt, zb)
        loose = be._loosely_visible_reels(zt, zb)
        # One bubble sits wholly inside the zone; a second is clipped at the
        # bottom (loosely visible only); the third starts above the zone top.
        assert len(fully) == 1
        assert len(loose) == 2
        # loosely-visible is a superset of fully-visible here.
        assert {n.bounds for n in fully} <= {n.bounds for n in loose}


# --- Rule 1/1b heuristics: no false positives from reel chrome ---------------
class TestRuleTextHeuristics:
    def test_reel_author_label_not_read_as_message(self):
        """Regression (P2): a reel's `title_text` author label ("thestevenhe")
        and the `message_footer_label` react-hint must NOT be mistaken for a
        preceding/following chat message — that would spuriously flag the reel
        for manual review under Rule 1/1b."""
        be = _backend("chat")
        w, h = be.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)
        for n in be._loosely_visible_reels(zt, zb):
            assert be._preceding_text_for_node(n) is None
            assert be._following_text_for_node(n) is None

    def test_non_message_ids_are_blocklisted(self):
        from insta_reactor.backends.android import _NON_MESSAGE_TEXT_IDS
        assert "com.instagram.android:id/title_text" in _NON_MESSAGE_TEXT_IDS
        assert "com.instagram.android:id/message_footer_label" in _NON_MESSAGE_TEXT_IDS


# --- COMMENTS: the collector reads real comment rows -------------------------
class TestCommentCollectionParity:
    def test_reads_real_comments_and_strips_said_prefix(self):
        d, _ = _device("comments")
        rows = CommentCollector(d)._visible_comments()
        texts = [c.text for c in rows]
        # Real comments from the capture, with the "<user> said " prefix removed.
        assert "Wad da haiiiiillllll" in texts
        assert "Ferrari design team best moments" in texts
        # None should still carry the "said" prefix.
        assert all(" said " not in t or not t.startswith(("enidmay", "oagarcia"))
                   for t in texts)

    def test_like_counts_parsed(self):
        d, _ = _device("comments")
        rows = CommentCollector(d)._visible_comments()
        by_text = {c.text: c.likes for c in rows}
        # The top comment in the capture carries a real like count in the thousands.
        assert by_text.get("Wad da haiiiiillllll", 0) > 1000


# --- INBOX: the thread list is scannable -------------------------------------
class TestInboxParity:
    def test_inbox_fingerprint_matches(self):
        d, _ = _device("inbox")
        assert Navigator(d)._exists_any(S.STATE_FINGERPRINTS["INBOX"])
