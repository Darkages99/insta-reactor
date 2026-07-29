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
        # NOTE: {"desc": "Message"} was removed — that content-desc actually
        # belongs to the bottom-nav Direct/Inbox tab icon (see
        # INBOX_TAB_BUTTON below), which is visible on Home/Inbox too, not
        # just inside an open thread. Using it here caused every non-chat
        # screen with the tab bar visible to be misdetected as CHAT, which
        # pre-empted the INBOX check (checked later) and made the inbox
        # unreachable.
        # NOTE: {"desc": "Camera"} was removed for the same reason — the
        # inbox list's own top bar has a "Direct camera" icon with the same
        # content-desc, confirmed on-device (P2 live run). It matched before
        # the INBOX check ever ran, so ensure_inbox() could never detect the
        # inbox and looped until NavigationError. The resource-id matchers
        # above are specific enough on their own.
    ],
    "REEL_VIEWER": [
        {"resource_id": "com.instagram.android:id/like_button"},
        {"resource_id": "com.instagram.android:id/comment_button"},
        # NOTE: bare {"desc": "Like"/"Comment"/"Share"} fallbacks were removed —
        # confirmed on-device (P2 live run) that the home feed's own post like
        # button (resource-id row_feed_button_like, NOT like_button) also
        # carries desc "Like". That false-matched REEL_VIEWER before the INBOX
        # check ever ran, trapping ensure_inbox() in a misdetected state with
        # no path back. The resource-id matchers above are reel-viewer-specific
        # (confirmed against the clips_viewer fixture) and sufficient alone.
    ],
    "COMMENTS": [
        # CONFIRMED live (shyam chat, "watch and comment" split layout): this
        # IG build exposes comment rows under the BARE resource-id
        # `row_comment_textview_comment` (no `com.instagram.android:id/`
        # prefix), and none of the older fingerprints below are present — so
        # wait_for_state(COMMENTS) was timing out even though comments had
        # opened cleanly, wrongly flagging every reel "unable to read
        # comments". The bare id only appears once comments are open (the plain
        # reel viewer has none), so it's a safe, comment-specific fingerprint.
        {"resource_id": "row_comment_textview_comment"},
        {"resource_id": "row_comment_textview_reply_button"},
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

# One inbox thread row, used to fingerprint the visible list so the scan knows
# when a scroll actually advanced (vs. reached the bottom). CONFIRMED live: this
# IG build renders each row as an `android.view.View` whose content-desc is
# "<name>, <preview> ·, <time>" (the username itself is a bare, resource-id-less
# TextView), and the old `row_inbox_username` id matches NOTHING here — so the
# scan's signature was always empty, instantly false-tripped "end of list", and
# never scrolled to reach a row further down (e.g. 'shyam'). The comma is always
# present in a row's desc, so descContains=", " selects exactly the rows.
INBOX_ROW = [
    {"className": "android.view.View", "descContains": ", "},
    {"resource_id": "com.instagram.android:id/row_inbox_username"},
]

# The open DM thread's header title (the account name at the top of the chat).
# CONFIRMED live: resource-id `header_title`, text/desc == the account name.
# Used to VERIFY we opened the exact chat we meant to — a substring name clash
# (e.g. tapping "40fitandshyam" when targeting "shyam") must never pass.
THREAD_TITLE = [
    {"resource_id": "com.instagram.android:id/header_title"},
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

# The "+"-style compose button in the inbox's top-right corner. CONFIRMED live
# (2026-07-29): some existing threads — e.g. an infrequently-opened group chat
# — never appear in the scrollable Direct list no matter how far down you
# scroll (confirmed by manually scrolling 150+ weeks deep), yet they DO exist:
# opening this button's search and typing the name surfaces them under a
# "Suggested" header, and tapping that result opens the REAL existing thread
# (its actual message history), not a newly created one. content-desc is
# "New Message"; no stable resource-id was present on this build.
NEW_MESSAGE_BUTTON = [
    {"desc": "New Message"},
]

# The search field on the "New message" screen opened by NEW_MESSAGE_BUTTON.
NEW_MESSAGE_SEARCH_FIELD = [
    {"resource_id": "com.instagram.android:id/search_edit_text"},
]

# One result row's primary name label on that search screen (used both for
# suggested/existing threads and for people-search results).
NEW_MESSAGE_RESULT_ROW = [
    {"resource_id": "com.instagram.android:id/row_user_primary_name"},
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
    # CONFIRMED live (shyam chat): comment rows carry the BARE resource-id
    # `row_comment_textview_comment` (no package prefix) whose `text` is the
    # clean comment ("Dholakpur files😂") and whose `desc` is the
    # "<username> said <comment>" form the stripper handles. Tried first so we
    # read real rows, never the broad TextView fallback (which also matches
    # usernames/counts/chrome). The package-prefixed id and the " said "
    # ViewGroup form are kept for other IG builds.
    {"resource_id": "row_comment_textview_comment"},
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

# Vanish / "disappearing messages" mode. Instagram engages this when you
# over-scroll (pull UP) past the newest message at the very bottom of a thread.
# It is dangerous for us: the composer switches to disappearing messages and the
# pull animation keeps the screen changing, so a naive scroll loop never settles.
# We detect it so the scroll code can abort and back out before it fully engages.
# NOTE: these strings are a best-effort guess at IG's vanish UI copy and MUST be
# calibrated against the real screen the first time vanish mode is reproduced on
# device (dump the hierarchy while it's engaged and confirm/extend this list).
# Kept broad (substring match) and vanish-specific so normal chat never matches.
VANISH_MODE_INDICATORS = [
    {"textContains": "Vanish"},
    {"descContains": "Vanish"},
    {"textContains": "vanish"},
    {"descContains": "vanish"},
    {"textContains": "Disappearing"},
    {"textContains": "disappearing"},
    {"descContains": "Disappearing"},
    {"textContains": "disappear"},
]

# Things that count as interruptions to dismiss (best-effort text/desc match).
DISMISS_INTERRUPTION = [
    {"text": "Not now"},
    {"text": "Not Now"},
    {"text": "Cancel"},
    {"text": "Dismiss"},
    {"text": "Later"},
    {"textContains": "Not now"},
    # IG's own "You've reached your daily limit" well-being nag (confirmed
    # on-device, P2 live run) — blocks the whole app behind a full-screen
    # interstitial until dismissed. "Ignore limit for today" is its only
    # non-destructive dismiss action (the other buttons snooze, they don't
    # close it, so a retry loop would just hit the same screen again).
    {"text": "Ignore limit for today"},
]
