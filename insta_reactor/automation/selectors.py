"""THE ONE FILE YOU CALIBRATE.

Instagram's Android UI has no public, stable API. Its resource-ids are
obfuscated and change between app versions, but content-descriptions
(accessibility labels) and visible text are far more durable. This file
centralizes every selector so that when Instagram updates, you fix things HERE
and nowhere else — exactly the "replaceable module" the spec asks for.

How to calibrate (do this once per major IG version):
  1. Connect your device:  adb devices   (and `pip install uiautomator2`)
  2. Dump a screen:         python -m insta_reactor.tools.calibrate
     (or use `weditor` / `uiautomator2` `d.dump_hierarchy()` interactively)
  3. Read off the content-description / text / resource-id of each control and
     paste it below. Prefer `desc` and `text` over `resource_id`.

Each entry is a list of candidate matchers tried in order (first hit wins), so
you can keep several fallbacks (e.g. an English label + an id) simultaneously.

A matcher is a dict of Device.find kwargs, e.g.:
    {"desc": "Comment"}                      # accessibility label
    {"textContains": "comments"}             # visible text
    {"resource_id": "com.instagram.android:id/row_thread_composer_edittext"}
"""

from __future__ import annotations

# ---- state fingerprints: presence of ANY matcher => we're in that state -----
STATE_FINGERPRINTS = {
    "INBOX": [
        {"resource_id": "com.instagram.android:id/inbox_refreshable_thread_list_recyclerview"},
        {"resource_id": "com.instagram.android:id/direct_inbox_action_bar"},
        {"resource_id": "com.instagram.android:id/row_inbox_container"},
    ],
    "CHAT": [
        {"resource_id": "com.instagram.android:id/row_thread_composer_edittext"},
        {"resource_id": "com.instagram.android:id/message_thread_container"},
        {"desc": "Camera"},                  # in-thread camera button
        # NOTE: {"desc": "Message"} was removed — that content-desc actually
        # belongs to the bottom-nav Direct/Inbox tab icon (see
        # INBOX_TAB_BUTTON below), which is visible on Home/Inbox too, not
        # just inside an open thread. Using it here caused every non-chat
        # screen with the tab bar visible to be misdetected as CHAT, which
        # pre-empted the INBOX check (checked later) and made the inbox
        # unreachable.
    ],
    "REEL_VIEWER": [
        {"resource_id": "com.instagram.android:id/like_button"},
        {"resource_id": "com.instagram.android:id/comment_button"},
        {"desc": "Like"},
        {"desc": "Comment"},
        {"desc": "Share"},
    ],
    "COMMENTS": [
        {"resource_id": "com.instagram.android:id/comment_overswipe_dismiss_container"},
        {"resource_id": "com.instagram.android:id/layout_comment_thread_edittext_multiline"},
        {"textContains": "Comments"},
        {"desc": "Add a comment"},
    ],
}

# ---- controls ---------------------------------------------------------------
# Bottom-nav icon that opens the DM inbox from anywhere in the app (confirmed
# content-desc is "Message", not "Direct" or "Inbox" — misleadingly named).
INBOX_TAB_BUTTON = [
    {"resource_id": "com.instagram.android:id/direct_tab"},
    {"desc": "Message"},
]

# NOTE: currently unused by navigator.open_chat — it used to fall back to this
# global search bar, but that bar is IG's universal ("Ask Meta AI") search,
# whose result rows open the account's *profile*, not the thread, so tapping a
# result there could never reach State.CHAT. open_chat now scrolls the inbox
# list instead (see Navigator._scan_inbox_for_row). Kept here in case Phase 2
# calibration finds an inbox-internal thread filter distinct from this bar.
INBOX_SEARCH = [
    {"desc": "Search"},
    {"resource_id": "com.instagram.android:id/action_bar_search_edit_text"},
]

# A received reel bubble inside a thread. Confirmed on-device: the bubble's
# media container has this resource-id and no content-desc; it's not itself
# marked clickable in the accessibility tree (the tap still lands correctly
# since we tap by coordinates, not through an accessibility click action).
REEL_BUBBLE = [
    {"resource_id": "com.instagram.android:id/reel_share_item_view"},
    {"descContains": "Reel"},
    {"descContains": "reel"},
    {"desc": "Play"},
]

