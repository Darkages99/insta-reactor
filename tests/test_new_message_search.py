"""New-Message-search fallback for chats missing from the scrollable inbox.

CONFIRMED live (2026-07-29): an infrequently-opened group thread never
appeared no matter how far the inbox was scrolled (150+ weeks deep), yet it
genuinely existed — the "New Message" (+) button's search surfaced it under a
"Suggested" header, and tapping that result opened the REAL existing thread
(its actual message history), not a new one. See selectors.NEW_MESSAGE_BUTTON.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `replay` importable

from replay.bridge import ReplayBridge  # noqa: E402

from insta_reactor.device.accessibility_device import AccessibilityDevice
from insta_reactor.automation.navigator import Navigator


def _node(cls, text="", desc="", id="", bounds=(0, 0, 100, 100), clickable=False):
    return {"cls": cls, "text": text, "desc": desc, "id": id, "bounds": list(bounds),
            "clickable": clickable, "scrollable": False, "longClickable": False,
            "enabled": True}


def _new_message_button():
    return _node("android.widget.ImageView", desc="New Message",
                 bounds=(1159, 144, 1249, 302), clickable=True)


def _search_field():
    return _node("android.widget.EditText", text="Search",
                 id="com.instagram.android:id/search_edit_text",
                 bounds=(165, 338, 381, 423), clickable=True)


def _result_row(name, bounds):
    return _node("android.widget.TextView", text=name,
                 id="com.instagram.android:id/row_user_primary_name",
                 bounds=bounds, clickable=False)


def _nav(nodes):
    bridge = ReplayBridge(nodes)
    return Navigator(AccessibilityDevice(bridge), settle=0.0), bridge


class TestSearchNewMessage:
    def test_finds_exact_match_result_row(self):
        nodes = [
            _new_message_button(),
            _search_field(),
            _result_row("little man, fat boy and ak47", (237, 647, 735, 697)),
        ]
        nav, bridge = _nav(nodes)
        row = nav._search_new_message("little man, fat boy and ak47")
        assert row is not None
        assert row.text == "little man, fat boy and ak47"
        assert ("inputText", "little man, fat boy and ak47") in bridge.calls

    def test_emoji_tolerant_match(self):
        """Target 'freakhan' must match a row titled 'freakhan\U0001F60B\U0001F4A5\U0001FA79' —
        same emoji-stripping rule as _find_inbox_row."""
        nodes = [
            _new_message_button(),
            _search_field(),
            _result_row("freakhan\U0001F60B\U0001F4A5\U0001FA79", (237, 647, 735, 697)),
        ]
        nav, _ = _nav(nodes)
        row = nav._search_new_message("freakhan")
        assert row is not None

    def test_does_not_substring_match(self):
        """A near-miss name (e.g. a different, similarly-worded suggestion)
        must NOT be treated as a match — exact-on-text, same as the inbox scan's
        '40fitandshyam' != 'shyam' guard."""
        nodes = [
            _new_message_button(),
            _search_field(),
            _result_row("little boy & fat man", (237, 851, 585, 901)),
            _result_row("Little boy fat man", (237, 1055, 564, 1105)),
        ]
        nav, _ = _nav(nodes)
        row = nav._search_new_message("little man, fat boy and ak47")
        assert row is None

    def test_no_new_message_button_returns_none_without_acting(self):
        nodes = [_search_field()]  # button absent
        nav, bridge = _nav(nodes)
        row = nav._search_new_message("anything")
        assert row is None
        assert bridge.calls == []

    def test_no_search_field_backs_out(self):
        nodes = [_new_message_button()]  # field never appears
        nav, bridge = _nav(nodes)
        row = nav._search_new_message("anything")
        assert row is None
        assert "pressBack" in [c[0] for c in bridge.calls]


class TestOpenChatFallsBackToSearch:
    def test_falls_back_when_row_absent_from_scroll(self, monkeypatch):
        """open_chat must try the New Message search once the ordinary inbox
        scan (scroll) comes up empty, rather than giving up immediately."""
        inbox_fingerprint = _node(
            "android.view.View", id="com.instagram.android:id/direct_inbox_action_bar")
        nodes = [inbox_fingerprint, _new_message_button(), _search_field()]
        nav, bridge = _nav(nodes)

        monkeypatch.setattr(nav, "_scan_inbox_for_row", lambda name: None)
        called = {}

        def fake_search(name):
            called["name"] = name
            return None

        monkeypatch.setattr(nav, "_search_new_message", fake_search)
        result = nav.open_chat("little man, fat boy and ak47", attempts=1)
        assert result is False
        assert called.get("name") == "little man, fat boy and ak47"
