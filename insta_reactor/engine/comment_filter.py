"""Decide which comments the brain is allowed to look at.

We only react based on *readable text/emoji* comments. Image, GIF, and sticker
comments carry no text signal (and the accessibility tree exposes them as a bare
placeholder label like "GIF" or an empty row), so they must be dropped before
scoring — otherwise they either add noise or get echoed back as a broken
"verbatim" reply.

Kept as a pure function so it can be unit-tested and reused by both the collector
(device side) and the reply selector (decision side).
"""

from __future__ import annotations

# Whole-text placeholders IG uses for media-only comment rows. We only reject a
# comment when its *entire* text is one of these (so a real comment that merely
# mentions "gif" is kept).
_MEDIA_PLACEHOLDERS = {
    "gif", "sticker", "photo", "image", "video", "attachment",
    "sent an attachment", "shared a post", "shared a reel",
}


def is_reactable_comment(text: str | None) -> bool:
    """True if `text` is a real text/emoji comment worth reacting to."""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if low in _MEDIA_PLACEHOLDERS:
        return False
    # A row that is only a like-count / reply-count artifact ("12 likes",
    # "3 replies", "Reply") carries no reaction signal either.
    if low in {"reply", "replies", "like", "likes", "view replies"}:
        return False
    return True


def reactable(comments):
    """Filter an iterable of Comment objects down to the reactable ones."""
    return [c for c in (comments or []) if is_reactable_comment(c.text)]
