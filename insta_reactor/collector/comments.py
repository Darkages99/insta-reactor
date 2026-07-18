"""Scrape comments (and, when available, like counts) from the open sheet.

Assumes the comments sheet is already open (the navigator's job). Scrolls the
list, reads visible comment rows, and dedupes by text so overlapping scroll
windows don't double-count. Like counts are best-effort — if IG doesn't expose
them, comments simply weigh equally.
"""

from __future__ import annotations

import logging
import re

from ..device.base import Device, UiNode
from ..models import Comment
from ..automation import selectors as S
from ..engine.comment_filter import is_reactable_comment

log = logging.getLogger("insta_reactor.collector")


_SAID_PREFIX = re.compile(r"^\S+\s+said\s+(.*)$", re.DOTALL)


def _strip_said_prefix(text: str) -> str:
    """IG's comment row content-desc is "<username> said <comment>"; keep
    just the comment. Falls back to the raw text if it doesn't match (e.g. a
    reply-count or other row matched by the same selector)."""
    m = _SAID_PREFIX.match(text)
    return m.group(1).strip() if m else text


def _parse_like_count(text: str) -> int:
    """'1,234 likes' -> 1234 ; '2.3K likes' -> 2300 ; '' -> 0."""
    if not text:
        return 0
    t = text.lower().replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([km]?)", t)
    if not m:
        return 0
    num = float(m.group(1))
    mult = {"k": 1_000, "m": 1_000_000, "": 1}.get(m.group(2), 1)
    return int(num * mult)


class CommentCollector:
    def __init__(self, device: Device, settle: float = 0.5):
        self.d = device
        self.settle = settle

    def _like_nodes(self) -> list[UiNode]:
        """All visible per-comment like-count elements (best-effort)."""
        for matcher in S.COMMENT_LIKE_COUNT:
            nodes = self.d.find_all(**matcher)
            if nodes:
                return nodes
        return []

    def _likes_for_row(self, row: UiNode, like_nodes: list[UiNode]) -> int:
        """Associate a like-count element with a comment row by geometry.

        IG renders the like count just under the comment text, left-aligned with
        it. We take the like node whose top sits at/below the row's top and is
        closest to it. Best-effort: returns 0 when nothing plausible is found.
        """
        _, row_top, _, row_bottom = row.bounds
        best_likes, best_dy = 0, 10 ** 9
        for ln in like_nodes:
            _, lt, _, _ = ln.bounds
            dy = lt - row_top
            # like count belongs to this row if it's within a comment's height
            # below the row's top and nearer than any other row we've seen.
            if -10 <= dy <= (row_bottom - row_top) + 160 and dy < best_dy:
                likes = _parse_like_count(ln.text or ln.desc or "")
                if likes > 0:
                    best_likes, best_dy = likes, dy
        return best_likes

    def _visible_comments(self) -> list[Comment]:
        rows: list[Comment] = []
        like_nodes = self._like_nodes()
        for matcher in S.COMMENT_ROW_TEXT:
            nodes = self.d.find_all(**matcher)
            if nodes:
                for n in nodes:
                    txt = (n.text or n.desc or "").strip()
                    txt = _strip_said_prefix(txt)
                    # Drop image/gif/sticker/artifact rows — only text/emoji
                    # comments carry a reaction signal.
                    if not is_reactable_comment(txt):
                        continue
                    likes = self._likes_for_row(n, like_nodes) if like_nodes else 0
                    rows.append(Comment(text=txt, likes=likes))
                break
        return rows

    def collect(self, limit: int = 50, max_scrolls: int = 12) -> tuple[list[Comment], int]:
        """Return (comments, observed_count).

        `observed_count` is the number of distinct comments we actually read;
        it is a lower bound on the true count, which is fine for the >= N gate.
        """
        import time

        w, h = self.d.window_size()
        by_text: dict[str, Comment] = {}
        order: list[str] = []

        for _ in range(max_scrolls):
            for c in self._visible_comments():
                key = c.text
                if key not in by_text:
                    by_text[key] = c
                    order.append(key)
                elif c.likes > by_text[key].likes:
                    # a later scroll window may reveal this row's like count
                    # (it can be off-screen the first time) — keep the max.
                    by_text[key].likes = c.likes
            if len(order) >= limit:
                break
            # scroll up within the comments sheet (bottom -> top of gesture)
            self.d.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 0.25)
            time.sleep(self.settle)

        collected = [by_text[k] for k in order]
        return collected[:limit], len(collected)