OPEN_COMMENTS_BUTTON = [
    {"resource_id": "com.instagram.android:id/comment_button"},
    {"desc": "Comment"},
    {"descContains": "Comment"},
]

# One comment row's text element (used by the collector to read comment text).
# Confirmed on-device: comment rows carry no resource-id and aren't plain
# TextViews — IG exposes each row as a `android.view.ViewGroup` whose
# content-desc (== its `text` in the accessibility tree) is the pattern
# "<username> said <comment text>". The collector strips the "X said " prefix.
COMMENT_ROW_TEXT = [
    {"className": "android.view.ViewGroup", "descContains": " said "},
    {"resource_id": "com.instagram.android:id/row_comment_textview_comment"},
    {"className": "android.widget.TextView"},
]

# Optional like-count element on a comment row (for weighting). Best-effort.
COMMENT_LIKE_COUNT = [
    {"resource_id": "com.instagram.android:id/row_comment_textview_like_count"},
    {"descContains": "like"},
]

# The reel-viewer's own reply bar (hint "Reply to <name>"). CONFIRMED on-device:
# a Reel opened inline from a DM thread shows this bar; typing here and sending
# posts the reply *attached to that specific reel* (the recipient sees the reel
# thumbnail quoted), which is exactly what we want — the plain thread composer
# would send a floating message with no indication of which reel it's about.
REEL_REPLY_BAR = [
    {"resource_id": "com.instagram.android:id/reply_bar_edittext"},
]

# After swiping right on a message bubble, IG opens the composer pre-loaded with
# a "replying to" quote chip (with a cancel/close button). Presence of this chip
# confirms the swipe-to-reply gesture landed on the reel. Best-effort — if none
# of these match we still try to type+send (the gesture usually succeeds).
REPLY_QUOTE_INDICATOR = [
    {"resource_id": "com.instagram.android:id/reply_bar_reply_preview"},
    {"descContains": "Cancel reply"},
    {"descContains": "Replying to"},
    {"textContains": "Replying to"},
]

# The reply composer inside a plain chat thread (fallback; used only if we ever
# reply without an open reel viewer).
CHAT_COMPOSER = [
    {"resource_id": "com.instagram.android:id/reply_bar_edittext"},
    {"resource_id": "com.instagram.android:id/row_thread_composer_edittext"},
    {"desc": "Message"},
]
# CONFIRMED on-device: the Send button only appears once text is typed. Both the
# reel-viewer reply bar and the thread composer reveal the same control:
# `row_thread_composer_send_button_container` (its background carries desc="Send").
CHAT_SEND_BUTTON = [
    {"resource_id": "com.instagram.android:id/row_thread_composer_send_button_container"},
    {"resource_id": "com.instagram.android:id/row_thread_composer_send_button_background"},
    {"desc": "Send"},
]

# Quick single-tap emoji reaction on the reply bar (e.g. the heart icon) —
# useful for the "single emoji" reply style. Confirmed content-desc="❤" for
# the heart; other slots exist but currently have no content-desc, so they'd
# need to be targeted by bounds/index instead. Tap "Open emoji reaction sheet"
# (reply_bar_reaction_sheet_button) for the full picker.
REPLY_BAR_HEART_REACTION = [
    {"resource_id": "com.instagram.android:id/item_emoji", "desc": "❤"},
]
REPLY_BAR_REACTION_SHEET_BUTTON = [
    {"resource_id": "com.instagram.android:id/reply_bar_reaction_sheet_button"},
    {"desc": "Open emoji reaction sheet"},
]

# Things that count as interruptions to dismiss (best-effort text/desc match).
DISMISS_INTERRUPTION = [
    {"text": "Not now"},
    {"text": "Not Now"},
    {"text": "Cancel"},
    {"text": "Dismiss"},
    {"text": "Later"},
    {"textContains": "Not now"},
]
