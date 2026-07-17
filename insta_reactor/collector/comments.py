"""Scrape comments (and, when available, like counts) from the open sheet.

Assumes the comments sheet is already open (the navigator's job). Scrolls the
list, reads visible comment rows, and dedupes by text so overlapping scroll
windows don't double-count. Like counts are best-effort — if IG doesn't expose
them, comments simply weigh equally.
"""

from __future__ import annotations

import logging
import re

from ..device.base import Device
from ..models import Comment
from ..automation import selectors as S

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

    def _visible_comments(self) -> list[Comment]:
        rows: list[Comment] = []
        for matcher in S.COMMENT_ROW_TEXT:
            nodes = self.d.find_all(**matcher)
            if nodes:
                for n in nodes:
                    txt = (n.text or n.desc or "").strip()
                    txt = _strip_said_prefix(txt)
                    if txt:
                        rows.append(Comment(text=txt, likes=0))
                break
        return rows

    def collect(self, limit: int = 50, max_scrolls: int = 12) -> tuple[list[Comment], int]:
        """Return (comments, observed_count).

        `observed_count` is the number of distinct comments we actually read;
        it is a lower bound on the true count, which is fine for the >= N gate.
        """
        import time

        w, h = self.d.window_size()
        seen: set[str] = set()
        collected: list[Comment] = []

        for _ in range(max_scrolls):
            for c in self._visible_comments():
                key = c.text
                if key not in seen:
                    seen.add(key)
                    collected.append(c)
            if len(collected) >= limit:
                break
            # scroll up within the comments sheet (bottom -> top of gesture)
            self.d.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 0.25)
            time.sleep(self.settle)

        return collected[:limit], len(collected)
